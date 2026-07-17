"""
Template Loader - 3-Layer Portal Template Resolver.

Layer 1 (Live API)   -> portal adapter (owned by another team; may be empty)
Layer 2 (Memory)     -> in-memory LRU cache with 48h TTL
Layer 3 (File)       -> app/templates/{ISO}.json (always present in Docker image)
Layer 4 (Default)    -> app/templates/DEFAULT.json (safety net)

Public entry point: load_template(country_code) -> (PortalTemplate, source)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Protocol

from core.portal_template import PortalTemplate, TemplateSource, now_iso

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

_CACHE_TTL_SEC = int(os.environ.get("TEMPLATE_CACHE_TTL_SEC", "172800"))   # 48h
_LIVE_TIMEOUT  = float(os.environ.get("TEMPLATE_LIVE_TIMEOUT_SEC", "5"))
_PREFER_LIVE   = os.environ.get("TEMPLATE_PREFER_LIVE", "true").lower() == "true"


class PortalAdapter(Protocol):
    """
    Contract for live portal template fetchers.
    Implementations live in app/core/portal_adapters/ and are owned by
    the URL/live-fetch team. When they land, populate PORTAL_ADAPTERS below.
    """
    country_code: str

    def fetch_template(self) -> PortalTemplate: ...


# Populated by the URL/live-fetch team via portal_adapters/__init__.py.
# Left as an empty dict here so this module works end-to-end even when
# no adapters exist yet.
PORTAL_ADAPTERS: dict[str, PortalAdapter] = {}


# In-memory cache: country_code -> (template, fetched_at_epoch)
_CACHE: dict[str, tuple[PortalTemplate, float]] = {}


# ── Layer helpers ─────────────────────────────────────────────────────────

def _try_live(country_code: str) -> PortalTemplate | None:
    """Layer 1: call the registered adapter, if any."""
    adapter = PORTAL_ADAPTERS.get(country_code)
    if not adapter:
        return None
    try:
        logger.info(f"[Template] Live fetch attempt for {country_code}")
        tmpl = adapter.fetch_template()
        # Normalise source + timestamp on the returned template
        return PortalTemplate.from_dict({**tmpl.to_dict(), "fetched_at": now_iso()}, source="live")
    except Exception as e:
        logger.warning(f"[Template] Live fetch failed for {country_code}: {e}")
        return None


def _try_cache(country_code: str) -> PortalTemplate | None:
    """Layer 2: in-memory cache with TTL."""
    hit = _CACHE.get(country_code)
    if not hit:
        return None
    tmpl, fetched = hit
    if (time.time() - fetched) >= _CACHE_TTL_SEC:
        return None
    # Return a copy tagged as "cache" source
    return PortalTemplate.from_dict(tmpl.to_dict(), source="cache")


def _try_file(country_code: str) -> PortalTemplate | None:
    """Layer 3: JSON file in app/templates/."""
    file_path = TEMPLATES_DIR / f"{country_code}.json"
    if not file_path.exists():
        return None
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return PortalTemplate.from_dict(data, source="file")
    except Exception as e:
        logger.error(f"[Template] Failed to parse {file_path}: {e}")
        return None


def _load_default() -> PortalTemplate:
    """Layer 4: DEFAULT.json - guaranteed to exist."""
    path = TEMPLATES_DIR / "DEFAULT.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return PortalTemplate.from_dict(data, source="default")


# ── Public API ────────────────────────────────────────────────────────────

def load_template(country_code: str, prefer_live: bool | None = None) -> tuple[PortalTemplate, TemplateSource]:
    """
    Resolve a PortalTemplate for a country using the 3-layer fallback chain.

    Returns:
        (template, source)  where source in {"live", "cache", "file", "default"}
    """
    country_code = (country_code or "DEFAULT").upper()
    prefer_live  = _PREFER_LIVE if prefer_live is None else prefer_live

    # Layer 2 - hot cache first (avoids Layer 1 hammering)
    cached = _try_cache(country_code)
    if cached:
        logger.info(f"[Template] Cache HIT for {country_code}")
        return cached, "cache"

    # Layer 1 - live fetch
    if prefer_live:
        live = _try_live(country_code)
        if live:
            _CACHE[country_code] = (live, time.time())
            logger.info(f"[Template] Live fetch OK for {country_code}")
            return live, "live"

    # Layer 3 - JSON file
    from_file = _try_file(country_code)
    if from_file:
        _CACHE[country_code] = (from_file, time.time())
        logger.info(f"[Template] File load OK for {country_code}")
        return from_file, "file"

    # Layer 4 - default
    default = _load_default()
    logger.warning(f"[Template] Falling back to DEFAULT for {country_code}")
    return default, "default"


def clear_cache(country_code: str | None = None) -> None:
    """Test/dev helper. Clear the entire cache or one country."""
    if country_code is None:
        _CACHE.clear()
    else:
        _CACHE.pop(country_code.upper(), None)


def register_adapter(adapter: PortalAdapter) -> None:
    """Called by portal_adapters/__init__.py at startup to register live fetchers."""
    PORTAL_ADAPTERS[adapter.country_code.upper()] = adapter
    logger.info(f"[Template] Registered live adapter for {adapter.country_code.upper()}")