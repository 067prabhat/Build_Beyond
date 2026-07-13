import json
import logging
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

ORD_DIR = Path(__file__).parent


def _load_template(filename: str) -> dict:
    with open(ORD_DIR / filename) as f:
        return json.load(f)


def _inject_tenant_id(doc: dict, tenant_id: str) -> dict:
    """Replace {{LOCAL_TENANT_ID}} placeholders in the ORD document."""
    raw = json.dumps(doc)
    raw = raw.replace("{{LOCAL_TENANT_ID}}", tenant_id)
    return json.loads(raw)


def _log_request(request: Request, endpoint: str) -> None:
    headers = dict(request.headers)
    logger.info(
        "\n>>> REQUEST [%s] %s %s\n    query: %s\n    headers: %s",
        endpoint,
        request.method,
        str(request.url),
        json.dumps(dict(request.query_params)),
        json.dumps(headers, indent=4),
    )


def _log_response(endpoint: str, body: dict) -> None:
    logger.info(
        "\n<<< RESPONSE [%s]\n%s",
        endpoint,
        json.dumps(body, indent=2),
    )


async def well_known_ord_config(request: Request) -> JSONResponse:
    """GET /.well-known/open-resource-discovery

    Branches on ord-perspective header: 'static' → system-type, else → system-instance.
    """
    perspective = request.headers.get("ord-perspective", "dynamic")
    if perspective == "static":
        doc_url = "/open-resource-discovery/v1/documents/system-type"
        ord_perspective = "system-type"
        label = "well-known-config[static]"
    else:
        doc_url = "/open-resource-discovery/v1/documents/system-instance"
        ord_perspective = "system-instance"
        label = "well-known-config[dynamic]"

    _log_request(request, label)
    config = {
        "openResourceDiscoveryV1": {
            "documents": [
                {
                    "url": doc_url,
                    "perspective": ord_perspective,
                    "accessStrategies": [
                        {
                            "type": "sap:cmp-mtls:v1"
                        }
                    ]
                },
            ]
        }
    }
    _log_response(label, config)
    return JSONResponse(config)


async def system_instance_document(request: Request) -> JSONResponse:
    """GET /open-resource-discovery/v1/documents/system-instance — tenant-aware ORD document."""
    _log_request(request, "system-instance-document")
    tenant_id = (
        request.query_params.get("tenantId")
        or request.headers.get("dwc-tenant")
        or request.headers.get("x-tenant-id", "")
    )
    logger.info("ORD system-instance-document resolved tenant_id=%r", tenant_id)
    doc = _load_template("system_instance.json")
    if tenant_id:
        doc = _inject_tenant_id(doc, tenant_id)
    _log_response("system-instance-document", doc)
    return JSONResponse(doc)


async def system_type_document(request: Request) -> JSONResponse:
    """GET /open-resource-discovery/v1/documents/system-type — static system-type ORD document."""
    _log_request(request, "system-type-document")
    doc = _load_template("system_type.json")
    _log_response("system-type-document", doc)
    return JSONResponse(doc)


def create_ord_routes() -> list[Route]:
    """Return ORD discovery routes to be mounted in the application."""
    return [
        Route(
            "/.well-known/open-resource-discovery",
            endpoint=well_known_ord_config,
            methods=["GET"],
        ),
        Route(
            "/open-resource-discovery/v1/documents/system-instance",
            endpoint=system_instance_document,
            methods=["GET"],
        ),
        Route(
            "/open-resource-discovery/v1/documents/system-type",
            endpoint=system_type_document,
            methods=["GET"],
        ),
    ]
