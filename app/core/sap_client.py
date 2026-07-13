"""
SAP PPS connectivity — OData V4 fetch for Sourcing Projects.
Reads credentials from .env.local (A2A project) or environment variables.
"""

import base64
import json
import os
import ssl
import urllib.request
import urllib.parse
from pathlib import Path


# ── Load .env.local from app/ directory ──────────────────────────────────────
def _load_env():
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

_load_env()

# ── SAP PPS System Configuration — EMT 601 ───────────────────────────────────
PPS_SYSTEM_URL = os.environ.get("SAP_EMT601_URL", "https://ldciemt.wdf.sap.corp:44322")
PPS_CLIENT     = os.environ.get("SAP_EMT601_CLIENT", "601")
PPS_USER       = os.environ.get("SAP_EMT601_USER", " ")
PPS_PASSWORD   = os.environ.get("SAP_EMT601_PASSWORD", " ")

ODATA_BASE = (
    f"{PPS_SYSTEM_URL}/sap/opu/odata4/sap/ui_sourcingproject_manage_2"
    f"/srvd/sap/ui_sourcingproject_manage_2/0001"
)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _build_opener():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def _auth_header() -> str:
    credentials = base64.b64encode(f"{PPS_USER}:{PPS_PASSWORD}".encode()).decode()
    return f"Basic {credentials}"


def _fetch_company_code_country(company_code: str) -> str:
    """
    Fetch the Country key for a CompanyCode via SAP OData V2 API_COMPANYCODE_SRV.
    Returns ISO 2-letter country code (e.g. 'DE', 'IN', 'US') or '' on failure.
    """
    if not company_code or not PPS_USER.strip() or not PPS_PASSWORD.strip():
        return ""
    try:
        params = urllib.parse.urlencode({
            "$filter": f"CompanyCode eq '{company_code}'",
            "$select": "CompanyCode,Country,Currency",
            "$format": "json",
            "sap-client": PPS_CLIENT,
        })
        url = f"{PPS_SYSTEM_URL}/sap/opu/odata/sap/API_COMPANYCODE_SRV/A_CompanyCode?{params}"
        opener = _build_opener()
        req = urllib.request.Request(url)
        req.add_header("Authorization", _auth_header())
        req.add_header("Accept", "application/json")
        resp = opener.open(req, timeout=10)
        raw = resp.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            payload = json.loads(raw.decode("latin-1"))
        records = payload.get("d", {}).get("results", [])
        if records:
            country = (records[0].get("Country") or "").strip().upper()
            if country:
                print(f"[INFO] CompanyCode '{company_code}' => Country '{country}' (via API_COMPANYCODE_SRV)")
                return country
    except Exception as e:
        print(f"[WARN] Could not fetch CompanyCode country for '{company_code}': {e}")
    return ""


# ── Mock data (fallback when SAP is unreachable) ──────────────────────────────

def _mock_tender_data(sourcing_project_id: str) -> dict:
    return {
        "SourcingProject":              sourcing_project_id,
        "SourcingProjectVersion":       "1",
        "SourcingProjectName":          "Supply and Installation of Smart Street Lighting Systems",
        "SourcingProjectType":          "Public Tender",
        "LifecycleStatus":              "Published",
        "ApprovalStatus":               "Approved",
        "ProcedureType":                "Open",
        "BidSubmissionDeadline":        "2026-09-15T14:00:00",
        "BidOpeningDateTime":           "2026-09-16T10:00:00",
        "ProjectStartDateTime":         "2026-07-04T08:00:00",
        "SupplierRegistrationDeadline": "2026-08-31T23:59:00",
        "TotalTargetAmount":            "45000000",
        "DocumentCurrency":             "SAR",
        "PurchasingOrganization":       "Ministry of Public Works",
        "PurchasingGroup":              "Infrastructure Procurement",
        "PurchasingCategory":           "Infrastructure Works",
        "MaterialGroup":                "Electrical Equipment",
        "ContractValidityStart":        "2026-11-01",
        "ContractValidityEnd":          "2029-10-31",
        "ExternalReference":            "EXT-2026-SPL-0042",
        "CreatedBy":                    "PROC_OFFICER_01",
        "CreationDateTime":             "2026-06-20T10:30:00",
        "CompanyCode":                  "",
        "CompanyCodeCountry":           "SA",
        "SynopsisNotes": (
            "Supply, deliver, install, test and commission 5,000 units of solar-powered "
            "smart street lighting systems across municipalities in the Northern Province. "
            "Scope includes civil works, electrical cabling, a central monitoring and control "
            "system, and a 3-year maintenance contract post-commissioning.\n\n"
            "Eligibility: Bidders must be registered with the National Contractors Registration Board (NCRB) "
            "with minimum Grade 5 classification. Minimum annual turnover of SAR 10 million for "
            "last 3 financial years required. Joint ventures permitted with lead partner holding "
            "at least 51% share. ISO 9001:2015 certification mandatory."
        ),
        "GeneralNotes": "",
    }


# ── Main fetch function ───────────────────────────────────────────────────────

def fetch_tender_from_sap(sourcing_project_id: str, use_mock: bool = False) -> dict:
    """
    Fetch Sourcing Project from SAP PPS EMT 601 OData V4.
    Falls back to mock data when use_mock=True or credentials are not set.
    """
    if use_mock or not PPS_USER.strip() or not PPS_PASSWORD.strip():
        reason = "--mock flag" if use_mock else "SAP credentials not set"
        print(f"[WARN] {reason}. Using mock data for demo.")
        return _mock_tender_data(sourcing_project_id)

    params = urllib.parse.urlencode({
        "$filter": f"SourcingProject eq '{sourcing_project_id}'",
        "$expand": "_NoteBasic",
        "sap-client": PPS_CLIENT,
    })

    url = f"{ODATA_BASE}/SourcingProject?{params}"
    print(f"[INFO] Fetching from SAP PPS EMT 601: {url}")

    opener = _build_opener()
    req = urllib.request.Request(url)
    req.add_header("Authorization", _auth_header())
    req.add_header("Accept", "application/json")

    try:
        response = opener.open(req, timeout=30)
        raw_bytes = response.read()
        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except UnicodeDecodeError:
            payload = json.loads(raw_bytes.decode("latin-1"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"SAP OData error {e.code}: {body[:400]}") from e

    records = payload.get("value", [])
    if not records:
        raise RuntimeError(
            f"No Sourcing Project found for ID '{sourcing_project_id}' in EMT 601 client {PPS_CLIENT}."
        )

    sp = records[0]

    notes = sp.get("_NoteBasic", {})
    if isinstance(notes, dict):
        notes = notes.get("value", [])
    synopsis_notes, general_notes = [], []
    for note in (notes or []):
        note_type = note.get("TextObjectType", "") or note.get("NoteType", "")
        note_text = note.get("NoteText") or note.get("LongText") or note.get("Text", "")
        if not note_text:
            continue
        if "SYNP" in note_type:
            synopsis_notes.append(note_text)
        else:
            general_notes.append(f"[{note_type}] {note_text}")

    return {
        "SourcingProject":              sp.get("SourcingProject", sourcing_project_id),
        "SourcingProjectVersion":       sp.get("SourcingProjectVersion", ""),
        "SourcingProjectName":          sp.get("SourcingProjectName", ""),
        "SourcingProjectType":          sp.get("SourcingProjectTypeText", ""),
        "LifecycleStatus":              sp.get("SrcgProjLifecycleStatusName", ""),
        "ApprovalStatus":               sp.get("SrcgProjApprovalStatusName", ""),
        "ProcedureType":                sp.get("PPSSrcgProjProcedureTypeText", ""),
        "BidSubmissionDeadline":        sp.get("QtnLatestSubmissionDateTime", ""),
        "BidOpeningDateTime":           sp.get("PPSSrcgProjOpngDateTime", ""),
        "ProjectStartDateTime":         sp.get("PPSSrcgProjStrtDateTime", ""),
        "SupplierRegistrationDeadline": sp.get("PPSSrcPrjSuplrRegnDdlnDateTime", ""),
        "TotalTargetAmount":            sp.get("SrcgProjTotalTargetAmount", ""),
        "DocumentCurrency":             sp.get("DocumentCurrency", ""),
        "PurchasingOrganization":       sp.get("PurchasingOrganizationName", ""),
        "PurchasingGroup":              sp.get("PurchasingGroupName", ""),
        "MaterialGroup":                sp.get("MaterialGroupName", ""),
        "PurchasingCategory":           sp.get("PurgCatName", ""),
        "ContractValidityStart":        sp.get("PurContrValidityStartDate", ""),
        "ContractValidityEnd":          sp.get("PurContrValidityEndDate", ""),
        "ExternalReference":            sp.get("ExternalSourcingProjectRef", ""),
        "CreatedBy":                    sp.get("CreatedByUser", ""),
        "CreationDateTime":             sp.get("CreationDateTime", ""),
        "CompanyCode":                  sp.get("CompanyCode", ""),
        "CompanyCodeCountry":           sp.get("CompanyCodeCountry", "")
                                        or _fetch_company_code_country(sp.get("CompanyCode", "")),
        "SynopsisNotes":                "\n\n".join(synopsis_notes) if synopsis_notes else "",
        "GeneralNotes":                 "\n\n".join(general_notes) if general_notes else "",
    }
