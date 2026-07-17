"""
Claude AI synopsis generation.

v3.0 changes vs v2.0:
  - Prompts loaded from app/prompts/ registry (not inline strings)
  - Accepts a PortalTemplate; template controls sections and field labels
  - Response cache honours (sp, version, template_hash, language, prompt_version)
  - Optional few-shot injection from SAP Agent Memory approvals

Backend priority:
  1. SAP AI Core via LiteLLM (when AICORE_CLIENT_ID is set)
  2. Hyperspace proxy (when HYPERSPACE_API_KEY is set)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core.portal_template import PortalTemplate
from core import synopsis_cache
from prompts import load_prompt, prompt_version

logger = logging.getLogger(__name__)


# ── env.local loader (unchanged from v2) ─────────────────────────────────

def _load_env_local():
    env_path = Path(__file__).parent.parent / ".env.local"
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _get_ai_config() -> tuple[str, str, str]:
    """Return (base_url, api_key, model) for the active AI backend."""
    _load_env_local()
    hyperspace_key = os.environ.get("HYPERSPACE_API_KEY", "").strip()
    hyperspace_url = os.environ.get("HYPERSPACE_URL", "http://localhost:6655/anthropic")
    model          = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")
    return hyperspace_url, hyperspace_key, model


# ── Public entry point ───────────────────────────────────────────────────

def generate_synopsis(
    tender_data: dict,
    target_language: str,
    template: PortalTemplate,
    few_shot_examples: list[dict] | None = None,
) -> dict:
    """
    Generate a synopsis for a Sourcing Project using the provided PortalTemplate.

    Args:
        tender_data:        26-field dict from SAP OData V4 fetch
        target_language:    e.g. "English", "German", "Hindi"
        template:           PortalTemplate resolved by template_loader
        few_shot_examples:  optional list of {"content","similarity","metadata"}
                            from synopsis_memory.find_similar_approvals()

    Returns:
        dict matching the synopsis output schema (see prompts/user/synopsis_generator.jinja)
    """
    _load_env_local()

    # ── Consistency cache (E6) ───────────────────────────────────────────
    ck = synopsis_cache.cache_key(
        sourcing_project_id = tender_data.get("SourcingProject", ""),
        project_version     = tender_data.get("SourcingProjectVersion", ""),
        template_hash       = template.template_hash,
        language            = target_language,
        prompt_version      = prompt_version("synopsis_generator"),
    )
    cached = synopsis_cache.get(ck)
    if cached:
        logger.info("[Synopsis] Serving cached synopsis (identical inputs)")
        return cached

    # ── Build prompt via registry (E4) ───────────────────────────────────
    prompt = load_prompt(
        "synopsis_generator",
        template          = template,
        tender_data_json  = json.dumps(tender_data, indent=2, ensure_ascii=False),
        target_language   = target_language,
        few_shot_examples = few_shot_examples or [],
    )
    logger.info(f"[Synopsis] Using prompt v{prompt['version']}, template v{template.version} ({template.source})")

    # ── Call Claude ──────────────────────────────────────────────────────
    aicore_set    = bool(os.environ.get("AICORE_CLIENT_ID", "").strip())
    hyperspace_url, hyperspace_key, _ = _get_ai_config()

    if aicore_set:
        try:
            raw = _call_via_litellm(prompt)
        except Exception as e:
            logger.warning(f"[Synopsis] SAP AI Core failed: {e}. Falling back to Hyperspace.")
            if not hyperspace_key:
                raise
            raw = _call_via_hyperspace(prompt, hyperspace_url, hyperspace_key)
    elif hyperspace_key:
        raw = _call_via_hyperspace(prompt, hyperspace_url, hyperspace_key)
    else:
        raise RuntimeError(
            "No AI credentials configured. Set AICORE_CLIENT_ID (SAP AI Core) "
            "or HYPERSPACE_API_KEY (local Hyperspace proxy) in app/.env.local"
        )

    synopsis = _parse_json(raw)

    # Store in cache for future identical calls
    synopsis_cache.put(ck, synopsis)
    return synopsis


# ── AI backend calls ─────────────────────────────────────────────────────

def _call_via_litellm(prompt: dict) -> str:
    """SAP AI Core via LiteLLM."""
    from langchain_litellm import ChatLiteLLM
    from langchain_core.messages import SystemMessage, HumanMessage

    model = os.environ.get("CLAUDE_MODEL", "sap/anthropic--claude-4.5-sonnet")
    llm = ChatLiteLLM(model=model, temperature=prompt["temperature"])
    resp = llm.invoke([
        SystemMessage(content=prompt["system"]),
        HumanMessage(content=prompt["user"]),
    ])
    return resp.content.strip()


def _call_via_hyperspace(prompt: dict, base_url: str, api_key: str) -> str:
    """Hyperspace proxy via Anthropic SDK."""
    import anthropic as anthropic_sdk

    model  = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")
    client = anthropic_sdk.Anthropic(base_url=base_url, api_key=api_key)

    kwargs = dict(
        model=model,
        max_tokens=prompt["max_tokens"],
        temperature=prompt["temperature"],
        system=prompt["system"],
        messages=[{"role": "user", "content": prompt["user"]}],
    )
    # Anthropic API may or may not accept seed depending on model
    if prompt.get("seed") is not None:
        kwargs["seed"] = prompt["seed"]

    try:
        message = client.messages.create(**kwargs)
    except TypeError:
        # Client version may not accept `seed`
        kwargs.pop("seed", None)
        message = client.messages.create(**kwargs)

    return message.content[0].text.strip()


def _parse_json(raw: str) -> dict:
    """Strip markdown fences if present, then parse JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:].lstrip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"AI returned invalid JSON:\n{raw[:300]}\n\nError: {e}") from e