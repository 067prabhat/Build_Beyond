# PPS Tender Synopsis (v3.0)

An A2A-compliant LangGraph agent that turns SAP PPS Sourcing Projects into
country-compliant supplier tender synopses, reviewed via HITL and delivered
as `.docx`.

Built on **SAP Application Foundation** with **A2A Protocol**, **LangGraph**,
**LiteLLM/SAP AI Core**, and **SAP Agent Memory**.

---

## What's new in v3.0

Seven co-ordinated enhancements moved the agent from a **hardcoded,
Claude-improvised** design to a **deterministic, memory-backed** system.

| # | Enhancement | Owner |
|---|---|---|
| E1 | Portal-template contract + 3-layer resolver (Live / Cache / File) | This repo |
| E2 | 48-hour template sync GH Action + semantic-diff auto-PR | **Other team** |
| E3 | Drift detection for portals without APIs (IN / SA / AE) | **Other team** |
| E4 | Centralised prompt registry (`app/prompts/`) | This repo |
| E5 | Weighted AI validation with critical-gate hard-fail | This repo |
| E6 | Section-superset consistency + response cache | This repo |
| E7 | Deterministic orchestrator + auto-rendered LangGraph diagram | This repo |
| E8 | SAP Agent Memory integration (checkpointer + semantic memories) | This repo |

> The **URL / live-fetch / drift-monitoring** pieces (E1 adapters, E2 & E3
> GitHub Actions) are being delivered by a separate team. This repo ships
> **Layer 3 + Layer 4** of the template loader and defines the
> `PortalAdapter` contract so their code plugs in with zero glue.

---

## Repository layout

```
PPS_Tender_Synopsis/
├── app.yaml                                # BTP Managed Runtime workload spec (adds hana-agent-memory)
├── Dockerfile                              # Container build
├── requirements.txt                        # +jinja2, +PyYAML, +sap-cloud-sdk[langgraph]
├── agent_spec.md                           # v2.0 spec — kept for reference
├── README.md                               # (this file)
│
├── app/
│   ├── main.py                             # A2A server entry (unchanged interface)
│   ├── agent_executor.py                   # A2A ↔ Agent bridge (unchanged)
│   ├── agent.py                            # LangGraph workflow — orchestrator + memory
│   │
│   ├── core/
│   │   ├── sap_client.py                   # SAP OData V4 fetch  (unchanged)
│   │   ├── docx_exporter.py                # .docx renderer     (unchanged)
│   │   ├── country_formats.py              # Country DETECTION only — dict removed
│   │   ├── portal_template.py              # 🆕 PortalTemplate + Section + FieldSpec
│   │   ├── template_loader.py              # 🆕 3-layer resolver (Live→Cache→File→Default)
│   │   ├── synopsis_generator.py           # Prompt registry + response cache
│   │   ├── synopsis_cache.py               # 🆕 1h TTL response cache
│   │   ├── validator.py                    # 🆕 Weighted score + critical gates
│   │   ├── hana_memory_saver.py            # 🆕 Persistent LangGraph checkpointer (SAP Agent Memory)
│   │   └── synopsis_memory.py              # 🆕 Agent Memory helpers (checkpointer / memories / feedback)
│   │
│   ├── prompts/                            # 🆕 CENTRALISED PROMPT REGISTRY
│   │   ├── __init__.py                     #    loader with @lru_cache
│   │   ├── manifest.yaml                   #    versions + weights + seeds
│   │   ├── system/synopsis_generator.md
│   │   ├── system/validator.md
│   │   ├── user/synopsis_generator.jinja
│   │   └── user/validator.jinja
│   │
│   ├── templates/                          # 🆕 PORTAL TEMPLATES (Layer 3)
│   │   ├── DEFAULT.json                    #    Layer 4 safety net
│   │   ├── IN.json  SA.json  AE.json       #    Hardcoded (no public API)
│   │   ├── US.json  DE.json  GB.json
│   │   ├── FR.json  AU.json                #    Refreshed nightly by other team's GH Action
│   │
│   ├── tools/
│   │   └── render_graph.py                 # 🆕 Auto-Mermaid diagram
│   │
│   ├── ord/                                # ORD discovery endpoints (unchanged)
│   └── ui/index.html                       # Test UI (badge + per-rule breakdown)
│
├── docs/
│   └── workflow.md                         # 🆕 Auto-generated (do not hand-edit)
│
├── tests/bruno/                            # 8 API tests (unchanged)
│
└── .github/workflows/
    ├── aggregate-dynamic-*.yml             # existing
    ├── aggregate-static-*.yml              # existing
    ├── managed-runtime-ci-cd.yml           # existing
    └── regenerate-graph-diagram.yml        # 🆕 auto-refresh diagram
```

---

## Workflow at a glance

```
                          START
                            │
                            ▼
              ┌─────────────────────────┐
              │      ORCHESTRATOR       │  ← pure Python router (E7)
              │   (single routing hub)  │
              └────────────┬────────────┘
                           │
     ┌──────────┬──────────┼──────────┬──────────┬──────────┐
     ▼          ▼          ▼          ▼          ▼          ▼
detect_country  load_template  sap_fetch  generate_synopsis  ai_validate  await_hitl  skill4_publish
     │          │          │          │          │          │          │
     └──────────┴──────────┴──────────┴──────────┴──────────┴──────────┘
                           │
                           ▼
                    (back to orchestrator)
                           │
                           ▼
                          END
```

The Mermaid version is auto-generated in
[`docs/workflow.md`](docs/workflow.md) whenever `app/agent.py` changes.

---

## Quick start (local)

```bash
cd PPS_Tender_Synopsis
python -m venv .venv
.venv\Scripts\activate         # PowerShell
pip install -r requirements.txt

# 1. Provide credentials in app/.env.local (see .env.local.example)
# 2. Start server
python app/main.py --host 0.0.0.0 --port 9000
```

Open http://localhost:9000/ui for the test chat, or use the Bruno collection
in `tests/bruno/`.

---

## Environment variables

### Required

| Variable | Purpose |
|---|---|
| `SAP_EMT601_USER` / `SAP_EMT601_PASSWORD` | SAP EMT 601 basic-auth |
| `HYPERSPACE_API_KEY` **or** `AICORE_CLIENT_ID`+`AICORE_SECRET` | AI backend |

### v3.0 additions (all optional with sensible defaults)

| Variable | Default | Effect |
|---|---|---|
| `TEMPLATE_CACHE_TTL_SEC` | `172800` (48h) | In-memory template cache TTL |
| `TEMPLATE_LIVE_TIMEOUT_SEC` | `5` | Timeout for Layer 1 live fetch |
| `TEMPLATE_PREFER_LIVE` | `true` | Set `false` to skip Layer 1 |
| `SYNOPSIS_CACHE_ENABLED` | `true` | 1h response cache (E6) |
| `SYNOPSIS_CACHE_TTL_SEC` | `3600` | Response cache TTL |
| `VALIDATOR_PASS_THRESHOLD` | `70` | Weighted score to pass validator |
| `VALIDATOR_CRITICAL_FLOOR` | `50` | Below this a critical rule blocks pass |
| `VALIDATOR_MAX_ATTEMPTS` | `2` | Max regeneration attempts |
| `AGENT_MEMORY_ENABLED` | `true` | Enable SAP Agent Memory (E8) |
| `AGENT_MEMORY_AGENT_ID` | `tender-synopsis-agent` | Agent id used in Memory service |
| `CLOUD_SDK_CFG_HANA_AGENT_MEMORY_DEFAULT_APPLICATION_URL` | — | Set by BTP binding |
| `CLOUD_SDK_CFG_HANA_AGENT_MEMORY_DEFAULT_UAA` | — | Set by BTP binding |

If `AGENT_MEMORY_ENABLED=false` or the SAP binding is missing, the agent
falls back to LangGraph `InMemorySaver` and disables few-shot / feedback
capture. Local dev without BTP still works.

---

## Enhancement details

### E1 — Portal Template Contract & 3-Layer Loader

The single source of truth for what a portal expects. See
[`app/core/portal_template.py`](app/core/portal_template.py) and
[`app/core/template_loader.py`](app/core/template_loader.py).

```
Layer 1  Live API adapter          (owned by other team)
   │       fail
Layer 2  In-memory cache (48h)     (this repo)
   │       miss/expired
Layer 3  app/templates/{ISO}.json  (this repo, committed to Git)
   │       missing
Layer 4  DEFAULT.json              (safety net)
```

Every layer returns a `PortalTemplate` — same shape whether it came from
SAM.gov live or a hand-maintained IN.json.

### E4 — Prompt Registry

All prompts live in [`app/prompts/`](app/prompts/) with a
[`manifest.yaml`](app/prompts/manifest.yaml) declaring version, temperature,
seed, and rubric weights.

```python
from prompts import load_prompt
p = load_prompt("synopsis_generator", template=..., tender_data_json=..., target_language=...)
# p["system"], p["user"], p["version"], p["max_tokens"], p["temperature"], p["seed"]
```

Bumping a prompt version + running snapshot tests is now the standard flow
for prompt tweaks.

### E5 — Weighted Validation

Instead of one opaque 0-100 score, the validator now returns per-rule scores.
`core/validator.py` applies **manifest weights + per-portal overrides**,
then enforces **critical-gate rules** (date_format, data_accuracy,
completeness) that hard-fail below 50 even if the overall score passes.

Per-portal overrides live in the template JSON:

```json
// app/templates/US.json
"validation_weights_override": {
  "data_accuracy": 25,   // US cares more about NAICS/PSC accuracy
  "portal_labels": 5
}
```

### E6 — Section Superset + Consistency

Claude no longer decides section names or ordering. Every template file
declares its full **`section_superset`** — Claude must:

- Use only these sections
- Use their exact titles
- Order them by `order`
- Skip optional sections that have no SAP data

Consistency knobs: `temperature=0.2`, `seed=42`, 1-hour response cache
keyed on `sha256(SP + version + template_hash + language + prompt_version)`.

### E7 — Orchestrator + Diagram

The `orchestrator()` node is a **pure-Python if/else** that decides the
next node based on state completeness and HITL decision.

- Every sub-skill returns to the orchestrator (star topology)
- Zero extra LLM calls, ~1 ms overhead per hop
- Adding a new skill = one case in `orchestrator()` — no edge surgery
- `python -m app.tools.render_graph` auto-generates `docs/workflow.md`

### E8 — SAP Agent Memory

Three integration points via [`app/core/synopsis_memory.py`](app/core/synopsis_memory.py):

**A) LangGraph Checkpointer**
Replaces the old `self._state: dict` singleton. HITL sessions are keyed by
A2A `context_id` = thread_id and are **persistent across pod restarts**
via [`HanaMemorySaver`](app/core/hana_memory_saver.py) — a
production-tested SAP checkpointer adopted from the negotiation-agent
reference implementation. Three-tier fallback:

  1. **`HanaMemorySaver`** — persistent, backed by SAP Agent Memory Service
     (checkpoints gzipped + base64 encoded + 64 KB chunked; parallel async
     writes; SHA-1 hashed thread ids for storage keying)
  2. **SDK factory** `create_checkpointer(ttl_seconds=...)` — in-memory
  3. **LangGraph `InMemorySaver`** — local-dev fallback

**B) Approved-Synopsis Memories**
Every HITL-approved synopsis is stored as a semantic memory via
`add_memory()`. On the next generation for the same invoker, up to 3
similar past approvals are retrieved via `search_memories()` and injected
as few-shot examples — Claude learns from your team's approvals.

**C) HITL Feedback Messages**
`add_message()` records every approve / reject / edit with
`message_group = SP ID`. The weekly drift-detection aggregator (owned by
the other team) queries these messages via `list_messages()` to spot
patterns like "field X was edited 15 times in a week → template drift".

---

## Local dev without BTP

Set `AGENT_MEMORY_ENABLED=false` in `app/.env.local`. The agent will use
LangGraph's `InMemorySaver` for HITL threading and skip the memory writes.
Everything else (templates, prompts, validator, orchestrator) works
unchanged.

---

## Deployment

```bash
# BTP Managed Runtime
cf push -f app.yaml
```

`app.yaml` declares two service bindings:
- `pps-destination` (Destination Service — SAP EMT 601 access)
- `hana-agent-memory` (v3.0 — see E8)

CI/CD via `.github/workflows/managed-runtime-ci-cd.yml` (unchanged).

---

## Ownership matrix

| Component | This repo | Other team |
|---|:---:|:---:|
| `PortalTemplate` dataclass | ✅ | |
| Template JSON files (IN/SA/AE/DEFAULT) | ✅ | |
| Template JSON files (US/DE/GB/FR/AU) — placeholder | ✅ | ⬅ refreshed nightly |
| `template_loader.py` Layers 3+4 | ✅ | |
| `PortalAdapter` protocol | ✅ (contract) | ⬅ implementations |
| `app/core/portal_adapters/*.py` | | ✅ |
| GH Action `portal-template-sync.yml` | | ✅ |
| GH Action `portal-drift-watch.yml` | | ✅ |
| `app/tools/sync_template.py`, `diff_templates.py`, `portal_hash_monitor.py`, etc. | | ✅ |
| Prompt registry (`app/prompts/`) | ✅ | |
| Weighted validator + critical gates | ✅ | |
| Section superset + response cache | ✅ | |
| Orchestrator + LangGraph diagram | ✅ | |
| SAP Agent Memory integration | ✅ | |

The other team's code slots into `template_loader.PORTAL_ADAPTERS` via a
single `register_adapter()` call at startup — no other changes needed.

---

## Roadmap (post-v3.0)

- Real portal publish adapters (POST to SAM.gov, TED, eProcure APIs)
- Event Mesh trigger on SAP "SP Published" → auto-generate
- PDF export alongside .docx
- Snapshot test corpus for every supported country
- Streaming progress messages (`status_message` events like negotiation-agent)
- OpenTelemetry span attributes per node
