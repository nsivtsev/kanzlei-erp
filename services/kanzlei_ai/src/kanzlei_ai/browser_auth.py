"""Browser handoff routes, mounted only when chat is enabled."""

from fastapi import APIRouter, Request

from kanzlei_ai.auth import Adapter
from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import BrowserExchangeRequest, Grant

router = APIRouter(prefix="/v1/auth/browser")


def _erp_origin(request: Request) -> str:
    origin = getattr(request.app.state, "chat_browser_erp_origin", None)
    if not origin:
        raise ConnectorError()
    return origin


@router.get("/config")
async def config(request: Request):
    return {"version": 1, "erp_origin": _erp_origin(request)}


@router.post("/exchange")
async def exchange(body: BrowserExchangeRequest, request: Request, adapter: Adapter) -> Grant:
    _erp_origin(request)
    origins = request.headers.getlist("Origin")
    if (
        len(origins) != 1
        or len(origins[0]) > 300
        or not origins[0].startswith(("https://", "http://"))
    ):
        raise ConnectorError("UNAUTHENTICATED", 401)
    # The ERP compares this browser-supplied header to its configured chat origin.
    return await adapter.exchange_browser_ticket(body.ticket, body.state, body.verifier, origins[0])
