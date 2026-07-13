# PPS Tender Synopsis

An AI agent that fetches SAP PPS Sourcing Projects, detects country-specific portal requirements, generates a compliant tender synopsis, and supports human review before publication.

## Overview

Uses A2A Protocol, LangGraph, LiteLLM, and Application Foundation SDK.

The agent implements 4 skills as a LangGraph workflow:
- **Skill 1** — Country & template discovery (CompanyCode → portal format)
- **Skill 2** — SAP data extraction (OData V4 fetch of SP fields)
- **Skill 3** — Synopsis generation via Claude AI + HITL review
- **Skill 4** — Publish approved synopsis to third-party portal

## Structure

- `app.yaml` - Workload configuration
- `Dockerfile` - Container build
- `app/main.py` - A2A server entry point (AgentCard, port 9000)
- `app/agent_executor.py` - A2A protocol bridge (context_id, task_id)
- `app/agent.py` - LangGraph 4-skill workflow
