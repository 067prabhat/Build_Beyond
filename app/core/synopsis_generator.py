"""
Claude AI synopsis generation.
Uses LiteLLM + SAP AI Core (production) or Hyperspace proxy (local dev).

Priority:
  1. SAP AI Core via LiteLLM (when AICORE_* env vars are set)
  2. Hyperspace proxy (when HYPERSPACE_API_KEY is set)
"""

import json
import logging
import os
from pathlib import Path

from core.country_formats import COUNTRY_FORMATS, _detect_country, _build_country_instructions

logger = logging.getLogger(__name__)

# Model name for SAP AI Core / LiteLLM
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sap/anthropic--claude-4.5-sonnet")


def _load_env_local():
    """Load .env.local once if keys not already in environment."""
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


def _get_ai_config() -> tuple:
    """
    Return (base_url, api_key, model) for the active AI backend.
    Reads .env.local first, then environment variables.
    Used by both generate_synopsis() and the AI Validator.
    """
    _load_env_local()
    aicore_set     = bool(os.environ.get("AICORE_CLIENT_ID", "").strip())
    hyperspace_key = os.environ.get("HYPERSPACE_API_KEY", "").strip()
    hyperspace_url = os.environ.get("HYPERSPACE_URL", "http://localhost:6655/anthropic")
    model          = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")

    if aicore_set:
        # SAP AI Core path — model uses LiteLLM prefix
        return hyperspace_url, hyperspace_key, os.environ.get("CLAUDE_MODEL", "sap/anthropic--claude-4.5-sonnet")
    else:
        return hyperspace_url, hyperspace_key, model


def generate_synopsis(tender_data: dict, target_language: str = "English",
                      country_code: str = "AUTO") -> dict:
    """
    Call Claude AI and return a structured synopsis dict.

    Uses LiteLLM -> SAP AI Core when AICORE_* credentials are available.
    Falls back to Hyperspace proxy when HYPERSPACE_API_KEY is set.
    """
    _load_env_local()

    if country_code == "AUTO":
        country_code = _detect_country(tender_data)

    portal = COUNTRY_FORMATS.get(country_code, COUNTRY_FORMATS["DEFAULT"])["portal"]
    prompt = _build_prompt(tender_data, target_language, country_code)

    # ── Determine which AI backend to use ────────────────────────────────────
    aicore_set    = bool(os.environ.get("AICORE_CLIENT_ID", "").strip())
    hyperspace_key = os.environ.get("HYPERSPACE_API_KEY", "").strip()
    hyperspace_url = os.environ.get("HYPERSPACE_URL", "http://localhost:6655/anthropic")

    if aicore_set:
        try:
            return _call_via_litellm(prompt, country_code, portal)
        except Exception as e:
            logger.warning(f"[AI] SAP AI Core failed: {e}. Falling back to Hyperspace proxy.")
            if hyperspace_key:
                return _call_via_hyperspace(prompt, hyperspace_url, hyperspace_key, country_code, portal)
            raise
    elif hyperspace_key:
        return _call_via_hyperspace(prompt, hyperspace_url, hyperspace_key, country_code, portal)
    else:
        raise RuntimeError(
            "No AI credentials configured. Set either AICORE_CLIENT_ID (SAP AI Core) "
            "or HYPERSPACE_API_KEY (local Hyperspace proxy) in app/.env.local"
        )


def _call_via_litellm(prompt: str, country_code: str, portal: str) -> dict:
    """Call Claude via LiteLLM + SAP AI Core."""
    from langchain_litellm import ChatLiteLLM
    from langchain_core.messages import SystemMessage, HumanMessage

    model = os.environ.get("CLAUDE_MODEL", "sap/anthropic--claude-4.5-sonnet")
    logger.info(f"[AI] Calling Claude via SAP AI Core LiteLLM: {model}")
    logger.info(f"[AI] Portal: {country_code} => {portal}")

    llm = ChatLiteLLM(model=model)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]
    response = llm.invoke(messages)
    raw = response.content.strip()
    return _parse_json(raw)


def _call_via_hyperspace(prompt: str, base_url: str, api_key: str,
                         country_code: str, portal: str) -> dict:
    """Call Claude via Hyperspace local proxy (Anthropic SDK)."""
    import anthropic as anthropic_sdk

    model = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")
    logger.info(f"[AI] Calling Claude via Hyperspace proxy: {base_url}")
    logger.info(f"[AI] Portal: {country_code} => {portal}")

    client = anthropic_sdk.Anthropic(base_url=base_url, api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    return _parse_json(raw)


def _parse_json(raw: str) -> dict:
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"AI returned invalid JSON:\n{raw[:300]}\n\nError: {e}") from e


# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an SAP Public Sector Procurement assistant.
Generate a concise supplier-facing tender synopsis from SAP PPS sourcing project data.

Rules:
1. Use only information present in the SAP input.
2. Do not invent facts, quantities, requirements, locations, or eligibility criteria.
3. If information is missing, return "Not specified" translated into the target language.
4. Keep all fields concise: max 1-3 sentences per narrative field; direct values for dates, statuses, and amounts.
5. Supplier-facing language only. Focus on: what is being procured, participation conditions, important dates, commercial context.
6. Dates must be formatted as: DD MMM YYYY, HH:MM UTC
7. Amounts must include currency code.
8. Output valid JSON only.
9. LANGUAGE: All narrative text fields MUST be written entirely in the target language specified in the prompt.

Avoid: internal SAP terminology explanations, procurement theory, generic filler text, repeating the same information across fields."""


def _build_prompt(tender_data: dict, target_language: str, country_code: str = "DEFAULT") -> str:
    from core.country_formats import _build_country_instructions, COUNTRY_FORMATS
    data_block = json.dumps(tender_data, indent=2, ensure_ascii=False)
    country_instructions = _build_country_instructions(country_code)
    fmt = COUNTRY_FORMATS.get(country_code, COUNTRY_FORMATS["DEFAULT"])
    portal_name = fmt["portal"]
    return f"""Analyse the following SAP PPS Sourcing Project data and generate a supplier-facing tender synopsis.

TARGET LANGUAGE: {target_language}
IMPORTANT: Write ALL narrative text (executiveSummary, supplierActions, portalComplianceNote, and any "Not specified" messages) in {target_language}. Field labels must use portal-standard terminology. SAP field names in sapSource stay in English.

{country_instructions}

SOURCING PROJECT DATA:
{data_block}

Return only valid JSON with exactly these keys:
{{
  "tenderTitle": "<SourcingProjectName exactly as provided>",
  "executiveSummary": "<1-2 sentence supplier summary in {target_language} using portal terminology>",
  "portalName": "<portal name from TARGET PORTAL above, or 'Generic'>",
  "portalCountryCode": "<ISO country code or 'DEFAULT'>",
  "portalComplianceNote": "<1-2 sentences in {target_language} on compliance readiness>",
  "supplierFields": [
    {{
      "label": "<portal-standard label in {target_language}>",
      "value": "<actual SAP value; if absent: 'Not specified' in {target_language}>",
      "sapSource": "<SAP field name>",
      "category": "<overview | commercial | dates | eligibility>",
      "important": <true if critical for supplier; else false>
    }}
  ],
  "supplierActions": [
    "<imperative action from SupplierRegistrationDeadline in {target_language}>",
    "<imperative action from BidSubmissionDeadline in {target_language}>",
    "<imperative action from BidOpeningDateTime in {target_language}>"
  ],
  "portalMissingFields": [
    {{
      "label": "<mandatory portal field name>",
      "reason": "<why required for {portal_name}>"
    }}
  ],
  "missingInformation": ["<empty SAP fields>"],
  "sourceReferences": ["<SAP field names used>"],
  "language": "{target_language}"
}}

RULES FOR supplierFields:
1. Include ONLY fields meaningful to a supplier — omit internal SAP fields
2. Every mandatory portal field must appear — use 'Not specified' if SAP value absent
3. Use portal-standard label in {target_language}
4. category must be exactly: overview, commercial, dates, eligibility
5. Mark important: true for 3-5 most critical fields
6. Typical count: 8-14 fields

Do not hallucinate. Use only values present in the SAP data."""
