"""Browser-facing routes delegate every authorization decision to the ERP connector."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.responses import Response

from kanzlei_ai.connectors.base import ConnectorError, ERPConnector
from kanzlei_ai.connectors.contracts import (
    AuthorizationRequest,
    DocumentRequest,
    ExchangeRequest,
    PageRequest,
    ReadRequest,
)

router = APIRouter(prefix="/v1")
bearer = HTTPBearer(auto_error=False)


def connector(request: Request) -> ERPConnector:
    adapter = request.app.state.erp_connector
    if adapter is None:
        raise ConnectorError()
    return adapter


def token(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ConnectorError("UNAUTHENTICATED", 401)
    from pydantic import TypeAdapter, ValidationError

    from kanzlei_ai.connectors.contracts import Token

    try:
        return TypeAdapter(Token).validate_python(credentials.credentials)
    except ValidationError:
        raise ConnectorError("UNAUTHENTICATED", 401) from None


Adapter = Annotated[ERPConnector, Depends(connector)]
AccessToken = Annotated[str, Depends(token)]


@router.post("/auth/exchange")
async def exchange(body: ExchangeRequest, adapter: Adapter):
    return await adapter.exchange_ticket(body.ticket)


@router.get("/auth/me")
async def identity(access_token: AccessToken, adapter: Adapter):
    return await adapter.identity(access_token)


@router.post("/auth/logout")
async def logout(access_token: AccessToken, adapter: Adapter):
    await adapter.revoke(access_token)
    return {"version": 1, "revoked": True}


@router.post("/connector/clients")
async def clients(body: PageRequest, access_token: AccessToken, adapter: Adapter):
    return await adapter.list_clients(access_token, body.limit, body.cursor)


@router.post("/connector/documents")
async def documents(body: DocumentRequest, access_token: AccessToken, adapter: Adapter):
    return await adapter.list_documents(access_token, body.client_id, body.limit, body.cursor)


@router.post("/connector/authorize")
async def authorize(body: AuthorizationRequest, access_token: AccessToken, adapter: Adapter):
    return await adapter.authorize_sources(access_token, body.scope, body.sources)


@router.post("/connector/read")
async def read(body: ReadRequest, access_token: AccessToken, adapter: Adapter):
    content = await adapter.read_source(access_token, body.scope, body.source)
    return Response(
        content,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
