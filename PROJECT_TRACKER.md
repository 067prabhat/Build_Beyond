# Use Case 4 — Tender Synopsis Generator
## Project Tracker & Development Log

> **Purpose:** AI-powered automatic generation of country-compliant tender synopses from SAP PPS Sourcing Projects.
> **Team:** BuildBeyond Programme · SAP PPS · Powered by Claude AI (claude-opus-4-5)

---

## Quick Reference

| Item | Value |
|---|---|
| SAP System | EMT 601 — `ldciemt.wdf.sap.corp:44322` |
| SAP Client | 601 |
| AI Model | `claude-opus-4-5` via Hyperspace proxy (`localhost:6655`) |
| Web UI | `http://localhost:8000` |
| A2A Agent | `http://localhost:9000` |
| SAP OData Service | `UI_SOURCINGPROJECT_MANAGE_2` (OData V4) |
| Test Project ID | SP 5189 — "Copy of Copy of Test RSP SP New" |

---

## Phase 1 — Prototype (USECASE_4)

### Step 1: Project Setup
- Created `USECASE_4/` folder under `BuildBeyond/`
- Added `.env` with SAP EMT 601 credentials and Hyperspace API key
- Added `.env.example` as a safe-to-commit credentials template

**Files created:** `.env`, `.env.example`, `generate_tender_synopsis.py`

---

### Step 2: SAP Data Fetch
**What:** Connected to SAP PPS EMT 601 via OData V4 using Python stdlib `urllib`.

**How it works:**
```
GET /SourcingProject
  ?$filter=SourcingProject eq '5189'
  &$expand=_NoteBasic
  &sap-client=601
```
- Basic Auth (Base64 encoded)
- SSL certificate verification disabled (`ssl.CERT_NONE`) — SAP uses self-signed cert
- Extracts **26 fields** from the JSON response

**Key SAP fields fetched:**

| SAP Field | Mapped Key | Meaning |
|---|---|---|
| `SourcingProjectName` | `SourcingProjectName` | Tender title |
| `QtnLatestSubmissionDateTime` | `BidSubmissionDeadline` | Bid closing date |
| `SrcgProjTotalTargetAmount` | `TotalTargetAmount` | Tender value |
| `PurchasingOrganizationName` | `PurchasingOrganization` | Procuring entity |
| `PPSSrcgProjProcedureTypeText` | `ProcedureType` | Open / Restricted |
| `CompanyCode` | `CompanyCode` | Used for country detection |
| `_NoteBasic` (type=SYNP) | `SynopsisNotes` | Synopsis text from Notes tab |

---

### Step 3: Claude AI Integration
**What:** Sends SAP data to Claude AI and gets a structured JSON synopsis back.

**Prompt design evolution:**

| Version | max_tokens | Approach | Problem |
|---|---|---|---|
| Original | 1024 | "Concise" — 2-4 sentences | Too brief, missing fields |
| Detailed | 4096 | 6-sentence paragraphs | Too verbose, hallucination risk |
| **Hybrid (current)** | **1500→2000** | **1-3 sentences + direct values** | ✅ Balanced |

**Current System Prompt rules:**
1. Use only SAP data — never invent facts
2. Missing fields → "Not specified"
3. Max 1-3 sentences per narrative field
4. Supplier-facing language only
5. Dates: `DD MMM YYYY, HH:MM UTC`
6. Amounts: include currency code
7. JSON output only
8. All narrative in target language

**Output schema (16 keys):**
`tenderTitle`, `executiveSummary`, `scopeOfWork`, `eligibility`, `procuringEntity`, `procedureType`, `tenderValue`, `materialGroup`, `lifecycleStatus`, `bidSubmissionDeadline`, `bidOpeningDate`, `supplierRegistrationDeadline`, `contractValidity`, `supplierActions[]`, `missingInformation[]`, `sourceReferences[]`

---

### Step 4: Human-in-the-Loop (HITL)
**What:** Officer reviews the generated synopsis before anything is saved.

**Options:** `[A] Approve` → saves .docx | `[E] Edit a field` → update value | `[R] Reject` → discard

**Output:** Formatted `.docx` file (python-docx, SAP Blue branding, audit trail)

---

### Step 5: FastAPI Web Server (`app.py`)
**What:** REST API backend serving the Fiori UI.

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Serve SAP Fiori HTML UI |
| `/generate` | POST | SAP fetch + Claude + return synopsis JSON |
| `/download/{id}` | GET | Stream .docx download |
| `/history` | GET | Last 50 synopsis entries |
| `/history/{id}` | GET | Load specific synopsis from history |
| `/img/*` | Static | Serve SAP logo SVG |

**Request body:** `{ project_id, language, country, use_mock }`

---

### Step 6: SAP Fiori UI (`templates/index.html`)
**What:** Browser UI built to SAP Fiori design standards.

**Components:**
- SAP Shell Bar with real SVG logo (`img/sap-logo.svg`)
- SAP colour tokens (`#0070F2` brand, `#1B2A4A` shell, `#32363A` text)
- Dynamic 3-tab layout (Overview / Commercial / Key Dates)
- Key Facts strip — `important=true` fields highlighted
- History drawer — side panel with last 50 synopses
- Portal badge, status badges (Restricted/Open, In Preparation/Published)
- Missing info (amber) + Portal missing fields (red)
- Download .docx button

---

### Step 7: Country-Specific Portal Formats
**What:** Auto-detects the country from SAP data and applies the correct national eProcurement portal format.

**Country detection — 4-level cascade:**
```
Level 1: CompanyCodeCountry (from API_COMPANYCODE_SRV second OData call)
Level 2: Org name keyword scan (e.g. "Ministry" → IN)
Level 3: Currency map (INR→IN, SAR→SA, GBP→GB, EUR→DE)
Level 4: DEFAULT fallback → user selects manually in UI
```

**Second OData call added:**
```
GET /sap/opu/odata/sap/API_COMPANYCODE_SRV/A_CompanyCode
  ?$filter=CompanyCode eq 'PS02'
  &$select=CompanyCode,Country
```
→ Returns `Country: DE` for PS02

**8 Portals supported:**

| Code | Portal | Standard | Date Format |
|---|---|---|---|
| IN | eProcure / GeM (India) | GFR 2017 | DD/MM/YYYY |
| US | SAM.gov (USA) | FAR/DFARS | MM/DD/YYYY |
| DE | TED / DTVP (EU/Germany) | Dir. 2014/24/EU | DD.MM.YYYY |
| SA | Etimad / Monafasat (Saudi) | NCAR | DD/MM/YYYY |
| AE | Tejari (UAE) | Fed. Law 6/2018 | DD/MM/YYYY |
| GB | Find a Tender (UK) | PCR 2015 | DD/MM/YYYY |
| FR | BOAMP (France) | Code commande | DD/MM/YYYY |
| AU | AusTender (Australia) | CPRs | DD/MM/YYYY |

**AI-driven supplierFields:** Claude maps each SAP value to the portal-specific label:
- India: `Tender Inviting Authority`, `Estimated Cost (INR)`, `Last Date & Time of Bid Submission`
- USA: `Contracting Office`, `Response Deadline`, `Solicitation Number`
- Germany: `Öffentlicher Auftraggeber`, `Vergabeverfahren`, `Schlusstermin für Angebote`

---

### Step 8: Language Support
**What:** Output language is selectable — Claude writes all narrative in the chosen language.

**17 languages supported:** English, German, French, Spanish, Arabic, Hindi, Chinese Simplified, Chinese Traditional, Japanese, Korean, Portuguese, Italian, Dutch, Russian, Turkish, Polish, Swedish

**Fix applied:** Added explicit `TARGET LANGUAGE` instruction to both system prompt and user prompt so Claude actually translates, not just labels.

---

### Step 9: History Feature
**What:** In-memory history of last 50 generated synopses, accessible via side drawer.

- Orange badge counter on History button
- Search by project ID, title, or procuring entity
- View → reloads synopsis without re-fetching SAP
- DOCX → downloads Word doc directly from history

---

## Phase 2 — A2A Agent (USECASE_4_A2A)

### Step 10: Framework — appfnd Agent Skills
**What:** Cloned `github.tools.sap/application-foundation/agent-skills` to get the SAP A2A agent bootstrap framework.

**Key skills used:**
- `sap-agent-bootstrap` — scaffolded the project structure
- `sap-agent-ord-endpoint` — added ORD discovery for Joule/UMS
- `sap-agent-run-local` — installed dependencies and ran locally

**Project structure created:**
```
USECASE_4_A2A/
├── app/
│   ├── main.py           ← A2A server (port 9000) + AgentCard
│   ├── agent.py          ← LangGraph 4-skill workflow
│   ├── agent_executor.py ← A2A protocol bridge
│   └── ord/              ← ORD discovery (system_type + system_instance JSON)
├── app.yaml              ← BTP deployment manifest
├── requirements.txt      ← All dependencies from SAP PyPI proxy
├── Dockerfile
├── .github/workflows/    ← CI/CD for BTP
└── tests/bruno/          ← Bruno API test collection
```

---

### Step 11: LangGraph 4-Skill Workflow (`agent.py`)
**What:** The core logic from USECASE_4 refactored into 4 independent skills orchestrated by LangGraph.

```
START (entry via next_skill)
  │
  ▼
[Skill 1] skill1_country_template
  → CompanyCode → API_COMPANYCODE_SRV → Country
  → Load COUNTRY_FORMATS portal template

[Skill 2] skill2_sap_fetch
  → OData V4 fetch → 26 SAP fields

[Skill 3] skill3_generate_synopsis
  → Claude AI → supplierFields[] JSON
  → HITL gate (hitl_wait node)

  ├── pending  → END (return input_required to caller)
  ├── approved → Skill 4
  └── rejected → END

[Skill 4] skill4_publish
  → save_synopsis_docx()
  → Publication reference: DRAFT-{SP_ID}-{COUNTRY}
  → [Production] POST to portal API
```

**Key design decisions:**
- Singleton `TenderSynopsisAgent` — state persists across A2A requests
- Conditional entry from `START` — `approve` jumps directly to Skill 4, skipping Skill 1+2+3
- `_state[context_id]` — multi-turn conversation memory per session

---

### Step 12: A2A Protocol & Multi-Turn HITL
**What:** Full A2A JSON-RPC 2.0 protocol with `context_id` for multi-turn conversations.

**Turn 1 — Generate:**
```json
POST /
{ "method": "message/send",
  "params": { "message": { "messageId": "m1", "role": "user",
    "parts": [{ "kind": "text", "text": "Generate synopsis for SP 5189" }]
  }}}
→ state: input-required  (HITL — user must review)
→ contextId: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

**Turn 2 — Approve:**
```json
POST /
{ "method": "message/send",
  "params": { "message": { "messageId": "m2",
    "contextId": "xxxxxxxx-...",   ← same context
    "parts": [{ "kind": "text", "text": "approve" }]
  }}}
→ state: completed
→ artifact: "Synopsis approved. Reference: DRAFT-5189-DE"
```

**Supported HITL commands:** `approve` | `reject` | `edit <field> <value>`

---

### Step 13: ORD Endpoint (Joule / UMS Discovery)
**What:** Added Open Resource Discovery endpoints so SAP UMS can discover the agent before provisioning and Joule can recommend it.

**Endpoints added:**
- `GET /.well-known/open-resource-discovery`
- `GET /open-resource-discovery/v1/documents/system-type`
- `GET /open-resource-discovery/v1/documents/system-instance`

**Agent namespace:** `sap.pps`
**ORD ID:** `sap.pps:agent:tender-synopsis-agent:v1`

---

### Step 14: Richer Synopsis Output (Option B)
**What:** `_format_synopsis()` updated to group fields by category with section headers — matching USECASE_4's tab layout.

**Grouped sections:**
```
📌 TENDER OVERVIEW      — who, what, status, procedure
💰 COMMERCIAL DETAILS   — value, contract, scope, eligibility
📅 KEY DATES            — registration, bid deadline, opening
✅ ELIGIBILITY          — qualification criteria
🎯 SUPPLIER ACTIONS     — 3 date-based imperative actions
⚠️  MISSING PORTAL FIELDS — with legal reason per missing field
```

---

### Step 15: Bruno API Test Collection
**What:** Pre-built Bruno request collection for testing the A2A agent.

**Location:** `USECASE_4_A2A/tests/bruno/`

| File | Purpose |
|---|---|
| `01 - Get Agent Card.bru` | Verify agent is running, check metadata |
| `02 - Get ORD Discovery.bru` | Verify Joule/UMS discovery endpoint |
| `03 - Generate Synopsis (Turn 1).bru` | Generate + auto-capture `context_id` |
| `04 - Approve (Turn 2).bru` | Approve with saved `context_id` |
| `05 - Reject (Turn 2).bru` | Reject synopsis |
| `06 - Edit Field (Turn 2).bru` | Edit a field value |
| `07 - Generate India Format.bru` | Test eProcure/GeM labels + assertions |
| `08 - Generate USA Format.bru` | Test SAM.gov labels + assertions |

**Key feature:** Request 03 auto-saves `context_id` to environment variable via `script:post-response` — no manual copy-paste needed for Turn 2.

---

## Bugs Fixed

| Bug | Root Cause | Fix |
|---|---|---|
| Synopsis too verbose | `max_tokens=1024` + "concise" rule | Hybrid prompt, `max_tokens=1500` |
| Language not working | No translation instruction in prompt | Added `TARGET LANGUAGE` to both prompts |
| Country always DEFAULT | `CompanyCodeCountry` empty on SP entity | Added second call to `API_COMPANYCODE_SRV` |
| `→` encoding crash | Windows `cp1252` can't encode `→` | Replaced `→` with `=>` in print statements |
| HITL recursion error | `hitl_wait` looped back to itself | Route `pending` → `END` (stop graph) |
| Multi-turn state lost | New `AgentExecutor` instance each call | Singleton `_agent_instance` at module level |
| Approve re-generates | `START → skill1` hardcoded entry | Conditional `START` routing via `next_skill` |
| Encoding on SAP fetch | Windows terminal display issue | Data is correct UTF-8; terminal-only artifact |

---

## Technology Stack

| Layer | Technology | Version |
|---|---|---|
| AI Model | claude-opus-4-5 via Hyperspace / SAP AI Core | — |
| Agent Framework | LangGraph + a2a-sdk | 1.2.6 / 0.3.22 |
| Web Backend | FastAPI + Uvicorn | 0.139 / 0.48 |
| LLM Client | LangChain-LiteLLM | 0.7.0 |
| SAP SDK | sap-cloud-sdk | 0.33 |
| Document Export | python-docx | 1.2 |
| UI Styling | Tailwind CSS (CDN) | — |
| SAP Connectivity | urllib + OData V4 | — |
| Python | Python 3.13 | — |

---

## Phase 3 — Refactoring & Production Readiness

### Step 16: Decoupled `core/` Package (Remove USECASE_4 Dependency)
**What:** Extracted all shared logic from `USECASE_4/generate_tender_synopsis.py` into a self-contained `core/` package inside `USECASE_4_A2A/app/core/`.

**Why:** `agent.py` was importing directly from USECASE_4 via `sys.path` manipulation — making USECASE_4_A2A dependent on the sibling folder being present on disk. Broken if moved, renamed, or deployed independently.

**New structure:**
```
app/core/
├── sap_client.py         ← fetch_tender_from_sap, _fetch_company_code_country, mock data
├── country_formats.py    ← COUNTRY_FORMATS (8 portals), _detect_country, _build_country_instructions
├── synopsis_generator.py ← SYSTEM_PROMPT, _build_prompt, generate_synopsis (Claude AI)
└── docx_exporter.py      ← save_synopsis_docx, _field_block, _section_heading
```

**agent.py before:**
```python
sys.path.insert(0, "../../USECASE_4")
from generate_tender_synopsis import (COUNTRY_FORMATS, _detect_country, ...)
```

**agent.py after:**
```python
from core.sap_client import fetch_tender_from_sap
from core.country_formats import COUNTRY_FORMATS, _detect_country
from core.synopsis_generator import generate_synopsis
from core.docx_exporter import save_synopsis_docx
```

**Verified:** Import test + live end-to-end A2A test (SP 5189, Germany format) passed. Bruno collection tested and working.

---

### Step 17: Enhancement Requirements Reviewed (PDF)
**What:** Reviewed `Usecase_4_enhancement.pdf` — 9 production-readiness requirements identified.

**Gap analysis summary:**

| # | Requirement | Status |
|---|---|---|
| 1 | Async processing | Partial — async def used, but skills sequential |
| 2 | Session persistence (PostgreSQL) | ❌ In-memory only |
| 3 | Workflow re-runs | ✅ Already fixed via conditional START routing |
| 4 | AI Validation node | ❌ Missing |
| 5 | Knowledge Base (Vector DB) | ❌ Hardcoded JSON dict |
| 6 | RAG Quality Checker | ❌ Not started (needs prod data) |
| 7 | Multi-agent architecture | ❌ Single agent (defer — not needed yet) |
| 8 | Retry & Recovery | ❌ No retry logic |
| 9 | Development standards | Partial — no PR workflow yet |

**Priority order for implementation:** 8 (retry) → 4 (AI validation) → 2 (session persistence)

| Feature | USECASE_4 (Web UI) | USECASE_4_A2A (Agent) |
|---|---|---|
| SAP live data fetch | ✅ | ✅ |
| Country auto-detection | ✅ | ✅ |
| 8-portal format support | ✅ | ✅ |
| AI synopsis generation | ✅ | ✅ |
| HITL review | ✅ (browser) | ✅ (multi-turn A2A) |
| .docx export | ✅ | ✅ |
| History | ✅ | ❌ (not yet) |
| 17 languages | ✅ | ✅ |
| A2A protocol | ❌ | ✅ |
| ORD / Joule discovery | ❌ | ✅ |
| Self-contained (no USECASE_4 dep) | — | ✅ core/ package |
| Bruno test collection | ❌ | ✅ (8 requests, tested) |
| Retry & Recovery | ❌ | ❌ |
| AI Validation node | ❌ | ❌ |
| Session persistence | ❌ | ❌ in-memory only |
| Skill 4 publish (real) | ❌ stub | ❌ stub |
| BTP deployment | ❌ local only | ❌ local only |

---

## Next Steps (Roadmap)

| Priority | Task | Notes |
|---|---|---|
| 🔴 High | Implement Skill 4 — real portal API publish | POST to eProcure/SAM.gov/TED API |
| 🔴 High | Add SAP fallback when EMT is down | Cache → mock auto-fallback |
| 🟡 Medium | BTP Cloud Foundry deployment | `cf push` + Destination Service + Cloud Connector |
| 🟡 Medium | Joule integration | Register via ORD → UMS provisioning → Joule skill |
| 🟡 Medium | Persistent history (file/DB) | Currently lost on server restart |
| 🟢 Low | Event Hub trigger | Auto-generate when SP is Published in SAP |
| 🟢 Low | Silverlit UI testing | Request access from App Foundation team |

---

## Run Commands

**USECASE_4 (Web UI):**
```cmd
cd "c:\Users\I775091\work@sap\BuildBeyond\USECASE_4"
C:\Users\I775091\AppData\Local\Microsoft\WindowsApps\python.exe -m uvicorn app:app --port 8000 --host 127.0.0.1
```
Open: http://localhost:8000

**USECASE_4_A2A (A2A Agent):**
```cmd
cd "c:\Users\I775091\work@sap\BuildBeyond\USECASE_4_A2A"
.venv\Scripts\python.exe app\main.py --host 0.0.0.0 --port 9000
```
Agent: http://localhost:9000 | Agent Card: http://localhost:9000/.well-known/agent-card.json
