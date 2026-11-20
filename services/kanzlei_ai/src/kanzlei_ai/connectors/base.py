"""Application-facing adapter interface and sanitized errors."""

from typing import Protocol

from kanzlei_ai.connectors.contracts import (
    AuthorizationResult,
    ClientPage,
    DocumentPage,
    Grant,
    Identity,
    Scope,
    SourceRef,
)


class ConnectorError(Exception):
    def __init__(self, code: str = "CONNECTOR_UNAVAILABLE", status: int = 503):
        super().__init__(code)
        self.code = code
        self.status = status


class ERPConnector(Protocol):
    async def exchange_ticket(self, ticket: str) -> Grant: ...
    async def exchange_browser_ticket(
        self, ticket: str, state: str, verifier: str, chat_origin: str
    ) -> Grant: ...
    async def identity(self, access_token: str) -> Identity: ...
    async def revoke(self, access_token: str) -> None: ...
    async def list_clients(
        self, access_token: str, limit: int = 50, cursor: str | None = None
    ) -> ClientPage: ...
    async def list_documents(
        self,
        access_token: str,
        client_id: str,
        limit: int = 50,
        cursor: str | None = None,
        *,
        query: str | None = None,
    ) -> DocumentPage: ...
    async def check_scope(self, access_token: str, scope: Scope) -> None: ...
    async def authorize_sources(
        self, access_token: str, scope: Scope, sources: list[SourceRef]
    ) -> AuthorizationResult: ...
    async def read_source(self, access_token: str, scope: Scope, source: SourceRef) -> bytes: ...
    async def read_background_source(self, source: SourceRef) -> bytes: ...
