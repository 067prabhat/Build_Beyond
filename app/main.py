# CRITICAL: Initialize logging and telemetry BEFORE importing AI frameworks
import logging
logging.basicConfig(level=logging.INFO)

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent / ".env.local")

from sap_cloud_sdk.aicore import set_aicore_config
from sap_cloud_sdk.core.telemetry import auto_instrument, StarletteIASTelemetryMiddleware
set_aicore_config()

import os
from contextvars import ContextVar
from urllib.parse import urlparse, urlunparse

import click
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.routing import Mount

from agent_executor import AgentExecutor
from ord import create_ord_routes

logger = logging.getLogger(__name__)

# AGENT_PUBLIC_URL carries the literal "provider" in the host label as a placeholder;
# at request time we replace it with the dwc-subdomain header so each tenant gets its
# own URL in the AgentCard.
_DWC_SUBDOMAIN_PLACEHOLDER = "provider"
_dwc_subdomain: ContextVar[str] = ContextVar("dwc_subdomain", default="")


class DwcSubdomainMiddleware(BaseHTTPMiddleware):
    """Captures the dwc-subdomain header into a ContextVar for the card_modifier."""

    async def dispatch(self, request: Request, call_next):
        token = _dwc_subdomain.set(request.headers.get("dwc-subdomain", ""))
        try:
            return await call_next(request)
        finally:
            _dwc_subdomain.reset(token)


def _apply_dwc_subdomain(card: AgentCard) -> AgentCard:
    subdomain = _dwc_subdomain.get()
    if not subdomain or not card.url:
        return card
    parsed = urlparse(card.url)
    prefix = _DWC_SUBDOMAIN_PLACEHOLDER + "."
    if not (parsed.hostname or "").startswith(prefix):
        return card
    new_netloc = parsed.netloc.replace(prefix, subdomain + ".", 1)
    new_url = urlunparse(parsed._replace(netloc=new_netloc))
    return card.model_copy(update={"url": new_url})

# =============================================================================
# CUSTOMIZE YOUR AGENT HERE
# =============================================================================
AGENT_NAME = "Tender Synopsis Agent"
AGENT_DESCRIPTION = "An AI agent that fetches SAP PPS Sourcing Projects, detects country-specific portal requirements, generates a compliant tender synopsis, and supports human review before publication."
AGENT_TAGS = ["tender", "synopsis", "sap-pps", "procurement", "public-sector"]
AGENT_EXAMPLES = [
    "Generate a tender synopsis for sourcing project 5189",
    "Create a synopsis for SP 4700000123 in German for the TED portal",
    "Fetch and summarise sourcing project 5200 for India eProcure format",
]
# =============================================================================

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "9000"))


@click.command()
@click.option("--host", default=HOST)
@click.option("--port", default=PORT)
def main(host: str, port: int):
    skill = AgentSkill(
        id="tender-synopsis-agent",
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        tags=AGENT_TAGS,
        examples=AGENT_EXAMPLES,
    )
    agent_card = AgentCard(
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        url=os.environ.get("AGENT_PUBLIC_URL", f"http://{host}:{port}/"),
        version="1.0.0",
        defaultInputModes=["text", "text/plain"],
        defaultOutputModes=["text", "text/plain"],
        capabilities=AgentCapabilities(streaming=True, pushNotifications=False),
        skills=[skill],
    )
    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=DefaultRequestHandler(
            agent_executor=AgentExecutor(),
            task_store=InMemoryTaskStore(),
        ),
        card_modifier=_apply_dwc_subdomain,
    )
    logger.info(f"Starting A2A server at http://{host}:{port}")
    a2a_app = server.build()
    a2a_app.add_middleware(DwcSubdomainMiddleware)
    auto_instrument(middlewares=[StarletteIASTelemetryMiddleware(app=a2a_app)])

    # Combine ORD discovery routes with A2A app
    combined_app = Starlette(
        routes=[
            *create_ord_routes(),
            Mount("/", app=a2a_app),
        ]
    )
    logger.info(f"ORD endpoint: http://{host}:{port}/.well-known/open-resource-discovery")
    uvicorn.run(combined_app, host=host, port=port)


if __name__ == "__main__":
    main()