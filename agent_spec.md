# Agent Specification — Tender Synopsis Generator
## USECASE_4_A2A · SAP Application Foundation · A2A Agent

---

## 1. Agent Identity

| Field | Value |
|---|---|
| **Agent Name** | Tender Synopsis Agent |
| **Agent ID** | `tender-synopsis-agent` |
| **Version** | 2.0 |
| **ORD Namespace** | `sap.pps` |
| **ORD ID** | `sap.pps:agent:tender-synopsis-agent:v1` |
| **Protocol** | A2A JSON-RPC 2.0 (a2a-sdk 0.3.22) |
| **Framework** | LangGraph 1.2.6 + SAP App Foundation |
| **Model** | claude-opus-4-5 via SAP AI Core / Hyperspace proxy |
| **Port** | 9000 |

**Description:**
An AI agent that fetches SAP PPS Sourcing Projects from EMT 601, detects the country-specific eProcurement portal format, generates a validated tender synopsis using Claude AI, supports human review with inline editing, and saves an approved .docx document — all via the A2A protocol with multi-turn conversation.

---

## 2. Agent Skills

The agent implements **4 LangGraph skills** plus an **AI Validation node**:

### Skill 1 — Country & Template Discovery
**Node:** `skill1_country_template`

| | |
|---|---|
| **Input** | `sourcing_project_id`, `country_override` ("AUTO" or ISO code) |
| **Output** | `country_code`, `portal_name`, `required_fields[]`, `tender_data` |
| **SAP Call** | `GET /SourcingProject?$filter=...` + `GET /A_CompanyCode?$filter=...` |
| **Logic** | If override set → use it. Else fetch SAP → `_detect_country()` 4-level cascade |
| **Passes forward** | `tender_data` to Skill 2 (no double fetch) |

**Country Detection Cascade:**
```
L1: CompanyCodeCountry from API_COMPANYCODE_SRV (most reliable)
L2: Org name keyword scan (Ministry→IN, Federal→US, Ministerium→DE)
L3: Currency map (INR→IN, SAR→SA, GBP→GB — USD excluded)
L4: DEFAULT fallback → user selects manually
```

---

### Skill 2 — SAP Data Extraction
**Node:** `skill2_sap_fetch`

| | |
|---|---|
| **Input** | `sourcing_project_id`, `tender_data` (from Skill 1) |
| **Output** | `tender_data` (26 normalised fields) |
| **SAP Call** | Reuses data from Skill 1 if available (no duplicate OData call) |
| **Retry** | 3 attempts, exponential backoff 2s/4s/8s |
| **On failure** | Cache → RuntimeError (no silent mock fallback) |

**26 Fields Extracted:**

| Category | Fields |
|---|---|
| Identity | SourcingProject, SourcingProjectVersion, SourcingProjectName, SourcingProjectType |
| Status | LifecycleStatus, ApprovalStatus, ProcedureType |
| Dates | BidSubmissionDeadline, BidOpeningDateTime, ProjectStartDateTime, SupplierRegistrationDeadline |
| Commercial | TotalTargetAmount, DocumentCurrency |
| Organisation | PurchasingOrganization, PurchasingGroup, MaterialGroup, PurchasingCategory |
| Contract | ContractValidityStart, ContractValidityEnd |
| Reference | ExternalReference, CreatedBy, CreationDateTime, CompanyCode, CompanyCodeCountry |
| Notes | SynopsisNotes (from `_NoteBasic` SYNP type), GeneralNotes |

---

### Skill 3 — Synopsis Generation
**Node:** `skill3_generate_synopsis`

| | |
|---|---|
| **Input** | `tender_data`, `language`, `country_code` |
| **Output** | `synopsis` (JSON with `supplierFields[]` + metadata) |
| **AI Call** | Claude claude-opus-4-5, max_tokens=2000 |
| **Prompt** | Country-specific instructions + portal terminology + date format |
| **Edit path** | If `hitl_decision="edit"` → apply `hitl_batch_edits[]` → skip AI validation |

**Synopsis Output Schema (16 keys):**
```json
{
  "tenderTitle": "...",
  "executiveSummary": "...",
  "portalName": "...",
  "portalCountryCode": "...",
  "portalComplianceNote": "...",
  "supplierFields": [
    {
      "label": "<portal-specific label in target language>",
      "value": "<SAP value>",
      "sapSource": "<SAP field name>",
      "category": "overview|commercial|dates|eligibility",
      "important": true|false
    }
  ],
  "supplierActions": ["...","...","..."],
  "portalMissingFields": [{"label":"...","reason":"..."}],
  "missingInformation": ["..."],
  "sourceReferences": ["..."],
  "language": "..."
}
```

---

### AI Validation Node
**Node:** `skill_ai_validate`

| | |
|---|---|
| **Input** | `synopsis`, `tender_data`, `country_code`, `language` |
| **Output** | `validation_score` (0-100), `validation_passed`, `validation_issues[]`, optionally `fixed_synopsis` |
| **AI Call** | Second Claude call — quality review prompt |
| **Max attempts** | 2 regeneration cycles |

**Checks performed:**
1. Date format consistency (DD.MM.YYYY for DE, MM/DD/YYYY for US, etc.)
2. Portal-specific label usage (German labels for TED, Hindi for IN, etc.)
3. Data accuracy — values traceable to SAP source fields
4. Completeness — mandatory portal fields present or flagged
5. Supplier actions — contain actual dates, not "Not specified"
6. Important flags — critical fields correctly marked
7. Uniformity — consistent formatting across all fields
8. Executive summary consistency — no contradictions with field values

**Decision logic:**
```
score ≥ 70           → proceed to HITL (green badge ✅)
score < 70 + fix     → use fixed_synopsis → proceed to HITL (amber badge ⚠️)
score < 70 + no fix  → regenerate Skill 3 (max 2 attempts)
validator error      → proceed to HITL anyway (non-blocking)
```

---

### Skill 4 — Publish
**Node:** `skill4_publish`

| | |
|---|---|
| **Input** | `synopsis` (approved, may include edits), `sourcing_project_id` |
| **Output** | `publication_ref`, `docx_path`, `docx_filename`, `download_url` |
| **Action** | Saves `.docx` to `output_docs/TenderSynopsis_{SP_ID}_{TIMESTAMP}.docx` |
| **Future** | POST to eProcure / SAM.gov / TED portal API |

---

## 3. LangGraph Workflow

```
START
  │  (conditional entry — approve/edit skip to correct node directly)
  ▼
[skill1] → country detection + SAP fetch
  ▼
[skill2] → reuse data (no second SAP call)
  ▼
[skill3] → Claude synopsis generation
  │  fresh generate → ai_validate
  │  after edit     → hitl_wait (skip validation)
  ▼
[ai_validate] → quality check
  │  score ≥ 70 or max attempts → hitl_wait
  │  score < 70 + fix           → hitl_wait (with fixed synopsis)
  │  score < 70 + no fix        → skill3 (regenerate)
  ▼
[hitl_wait] → HITL gate
  │  pending  → END (A2A state: input_required)
  │  approved → skill4
  │  rejected → END
  ▼
[skill4] → save .docx, return download_url
  ▼
END (A2A state: completed)
```

---

## 4. A2A Protocol

**Endpoint:** `POST http://localhost:9000/`

**Turn 1 — Generate:**
```json
{
  "jsonrpc": "2.0",
  "method": "message/send",
  "params": {
    "message": {
      "messageId": "msg-001",
      "role": "user",
      "parts": [{"kind": "text", "text": "Generate synopsis for SP 5189"}]
    }
  }
}
→ state: input-required
→ content: {"__synopsis__": {...}, "__validation_score__": 82, ...}
```

**Turn 2 — HITL options:**
```
"approve"                          → runs Skill 4, returns download_url
"reject"                           → ends session
{"__batch_edit__": [{label,value}]}→ updates fields, re-renders synopsis
```

**Turn 2 — Approve response:**
```json
→ state: completed
→ content: {"__approved__": true, "__ref__": "DRAFT-5189-DE",
             "__download_url__": "/download/TenderSynopsis_5189_20260714.docx"}
```

---

## 5. State Schema

```python
class TenderSynopsisState(TypedDict):
    # Input
    sourcing_project_id: str
    language: str
    country_override: str        # "AUTO" | ISO code e.g. "IN", "DE"
    # Skill 1
    country_code: str
    portal_name: str
    required_fields: list
    # Skill 2
    tender_data: dict
    # Skill 3
    synopsis: dict
    hitl_decision: str           # "pending" | "approved" | "rejected" | "edit"
    hitl_edit_field: str
    hitl_edit_value: str
    hitl_batch_edits: list       # [{label, value}, ...]
    # AI Validation
    validation_score: int
    validation_passed: bool
    validation_issues: list
    validation_attempts: int
    # Skill 4
    publication_ref: str
    publication_status: str
    docx_path: str
    docx_filename: str
    # Control
    error: str
    next_skill: str
```

---

## 6. Portal Formats Supported

| Code | Portal | Standard | Date Format | Mandatory Fields |
|---|---|---|---|---|
| IN | eProcure / GeM (India) | GFR 2017 | DD/MM/YYYY | NIT Number, EMD Amount, Tender Fee, Work Location |
| US | SAM.gov (USA) | FAR/DFARS | MM/DD/YYYY | Solicitation Number, NAICS Code, PSC Code, CAGE Code |
| DE | TED / DTVP (EU/Germany) | Dir. 2014/24/EU | DD.MM.YYYY | CPV Code, Lot Structure, Award Criteria |
| SA | Etimad (Saudi Arabia) | NCAR | DD/MM/YYYY | Tender Number, GOSI Certificate, Nitaqat % |
| AE | Tejari (UAE) | Fed. Law 6/2018 | DD/MM/YYYY | Tender Reference, Performance Bond |
| GB | Find a Tender (UK) | PCR 2015 | DD/MM/YYYY | CPV Code, Award Criteria, Lots |
| FR | BOAMP (France) | Code commande | DD/MM/YYYY | Code CPV, Valeur estimée |
| AU | AusTender (Australia) | CPRs | DD/MM/YYYY | ATM ID, UNSPSC Code |
| DEFAULT | Generic Portal | Standard | DD MMM YYYY | None required |

---

## 7. SAP Integration

**System:** EMT 601 (`ldciemt.wdf.sap.corp:44322`, client 601)

**OData V4 — Sourcing Project:**
```
GET /sap/opu/odata4/sap/ui_sourcingproject_manage_2/
    srvd/sap/ui_sourcingproject_manage_2/0001/
    SourcingProject?$filter=SourcingProject eq '{id}'&$expand=_NoteBasic
```

**OData V2 — Company Code (country lookup):**
```
GET /sap/opu/odata/sap/API_COMPANYCODE_SRV/
    A_CompanyCode?$filter=CompanyCode eq '{code}'&$select=CompanyCode,Country
```

**Auth:** HTTP Basic Auth
**SSL:** Self-signed cert (verify disabled)
**Health check:** `GET /sap/public/ping` → "Server reached successfully"

**Error handling:**
- 4xx → fail immediately (not retryable)
- 5xx / network → 3 retries with exponential backoff
- All retries fail + cache → serve cached data
- All retries fail + no cache → `RuntimeError: SAP EMT 601 is unreachable`

---

## 8. AI Backend

**Primary (Production):** SAP AI Core via LiteLLM
```python
model = "sap/anthropic--claude-4.5-sonnet"
ChatLiteLLM(model=model).invoke(messages)
# Credentials: AICORE_CLIENT_ID, AICORE_SECRET, AICORE_AUTH_URL, AICORE_BASE_URL
```

**Fallback (Local Dev):** Hyperspace proxy
```python
anthropic.Anthropic(base_url="http://localhost:6655/anthropic", api_key=HYPERSPACE_API_KEY)
# Auto-fallback when AI Core model deployment unavailable
```

---

## 9. Discovery & Engagement Layer

**ORD Endpoints (Joule / UMS discovery):**
```
GET /.well-known/open-resource-discovery
GET /open-resource-discovery/v1/documents/system-type
GET /open-resource-discovery/v1/documents/system-instance
```

**Agent Card:**
```
GET /.well-known/agent-card.json
{
  "name": "Tender Synopsis Agent",
  "description": "...",
  "protocolVersion": "0.3.0",
  "capabilities": {"streaming": true},
  "skills": [{"id": "tender-synopsis-agent", "tags": ["tender","sap-pps","procurement"]}]
}
```

**Engagement options:**
| Layer | Status | Notes |
|---|---|---|
| Test UI (`/ui`) | ✅ Available | Chat interface at localhost:9000/ui |
| Bruno tests | ✅ Available | `tests/bruno/` — 8 requests |
| Joule | ❌ Pending | Needs BTP deployment + Joule Skills access |
| Event Hub | ❌ Pending | Auto-trigger on SP Publish event |

---

## 10. Deployment

**Local:**
```cmd
cd USECASE_4_A2A
.venv\Scripts\python.exe app\main.py --host 0.0.0.0 --port 9000
```

**BTP (Target):**
```yaml
# app.yaml
metadata:
  name: tender-synopsis-agent
  namespace: tender-synopsis-agent
spec:
  container:
    buildPath: .
    port: 9000
  models:
    - executableId: aws-bedrock
      name: anthropic--claude-4.5-sonnet
  requires:
    - name: pps-destination
      service: destination
      plan: lite
```

**CI/CD:** `.github/workflows/managed-runtime-ci-cd.yml` (BTP Managed Runtime)

---

## 11. Roadmap

| Priority | Enhancement | Description |
|---|---|---|
| 🔴 High | Session persistence | Replace in-memory `_state` with Redis/PostgreSQL — survives restarts |
| 🔴 High | Async parallel skills | Run Skill 1+2 in parallel with `asyncio.gather` |
| 🟡 Medium | Knowledge Base | Move `COUNTRY_FORMATS` from hardcoded dict to PostgreSQL/SAP HANA Cloud |
| 🟡 Medium | BTP deployment | `cf push` + Destination Service + Cloud Connector for private network |
| 🟡 Medium | Joule integration | Register via ORD → UMS provisioning → natural language in Joule |
| 🟢 Low | Real portal publish | POST approved synopsis to eProcure/SAM.gov/TED API (Skill 4) |
| 🟢 Low | RAG quality checker | Vector DB of approved synopses → few-shot examples for Claude |
| 🟢 Low | Event Hub trigger | Auto-generate when SP is Published in SAP (event-driven) |
