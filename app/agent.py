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
import tempfile
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
    # Skill 4 outputs
    publication_ref: str
    publication_status: str
    # Control
    error: str
    next_skill: str


# ── Skill 1: Country & Template Discovery ─────────────────────────────────

async def skill1_country_template(state: TenderSynopsisState) -> TenderSynopsisState:
    logger.info("[Skill 1] Country & template discovery")
    try:
        override = state.get("country_override", "AUTO")
        if override != "AUTO":
            country_code = override
        else:
            tender_data = fetch_tender_from_sap(state["sourcing_project_id"])
            country_code = _detect_country(tender_data)

        fmt = COUNTRY_FORMATS.get(country_code, COUNTRY_FORMATS["DEFAULT"])
        logger.info(f"[Skill 1] Country={country_code} Portal={fmt['portal']}")
        return {
            **state,
            "country_code": country_code,
            "portal_name": fmt["portal"],
            "required_fields": fmt["required_fields"],
            "next_skill": "skill2",
        }
    except Exception as e:
        logger.exception("[Skill 1] Error")
        return {**state, "error": f"Skill 1 failed: {e}", "next_skill": "end"}


# ── Skill 2: SAP Data Extraction ──────────────────────────────────────────

async def skill2_sap_fetch(state: TenderSynopsisState) -> TenderSynopsisState:
    logger.info(f"[Skill 2] Fetching SP {state['sourcing_project_id']} from SAP")
    try:
        tender_data = fetch_tender_from_sap(state["sourcing_project_id"])
        logger.info(f"[Skill 2] Fetched: {tender_data.get('SourcingProjectName')}")
        return {**state, "tender_data": tender_data, "next_skill": "skill3"}
    except Exception as e:
        logger.exception("[Skill 2] SAP fetch error")
        return {**state, "error": f"Skill 2 failed: {e}", "next_skill": "end"}


# ── Skill 3: Synopsis Generation + HITL ───────────────────────────────────

async def skill3_generate_synopsis(state: TenderSynopsisState) -> TenderSynopsisState:
    logger.info("[Skill 3] Generating synopsis")
    try:
        # Handle edit re-entry
        if state.get("hitl_decision") == "edit" and state.get("hitl_edit_field"):
            synopsis = dict(state.get("synopsis", {}))
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
                "hitl_decision": "pending", "next_skill": "hitl_wait"}
    except Exception as e:
        logger.exception("[Skill 3] Error")
        return {**state, "error": f"Skill 3 failed: {e}", "next_skill": "end"}


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
        sp_id = state["sourcing_project_id"]
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp_path = tmp.name
        save_synopsis_docx(synopsis, sp_id, tmp_path)
        ref = f"DRAFT-{sp_id}-{synopsis.get('portalCountryCode', 'XX')}"
        logger.info(f"[Skill 4] Saved .docx, ref={ref}")
        return {**state, "publication_ref": ref,
                "publication_status": "draft_saved", "next_skill": "end"}
    except Exception as e:
        logger.exception("[Skill 4] Error")
        return {**state, "error": f"Skill 4 failed: {e}", "next_skill": "end"}


# ── Router + Graph ─────────────────────────────────────────────────────────

def route(state: TenderSynopsisState) -> str:
    return state.get("next_skill", "end")


def build_graph():
    b = StateGraph(TenderSynopsisState)
    b.add_node("skill1",    skill1_country_template)
    b.add_node("skill2",    skill2_sap_fetch)
    b.add_node("skill3",    skill3_generate_synopsis)
    b.add_node("hitl_wait", skill3_hitl_wait)
    b.add_node("skill4",    skill4_publish)

    # Entry point: route from START based on next_skill in the incoming state
    # This allows HITL continuation (approve → skill4) without re-running skill1+2
    def entry_route(state: TenderSynopsisState) -> str:
        return state.get("next_skill", "skill1")

    b.add_conditional_edges(START, entry_route, {
        "skill1":    "skill1",
        "skill2":    "skill2",
        "skill3":    "skill3",
        "skill4":    "skill4",
        "hitl_wait": "hitl_wait",
        "end":        END,
    })

    b.add_conditional_edges("skill1",    route, {"skill2": "skill2", "end": END})
    b.add_conditional_edges("skill2",    route, {"skill3": "skill3", "end": END})
    b.add_conditional_edges("skill3",    route, {"hitl_wait": "hitl_wait", "end": END})
    b.add_conditional_edges("hitl_wait", route, {
        "skill4": "skill4",
        "end":    END,
    })
    b.add_conditional_edges("skill4",    route, {"end": END})
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
            if q.startswith("edit "):
                parts = query.strip().split(" ", 2)
                return {**existing, "hitl_decision": "edit",
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
            hitl_edit_field="", hitl_edit_value="",
            publication_ref="", publication_status="", error="", next_skill="skill1",
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
                yield {"is_task_complete": False, "require_user_input": True,
                       "content": self._format_synopsis(result["synopsis"])}
                return

            if result.get("publication_ref"):
                self._state.pop(context_id, None)
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": (f"Synopsis approved and saved.\n"
                                   f"Reference: {result['publication_ref']}\n"
                                   f"Portal: {result.get('portal_name','—')}")}
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
