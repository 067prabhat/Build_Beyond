"""
Tender Synopsis Agent — LangGraph 4-Skill Workflow.

Skills:
  Skill 1 — Country & template discovery  (skill1_country_template)
  Skill 2 — SAP data extraction           (skill2_sap_fetch)
  Skill 3 — Synopsis generation + HITL    (skill3_generate_synopsis + hitl_wait)
  Skill 4 — Publish to portal             (skill4_publish)

Multi-turn conversation is supported via context_id.
HITL review uses A2A task state input_required.
"""

import logging
import os
import re
import sys
from pathlib import Path
from typing import AsyncGenerator, TypedDict

from langgraph.graph import END, START, StateGraph

from core.sap_client import fetch_tender_from_sap
from core.country_formats import COUNTRY_FORMATS, _detect_country
from core.synopsis_generator import generate_synopsis
from core.docx_exporter import save_synopsis_docx

logger = logging.getLogger(__name__)


# ── Agent State ────────────────────────────────────────────────────────────

class TenderSynopsisState(TypedDict):
    sourcing_project_id: str
    language: str
    country_override: str
    # Skill 1 outputs
    country_code: str
    portal_name: str
    required_fields: list
    # Skill 2 outputs
    tender_data: dict
    # Skill 3 outputs
    synopsis: dict
    hitl_decision: str
    hitl_edit_field: str
    hitl_edit_value: str
    hitl_batch_edits: list   # [{label, value}, ...] for inline edit
    # AI Validation outputs
    validation_score: int
    validation_passed: bool
    validation_issues: list
    validation_attempts: int
    # Skill 4 outputs
    publication_ref: str
    publication_status: str
    docx_path: str           # full path to saved .docx
    docx_filename: str       # filename only e.g. TenderSynopsis_5189_20260714.docx
    # Control
    error: str
    next_skill: str


# ── Skill 1: Country & Template Discovery ─────────────────────────────────

async def skill1_country_template(state: TenderSynopsisState) -> TenderSynopsisState:
    logger.info("[Skill 1] Country & template discovery")
    try:
        override = state.get("country_override", "AUTO")

        if override not in ("AUTO", "DEFAULT", ""):
            # User explicitly selected a country in UI — use it directly
            country_code = override
            logger.info(f"[Skill 1] Country override from UI: {country_code}")
            # Fetch SAP data now so Skill 2 can reuse it
            tender_data = fetch_tender_from_sap(state["sourcing_project_id"])
        else:
            # Auto-detect: fetch SAP data and detect country from it
            tender_data = fetch_tender_from_sap(state["sourcing_project_id"])
            is_offline = tender_data.get("SourcingProjectName", "").startswith("[OFFLINE]")
            if is_offline:
                # SAP offline and mock data has no country — use DEFAULT
                country_code = "DEFAULT"
                logger.warning("[Skill 1] SAP offline — country detection unavailable, using DEFAULT")
            else:
                country_code = _detect_country(tender_data)

        fmt = COUNTRY_FORMATS.get(country_code, COUNTRY_FORMATS["DEFAULT"])
        logger.info(f"[Skill 1] Country={country_code} Portal={fmt['portal']}")
        return {
            **state,
            "country_code":   country_code,
            "portal_name":    fmt["portal"],
            "required_fields": fmt["required_fields"],
            "tender_data":    tender_data,   # pass forward so Skill 2 reuses it
            "next_skill":     "skill2",
        }
    except Exception as e:
        logger.exception("[Skill 1] Error")
        return {**state, "error": f"Skill 1 failed: {e}", "next_skill": "end"}


# ── Skill 2: SAP Data Extraction ──────────────────────────────────────────

async def skill2_sap_fetch(state: TenderSynopsisState) -> TenderSynopsisState:
    logger.info(f"[Skill 2] SAP data for SP {state['sourcing_project_id']}")
    try:
        # Reuse tender_data already fetched in Skill 1 if available
        existing_data = state.get("tender_data", {})
        if existing_data and existing_data.get("SourcingProjectName"):
            logger.info(f"[Skill 2] Reusing data from Skill 1: {existing_data.get('SourcingProjectName')}")
            tender_data = existing_data
        else:
            tender_data = fetch_tender_from_sap(state["sourcing_project_id"])
            logger.info(f"[Skill 2] Fetched: {tender_data.get('SourcingProjectName')}")

        is_offline = tender_data.get("SourcingProjectName", "").startswith("[OFFLINE]")
        if is_offline:
            logger.warning("[Skill 2] SAP unreachable — using offline placeholder data")
        return {**state, "tender_data": tender_data, "next_skill": "skill3"}
    except Exception as e:
        logger.exception("[Skill 2] SAP fetch error")
        return {**state, "error": f"Skill 2 failed: {e}", "next_skill": "end"}


# ── Skill 3: Synopsis Generation + HITL ───────────────────────────────────

async def skill3_generate_synopsis(state: TenderSynopsisState) -> TenderSynopsisState:
    logger.info("[Skill 3] Generating synopsis")
    try:
        # Handle batch edit re-entry — UI sends all changed fields at once
        if state.get("hitl_decision") == "edit":
            synopsis = dict(state.get("synopsis", {}))
            import json as _json

            # batch_edits: list of {label, value} dicts
            batch = state.get("hitl_batch_edits", [])
            if batch:
                supplier_fields = synopsis.get("supplierFields", [])
                for edit in batch:
                    lbl = edit.get("label", "").lower()
                    val = edit.get("value", "")
                    matched = False
                    for f in supplier_fields:
                        if f.get("label", "").lower() == lbl or \
                           f.get("sapSource", "").lower() == lbl:
                            f["value"] = val
                            matched = True
                            break
                    if not matched:
                        # top-level key (tenderTitle, executiveSummary, etc.)
                        synopsis[edit.get("label", "")] = val
                logger.info(f"[Skill 3] Batch edit applied: {len(batch)} field(s) updated")
            elif state.get("hitl_edit_field"):
                # Single field edit (fallback / text command)
                field, value = state["hitl_edit_field"], state["hitl_edit_value"]
                for f in synopsis.get("supplierFields", []):
                    if f.get("label", "").lower() == field.lower() or \
                       f.get("sapSource", "").lower() == field.lower():
                        f["value"] = value
                        break
                else:
                    synopsis[field] = value

            return {**state, "synopsis": synopsis,
                    "hitl_decision": "pending", "next_skill": "hitl_wait"}

        synopsis = generate_synopsis(
            state["tender_data"],
            state.get("language", "English"),
            state.get("country_code", "DEFAULT"),
        )
        logger.info(f"[Skill 3] Done: {synopsis.get('tenderTitle')}")
        return {**state, "synopsis": synopsis,
                "hitl_decision": "pending", "next_skill": "ai_validate"}
    except Exception as e:
        logger.exception("[Skill 3] Error")
        return {**state, "error": f"Skill 3 failed: {e}", "next_skill": "end"}


# ── AI Validation Node ────────────────────────────────────────────────────────

async def skill_ai_validate(state: TenderSynopsisState) -> TenderSynopsisState:
    """
    AI Validation — second Claude call to review the generated synopsis.
    Checks: portal format, date format, data accuracy, completeness, uniformity.
    If score < 70 and attempts < 2, returns to Skill 3 for regeneration.
    Otherwise proceeds to HITL.
    """
    import json as _json
    import anthropic as _anthropic

    synopsis     = state.get("synopsis", {})
    tender_data  = state.get("tender_data", {})
    country_code = state.get("country_code", "DEFAULT")
    language     = state.get("language", "English")
    portal_name  = synopsis.get("portalName", "Generic")
    attempts     = state.get("validation_attempts", 0)

    logger.info(f"[AI Validator] Validating synopsis for {portal_name} (attempt {attempts + 1})")

    validation_prompt = f"""You are a quality validator for SAP PPS tender synopses.

Review the following generated synopsis and check for quality issues.

PORTAL: {portal_name}
COUNTRY: {country_code}
LANGUAGE: {language}

ORIGINAL SAP DATA:
{_json.dumps(tender_data, indent=2, ensure_ascii=False)[:4000]}

GENERATED SYNOPSIS (complete — do NOT report truncation as an issue):
{_json.dumps(synopsis, indent=2, ensure_ascii=False)}

Check ALL of the following:
1. DATE FORMAT: Are dates formatted correctly for the portal?
   - DE/EU: DD.MM.YYYY  |  IN: DD/MM/YYYY  |  US: MM/DD/YYYY  |  Generic: DD MMM YYYY
2. PORTAL LABELS: Are field labels using correct portal-specific terminology (not generic English unless it's the Generic portal)?
3. DATA ACCURACY: Do all field values match the SAP source data? Flag any value not traceable to the SAP data.
4. COMPLETENESS: Are all mandatory portal fields either filled or listed in portalMissingFields?
5. SUPPLIER ACTIONS: Do all 3 actions contain actual dates (not "Not specified") when dates exist in SAP data?
6. IMPORTANT FLAGS: Are the 3-5 most critical supplier fields marked important=true?
7. UNIFORMITY: Are all dates in the same format? Are currency amounts consistent?
8. EXECUTIVE SUMMARY: Does it match the field values (no contradictions)?

Return ONLY valid JSON:
{{
  "score": <0-100 integer>,
  "passed": <true if score >= 70>,
  "issues": ["<specific issue 1>", "<specific issue 2>"],
  "fixed_synopsis": <corrected synopsis JSON if score < 70 and fixable, else null>
}}

Be strict but fair. Score 90-100 = excellent, 70-89 = acceptable, below 70 = needs correction."""

    try:
        from core.synopsis_generator import _load_env_local, _get_ai_config
        _load_env_local()
        base_url, api_key, model = _get_ai_config()

        # Always use Hyperspace for validation (direct Anthropic SDK call)
        # Fall back to AI Core LiteLLM if Hyperspace not available
        hyperspace_key = api_key.strip() if api_key else ""
        hyperspace_url = base_url or "http://localhost:6655/anthropic"
        use_model      = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")

        if hyperspace_key:
            client = _anthropic.Anthropic(base_url=hyperspace_url, api_key=hyperspace_key)
            msg = client.messages.create(
                model=use_model,
                max_tokens=2000,
                messages=[{"role": "user", "content": validation_prompt}],
            )
            raw = msg.content[0].text.strip()
        else:
            # LiteLLM path
            from langchain_litellm import ChatLiteLLM
            from langchain_core.messages import HumanMessage
            llm = ChatLiteLLM(model=os.environ.get("CLAUDE_MODEL", "sap/anthropic--claude-4.5-sonnet"))
            resp = llm.invoke([HumanMessage(content=validation_prompt)])
            raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:].strip()
        result = _json.loads(raw)

        score   = int(result.get("score", 0))
        passed  = result.get("passed", score >= 70)
        issues  = result.get("issues", [])
        fixed   = result.get("fixed_synopsis")

        logger.info(f"[AI Validator] Score: {score}/100, Passed: {passed}, Issues: {len(issues)}")

        if not passed and attempts < 2 and fixed:
            # Use the fixed synopsis and skip back to HITL (don't re-run Skill 3)
            logger.info("[AI Validator] Using AI-fixed synopsis")
            return {**state,
                    "synopsis": fixed,
                    "validation_score": score,
                    "validation_passed": True,
                    "validation_issues": issues,
                    "validation_attempts": attempts + 1,
                    "next_skill": "hitl_wait"}
        elif not passed and attempts < 2:
            # No fixed synopsis provided — regenerate via Skill 3
            logger.info("[AI Validator] Regenerating synopsis via Skill 3")
            return {**state,
                    "validation_score": score,
                    "validation_passed": False,
                    "validation_issues": issues,
                    "validation_attempts": attempts + 1,
                    "hitl_decision": "pending",
                    "next_skill": "skill3"}
        else:
            # Passed or max attempts reached — proceed to HITL
            return {**state,
                    "validation_score": score,
                    "validation_passed": passed,
                    "validation_issues": issues,
                    "validation_attempts": attempts + 1,
                    "next_skill": "hitl_wait"}

    except Exception as e:
        logger.warning(f"[AI Validator] Validation failed: {e} — proceeding to HITL anyway")
        return {**state,
                "validation_score": -1,
                "validation_passed": True,
                "validation_issues": [f"Validation skipped: {e}"],
                "validation_attempts": attempts + 1,
                "next_skill": "hitl_wait"}


async def skill3_hitl_wait(state: TenderSynopsisState) -> TenderSynopsisState:
    """
    HITL gate. When pending, route to END to stop the graph — the stream()
    method handles the input_required signal to the A2A layer.
    On re-entry after user decision, route to skill4 or end.
    """
    decision = state.get("hitl_decision", "pending")
    if decision == "approved":
        return {**state, "next_skill": "skill4"}
    elif decision == "rejected":
        return {**state, "next_skill": "end"}
    # pending — stop the graph, stream() will yield require_user_input=True
    return {**state, "next_skill": "end"}


# ── Skill 4: Publish to Portal ────────────────────────────────────────────

async def skill4_publish(state: TenderSynopsisState) -> TenderSynopsisState:
    logger.info(f"[Skill 4] Publishing to {state.get('portal_name')}")
    try:
        synopsis = state["synopsis"]
        sp_id    = state["sourcing_project_id"]

        # Save to permanent output_docs/ folder (not temp — survives state cleanup)
        from datetime import datetime as _dt
        timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path(__file__).parent.parent / "output_docs"
        output_dir.mkdir(exist_ok=True)
        filename  = f"TenderSynopsis_{sp_id}_{timestamp}.docx"
        docx_path = str(output_dir / filename)

        save_synopsis_docx(synopsis, sp_id, docx_path)
        ref = f"DRAFT-{sp_id}-{synopsis.get('portalCountryCode', 'XX')}"
        logger.info(f"[Skill 4] .docx saved: {docx_path}, ref={ref}")
        return {**state, "publication_ref": ref,
                "publication_status": "draft_saved",
                "docx_path": docx_path,
                "docx_filename": filename,
                "next_skill": "end"}
    except Exception as e:
        logger.exception("[Skill 4] Error")
        return {**state, "error": f"Skill 4 failed: {e}", "next_skill": "end"}


# ── Router + Graph ─────────────────────────────────────────────────────────

def route(state: TenderSynopsisState) -> str:
    return state.get("next_skill", "end")


def build_graph():
    b = StateGraph(TenderSynopsisState)
    b.add_node("skill1",      skill1_country_template)
    b.add_node("skill2",      skill2_sap_fetch)
    b.add_node("skill3",      skill3_generate_synopsis)
    b.add_node("ai_validate", skill_ai_validate)
    b.add_node("hitl_wait",   skill3_hitl_wait)
    b.add_node("skill4",      skill4_publish)

    def entry_route(state: TenderSynopsisState) -> str:
        return state.get("next_skill", "skill1")

    b.add_conditional_edges(START, entry_route, {
        "skill1":      "skill1",
        "skill2":      "skill2",
        "skill3":      "skill3",
        "ai_validate": "ai_validate",
        "skill4":      "skill4",
        "hitl_wait":   "hitl_wait",
        "end":          END,
    })

    b.add_conditional_edges("skill1",      route, {"skill2":      "skill2",      "end": END})
    b.add_conditional_edges("skill2",      route, {"skill3":      "skill3",      "end": END})
    b.add_conditional_edges("skill3",      route, {
        "ai_validate": "ai_validate",  # fresh generation → validate first
        "hitl_wait":   "hitl_wait",    # after manual edit → skip validation, go straight to HITL
        "end":          END,
    })
    b.add_conditional_edges("ai_validate", route, {
        "skill3":    "skill3",    # regenerate if score < 70
        "hitl_wait": "hitl_wait", # proceed to human review
        "end":        END,
    })
    b.add_conditional_edges("hitl_wait",   route, {"skill4": "skill4", "end": END})
    b.add_conditional_edges("skill4",      route, {"end": END})
    return b.compile()


# ── Agent Class ────────────────────────────────────────────────────────────

class TenderSynopsisAgent:
    """
    A2A-compatible LangGraph agent with 4 skills and HITL support.
    context_id maintains multi-turn conversation state.
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self):
        self.graph = build_graph()
        self._state: dict[str, TenderSynopsisState] = {}

    def _parse_input(self, query: str, context_id: str) -> TenderSynopsisState:
        q = query.strip().lower()

        # HITL continuation
        if context_id in self._state:
            existing = self._state[context_id]
            if q == "approve":
                return {**existing, "hitl_decision": "approved", "next_skill": "skill4"}
            if q == "reject":
                return {**existing, "hitl_decision": "rejected", "next_skill": "end"}
            # Batch edit from inline UI form
            if q.startswith('{"__batch_edit__"') or '"__batch_edit__"' in q:
                try:
                    import json as _j
                    payload = _j.loads(query)
                    batch = payload.get("__batch_edit__", [])
                    return {**existing, "hitl_decision": "edit",
                            "hitl_batch_edits": batch,
                            "hitl_edit_field": "", "hitl_edit_value": "",
                            "next_skill": "skill3"}
                except Exception:
                    pass
            # Single field edit (text command fallback)
            if q.startswith("edit "):
                parts = query.strip().split(" ", 2)
                return {**existing, "hitl_decision": "edit",
                        "hitl_batch_edits": [],
                        "hitl_edit_field": parts[1] if len(parts) > 1 else "",
                        "hitl_edit_value": parts[2] if len(parts) > 2 else "",
                        "next_skill": "skill3"}

        # New request
        sp_match = re.search(r'\b(\d{4,})\b', query)
        sp_id = sp_match.group(1) if sp_match else query.strip()

        lang = "English"
        for l in ["german", "french", "arabic", "hindi", "spanish"]:
            if l in q:
                lang = l.capitalize()
                break

        country = "AUTO"
        for kw, code in [("india","IN"),("usa","US"),("germany","DE"),
                          ("saudi","SA"),("uae","AE"),("uk","GB"),
                          ("france","FR"),("australia","AU")]:
            if kw in q:
                country = code
                break

        return TenderSynopsisState(
            sourcing_project_id=sp_id, language=lang, country_override=country,
            country_code="", portal_name="", required_fields=[],
            tender_data={}, synopsis={}, hitl_decision="pending",
            hitl_edit_field="", hitl_edit_value="", hitl_batch_edits=[],
            validation_score=-1, validation_passed=False,
            validation_issues=[], validation_attempts=0,
            publication_ref="", publication_status="",
            docx_path="", docx_filename="", error="", next_skill="skill1",
        )

    def _format_synopsis(self, synopsis: dict) -> str:
        SEP = "─" * 50
        lines = [
            f"📋 **TENDER SYNOPSIS**",
            f"🏛️  Portal: {synopsis.get('portalName', 'Generic Portal')}",
            f"",
            f"## {synopsis.get('tenderTitle', '—')}",
            f"",
            synopsis.get("executiveSummary", ""),
        ]

        # Portal compliance note (if present)
        pcn = synopsis.get("portalComplianceNote", "")
        if pcn and not pcn.lower().startswith("not spec"):
            lines += ["", f"ℹ️  {pcn}"]

        fields = synopsis.get("supplierFields", [])

        # ── Section: TENDER OVERVIEW ─────────────────────────────
        overview = [f for f in fields if f.get("category") == "overview"]
        if overview:
            lines += ["", SEP, "**📌 TENDER OVERVIEW**", SEP]
            for f in overview:
                star = "★ " if f.get("important") else "   "
                val = f.get("value", "—")
                muted = "*(not specified)*" if val.lower().startswith("not spec") else f"**{val}**"
                lines.append(f"{star}{f['label']}: {muted}")

        # ── Section: COMMERCIAL DETAILS ──────────────────────────
        commercial = [f for f in fields if f.get("category") == "commercial"]
        if commercial:
            lines += ["", SEP, "**💰 COMMERCIAL DETAILS**", SEP]
            for f in commercial:
                star = "★ " if f.get("important") else "   "
                val = f.get("value", "—")
                muted = "*(not specified)*" if val.lower().startswith("not spec") else f"**{val}**"
                lines.append(f"{star}{f['label']}: {muted}")

        # ── Section: KEY DATES ───────────────────────────────────
        dates = [f for f in fields if f.get("category") == "dates"]
        if dates:
            lines += ["", SEP, "**📅 KEY DATES**", SEP]
            for f in dates:
                star = "★ " if f.get("important") else "   "
                val = f.get("value", "—")
                muted = "*(not specified)*" if val.lower().startswith("not spec") else f"**{val}**"
                lines.append(f"{star}{f['label']}: {muted}")

        # ── Section: ELIGIBILITY ─────────────────────────────────
        eligibility = [f for f in fields if f.get("category") == "eligibility"]
        if eligibility:
            lines += ["", SEP, "**✅ ELIGIBILITY & QUALIFICATION**", SEP]
            for f in eligibility:
                star = "★ " if f.get("important") else "   "
                val = f.get("value", "—")
                muted = "*(not specified)*" if val.lower().startswith("not spec") else f"**{val}**"
                lines.append(f"{star}{f['label']}: {muted}")

        # ── Section: SUPPLIER ACTIONS ────────────────────────────
        actions = [a for a in synopsis.get("supplierActions", []) if a]
        if actions:
            lines += ["", SEP, "**🎯 SUPPLIER ACTIONS**", SEP]
            for a in actions:
                lines.append(f"  ✓ {a}")

        # ── Missing portal fields (red flags) ────────────────────
        pmf = synopsis.get("portalMissingFields", [])
        if pmf:
            lines += ["", SEP, "**⚠️  MISSING PORTAL FIELDS**", SEP]
            for m in pmf:
                label = m.get("label", m) if isinstance(m, dict) else m
                reason = f" — {m['reason']}" if isinstance(m, dict) and m.get("reason") else ""
                lines.append(f"  • {label}{reason}")

        # ── HITL prompt ──────────────────────────────────────────
        lines += [
            "", SEP,
            "**Reply:**",
            "  `approve` — approve and publish",
            "  `reject`  — discard synopsis",
            "  `edit <field> <new value>` — edit a field",
            SEP,
        ]
        return "\n".join(lines)

    async def stream(self, query: str, context_id: str) -> AsyncGenerator[dict, None]:
        logger.info(f"[Agent] stream() called: context_id={context_id}, query='{query[:50]}'")
        logger.info(f"[Agent] Known context_ids in _state: {list(self._state.keys())}")
        yield {"is_task_complete": False, "require_user_input": False,
               "content": "Processing..."}
        try:
            state = self._parse_input(query, context_id)
            logger.info(f"[Agent] Parsed state: next_skill={state.get('next_skill')}, hitl_decision={state.get('hitl_decision')}")
            result = await self.graph.ainvoke(state)
            logger.info(f"[Agent] Graph result: next_skill={result.get('next_skill')}, hitl_decision={result.get('hitl_decision')}, pub_ref={result.get('publication_ref')}, error={result.get('error')}")
            self._state[context_id] = result

            if result.get("error"):
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": f"Error: {result['error']}"}
                return

            if result.get("hitl_decision") == "pending" and result.get("synopsis"):
                import json as _json
                # Include validation results in HITL payload for UI display
                payload = _json.dumps({
                    "__synopsis__":         result["synopsis"],
                    "__text__":             self._format_synopsis(result["synopsis"]),
                    "__validation_score__": result.get("validation_score", -1),
                    "__validation_passed__":result.get("validation_passed", True),
                    "__validation_issues__":result.get("validation_issues", []),
                }, ensure_ascii=False)
                yield {"is_task_complete": False, "require_user_input": True,
                       "content": payload}
                return

            if result.get("publication_ref"):
                sp_id    = result.get("sourcing_project_id", "unknown")
                docx     = result.get("docx_path", "")
                fname    = result.get("docx_filename", f"TenderSynopsis_{sp_id}.docx")
                self._state.pop(context_id, None)
                import json as _json
                artifact = _json.dumps({
                    "__approved__":    True,
                    "__ref__":         result["publication_ref"],
                    "__portal__":      result.get("portal_name", "—"),
                    "__docx_path__":   docx,
                    "__docx_filename__": fname,
                    "__download_url__":f"/download/{fname}",
                    "__sp_id__":       sp_id,
                })
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": artifact}
                return

            if result.get("hitl_decision") == "rejected":
                self._state.pop(context_id, None)
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": "Synopsis rejected. Nothing published."}
                return

            yield {"is_task_complete": True, "require_user_input": False,
                   "content": "Completed."}

        except Exception as e:
            logger.exception("Stream error")
            yield {"is_task_complete": True, "require_user_input": False,
                   "content": f"Error: {e}"}
