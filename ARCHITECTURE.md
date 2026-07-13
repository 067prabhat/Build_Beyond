# Architecture — Use Case 4: Tender Synopsis Generator

## 1. Overall System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        SAP BuildBeyond — Use Case 4                         │
│                     AI-Powered Tender Synopsis Generator                     │
└─────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────┐          ┌──────────────────────────────────────┐
│      USECASE_4           │          │         USECASE_4_A2A                │
│   Prototype / Web UI     │          │     A2A Agent (LangGraph)            │
│   localhost:8000         │          │     localhost:9000                   │
└──────────────────────────┘          └──────────────────────────────────────┘
           │                                          │
           └──────────────────┬───────────────────────┘
                              │  Shared Core Logic
                              │  generate_tender_synopsis.py
                              │  ├── fetch_tender_from_sap()
                              │  ├── _fetch_company_code_country()
                              │  ├── _detect_country()
                              │  ├── generate_synopsis() → Claude AI
                              │  └── save_synopsis_docx()
                              │
           ┌──────────────────┴───────────────────────┐
           │                                          │
           ▼                                          ▼
┌─────────────────────┐                   ┌─────────────────────┐
│  SAP PPS EMT 601    │                   │  Claude AI (LLM)    │
│  OData V4 API       │                   │  via Hyperspace /   │
│  ldciemt.wdf.sap    │                   │  SAP AI Core        │
└─────────────────────┘                   └─────────────────────┘
```

---

## 2. USECASE_4 — Web UI Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           USECASE_4  (localhost:8000)                       │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                    Browser  (SAP Fiori UI)                           │   │
│  │  templates/index.html  —  Tailwind CSS  —  Vanilla JS               │   │
│  │                                                                     │   │
│  │  ┌─────────────┐  ┌──────────────────┐  ┌──────────────────────┐  │   │
│  │  │  Input Form  │  │ 3 Dynamic Tabs   │  │  History Drawer      │  │   │
│  │  │  SP ID       │  │ ├─ Overview      │  │  (last 50 synopses)  │  │   │
│  │  │  Language    │  │ ├─ Commercial    │  └──────────────────────┘  │   │
│  │  │  Country     │  │ └─ Key Dates     │                            │   │
│  │  └─────────────┘  └──────────────────┘                            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              │ HTTP REST                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                    FastAPI Backend  (app.py)                         │   │
│  │                                                                     │   │
│  │  POST /generate  →  fetch SAP  →  Claude  →  return synopsis JSON   │   │
│  │  GET  /download  →  save_synopsis_docx()  →  stream .docx file      │   │
│  │  GET  /history   →  return last 50 entries                          │   │
│  │  GET  /img/*     →  serve SAP logo SVG                              │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              │                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │               generate_tender_synopsis.py  (Core Engine)            │   │
│  │                                                                     │   │
│  │  fetch_tender_from_sap()                                            │   │
│  │   └── OData V4: /SourcingProject?$filter=...&$expand=_NoteBasic    │   │
│  │   └── API_COMPANYCODE_SRV → CompanyCode → Country                  │   │
│  │                                                                     │   │
│  │  _detect_country()                                                  │   │
│  │   └── Level 1: CompanyCodeCountry (from API call)                  │   │
│  │   └── Level 2: Org name keyword scan                               │   │
│  │   └── Level 3: Currency map (INR→IN, SAR→SA, GBP→GB...)           │   │
│  │   └── Level 4: DEFAULT fallback                                    │   │
│  │                                                                     │   │
│  │  COUNTRY_FORMATS  (8 portals)                                       │   │
│  │   ├── IN: eProcure/GeM  (GFR 2017)                                 │   │
│  │   ├── US: SAM.gov  (FAR/DFARS)                                     │   │
│  │   ├── DE: TED/DTVP  (EU Directive 2014/24)                         │   │
│  │   ├── SA: Etimad  (NCAR)                                           │   │
│  │   ├── AE: Tejari  (Fed Law 6/2018)                                 │   │
│  │   ├── GB: Find a Tender  (PCR 2015)                                │   │
│  │   ├── FR: BOAMP                                                    │   │
│  │   └── AU: AusTender  (CPRs)                                        │   │
│  │                                                                     │   │
│  │  generate_synopsis(tender_data, language, country_code)             │   │
│  │   └── Anthropic SDK → Hyperspace proxy → Claude claude-opus-4-5   │   │
│  │   └── Dynamic supplierFields[] with portal-specific labels         │   │
│  │   └── max_tokens: 2000                                             │   │
│  │                                                                     │   │
│  │  save_synopsis_docx()  →  .docx  (python-docx)                     │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. USECASE_4_A2A — A2A Agent Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      USECASE_4_A2A  (localhost:9000)                        │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │              A2A Protocol Layer  (a2a-sdk 0.3.22)                   │   │
│  │                                                                     │   │
│  │  POST /                →  message/send  (JSON-RPC 2.0)              │   │
│  │  GET  /.well-known/    →  agent-card.json                           │   │
│  │  GET  /ord/...         →  ORD discovery (for Joule/UMS)             │   │
│  │                                                                     │   │
│  │  AgentCard:  name, description, skills, capabilities, url           │   │
│  │  TaskStore:  InMemoryTaskStore (context_id + task_id tracking)      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              │                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │              AgentExecutor  (agent_executor.py)                     │   │
│  │                                                                     │   │
│  │  execute(context, event_queue)                                      │   │
│  │   └── get_user_input()  →  task.context_id  →  agent.stream()      │   │
│  │   └── TaskState.working / input_required / completed               │   │
│  │   └── Singleton pattern: reuses same TenderSynopsisAgent instance  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              │                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │              TenderSynopsisAgent  (agent.py)                        │   │
│  │                                                                     │   │
│  │  _state: dict[context_id → TenderSynopsisState]                    │   │
│  │   └── Persists across multi-turn conversations                     │   │
│  │                                                                     │   │
│  │  _parse_input(query, context_id)                                   │   │
│  │   └── New request   → parse SP ID, language, country               │   │
│  │   └── "approve"     → set hitl_decision=approved, skip to Skill 4  │   │
│  │   └── "reject"      → set hitl_decision=rejected                   │   │
│  │   └── "edit f v"    → update field, re-run Skill 3                 │   │
│  │                                                                     │   │
│  │  LangGraph Workflow:                                                │   │
│  │                                                                     │   │
│  │  START                                                              │   │
│  │    │  (routes via next_skill in state)                              │   │
│  │    ▼                                                                │   │
│  │  [Skill 1] skill1_country_template                                  │   │
│  │    │  CompanyCode → API_COMPANYCODE_SRV → Country                  │   │
│  │    │  Load COUNTRY_FORMATS portal template                         │   │
│  │    ▼                                                                │   │
│  │  [Skill 2] skill2_sap_fetch                                         │   │
│  │    │  OData V4 fetch → 26 SAP fields                               │   │
│  │    ▼                                                                │   │
│  │  [Skill 3] skill3_generate_synopsis                                 │   │
│  │    │  Claude AI → supplierFields[] → synopsis JSON                 │   │
│  │    ▼                                                                │   │
│  │  [HITL Gate] skill3_hitl_wait                                       │   │
│  │    │                                                                │   │
│  │    ├── pending  → END (return input_required to user)              │   │
│  │    ├── approved → Skill 4                                          │   │
│  │    └── rejected → END                                              │   │
│  │                         ▼                                           │   │
│  │                  [Skill 4] skill4_publish                           │   │
│  │                    │  save_synopsis_docx()                         │   │
│  │                    │  POST to portal API (stub → production later) │   │
│  │                    └── END (return completed)                      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │              ORD Endpoint  (app/ord/)                               │   │
│  │  GET /.well-known/open-resource-discovery                           │   │
│  │  GET /open-resource-discovery/v1/documents/system-type             │   │
│  │  GET /open-resource-discovery/v1/documents/system-instance         │   │
│  │  → Enables UMS to discover agent before provisioning               │   │
│  │  → Enables Joule to recommend agent by conversation context        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Data Flow — End to End

```
User Input: "Generate synopsis for SP 5189"
│
▼
┌──────────────────────────────────────────────────────────────────┐
│  SKILL 1 — Country & Template Discovery                          │
│                                                                  │
│  SAP PPS EMT 601                                                 │
│  GET /SourcingProject?$filter=SourcingProject eq '5189'         │
│      → CompanyCode: PS02                                         │
│                                                                  │
│  SAP API_COMPANYCODE_SRV                                         │
│  GET /A_CompanyCode?$filter=CompanyCode eq 'PS02'               │
│      → Country: DE                                               │
│                                                                  │
│  COUNTRY_FORMATS['DE']                                           │
│      → Portal: TED/DTVP                                          │
│      → Required fields: CPV Code, Lot Structure, Award Criteria  │
│      → Date format: DD.MM.YYYY                                   │
│      → Terminology: Vergabeverfahren, Öffentlicher Auftraggeber  │
└──────────────────────────────────────────────────────────────────┘
│
▼
┌──────────────────────────────────────────────────────────────────┐
│  SKILL 2 — SAP Data Extraction (26 fields)                       │
│                                                                  │
│  OData V4:  /SourcingProject?$expand=_NoteBasic                 │
│                                                                  │
│  Fields extracted:                                               │
│  SourcingProjectName, LifecycleStatus, ProcedureType            │
│  BidSubmissionDeadline, BidOpeningDateTime                       │
│  SupplierRegistrationDeadline, TotalTargetAmount                 │
│  DocumentCurrency, PurchasingOrganization, PurchasingGroup       │
│  MaterialGroup, ContractValidityStart/End                        │
│  SynopsisNotes (from _NoteBasic SYNP type)                       │
│  + 12 more fields                                                │
└──────────────────────────────────────────────────────────────────┘
│
▼
┌──────────────────────────────────────────────────────────────────┐
│  SKILL 3 — Claude AI Synopsis Generation                         │
│                                                                  │
│  Input:  26 SAP fields + country instructions + language         │
│                                                                  │
│  Prompt contains:                                                │
│  ├── TARGET LANGUAGE: English                                    │
│  ├── TARGET PORTAL: TED/DTVP [Directive 2014/24/EU]             │
│  ├── MANDATORY FIELDS: CPV Code, Award Criteria...              │
│  ├── DATE FORMAT: DD.MM.YYYY                                     │
│  └── TERMINOLOGY MAP: procedureType → Vergabeverfahren           │
│                                                                  │
│  Output JSON (16 keys):                                          │
│  ├── tenderTitle, executiveSummary, portalName                  │
│  ├── supplierFields[] (8-14 dynamic fields, portal-labelled)    │
│  ├── supplierActions[] (3 imperative date-based actions)        │
│  ├── portalMissingFields[] (with legal reason per field)        │
│  └── missingInformation[], sourceReferences[]                   │
└──────────────────────────────────────────────────────────────────┘
│
▼
┌──────────────────────────────────────────────────────────────────┐
│  HITL — Human Review                                             │
│                                                                  │
│  State: input_required                                           │
│                                                                  │
│  Displayed to user (grouped sections):                           │
│  📌 TENDER OVERVIEW    💰 COMMERCIAL DETAILS                     │
│  📅 KEY DATES          ✅ ELIGIBILITY                            │
│  🎯 SUPPLIER ACTIONS   ⚠️  MISSING PORTAL FIELDS                 │
│                                                                  │
│  User replies: approve / reject / edit <field> <value>          │
└──────────────────────────────────────────────────────────────────┘
│
▼
┌──────────────────────────────────────────────────────────────────┐
│  SKILL 4 — Publish                                               │
│                                                                  │
│  save_synopsis_docx() → TenderSynopsis_5189.docx                │
│  Publication ref:  DRAFT-5189-DE                                 │
│  Status: draft_saved                                             │
│                                                                  │
│  [Production] POST to TED/eProcure/SAM.gov API                  │
└──────────────────────────────────────────────────────────────────┘
│
▼
State: completed
Result: "Synopsis approved. Reference: DRAFT-5189-DE. Portal: TED/DTVP"
```

---

## 5. Deployment Architecture (Current vs Target)

```
CURRENT (Local Development)                TARGET (SAP BTP Production)
─────────────────────────────              ────────────────────────────────────

Developer Machine                          SAP BTP Subaccount
│                                          │
├── USECASE_4 (localhost:8000)             ├── Cloud Foundry App
│   FastAPI + Fiori HTML                   │   (cf push tender-synopsis-ui)
│                                          │
├── USECASE_4_A2A (localhost:9000)         ├── Cloud Foundry App
│   A2A Agent + LangGraph                  │   (cf push tender-synopsis-agent)
│                                          │
├── .env  (hardcoded credentials)          ├── BTP Destination Service
│   SAP_EMT601_URL=ldciemt...              │   → Named destination for EMT 601
│   HYPERSPACE_API_KEY=...                 │   → OAuth2/mTLS auth (no secrets)
│                                          │
├── Hyperspace proxy                       ├── SAP AI Core
│   localhost:6655/anthropic               │   → claude-opus-4-5 via GenAI Hub
│   (Claude via local proxy)               │   → AICORE_* env vars from BTP
│                                          │
└── Direct HTTPS to EMT 601               ├── Cloud Connector
    (requires VPN / internal network)      │   → Secure tunnel: BTP → EMT 601
                                           │   (no VPN needed for end users)
                                           │
                                           ├── Joule Integration
                                           │   → ORD endpoint → UMS discovery
                                           │   → Agent appears in Joule
                                           │   → Natural language: "Synopsis for SP 5189"
                                           │
                                           └── Event Hub (optional)
                                               → SP Published event → auto-trigger
                                               → Synopsis generated automatically
```

---

## 6. Component Summary

| Component | Technology | Purpose |
|---|---|---|
| Web UI | HTML + Tailwind CSS + Vanilla JS | SAP Fiori-compliant browser interface |
| API Server (UI) | FastAPI + Uvicorn | REST endpoints for UI |
| A2A Agent | a2a-sdk + LangGraph | Conversational agent with HITL |
| ORD Endpoint | Starlette routes + JSON | Joule/UMS discovery |
| SAP Fetch | Python urllib + OData V4 | Live data from SAP PPS |
| Country Detection | 4-level cascade | Auto-detect portal from CompanyCode |
| AI Generation | Claude claude-opus-4-5 | Country-specific synopsis |
| Document Export | python-docx | Formatted .docx output |
| State Management | In-memory dict | Multi-turn HITL conversation |
| Config | .env / BTP Destination | Credentials and endpoints |
