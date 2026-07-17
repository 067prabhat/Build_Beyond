"""
In-memory response cache for generated synopses.

Purpose: keep Enhancement 6 (consistency) rock-solid. For the same
(SP, template, language, prompt) tuple we return the previously-approved
synopsis instead of asking Claude again. TTL keeps the cache from serving
stale content if any input has changed.

Not a database - just a dict with TTL. Lost on pod restart, which is fine:
Claude will regenerate deterministically because temperature and seed are
fixed in prompts/manifest.yaml.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[dict, float]] = {}
_TTL_SEC = int(os.environ.get("SYNOPSIS_CACHE_TTL_SEC", "3600"))   # 1h

_ENABLED = os.environ.get("SYNOPSIS_CACHE_ENABLED", "true").lower() == "true"


def cache_key(
    sourcing_project_id: str,
    project_version: str,
    template_hash: str,
    language: str,
    prompt_version: str,
) -> str:
    payload = {
        "sp":       sourcing_project_id,
        "ver":      project_version,
        "tmpl":     template_hash,
        "lang":     language,
        "prompt":   prompt_version,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get(key: str) -> dict | None:
    if not _ENABLED:
        return None
    hit = _CACHE.get(key)
    if not hit:
        return None
    synopsis, ts = hit
    if (time.time() - ts) >= _TTL_SEC:
        _CACHE.pop(key, None)
        return None
    logger.info(f"[SynopsisCache] HIT key={key[:12]}")
    return synopsis


def put(key: str, synopsis: dict) -> None:
    if not _ENABLED:
        return
    _CACHE[key] = (synopsis, time.time())
    logger.info(f"[SynopsisCache] Stored key={key[:12]}")


def clear() -> None:
    _CACHE.clear()


def stats() -> dict:
    return {
        "enabled": _ENABLED,
        "size":    len(_CACHE),
        "ttl_sec": _TTL_SEC,
    }