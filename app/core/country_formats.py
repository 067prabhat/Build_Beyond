"""
Country / Portal Format Registry.
Maps ISO country codes to national eProcurement portal requirements,
terminology, date formats, and mandatory fields.
"""

# ── Portal format definitions ─────────────────────────────────────────────────

COUNTRY_FORMATS: dict = {
    "IN": {
        "portal":          "eProcure / GeM Portal (India)",
        "flag":            "IN",
        "standard":        "GFR 2017 / CVC Guidelines",
        "required_fields": ["NIT Number", "Estimated Cost (INR)", "EMD Amount",
                            "Tender Fee", "Bid Validity Period", "Work Location",
                            "Contractor Category / Class"],
        "date_format":     "DD/MM/YYYY",
        "terminology":     {
            "procedureType":         "Tender Type",
            "procuringEntity":       "Tender Inviting Authority (TIA)",
            "bidSubmissionDeadline": "Last Date & Time of Bid Submission",
            "tenderValue":           "Estimated Cost / Tender Value",
            "contractValidity":      "Contract Period",
        },
    },
    "US": {
        "portal":          "SAM.gov (USA)",
        "flag":            "US",
        "standard":        "FAR / DFARS",
        "required_fields": ["Solicitation Number", "NAICS Code", "PSC Code",
                            "Set-Aside Type", "Place of Performance", "CAGE Code",
                            "Contract Type (FFP / T&M / CPFF)"],
        "date_format":     "MM/DD/YYYY",
        "terminology":     {
            "procedureType":         "Solicitation Type",
            "procuringEntity":       "Contracting Office",
            "bidSubmissionDeadline": "Response Deadline",
            "tenderValue":           "Estimated Contract Value",
            "contractValidity":      "Period of Performance",
        },
    },
    "DE": {
        "portal":          "TED / DTVP (EU / Germany)",
        "flag":            "DE",
        "standard":        "Directive 2014/24/EU / VgV",
        "required_fields": ["CPV Code", "Lot Structure", "Prior Information Notice Ref",
                            "Estimated Value (EUR)", "Award Criteria",
                            "Suitability Requirements (Technical / Financial)"],
        "date_format":     "DD.MM.YYYY",
        "terminology":     {
            "procedureType":         "Vergabeverfahren",
            "procuringEntity":       "Oeffentlicher Auftraggeber",
            "bidSubmissionDeadline": "Schlusstermin fuer Angebote",
            "tenderValue":           "Geschaetzter Auftragswert",
            "contractValidity":      "Laufzeit des Auftrags",
        },
    },
    "SA": {
        "portal":          "Etimad / Monafasat (Saudi Arabia)",
        "flag":            "SA",
        "standard":        "NCAR (National Competitive Bidding)",
        "required_fields": ["Tender Number", "Tender Category", "CR Number",
                            "GOSI Certificate Requirement", "Zakat Certificate",
                            "Saudization Percentage (Nitaqat)", "Performance Bond %"],
        "date_format":     "DD/MM/YYYY",
        "terminology":     {
            "procedureType":         "Tender Procedure Type",
            "procuringEntity":       "Procuring Entity",
            "bidSubmissionDeadline": "Bid Submission Deadline",
            "tenderValue":           "Estimated Value",
            "contractValidity":      "Contract Duration",
        },
    },
    "AE": {
        "portal":          "Tejari / Federal Portal (UAE)",
        "flag":            "AE",
        "standard":        "Federal Law No. 6/2018",
        "required_fields": ["Tender Reference", "Category", "Performance Bond",
                            "Trade License", "Vendor Registration Certificate"],
        "date_format":     "DD/MM/YYYY",
        "terminology":     {},
    },
    "GB": {
        "portal":          "Find a Tender / Contracts Finder (UK)",
        "flag":            "GB",
        "standard":        "PCR 2015 / Procurement Act 2023",
        "required_fields": ["Tender Reference", "CPV Code", "Above / Below Threshold",
                            "Award Criteria", "Lots"],
        "date_format":     "DD/MM/YYYY",
        "terminology":     {},
    },
    "FR": {
        "portal":          "BOAMP / Marches Publics (France)",
        "flag":            "FR",
        "standard":        "Code de la commande publique",
        "required_fields": ["Reference de l'avis", "Code CPV", "Valeur estimee (EUR)",
                            "Criteres d'attribution", "Lots"],
        "date_format":     "DD/MM/YYYY",
        "terminology":     {},
    },
    "AU": {
        "portal":          "AusTender (Australia)",
        "flag":            "AU",
        "standard":        "Commonwealth Procurement Rules (CPRs)",
        "required_fields": ["ATM ID", "UNSPSC Code", "Estimated Value (AUD)",
                            "Indigenous Procurement Policy", "SME Participation"],
        "date_format":     "DD/MM/YYYY",
        "terminology":     {},
    },
    "DEFAULT": {
        "portal":          "Generic Tender Portal",
        "flag":            "GL",
        "standard":        "Standard procurement notice",
        "required_fields": [],
        "date_format":     "DD MMM YYYY",
        "terminology":     {},
    },
}

# ── Currency => country fallback (unambiguous only) ───────────────────────────

_CURRENCY_TO_COUNTRY: dict = {
    "INR": "IN", "SAR": "SA", "AED": "AE", "GBP": "GB",
    "AUD": "AU", "CAD": "CA", "JPY": "JP", "CNY": "CN",
    "BRL": "BR", "ZAR": "ZA", "SGD": "SG", "MYR": "MY",
    "CHF": "DE",
    "EUR": "DE",
    # USD excluded — used by 50+ countries
}

# ── Org name keyword hints => country ─────────────────────────────────────────

_ORG_NAME_HINTS: dict = {
    "india": "IN", "ministry": "IN", "government of india": "IN",
    "sam.gov": "US", "gsa": "US", "federal": "US", "defense": "US",
    "bundesmin": "DE", "ministerium": "DE", "vergabe": "DE",
    "saudi": "SA", "ksa": "SA", "etimad": "SA",
    "emirates": "AE", "uae": "AE", "abu dhabi": "AE", "dubai": "AE",
    "australia": "AU", "commonwealth": "AU", "austender": "AU",
    "france": "FR", "republique": "FR",
    "uk": "GB", "crown": "GB", "hmrc": "GB",
}


# ── Country detection — 4-level cascade ──────────────────────────────────────

def _detect_country(tender_data: dict) -> str:
    """
    Return ISO 2-letter country code from SAP tender data.

    Level 1: CompanyCodeCountry (from API_COMPANYCODE_SRV second OData call)
    Level 2: Org name keyword scan
    Level 3: DocumentCurrency map (unambiguous only)
    Level 4: DEFAULT fallback
    """
    # Level 1
    code = (tender_data.get("CompanyCodeCountry") or "").strip().upper()
    if code and code in COUNTRY_FORMATS:
        print(f"[INFO] Country detected via CompanyCodeCountry: {code}")
        return code

    # Level 2
    org_text = " ".join([
        tender_data.get("PurchasingOrganization") or "",
        tender_data.get("PurchasingGroup") or "",
        tender_data.get("ExternalReference") or "",
    ]).lower()
    for keyword, country in _ORG_NAME_HINTS.items():
        if country and keyword in org_text and country in COUNTRY_FORMATS:
            print(f"[INFO] Country detected via org name hint '{keyword}': {country}")
            return country

    # Level 3
    currency = (tender_data.get("DocumentCurrency") or "").strip().upper()
    country_from_currency = _CURRENCY_TO_COUNTRY.get(currency, "")
    if country_from_currency and country_from_currency in COUNTRY_FORMATS:
        print(f"[INFO] Country detected via currency '{currency}': {country_from_currency}")
        return country_from_currency

    # Level 4
    print(f"[INFO] Country not detected — using DEFAULT "
          f"(CompanyCodeCountry='{tender_data.get('CompanyCodeCountry','')}', "
          f"Currency='{currency}')")
    return "DEFAULT"


def _build_country_instructions(country_code: str) -> str:
    """Return prompt instructions for the target country's portal format."""
    fmt = COUNTRY_FORMATS.get(country_code, COUNTRY_FORMATS["DEFAULT"])
    portal   = fmt["portal"]
    standard = fmt["standard"]
    req      = fmt["required_fields"]
    date_fmt = fmt["date_format"]
    terms    = fmt["terminology"]

    lines = [
        f"TARGET PORTAL: {portal} [{standard}]",
        f"DATE FORMAT: Use {date_fmt} for all dates in this response.",
    ]
    if req:
        lines.append("MANDATORY FIELDS FOR THIS PORTAL (include in output if derivable from SAP data):")
        for f in req:
            lines.append(f"  - {f}")
    if terms:
        lines.append("USE THESE TERMS (replace generic labels with portal-specific terminology):")
        for generic, specific in terms.items():
            lines.append(f"  - '{generic}' => '{specific}'")
    lines += [
        "If a mandatory portal field cannot be derived from SAP data, list it in 'portalMissingFields'.",
        "Add a 'portalComplianceNote' key: 1-2 sentences summarising compliance readiness for this portal.",
        "Add a 'portalName' key: the exact portal name string above.",
        "Add a 'portalCountryCode' key: the ISO country code.",
    ]
    return "\n".join(lines)
