"""Bounded RPC transport: fixed methods, no redirects, no ERP cookies or raw errors."""

import asyncio
import hashlib
import json
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import (
    AuthorizationResult,
    ChangePage,
    ClientPage,
    DocumentPage,
    Grant,
    Identity,
    InventoryPage,
    Revoked,
    Scope,
    ScopeCheck,
    SourceRef,
)

_AUTHORIZATION_BATCH_SIZE = 4
_MAX_AUTHORIZATION_REFERENCES = 50


class FrappeConnector:
    def __init__(
        self,
        base_url: str,
        service_key: SecretStr,
        *,
        allow_insecure_http: bool = False,
        site_name: str | None = None,
        timeout_seconds: float = 5,
        max_json_bytes: int = 2 * 1024 * 1024,
        max_source_bytes: int = 20 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (parsed.scheme == "http" and not allow_insecure_http)
        ):
            raise ValueError("ERP requires an HTTPS origin; HTTP needs explicit local opt-in")
        if not 32 <= len(service_key.get_secret_value()) <= 256:
            raise ValueError("Invalid service credential")
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key
        self.site_name = site_name
        self.timeout = timeout_seconds
        self.max_json_bytes = max_json_bytes
        self.max_source_bytes = max_source_bytes
        self.transport = transport

    async def _rpc(self, method: str, payload: dict, *, binary: bool = False):
        headers = {
            "X-Kanzlei-AI-Service-Key": self.service_key.get_secret_value(),
            "Accept-Encoding": "identity",
        }
        if self.site_name:
            headers["X-Frappe-Site-Name"] = self.site_name
        maximum = self.max_source_bytes if binary else self.max_json_bytes
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    trust_env=False,
                    follow_redirects=False,
                    transport=self.transport,
                ) as client:
                    url = self.base_url + "/api/method/kanzlei_erp.ai_connector.api." + method
                    async with client.stream(
                        "POST", url, json={"version": 1, **payload}, headers=headers
                    ) as response:
                        if response.status_code != 200:
                            mapping = {
                                400: ("INVALID_REQUEST", 400),
                                401: ("UNAUTHENTICATED", 401),
                                403: ("FORBIDDEN", 403),
                                409: ("STALE_SOURCE", 409),
                                413: ("LIMIT_EXCEEDED", 413),
                                429: ("CONNECTOR_UNAVAILABLE", 503),
                            }
                            raise ConnectorError(
                                *mapping.get(response.status_code, ("CONNECTOR_UNAVAILABLE", 503))
                            )
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(content) + len(chunk) > maximum:
                                raise ConnectorError("LIMIT_EXCEEDED", 413)
                            content.extend(chunk)
                        if binary:
                            return bytes(content), response.headers
                        body = json.loads(content)
                        if not isinstance(body, dict) or "message" not in body:
                            raise ConnectorError()
                        return body["message"]
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
            raise ConnectorError() from None

    async def _typed(self, model, method: str, **payload):
        body = await self._rpc(method, payload)
        try:
            return model.model_validate(body)
        except ValidationError:
            raise ConnectorError() from None

    async def exchange_ticket(self, ticket: str) -> Grant:
        return await self._typed(Grant, "exchange_ticket", ticket=ticket)

    async def exchange_browser_ticket(
        self, ticket: str, state: str, verifier: str, chat_origin: str
    ) -> Grant:
        return await self._typed(
            Grant,
            "exchange_browser_ticket",
            ticket=ticket,
            state=state,
            verifier=verifier,
            chat_origin=chat_origin,
        )

    async def identity(self, access_token: str) -> Identity:
        return await self._typed(Identity, "identity", access_token=access_token)

    async def revoke(self, access_token: str) -> None:
        await self._typed(Revoked, "revoke", access_token=access_token)

    async def list_clients(
        self, access_token: str, limit: int = 50, cursor: str | None = None
    ) -> ClientPage:
        return await self._typed(
            ClientPage, "list_clients", access_token=access_token, limit=limit, cursor=cursor
        )

    async def list_documents(
        self,
        access_token: str,
        client_id: str,
        limit: int = 50,
        cursor: str | None = None,
        *,
        query: str | None = None,
    ) -> DocumentPage:
        return await self._typed(
            DocumentPage,
            "list_documents",
            access_token=access_token,
            client_id=client_id,
            limit=limit,
            cursor=cursor,
            **({"query": query} if query else {}),
        )

    async def check_scope(self, access_token: str, scope: Scope) -> None:
        result = await self._typed(
            ScopeCheck,
            "check_scope",
            access_token=access_token,
            scope=scope.model_dump(exclude_none=True, exclude={"version"}),
        )
        if not result.allowed:
            raise ConnectorError()

    async def authorize_sources(
        self, access_token: str, scope: Scope, sources: list[SourceRef]
    ) -> AuthorizationResult:
        if not 1 <= len(sources) <= _MAX_AUTHORIZATION_REFERENCES:
            raise ConnectorError("INVALID_REQUEST", 400)
        requested_scope = scope.model_dump(exclude_none=True)
        decisions = []
        for offset in range(0, len(sources), _AUTHORIZATION_BATCH_SIZE):
            references = sources[offset : offset + _AUTHORIZATION_BATCH_SIZE]
            batch = [ref.model_dump() for ref in references]
            result = await self._typed(
                AuthorizationResult,
                "authorize_sources",
                access_token=access_token,
                scope=requested_scope,
                sources=batch,
            )
            if [decision.model_dump(exclude={"allowed"}) for decision in result.decisions] != batch:
                raise ConnectorError()
            decisions.extend(result.decisions)
        return AuthorizationResult(version=1, decisions=decisions)

    async def read_source(self, access_token: str, scope: Scope, source: SourceRef) -> bytes:
        content, headers = await self._rpc(
            "read_source",
            {
                "access_token": access_token,
                "scope": scope.model_dump(exclude_none=True),
                "source": source.model_dump(),
            },
            binary=True,
        )
        if (
            headers.get("X-AI-Revision") != source.revision
            or headers.get("X-AI-Content-SHA256") != hashlib.sha256(content).hexdigest()
        ):
            raise ConnectorError()
        return content

    async def read_background_source(self, source: SourceRef) -> bytes:
        """Read a current source under the fixed ingestion principal only."""
        content, headers = await self._rpc(
            "background_read_source", {"source": source.model_dump()}, binary=True
        )
        if (
            headers.get("X-AI-Revision") != source.revision
            or headers.get("X-AI-Content-SHA256") != hashlib.sha256(content).hexdigest()
        ):
            raise ConnectorError()
        return content

    async def inventory(self, cursor: str | None = None, limit: int = 50) -> InventoryPage:
        return await self._typed(InventoryPage, "inventory", cursor=cursor, limit=limit)

    async def changes(self, cursor: str, limit: int = 50) -> ChangePage:
        return await self._typed(ChangePage, "changes", cursor=cursor, limit=limit)
