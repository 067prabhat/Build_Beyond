"""
SAP Agent Memory Integration (E8).

Three integration points:

  A) LangGraph Checkpointer - kills the in-memory _state singleton.
     Provided by get_checkpointer().

  B) Approved-Synopsis Memories - every approved synopsis becomes a
     semantic memory. Retrieved as few-shot examples for future generations.
     Provided by remember_approval() and find_similar_approvals().

  C) HITL Feedback Messages - user edits and rejections captured as messages
     for the weekly drift-detection aggregator.
     Provided by record_hitl_feedback().

All three degrade gracefully when AGENT_MEMORY_ENABLED=false or the SAP
service binding is unavailable. In that case:
  A) falls back to LangGraph InMemorySaver
  B) is a no-op (add_memory returns None, find returns empty list)
  C) is a no-op

AGENT_ID is the fixed identifier for this agent across all tenants.
INVOKER_ID is the per-user id passed by the A2A layer (or a fallback).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

AGENT_ID = os.environ.get("AGENT_MEMORY_AGENT_ID", "tender-synopsis-agent")

_ENABLED = os.environ.get("AGENT_MEMORY_ENABLED", "true").lower() == "true"

# Module-level lazy singleton
_client = None
_checkpointer = None


# ── Client bootstrap ─────────────────────────────────────────────────────

def _try_get_client():
    """Lazy-create the Agent Memory client. Returns None on any failure."""
    global _client
    if _client is not None:
        return _client
    if not _ENABLED:
        return None
    try:
        from sap_cloud_sdk.agent_memory import create_client
        _client = create_client()
        logger.info("[AgentMemory] Client initialised")
        return _client
    except Exception as e:
        logger.warning(f"[AgentMemory] Client unavailable: {e}. Memory features disabled.")
        return None


def get_checkpointer(ttl_seconds: int = 3600):
    """
    Return a LangGraph checkpointer. Priority order:

      1. HanaMemorySaver (persistent) - custom saver over SAP Agent Memory
         Adopted from negotiation-agent (production-tested SAP pattern).
         HITL sessions survive pod restarts.
      2. SDK factory create_checkpointer(ttl_seconds=...)
         In-memory saver from sap-cloud-sdk (state lost on restart).
      3. LangGraph InMemorySaver
         Local fallback so dev works without any BTP binding.
    """
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    if _ENABLED:
        # Priority 1: persistent HanaMemorySaver (E8 upgrade)
        try:
            from core.hana_memory_saver import HanaMemorySaver
            _checkpointer = HanaMemorySaver(agent_id=AGENT_ID)
            # Verify the underlying client can be created; fall through on failure
            _checkpointer.probe_client()
            logger.info(f"[AgentMemory] Checkpointer via HanaMemorySaver (persistent, agent_id={AGENT_ID})")
            return _checkpointer
        except Exception as e:
            logger.warning(f"[AgentMemory] HanaMemorySaver unavailable: {e}")
            _checkpointer = None  # reset - fall through to next option

        # Priority 2: SDK factory (in-memory)
        try:
            from sap_cloud_sdk.agent_memory.factory.langgraph_checkpoint import create_checkpointer
            _checkpointer = create_checkpointer(ttl_seconds=ttl_seconds)
            logger.info(f"[AgentMemory] Checkpointer via SDK factory (in-memory, ttl={ttl_seconds}s)")
            return _checkpointer
        except Exception as e:
            logger.warning(f"[AgentMemory] SDK checkpointer unavailable: {e}. Using InMemorySaver.")

    # Priority 3: local / offline fallback
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        _checkpointer = InMemorySaver()
        logger.info("[AgentMemory] Checkpointer via LangGraph InMemorySaver (no persistence)")
        return _checkpointer
    except Exception as e:
        logger.error(f"[AgentMemory] No checkpointer available at all: {e}")
        _checkpointer = None
        return None


# ── B) Approved-Synopsis Memories ────────────────────────────────────────

def _summarize_approval(synopsis: dict, tender_data: dict) -> str:
    """Compact summary that becomes the searchable memory content."""
    portal = synopsis.get("portalName", "Generic")
    country = synopsis.get("portalCountryCode", "??")
    title = synopsis.get("tenderTitle", tender_data.get("SourcingProjectName", "Untitled"))

    key_fields = []
    for f in synopsis.get("supplierFields", []):
        if f.get("important") and f.get("value") and not f["value"].lower().startswith("not spec"):
            key_fields.append(f"{f.get('label', '')}: {f.get('value', '')}")

    key_line = " | ".join(key_fields[:6])
    return (
        f"Approved {portal} synopsis for {country} tender \"{title}\". "
        f"Key fields: {key_line}"
    )


def remember_approval(
    sourcing_project_id: str,
    tender_data: dict,
    synopsis: dict,
    invoker_id: str,
) -> str | None:
    """
    Store an approved synopsis as a semantic memory. Returns the memory id
    if stored, None if the memory service was unavailable.
    """
    client = _try_get_client()
    if not client:
        return None

    try:
        content = _summarize_approval(synopsis, tender_data)
        metadata = {
            "country":         synopsis.get("portalCountryCode", ""),
            "portal":          synopsis.get("portalName", ""),
            "sp_id":           sourcing_project_id,
            "sp_version":      tender_data.get("SourcingProjectVersion", ""),
            "material_group":  tender_data.get("MaterialGroup", ""),
            "amount":          str(tender_data.get("TotalTargetAmount", "")),
            "currency":        tender_data.get("DocumentCurrency", ""),
            "language":        synopsis.get("language", ""),
            "template_hash":   synopsis.get("templateHash", ""),
            "template_version":synopsis.get("templateVersion", ""),
            "approved_at":     datetime.now(timezone.utc).isoformat(),
        }
        mem = client.add_memory(
            agent_id=AGENT_ID,
            invoker_id=invoker_id,
            content=content,
            metadata=metadata,
        )
        logger.info(f"[AgentMemory] Stored approval memory id={mem.id} for SP {sourcing_project_id}")
        return mem.id
    except Exception as e:
        logger.warning(f"[AgentMemory] remember_approval failed: {e}")
        return None


def find_similar_approvals(
    tender_data: dict,
    country_code: str,
    invoker_id: str,
    limit: int = 3,
    threshold: float = 0.65,
) -> list[dict]:
    """
    Retrieve past approved synopses semantically similar to the current tender.
    Feeds them into Skill 3 as few-shot examples so Claude imitates approved style.
    """
    client = _try_get_client()
    if not client:
        return []

    try:
        query = (
            f"{country_code} tender for "
            f"{tender_data.get('SourcingProjectName', '')} "
            f"{tender_data.get('MaterialGroup', '')} "
            f"{tender_data.get('PurchasingCategory', '')}"
        ).strip()
        if not query:
            return []
        results = client.search_memories(
            agent_id=AGENT_ID,
            invoker_id=invoker_id,
            query=query,
            threshold=threshold,
            limit=limit,
        )
        return [
            {
                "content":    r.content,
                "similarity": getattr(r, "similarity", None),
                "metadata":   getattr(r, "metadata", {}) or {},
            }
            for r in (results or [])
        ]
    except Exception as e:
        logger.warning(f"[AgentMemory] find_similar_approvals failed: {e}")
        return []


# ── C) HITL Feedback Messages ────────────────────────────────────────────

def record_hitl_feedback(
    sourcing_project_id: str,
    country_code: str,
    invoker_id: str,
    action: str,                # "approve" | "reject" | "edit"
    detail: dict | None = None,
) -> str | None:
    """
    Log a HITL decision as an Agent Memory Message. Used later by the weekly
    drift-detection aggregator to notice patterns like "same field edited 10x".
    """
    client = _try_get_client()
    if not client:
        return None

    try:
        from sap_cloud_sdk.agent_memory import MessageRole
        payload = {
            "action":   action,
            "detail":   detail or {},
            "at":       datetime.now(timezone.utc).isoformat(),
        }
        metadata = {
            "country":   country_code,
            "sp_id":     sourcing_project_id,
            "action":    action,
        }
        msg = client.add_message(
            agent_id=AGENT_ID,
            invoker_id=invoker_id,
            message_group=sourcing_project_id,
            role=MessageRole.SYSTEM,
            content=json.dumps(payload, ensure_ascii=False),
            metadata=metadata,
        )
        logger.info(f"[AgentMemory] Recorded HITL '{action}' for SP {sourcing_project_id}")
        return msg.id
    except Exception as e:
        logger.warning(f"[AgentMemory] record_hitl_feedback failed: {e}")
        return None


# ── Health / diagnostics ─────────────────────────────────────────────────

def status() -> dict:
    return {
        "enabled":        _ENABLED,
        "client_ready":   _try_get_client() is not None,
        "agent_id":       AGENT_ID,
    }