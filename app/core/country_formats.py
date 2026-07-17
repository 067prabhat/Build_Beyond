"""
Country Detection - 4-level cascade for identifying which portal applies.

Portal templates themselves are NO LONGER kept here (v3.0 refactor).
They now live in app/templates/*.json and are resolved by template_loader.

This module keeps ONLY the country-detection logic used by Skill 1:
  Level 1: CompanyCodeCountry (from API_COMPANYCODE_SRV)
  Level 2: Org name keyword scan
  Level 3: Currency map (unambiguous only)
  Level 4: DEFAULT fallback
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


# ── Currency => country fallback (unambiguous only) ───────────────────────

_CURRENCY_TO_COUNTRY: dict[str, str] = {
    "INR": "IN",
    "SAR": "SA",
    "AED": "AE",
    "GBP": "GB",
    "AUD": "AU",
    "EUR": "DE",   # bias toward DE for Eurozone (TED template covers all EU)
    "CHF": "DE",
    # USD deliberately excluded - too many countries use it
}


# ── Org name keyword hints => country ────────────────────────────────────

_ORG_NAME_HINTS: dict[str, str] = {
    "india": "IN", "ministry": "IN", "government of india": "IN",
    "sam.gov": "US", "gsa": "US", "federal": "US", "defense": "US",
    "bundesmin": "DE", "ministerium": "DE", "vergabe": "DE",
    "saudi": "SA", "ksa": "SA", "etimad": "SA",
    "emirates": "AE", "uae": "AE", "abu dhabi": "AE", "dubai": "AE",
    "australia": "AU", "commonwealth": "AU", "austender": "AU",
    "france": "FR", "republique": "FR",
    "uk": "GB", "crown": "GB", "hmrc": "GB",
}


# Known countries we support - anything else falls to DEFAULT
_SUPPORTED_COUNTRIES = {"IN", "US", "DE", "SA", "AE", "GB", "FR", "AU"}


def _detect_country(tender_data: dict) -> str:
    """
    Return the ISO 2-letter country code for a tender's target portal.

    Cascade (returns as soon as a level succeeds):
      1. CompanyCodeCountry from API_COMPANYCODE_SRV (most reliable)
      2. Keyword scan on org/reference text
      3. Currency map (unambiguous currencies only)
      4. DEFAULT
    """

    # Level 1 - direct SAP CompanyCodeCountry
    code = (tender_data.get("CompanyCodeCountry") or "").strip().upper()
    if code and code in _SUPPORTED_COUNTRIES:
        logger.info(f"[CountryDetect] L1 CompanyCodeCountry -> {code}")
        return code

    # Level 2 - org name / reference keyword scan
    org_text = " ".join([
        tender_data.get("PurchasingOrganization") or "",
        tender_data.get("PurchasingGroup")        or "",
        tender_data.get("ExternalReference")      or "",
        tender_data.get("SourcingProjectName")    or "",
    ]).lower()
    for keyword, country in _ORG_NAME_HINTS.items():
        if keyword in org_text and country in _SUPPORTED_COUNTRIES:
            logger.info(f"[CountryDetect] L2 keyword '{keyword}' -> {country}")
            return country

    # Level 3 - currency
    currency = (tender_data.get("DocumentCurrency") or "").strip().upper()
    if currency in _CURRENCY_TO_COUNTRY:
        c = _CURRENCY_TO_COUNTRY[currency]
        if c in _SUPPORTED_COUNTRIES:
            logger.info(f"[CountryDetect] L3 currency '{currency}' -> {c}")
            return c

    # Level 4 - default
    logger.info(
        f"[CountryDetect] L4 DEFAULT "
        f"(CompanyCodeCountry='{code}', currency='{currency}')"
    )
    return "DEFAULT"


def supported_countries() -> set[str]:
    """Return the set of country codes that have a dedicated template file."""
    return set(_SUPPORTED_COUNTRIES)