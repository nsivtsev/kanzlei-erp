"""Opt-in client-scoped chat routes with delegated authorization and bounded SSE."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, select, tuple_
from sqlalchemy.engine import Engine
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, StreamingResponse

from kanzlei_ai.auth import AccessToken, Adapter
from kanzlei_ai.chat import history
from kanzlei_ai.chat.citations import ChatProvider, validate_answer
from kanzlei_ai.chat.context import (
    EMAIL_MIME,
    PDF_MIME,
    Session,
    checked_evidence,
    configure_query,
    database_call,
    retrieve_context,
)
from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import DocumentPage, Identifier, Identity, Scope
from kanzlei_ai.models import document_artifacts, index_publications, jobs, relations, sources
from kanzlei_ai.providers.chat_fake import ExtractiveChatProvider
from kanzlei_ai.search.index import INDEX_PROFILE, MISTRAL_INDEX_PROFILE, SearchIndex
from kanzlei_ai.search.publication import INDEX_STAGE

_POLL_SECONDS = 2.0
_ENDPOINT_SECONDS = 15
_STREAM_SECONDS = 25
_REQUEST_LIMIT = 8192
_MAX_ACTIVE_REQUESTS = 8
_MAX_ACTIVE_TURNS = 4
_PDF_LIMIT = 20 * 1024 * 1024
_SOURCE_PAGE_SIZE = 25


class ChatRuntime:
    def __init__(self, engine: Engine, index: SearchIndex, provider: ChatProvider | None = None):
        self.engine = engine
        self.index = index
        self.provider = provider if provider is not None else ExtractiveChatProvider()
        self.active_requests = 0
        self.active_turns = 0


def runtime(request: Request) -> ChatRuntime:
    value = getattr(request.app.state, "chat_runtime", None)
    if value is None:
        raise ConnectorError("NOT_FOUND", 404)
    return value


class BoundedRoute(APIRoute):
    def get_route_handler(self):
        endpoint = super().get_route_handler()

        async def handle(request: Request) -> Response:
            service = runtime(request)
            if service.active_requests >= _MAX_ACTIVE_REQUESTS:
                raise ConnectorError("CHAT_BUSY", 429)
            service.active_requests += 1
            try:
                async with asyncio.timeout(_ENDPOINT_SECONDS):
                    return await endpoint(request)
            except TimeoutError:
                raise ConnectorError("CHAT_UNAVAILABLE", 503) from None
            finally:
                service.active_requests -= 1

        return handle


router = APIRouter(prefix="/v1/chat/conversations", route_class=BoundedRoute)
Runtime = Annotated[ChatRuntime, Depends(runtime)]
SourceCursor = Annotated[str | None, Query(min_length=1, max_length=4096)]
HistoryCursor = Annotated[str | None, Query(min_length=1, max_length=36)]


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_id: Identifier


class TurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: Annotated[str, Field(min_length=1, max_length=1000, repr=False)]


class SourcePageBody(CreateBody):
    cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None = Field(
        default=None, repr=False
    )
    query: Annotated[str, Field(max_length=200, pattern=r"^[^\x00-\x1f]*$", repr=False)] = ""
    status: Literal["all", "ready", "pending", "failed"] = "all"


async def _body(request, model):
    data = bytearray()
    try:
        async for chunk in request.stream():
            if len(data) + len(chunk) > _REQUEST_LIMIT:
                raise ConnectorError("LIMIT_EXCEEDED", 413)
            data.extend(chunk)
        return model.model_validate_json(data)
    except (ClientDisconnect, ValidationError):
        raise ConnectorError("INVALID_REQUEST", 422) from None


def _id(value, *, cursor=False):
    if len(value) == 36:
        try:
            if str(UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise ConnectorError("INVALID_REQUEST", 422) if cursor else ConnectorError("NOT_FOUND", 404)


async def _identity(adapter, access_token):
    identity = await adapter.identity(access_token)
    if not isinstance(identity, Identity):
        raise ConnectorError()
    return identity


async def _session(adapter, access_token, service, *, client_id=None, conversation_id=None):
    identity = await _identity(adapter, access_token)
    if conversation_id is not None:
        client_id = await database_call(
            history.lookup,
            service.engine,
            identity.connector_id,
            identity.subject,
            _id(conversation_id),
        )
    if "\x00" in client_id:
        raise ConnectorError("INVALID_REQUEST", 422)
    session = Session(
        adapter,
        access_token,
        identity.connector_id,
        identity.subject,
        Scope(kind="client", client_id=client_id),
        index_profile=getattr(service.index, "profile", INDEX_PROFILE),
    )
    await adapter.check_scope(access_token, session.scope)
    return session


async def _verify_session(session):
    identity = await _identity(session.connector, session.token)
    if (identity.connector_id, identity.subject) != (session.connector_id, session.subject):
        raise ConnectorError("UNAUTHENTICATED", 401)
    await session.connector.check_scope(session.token, session.scope)


@router.post("", status_code=201)
async def create(request: Request, adapter: Adapter, access_token: AccessToken, service: Runtime):
    body = await _body(request, CreateBody)
    session = await _session(adapter, access_token, service, client_id=body.client_id)
    return await database_call(history.create_conversation, service.engine, session)


@router.get("")
async def list_owned(
    client_id: Identifier, adapter: Adapter, access_token: AccessToken, service: Runtime
):
    session = await _session(adapter, access_token, service, client_id=client_id)
    return {"items": await database_call(history.list_conversations, service.engine, session)}


@router.get("/{conversation_id}/history")
async def conversation_history(
    conversation_id: str,
    adapter: Adapter,
    access_token: AccessToken,
    service: Runtime,
    cursor: HistoryCursor = None,
):
    session = await _session(adapter, access_token, service, conversation_id=conversation_id)
    return await history.read_history(
        service.engine,
        session,
        conversation_id,
        _id(cursor, cursor=True) if cursor is not None else None,
    )


def _source_status(engine, session, entries):
    src, pub, artifact = sources.c, index_publications.c, document_artifacts.c
    job = jobs.c
    projection = (
        sources.join(relations, relations.c.source_id == src.id)
        .outerjoin(
            index_publications,
            and_(
                pub.source_id == src.id,
                pub.revision == src.revision,
                pub.generation == src.generation,
                pub.profile == session.index_profile,
            ),
        )
        .outerjoin(
            document_artifacts,
            and_(
                artifact.id == pub.artifact_id,
                artifact.source_id == src.id,
                artifact.revision == src.revision,
                artifact.generation == src.generation,
            ),
        )
        .outerjoin(
            jobs,
            and_(
                job.source_id == src.id,
                job.revision == src.revision,
                job.generation == src.generation,
                job.stage == INDEX_STAGE,
                job.pipeline_version == session.index_profile,
            ),
        )
    )
    with engine.begin() as connection:
        configure_query(connection)
        rows = connection.execute(
            select(
                src.source_id,
                src.revision,
                src.status,
                artifact.id.label("artifact_id"),
                job.status.label("index_status"),
            )
            .select_from(projection)
            .where(
                src.connector_id == session.connector_id,
                relations.c.client_id == session.scope.client_id,
                src.active.is_(True),
                tuple_(src.source_id, src.revision).in_(
                    [(doc.source_id, doc.revision) for doc in entries]
                ),
            )
        ).all()
    return {
        (row.source_id, row.revision): (
            "ready"
            if row.status == "ready" and row.artifact_id is not None
            else "failed"
            if row.status == "failed" or row.index_status == "failed"
            else "pending"
        )
        for row in rows
    }


@router.get("/{conversation_id}/sources")
async def source_list(
    conversation_id: str,
    adapter: Adapter,
    access_token: AccessToken,
    service: Runtime,
    cursor: SourceCursor = None,
):
    session = await _session(adapter, access_token, service, conversation_id=conversation_id)
    return await _list_source_page(service, session, cursor)


@router.post("/sources")
async def source_catalogue(
    request: Request, adapter: Adapter, access_token: AccessToken, service: Runtime
):
    body = await _body(request, SourcePageBody)
    session = await _session(adapter, access_token, service, client_id=body.client_id)
    return await _list_source_page(service, session, body.cursor, body.query.strip(), body.status)


async def _list_source_page(service, session, cursor, query="", status="all"):
    page = await session.connector.list_documents(
        session.token,
        session.scope.client_id,
        _SOURCE_PAGE_SIZE,
        cursor,
        **({"query": query} if query else {}),
    )
    if (
        not isinstance(page, DocumentPage)
        or len(page.items) > _SOURCE_PAGE_SIZE
        or page.next_cursor == ""
        or (cursor is not None and page.next_cursor == cursor)
        or any(session.scope.client_id not in doc.client_ids for doc in page.items)
    ):
        raise ConnectorError()
    entries = [
        doc
        for doc in page.items
        if doc.mime_type in (PDF_MIME, EMAIL_MIME) and query.casefold() in doc.title.casefold()
    ]
    statuses = (
        await database_call(_source_status, service.engine, session, entries) if entries else {}
    )
    items = []
    for doc in entries:
        processing = statuses.get((doc.source_id, doc.revision), "pending")
        if status == "all" or status == processing:
            items.append({"source_id": doc.source_id, "title": doc.title, "status": processing})
    return {"items": items, "next_cursor": page.next_cursor}


def _sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=True)}\n\n"


async def _refresh_context(service, session, context):
    await _verify_session(session)
    deps = [item.dependency for item in context]
    current = await checked_evidence(service.engine, session, deps)
    if any(current.get(item.dependency) != item for item in context):
        raise ConnectorError("STALE_SOURCE", 409)


async def _generate(service, session, question, context):
    if session.index_profile == MISTRAL_INDEX_PROFILE:
        refs = list(dict.fromkeys(item.dependency.source for item in context))
        await session.connector.check_processing_policy(
            "generate", session.scope, refs, access_token=session.token
        )
    task = asyncio.create_task(service.provider.generate(question, context))
    try:
        while True:
            completed, _ = await asyncio.wait({task}, timeout=_POLL_SECONDS)
            if completed:
                try:
                    return task.result()
                except (asyncio.CancelledError, Exception):
                    raise ConnectorError("CHAT_UNAVAILABLE", 503) from None
            await _refresh_context(service, session, context)
    finally:
        if not task.done():
            task.cancel()
        # Retrieve any exception, including on a disconnect or invalidation.
        await asyncio.gather(task, return_exceptions=True)


async def _stream(service, session, question, conversation_id, commit: history.TurnCommit):
    if service.active_turns >= _MAX_ACTIVE_TURNS:
        yield _sse("error", {"code": "CHAT_BUSY"})
        return
    service.active_turns += 1
    try:
        async with asyncio.timeout(commit.remaining()):
            yield _sse("status", {"phase": "retrieving"})
            context = await retrieve_context(service.engine, service.index, session, question)
            yield _sse("status", {"phase": "generating"})
            await _refresh_context(service, session, context)
            packet = await _generate(service, session, question, context) if context else None
            yield _sse("status", {"phase": "validating"})
            answer = validate_answer(question, context, packet)
            await _refresh_context(service, session, context)
            item = await database_call(
                history.store_turn,
                service.engine,
                session,
                conversation_id,
                question,
                context,
                answer,
                commit,
            )
            await _refresh_context(service, session, context)
        yield _sse("answer", item)
    except ConnectorError as error:
        yield _sse("error", {"code": error.code})
    except (TimeoutError, RuntimeError):
        yield _sse("error", {"code": "CHAT_UNAVAILABLE"})
    finally:
        commit.cancel()
        service.active_turns -= 1


class ChatStream(StreamingResponse):
    """Listen for disconnect throughout retrieval, generation, and validation."""

    def __init__(self, body, commit: history.TurnCommit):
        super().__init__(
            body,
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )
        self.commit = commit

    async def __call__(self, scope, receive, send):
        async def stream():
            try:
                await self.stream_response(send)
            except OSError:
                pass
            finally:
                self.commit.cancel()
                disconnect.cancel()
                await self.body_iterator.aclose()

        async def watch_disconnect():
            await self.listen_for_disconnect(receive)
            self.commit.cancel()
            writer.cancel()

        async with asyncio.TaskGroup() as tasks:
            writer = tasks.create_task(stream())
            disconnect = tasks.create_task(watch_disconnect())


@router.post("/{conversation_id}/turns")
async def turn(
    conversation_id: str,
    request: Request,
    adapter: Adapter,
    access_token: AccessToken,
    service: Runtime,
):
    body = await _body(request, TurnBody)
    question = body.question.strip()
    if not question or "\x00" in question:
        raise ConnectorError("INVALID_REQUEST", 422)
    session = await _session(adapter, access_token, service, conversation_id=conversation_id)
    commit = history.TurnCommit(_STREAM_SECONDS)
    return ChatStream(
        _stream(service, session, question, conversation_id, commit),
        commit,
    )


@router.get("/{conversation_id}/turns/{turn_id}/citations/{citation_id}/pdf")
async def citation_pdf(
    conversation_id: str,
    turn_id: str,
    citation_id: str,
    adapter: Adapter,
    access_token: AccessToken,
    service: Runtime,
):
    session = await _session(adapter, access_token, service, conversation_id=conversation_id)
    deps, citation, evidence = await history.find_citation(
        service.engine, session, conversation_id, _id(turn_id), _id(citation_id)
    )
    if citation.source_kind != "pdf":
        raise ConnectorError("NOT_FOUND", 404)
    try:
        content = await adapter.read_source(access_token, session.scope, evidence.dependency.source)
    except ConnectorError as error:
        if error.status in (403, 404, 409):
            raise ConnectorError("NOT_FOUND", 404) from None
        raise
    if (
        not isinstance(content, bytes)
        or len(content) > _PDF_LIMIT
        or not content.startswith(b"%PDF-")
        or hashlib.sha256(content).hexdigest() != evidence.content_sha256
    ):
        raise ConnectorError("NOT_FOUND", 404)
    await _verify_session(session)
    if await history.check_citation(service.engine, session, deps, citation) != evidence:
        raise ConnectorError("NOT_FOUND", 404)
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="dokument.pdf"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{conversation_id}/turns/{turn_id}/citations/{citation_id}/original")
async def citation_original(
    conversation_id: str,
    turn_id: str,
    citation_id: str,
    adapter: Adapter,
    access_token: AccessToken,
    service: Runtime,
):
    session = await _session(adapter, access_token, service, conversation_id=conversation_id)
    deps, citation, evidence = await history.find_citation(
        service.engine, session, conversation_id, _id(turn_id), _id(citation_id)
    )
    source = evidence.dependency.source
    prefix, _, name = source.source_id.partition(":")
    if (
        citation.source_kind != "email"
        or prefix != "email"
        or name in ("", ".", "..")
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ConnectorError("NOT_FOUND", 404)
    expected = "/desk/communication/" + quote(name, safe="")
    try:
        locator = await adapter.resolve_source_locator(access_token, session.scope, source)
    except ConnectorError as error:
        if error.status in (403, 404, 409):
            raise ConnectorError("NOT_FOUND", 404) from None
        raise
    if locator != expected:
        raise ConnectorError("NOT_FOUND", 404)
    await _verify_session(session)
    if await history.check_citation(service.engine, session, deps, citation) != evidence:
        raise ConnectorError("NOT_FOUND", 404)
    return JSONResponse(
        {"source_kind": "email", "locator": locator},
        headers={"Cache-Control": "private, no-store"},
    )
