"""
SAP PPS connectivity — OData V4 fetch for Sourcing Projects.
Reads credentials from .env.local (A2A project) or environment variables.

Retry & Recovery:
  - Configurable retries with exponential backoff
  - In-memory cache fallback when SAP is unreachable
  - Auto-fallback to mock data when cache is empty
"""

import base64
import json
import logging
import os
import ssl
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)


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

# ── Retry configuration ───────────────────────────────────────────────────────
SAP_MAX_RETRIES  = int(os.environ.get("SAP_MAX_RETRIES", "3"))
SAP_TIMEOUT      = int(os.environ.get("SAP_TIMEOUT_SEC", "15"))   # reduced from 30
SAP_BACKOFF_BASE = float(os.environ.get("SAP_BACKOFF_BASE", "2")) # seconds: 2, 4, 8

# ── In-memory cache: sp_id -> tender_data (survives transient SAP failures) ──
_tender_cache: dict = {}


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
    """
    Returns neutral mock data when SAP is unreachable.
    Currency is USD (not SAR/INR/EUR) to avoid false country detection.
    CompanyCodeCountry is empty so user must select country manually in UI.
    Shows clearly this is offline/demo data.
    """
    return {
        "SourcingProject":              sourcing_project_id,
        "SourcingProjectVersion":       "1",
        "SourcingProjectName":          f"[OFFLINE] Sourcing Project {sourcing_project_id}",
        "SourcingProjectType":          "PPS Sourcing Project",
        "LifecycleStatus":              "In Preparation",
        "ApprovalStatus":               "",
        "ProcedureType":                "Restricted",
        "BidSubmissionDeadline":        "2026-09-30T12:00:00",
        "BidOpeningDateTime":           "2026-10-01T10:00:00",
        "ProjectStartDateTime":         "2026-08-01T08:00:00",
        "SupplierRegistrationDeadline": "2026-09-15T23:59:00",
        "TotalTargetAmount":            "100000",
        "DocumentCurrency":             "USD",
        "PurchasingOrganization":       "Demo Organization",
        "PurchasingGroup":              "Demo Buyer Group",
        "PurchasingCategory":           "",
        "MaterialGroup":                "General Goods",
        "ContractValidityStart":        "2026-11-01",
        "ContractValidityEnd":          "2027-10-31",
        "ExternalReference":            "",
        "CreatedBy":                    "DEMO_USER",
        "CreationDateTime":             "2026-07-14T08:00:00",
        "CompanyCode":                  "",
        "CompanyCodeCountry":           "",   # empty → forces manual country selection
        "SynopsisNotes":                "",
        "GeneralNotes":                 f"[SAP OFFLINE] Live data unavailable for SP {sourcing_project_id}. "
                                        f"This is placeholder data. Please select the correct country/portal manually.",
    }


# ── Main fetch function with retry & recovery ─────────────────────────────────

def fetch_tender_from_sap(sourcing_project_id: str, use_mock: bool = False) -> dict:
    """
    Fetch Sourcing Project from SAP PPS EMT 601 OData V4.

    Recovery strategy (2 levels):
      Level 1: Live SAP fetch with retry + exponential backoff
      Level 2: Return cached data if SAP is unreachable (same SP fetched before)
      No mock fallback — raises SAPUnavailableError with a clear message instead

    Config via env vars:
      SAP_MAX_RETRIES   (default: 3)
      SAP_TIMEOUT_SEC   (default: 15)
      SAP_BACKOFF_BASE  (default: 2.0 seconds)
    """
    if use_mock or not PPS_USER.strip() or not PPS_PASSWORD.strip():
        reason = "--mock flag" if use_mock else "SAP credentials not set"
        logger.warning(f"[SAP] {reason}. Using mock data.")
        return _mock_tender_data(sourcing_project_id)

    params = urllib.parse.urlencode({
        "$filter": f"SourcingProject eq '{sourcing_project_id}'",
        "$expand": "_NoteBasic",
        "sap-client": PPS_CLIENT,
    })
    url = f"{ODATA_BASE}/SourcingProject?{params}"
    logger.info(f"[SAP] Fetching SP {sourcing_project_id} from EMT 601")

    last_error = None

    for attempt in range(1, SAP_MAX_RETRIES + 1):
        try:
            opener = _build_opener()
            req = urllib.request.Request(url)
            req.add_header("Authorization", _auth_header())
            req.add_header("Accept", "application/json")

            response = opener.open(req, timeout=SAP_TIMEOUT)
            raw_bytes = response.read()
            try:
                payload = json.loads(raw_bytes.decode("utf-8"))
            except UnicodeDecodeError:
                payload = json.loads(raw_bytes.decode("latin-1"))

            records = payload.get("value", [])
            if not records:
                raise RuntimeError(
                    f"No Sourcing Project found for ID '{sourcing_project_id}' "
                    f"in EMT 601 client {PPS_CLIENT}."
                )

            result = _parse_sp_response(records[0], sourcing_project_id)

            # Cache on success for future fallback
            _tender_cache[sourcing_project_id] = result
            logger.info(f"[SAP] Fetched: {result.get('SourcingProjectName')} (attempt {attempt})")
            return result

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            # 4xx errors are not retryable (bad request / auth / not found)
            if 400 <= e.code < 500:
                raise RuntimeError(f"SAP OData error {e.code}: {body[:300]}") from e
            last_error = RuntimeError(f"SAP OData error {e.code}: {body[:300]}")
            logger.warning(f"[SAP] Attempt {attempt}/{SAP_MAX_RETRIES} failed: HTTP {e.code}")

        except Exception as e:
            last_error = e
            logger.warning(f"[SAP] Attempt {attempt}/{SAP_MAX_RETRIES} failed: {type(e).__name__}: {e}")

        # Exponential backoff before next retry (skip on last attempt)
        if attempt < SAP_MAX_RETRIES:
            wait = SAP_BACKOFF_BASE ** attempt
            logger.info(f"[SAP] Retrying in {wait:.0f}s...")
            time.sleep(wait)

    # All retries exhausted — try cache (same SP fetched successfully before)
    if sourcing_project_id in _tender_cache:
        logger.warning(
            f"[SAP] All {SAP_MAX_RETRIES} retries failed. "
            f"Serving cached data for SP {sourcing_project_id}. "
            f"Last error: {last_error}"
        )
        return _tender_cache[sourcing_project_id]

    # No cache — raise a clear error, no silent mock fallback
    error_msg = str(last_error) if last_error else "Unknown error"
    raise RuntimeError(
        f"SAP EMT 601 is unreachable for SP '{sourcing_project_id}'. "
        f"Please check the SAP system status and try again. "
        f"Technical details: {error_msg[:200]}"
    )


def _parse_sp_response(sp: dict, sourcing_project_id: str) -> dict:
    """Extract and normalise the 26 fields from a raw SourcingProject OData record."""
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
