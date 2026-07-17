"""
Tender Synopsis Agent v3.0 - LangGraph workflow.

Changes from v2.0:
  E4/E5/E6: Prompts loaded from app/prompts/, weighted validator, section superset
  E7: Deterministic orchestrator node (star topology) - single routing authority
  E8: SAP Agent Memory - checkpointer replaces _state dict; approvals -> semantic memories

Node map:
  START -> orchestrator
    orchestrator -> detect_country | load_template | sap_fetch |
                    generate_synopsis | ai_validate | await_hitl |
                    skill4_publish | end
    every sub-node -> orchestrator

The orchestrator is pure Python (no LLM cost, no latency penalty).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import AsyncGenerator, TypedDict

from langgraph.graph import END, START, StateGraph

from core.country_formats  import _detect_country
from core.template_loader   import load_template
from core.portal_template   import PortalTemplate
from core.sap_client        import fetch_tender_from_sap
from core.synopsis_generator import generate_synopsis
from core.docx_exporter     import save_synopsis_docx
from core import synopsis_memory
from core import validator as v_mod
from prompts import load_prompt, prompt_version

logger = logging.getLogger(__name__)


# ── State ───────────────────────────────────────────────────────────────

class TenderSynopsisState(TypedDict, total=False):
    # Input
    sourcing_project_id: str
    language:            str
    country_override:    str
    invoker_id:          str

    # Skill 1 - country + template
    country_code:        str
    portal_name:         str
    template_source:     str          # live | cache | file | default
    template_version:    str
    portal_template:     dict         # serialised PortalTemplate

    # Skill 2 - SAP data
    tender_data:         dict

    # Skill 3 - synopsis
    synopsis:            dict
    prompt_version:      str
    hitl_decision:       str          # pending | approved | rejected | edit
    hitl_batch_edits:    list
    hitl_edit_field:     str
    hitl_edit_value:     str

    # AI Validation
    validation:          dict         # full grade() output
    validation_score:    float
    validation_passed:   bool
    validation_issues:   list
    validation_attempts: int

    # Skill 4 - publish
    publication_ref:     str
    publication_status:  str
    docx_path:           str
    docx_filename:       str

    # Control
    error:               str
    next_skill:          str


# ── Orchestrator (E7) ───────────────────────────────────────────────────

def orchestrator(state: TenderSynopsisState) -> dict:
    """
    Pure-Python router. Decides next node based on state completeness and
    the HITL decision. Zero LLM calls, ~1 ms overhead per hop.
    """
    if state.get("error"):
        logger.error(f"[Orchestrator] Halting on error: {state['error']}")
        return {**state, "next_skill": "end"}

    hitl = state.get("hitl_decision", "pending")

    if hitl == "approved":
        return {**state, "next_skill": "skill4_publish"}

    if hitl == "rejected":
        return {**state, "next_skill": "end"}

    if hitl == "edit":
        # Apply the edits to synopsis and re-enter HITL (skip validation)
        state = _apply_hitl_edits(state)
        return {**state, "hitl_decision": "pending", "next_skill": "await_hitl"}

    # Regular top-down flow
    if not state.get("country_code"):
        return {**state, "next_skill": "detect_country"}

    if not state.get("portal_template"):
        return {**state, "next_skill": "load_template"}

    if not state.get("tender_data"):
        return {**state, "next_skill": "sap_fetch"}

    if not state.get("synopsis"):
        return {**state, "next_skill": "generate_synopsis"}

    # Have a synopsis - check validation
    if not state.get("validation"):
        return {**state, "next_skill": "ai_validate"}

    val = state["validation"]
    attempts = state.get("validation_attempts", 0)
    if val.get("should_regenerate") and attempts < v_mod.MAX_ATTEMPTS:
        # Clear synopsis + validation to trigger regeneration
        return {
            **state,
            "synopsis":            {},
            "validation":          {},
            "validation_attempts": attempts + 1,
            "next_skill":          "generate_synopsis",
        }

    # Passed OR max attempts reached OR amber-fix accepted -> HITL
    return {**state, "next_skill": "await_hitl"}


def _apply_hitl_edits(state: TenderSynopsisState) -> TenderSynopsisState:
    """Apply user's HITL edits to the synopsis in-place."""
    synopsis = dict(state.get("synopsis") or {})
    batch = state.get("hitl_batch_edits") or []
    if batch:
        supplier_fields = synopsis.get("supplierFields", [])
        for edit in batch:
            lbl = (edit.get("label") or "").lower()
            val = edit.get("value", "")
            matched = False
            for f in supplier_fields:
                if f.get("label", "").lower() == lbl or f.get("portalFieldId", "").lower() == lbl:
                    f["value"] = val
                    matched = True
                    break
            if not matched:
                synopsis[edit.get("label", "")] = val
        logger.info(f"[Orchestrator] Applied {len(batch)} HITL edit(s)")
    elif state.get("hitl_edit_field"):
        f_lbl = state["hitl_edit_field"]
        f_val = state.get("hitl_edit_value", "")
        for f in synopsis.get("supplierFields", []):
            if f.get("label", "").lower() == f_lbl.lower():
                f["value"] = f_val
                break
        else:
            synopsis[f_lbl] = f_val
    return {**state, "synopsis": synopsis}


# ── Sub-skills (pure - no next_skill decisions) ─────────────────────────

async def node_detect_country(state: TenderSynopsisState) -> dict:
    logger.info("[Node] detect_country")
    try:
        override = state.get("country_override", "AUTO")
        # Detect country needs tender_data for auto-detect. If we don't have
        # it yet, fetch once here (Skill 1a).
        tender_data = state.get("tender_data") or {}
        if override not in ("AUTO", "DEFAULT", ""):
            country_code = override
            if not tender_data:
                tender_data = fetch_tender_from_sap(state["sourcing_project_id"])
        else:
            if not tender_data:
                tender_data = fetch_tender_from_sap(state["sourcing_project_id"])
            country_code = _detect_country(tender_data)
        return {
            **state,
            "country_code": country_code,
            "tender_data":  tender_data,
        }
    except Exception as e:
        logger.exception("[Node] detect_country failed")
        return {**state, "error": f"detect_country: {e}"}


async def node_load_template(state: TenderSynopsisState) -> dict:
    logger.info(f"[Node] load_template for {state.get('country_code')}")
    try:
        tmpl, source = load_template(state["country_code"])
        return {
            **state,
            "portal_template":  tmpl.to_dict(),
            "portal_name":      tmpl.portal_name,
            "template_source":  source,
            "template_version": tmpl.version,
        }
    except Exception as e:
        logger.exception("[Node] load_template failed")
        return {**state, "error": f"load_template: {e}"}


async def node_sap_fetch(state: TenderSynopsisState) -> dict:
    logger.info(f"[Node] sap_fetch for SP {state['sourcing_project_id']}")
    try:
        # Skill 1 usually populates tender_data. This is a safety net.
        existing = state.get("tender_data") or {}
        if existing.get("SourcingProjectName"):
            logger.info("[Node] sap_fetch - reusing data from detect_country")
            return state
        tender_data = fetch_tender_from_sap(state["sourcing_project_id"])
        return {**state, "tender_data": tender_data}
    except Exception as e:
        logger.exception("[Node] sap_fetch failed")
        return {**state, "error": f"sap_fetch: {e}"}


async def node_generate_synopsis(state: TenderSynopsisState) -> dict:
    logger.info("[Node] generate_synopsis")
    try:
        tmpl = PortalTemplate.from_dict(state["portal_template"], source=state.get("template_source", "file"))
        invoker_id = state.get("invoker_id", "anonymous")

        # E8: Few-shot from Agent Memory
        few_shot = synopsis_memory.find_similar_approvals(
            tender_data  = state["tender_data"],
            country_code = state["country_code"],
            invoker_id   = invoker_id,
            limit        = 3,
        )
        if few_shot:
            logger.info(f"[Node] Retrieved {len(few_shot)} similar approvals from memory")

        synopsis = generate_synopsis(
            tender_data       = state["tender_data"],
            target_language   = state.get("language", "English"),
            template          = tmpl,
            few_shot_examples = few_shot,
        )
        return {
            **state,
            "synopsis":       synopsis,
            "prompt_version": prompt_version("synopsis_generator"),
            "validation":     {},          # clear so validator will run
            "hitl_decision":  "pending",
        }
    except Exception as e:
        logger.exception("[Node] generate_synopsis failed")
        return {**state, "error": f"generate_synopsis: {e}"}


async def node_ai_validate(state: TenderSynopsisState) -> dict:
    """Runs Claude on the validator prompt and grades the result."""
    logger.info("[Node] ai_validate")
    try:
        tmpl = PortalTemplate.from_dict(state["portal_template"], source=state.get("template_source", "file"))
        v_prompt = load_prompt(
            "validator",
            template            = tmpl,
            effective_weights   = v_mod.effective_weights(
                load_prompt("validator")["weights"], tmpl
            ),
            tender_data_json    = json.dumps(state["tender_data"], indent=2, ensure_ascii=False),
            synopsis_json       = json.dumps(state["synopsis"],    indent=2, ensure_ascii=False),
            target_language     = state.get("language", "English"),
        )

        raw = _call_validator_llm(v_prompt)
        parsed = v_mod.parse_llm_json(raw)
        graded = v_mod.grade(parsed, weights=v_prompt["weights"])

        # If verdict is "amber" the validator gave us a fixed synopsis - use it
        synopsis = state["synopsis"]
        if graded["verdict"] == "amber" and graded.get("fixed_synopsis"):
            synopsis = graded["fixed_synopsis"]
            logger.info("[Node] ai_validate - using AI-fixed synopsis")

        return {
            **state,
            "synopsis":            synopsis,
            "validation":          graded,
            "validation_score":    graded["weighted_score"],
            "validation_passed":   graded["passed"],
            "validation_issues":   graded["issues"],
            "validation_attempts": state.get("validation_attempts", 0),
        }
    except Exception as e:
        logger.warning(f"[Node] ai_validate failed: {e} - proceeding to HITL anyway")
        return {
            **state,
            "validation": {
                "weighted_score":    -1,
                "passed":            True,           # fail-open
                "should_regenerate": False,
                "verdict":           "pass",
                "issues":            [{"rule": "validator", "detail": f"Validator errored: {e}"}],
                "rule_scores":       {},
                "critical_failures": [],
                "weights":           {},
                "fixed_synopsis":    None,
            },
            "validation_score":    -1,
            "validation_passed":   True,
            "validation_issues":   [f"validator errored: {e}"],
            "validation_attempts": state.get("validation_attempts", 0) + 1,
        }


async def node_await_hitl(state: TenderSynopsisState) -> dict:
    """Marker node - actual HITL wait happens by graph completion."""
    logger.info(f"[Node] await_hitl (decision={state.get('hitl_decision', 'pending')})")
    return state


async def node_skill4_publish(state: TenderSynopsisState) -> dict:
    logger.info(f"[Node] skill4_publish to {state.get('portal_name')}")
    try:
        from datetime import datetime as _dt
        synopsis = state["synopsis"]
        sp_id    = state["sourcing_project_id"]

        timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path(__file__).parent.parent / "output_docs"
        output_dir.mkdir(exist_ok=True)
        filename  = f"TenderSynopsis_{sp_id}_{timestamp}.docx"
        docx_path = str(output_dir / filename)

        save_synopsis_docx(synopsis, sp_id, docx_path)
        ref = f"DRAFT-{sp_id}-{synopsis.get('portalCountryCode', 'XX')}"
        logger.info(f"[Node] .docx saved: {docx_path}, ref={ref}")

        # E8: Remember this approval as a semantic memory
        invoker_id = state.get("invoker_id", "anonymous")
        synopsis_memory.remember_approval(
            sourcing_project_id = sp_id,
            tender_data         = state["tender_data"],
            synopsis            = synopsis,
            invoker_id          = invoker_id,
        )
        synopsis_memory.record_hitl_feedback(
            sourcing_project_id = sp_id,
            country_code        = state.get("country_code", ""),
            invoker_id          = invoker_id,
            action              = "approve",
            detail              = {"docx": filename, "ref": ref},
        )

        return {
            **state,
            "publication_ref":    ref,
            "publication_status": "draft_saved",
            "docx_path":          docx_path,
            "docx_filename":      filename,
        }
    except Exception as e:
        logger.exception("[Node] skill4_publish failed")
        return {**state, "error": f"skill4_publish: {e}"}


# ── Validator LLM call (shared with skill3) ─────────────────────────────

def _call_validator_llm(v_prompt: dict) -> str:
    """
    Call Claude for the validator prompt. Uses the same backend chain as
    synopsis_generator - SAP AI Core primary, Hyperspace fallback.
    """
    aicore_set = bool(os.environ.get("AICORE_CLIENT_ID", "").strip())

    if aicore_set:
        try:
            from langchain_litellm import ChatLiteLLM
            from langchain_core.messages import SystemMessage, HumanMessage
            model = os.environ.get("CLAUDE_MODEL", "sap/anthropic--claude-4.5-sonnet")
            llm = ChatLiteLLM(model=model, temperature=v_prompt["temperature"])
            resp = llm.invoke([
                SystemMessage(content=v_prompt["system"]),
                HumanMessage(content=v_prompt["user"]),
            ])
            return resp.content.strip()
        except Exception as e:
            logger.warning(f"[Validator] AI Core failed: {e}, falling back to Hyperspace")

    # Hyperspace path
    import anthropic as _anthropic
    hyperspace_key = os.environ.get("HYPERSPACE_API_KEY", "").strip()
    hyperspace_url = os.environ.get("HYPERSPACE_URL", "http://localhost:6655/anthropic")
    model = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")
    if not hyperspace_key:
        raise RuntimeError("No AI credentials configured for validator")
    client = _anthropic.Anthropic(base_url=hyperspace_url, api_key=hyperspace_key)
    msg = client.messages.create(
        model=model,
        max_tokens=v_prompt["max_tokens"],
        temperature=v_prompt["temperature"],
        system=v_prompt["system"],
        messages=[{"role": "user", "content": v_prompt["user"]}],
    )
    return msg.content[0].text.strip()


# ── Graph construction (Star topology via orchestrator) ─────────────────

def _route(state: TenderSynopsisState) -> str:
    return state.get("next_skill", "end")


def build_graph(checkpointer=None):
    """
    Build the star-topology graph. Every sub-node returns control to the
    orchestrator, which alone decides the next hop.
    """
    b = StateGraph(TenderSynopsisState)

    b.add_node("orchestrator",       orchestrator)
    b.add_node("detect_country",     node_detect_country)
    b.add_node("load_template",      node_load_template)
    b.add_node("sap_fetch",          node_sap_fetch)
    b.add_node("generate_synopsis",  node_generate_synopsis)
    b.add_node("ai_validate",        node_ai_validate)
    b.add_node("await_hitl",         node_await_hitl)
    b.add_node("skill4_publish",     node_skill4_publish)

    b.add_edge(START, "orchestrator")

    # Every sub-node returns to the orchestrator
    for sub in ("detect_country", "load_template", "sap_fetch",
                "generate_synopsis", "ai_validate", "await_hitl",
                "skill4_publish"):
        b.add_edge(sub, "orchestrator")

    b.add_conditional_edges("orchestrator", _route, {
        "detect_country":    "detect_country",
        "load_template":     "load_template",
        "sap_fetch":         "sap_fetch",
        "generate_synopsis": "generate_synopsis",
        "ai_validate":       "ai_validate",
        "await_hitl":        "await_hitl",
        "skill4_publish":    "skill4_publish",
        "end":               END,
    })

    return b.compile(checkpointer=checkpointer) if checkpointer else b.compile()


# ── Agent class (A2A wrapper) ───────────────────────────────────────────

class TenderSynopsisAgent:
    """
    A2A-compatible LangGraph agent with 4-skill workflow, orchestrator,
    weighted validator, and SAP Agent Memory integration.

    Multi-turn conversation state is now handled by the LangGraph checkpointer
    (Agent Memory when available, InMemorySaver as local fallback). The old
    self._state dict is gone.
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self):
        # Checkpointer replaces the in-memory _state dict (E8)
        self.checkpointer = synopsis_memory.get_checkpointer(ttl_seconds=3600)
        self.graph = build_graph(checkpointer=self.checkpointer)

    # ── Query parsing ─────────────────────────────────────────────

    def _parse_new_request(self, query: str) -> TenderSynopsisState:
        q = query.strip().lower()

        sp_match = re.search(r"\b(\d{4,})\b", query)
        sp_id = sp_match.group(1) if sp_match else query.strip()

        lang = "English"
        for l in ("german", "french", "arabic", "hindi", "spanish"):
            if l in q:
                lang = l.capitalize()
                break

        country = "AUTO"
        for kw, code in (("india","IN"),("usa","US"),("germany","DE"),
                         ("saudi","SA"),("uae","AE"),("uk","GB"),
                         ("france","FR"),("australia","AU")):
            if kw in q:
                country = code
                break

        return {
            "sourcing_project_id": sp_id,
            "language":            lang,
            "country_override":    country,
            "hitl_decision":       "pending",
            "validation_attempts": 0,
        }

    def _parse_hitl_response(self, query: str) -> dict:
        """Returns partial state update for HITL turn 2+."""
        q = query.strip().lower()
        if q == "approve":
            return {"hitl_decision": "approved"}
        if q == "reject":
            return {"hitl_decision": "rejected"}
        if '"__batch_edit__"' in q:
            try:
                payload = json.loads(query)
                return {
                    "hitl_decision":    "edit",
                    "hitl_batch_edits": payload.get("__batch_edit__", []),
                }
            except Exception:
                pass
        if q.startswith("edit "):
            parts = query.strip().split(" ", 2)
            return {
                "hitl_decision":    "edit",
                "hitl_batch_edits": [],
                "hitl_edit_field":  parts[1] if len(parts) > 1 else "",
                "hitl_edit_value":  parts[2] if len(parts) > 2 else "",
            }
        # Not a HITL command - treat as a new request
        return {}

    # ── Rendering helpers ─────────────────────────────────────────

    def _format_synopsis(self, synopsis: dict, template_source: str = "file",
                         template_version: str = "") -> str:
        SEP = "─" * 50
        badge = {"live": "🟢 Live", "cache": "🟡 Cached",
                 "file": "🟠 Offline snapshot", "default": "🔴 Generic"}.get(template_source, "🟠")

        lines = [
            f"📋 **TENDER SYNOPSIS**",
            f"🏛️  Portal: {synopsis.get('portalName', 'Generic')}  |  Template: {badge} ({template_version})",
            "",
            f"## {synopsis.get('tenderTitle', '—')}",
            "",
            synopsis.get("executiveSummary", ""),
        ]

        pcn = synopsis.get("portalComplianceNote", "")
        if pcn and not pcn.lower().startswith("not spec"):
            lines += ["", f"ℹ️  {pcn}"]

        # Group fields by section id, ordering preserved from template.section_superset
        fields = synopsis.get("supplierFields", [])
        # Collect the section ordering from the fields themselves in the order they appear
        seen_section_ids: list[str] = []
        by_section: dict[str, list] = {}
        for f in fields:
            sid = f.get("sectionId") or f.get("section_id") or "sec_misc"
            if sid not in by_section:
                by_section[sid] = []
                seen_section_ids.append(sid)
            by_section[sid].append(f)

        # Map section id -> title from the synopsis output (Claude includes it via template)
        # Fallback: humanise the id
        def _humanise(sid: str) -> str:
            return sid.replace("sec_", "").replace("_", " ").title()

        for sid in seen_section_ids:
            lines += ["", SEP, f"**{_humanise(sid).upper()}**", SEP]
            for f in by_section[sid]:
                star = "★ " if f.get("important") else "   "
                val = f.get("value", "—")
                muted = "*(not specified)*" if str(val).lower().startswith("not spec") else f"**{val}**"
                lines.append(f"{star}{f.get('label', '')}: {muted}")

        actions = [a for a in synopsis.get("supplierActions", []) if a]
        if actions:
            lines += ["", SEP, "**🎯 SUPPLIER ACTIONS**", SEP]
            for a in actions:
                lines.append(f"  ✓ {a}")

        pmf = synopsis.get("portalMissingFields", [])
        if pmf:
            lines += ["", SEP, "**⚠️  MISSING PORTAL FIELDS**", SEP]
            for m in pmf:
                label = m.get("label", m) if isinstance(m, dict) else m
                reason = f" — {m['reason']}" if isinstance(m, dict) and m.get("reason") else ""
                lines.append(f"  • {label}{reason}")

        lines += [
            "", SEP,
            "**Reply:**",
            "  `approve` — approve and publish",
            "  `reject`  — discard synopsis",
            "  `edit <field> <new value>` — edit a field",
            SEP,
        ]
        return "\n".join(lines)

    # ── Main streaming entry point (called by A2A executor) ────────

    async def stream(self, query: str, context_id: str) -> AsyncGenerator[dict, None]:
        logger.info(f"[Agent] stream() context_id={context_id} query='{query[:60]}'")
        yield {"is_task_complete": False, "require_user_input": False,
               "content": "Processing..."}

        try:
            # Config for LangGraph checkpointer - thread_id keys the session
            cfg = {"configurable": {"thread_id": context_id}}

            # Was there an in-progress session on this context_id?
            existing_state: dict | None = None
            try:
                snapshot = self.graph.get_state(cfg)
                if snapshot and snapshot.values:
                    existing_state = snapshot.values
            except Exception:
                existing_state = None

            # Decide whether this is a HITL turn or a fresh request
            if existing_state and existing_state.get("synopsis") \
                    and existing_state.get("hitl_decision") == "pending":
                # HITL turn 2+
                hitl_update = self._parse_hitl_response(query)
                if not hitl_update:
                    # User typed something else - treat as a new request in a new thread
                    logger.info("[Agent] Non-HITL command received - starting fresh request")
                    initial = self._parse_new_request(query)
                    initial["invoker_id"] = context_id
                    result = await self.graph.ainvoke(initial, config=cfg)
                else:
                    logger.info(f"[Agent] HITL continuation: {list(hitl_update.keys())}")
                    result = await self.graph.ainvoke({**existing_state, **hitl_update}, config=cfg)
            else:
                # Fresh request
                initial = self._parse_new_request(query)
                initial["invoker_id"] = context_id
                result = await self.graph.ainvoke(initial, config=cfg)

            # ── Interpret final state ──────────────────────────────
            if result.get("error"):
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": f"Error: {result['error']}"}
                return

            if result.get("hitl_decision") == "pending" and result.get("synopsis"):
                payload = json.dumps({
                    "__synopsis__":          result["synopsis"],
                    "__text__":              self._format_synopsis(
                        result["synopsis"],
                        template_source=result.get("template_source", "file"),
                        template_version=result.get("template_version", ""),
                    ),
                    "__template_source__":   result.get("template_source", "file"),
                    "__template_version__":  result.get("template_version", ""),
                    "__validation__":        result.get("validation", {}),
                    "__validation_score__":  result.get("validation_score", -1),
                    "__validation_passed__": result.get("validation_passed", True),
                    "__validation_issues__": result.get("validation_issues", []),
                    "__prompt_version__":    result.get("prompt_version", ""),
                }, ensure_ascii=False)
                yield {"is_task_complete": False, "require_user_input": True,
                       "content": payload}
                return

            if result.get("publication_ref"):
                sp_id = result.get("sourcing_project_id", "unknown")
                fname = result.get("docx_filename", f"TenderSynopsis_{sp_id}.docx")
                artifact = json.dumps({
                    "__approved__":       True,
                    "__ref__":            result["publication_ref"],
                    "__portal__":         result.get("portal_name", "—"),
                    "__docx_path__":      result.get("docx_path", ""),
                    "__docx_filename__":  fname,
                    "__download_url__":   f"/download/{fname}",
                    "__sp_id__":          sp_id,
                    "__template_source__":result.get("template_source", "file"),
                })
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": artifact}
                return

            if result.get("hitl_decision") == "rejected":
                # E8: capture the rejection signal
                synopsis_memory.record_hitl_feedback(
                    sourcing_project_id = result.get("sourcing_project_id", ""),
                    country_code        = result.get("country_code", ""),
                    invoker_id          = context_id,
                    action              = "reject",
                    detail              = {"reason": "user_rejected"},
                )
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": "Synopsis rejected. Nothing published."}
                return

            yield {"is_task_complete": True, "require_user_input": False,
                   "content": "Completed."}

        except Exception as e:
            logger.exception("Stream error")
            yield {"is_task_complete": True, "require_user_input": False,
                   "content": f"Error: {e}"}
