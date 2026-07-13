"""
Claude AI synopsis generation.
Sends SAP tender data + country-specific instructions to Claude
and returns a structured JSON synopsis.
"""

import json
import os

import anthropic

from core.country_formats import COUNTRY_FORMATS, _detect_country, _build_country_instructions


# ── AI configuration ──────────────────────────────────────────────────────────
HYPERSPACE_BASE_URL = os.environ.get("HYPERSPACE_URL", "http://localhost:6655/anthropic")
HYPERSPACE_API_KEY  = os.environ.get("HYPERSPACE_API_KEY", " ")
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")

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
      "value": "<actual SAP value, formatted per DATE FORMAT; if absent: 'Not specified' in {target_language}>",
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
1. Include ONLY fields meaningful to a supplier — omit internal SAP fields (CreatedBy, SourcingProjectVersion, etc.)
2. Every mandatory portal field must appear — use 'Not specified' if SAP value absent
3. Use portal-standard label in {target_language}
4. Map SAP values to portal fields intelligently
5. category must be exactly: overview, commercial, dates, eligibility
6. Mark important: true for 3-5 most critical fields
7. Typical count: 8-14 fields

Do not hallucinate. Use only values present in the SAP data."""


def generate_synopsis(tender_data: dict, target_language: str = "English",
                      country_code: str = "AUTO") -> dict:
    """
    Call Claude AI and return a structured synopsis dict.
    Uses SAP AI Core (via AICORE_* env vars) in production,
    or Hyperspace proxy locally.
    """
    if country_code == "AUTO":
        country_code = _detect_country(tender_data)

    print(f"[INFO] Calling Claude ({CLAUDE_MODEL})...")
    print(f"[INFO] Country/portal: {country_code} => {COUNTRY_FORMATS.get(country_code, COUNTRY_FORMATS['DEFAULT'])['portal']}")

    client = anthropic.Anthropic(
        base_url=HYPERSPACE_BASE_URL,
        api_key=HYPERSPACE_API_KEY,
    )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_prompt(tender_data, target_language, country_code)}],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude returned invalid JSON:\n{raw}\n\nError: {e}") from e
