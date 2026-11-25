"""Resolve exact, currently published SQL chunks after delegated source checks."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.exc import SQLAlchemyError

from kanzlei_ai.connectors.base import ConnectorError, ERPConnector
from kanzlei_ai.connectors.contracts import AuthorizationResult, Decision, Scope, SourceRef
from kanzlei_ai.models import (
    document_artifacts,
    document_chunks,
    index_publications,
    relations,
    sources,
)
from kanzlei_ai.search.index import INDEX_PROFILE, exact_identifiers
from kanzlei_ai.search.retrieve import retrieve

RETRIEVAL_LIMIT = 6
RELATED_TABLE_LIMIT = 4
CONTEXT_LIMIT = RETRIEVAL_LIMIT + RELATED_TABLE_LIMIT
CHUNK_LIMIT = 1800
PDF_MIME = "application/pdf"
EMAIL_MIME = "application/vnd.kanzlei.email+json"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SourceKind = Literal["pdf", "email", "docx"]
_SOURCE_KINDS: dict[str, SourceKind] = {PDF_MIME: "pdf", EMAIL_MIME: "email", DOCX_MIME: "docx"}


@dataclass(frozen=True)
class Dependency:
    source: SourceRef
    generation: int
    chunk_id: str

    def version_key(self):
        return self.source.source_id, self.source.revision, self.generation, self.source.client_id

    def key(self):
        return (*self.version_key(), self.chunk_id)


@dataclass(frozen=True)
class Evidence:
    dependency: Dependency
    page: int | None
    kind: str
    text: str = field(repr=False)
    title: str = "PDF"
    mime_type: str = PDF_MIME
    content_sha256: str = ""
    table_fragment: bool = False

    @property
    def source_kind(self) -> SourceKind | None:
        return _SOURCE_KINDS.get(self.mime_type)


@dataclass(frozen=True)
class Session:
    connector: ERPConnector
    token: str = field(repr=False)
    connector_id: str
    subject: str
    # None: an assistant conversation without access to documents.
    scope: Scope | None
    index_profile: str = INDEX_PROFILE


async def database_call(operation, *args):
    try:
        return await asyncio.to_thread(operation, *args)
    except SQLAlchemyError:
        raise ConnectorError("CHAT_UNAVAILABLE", 503) from None


def configure_query(connection):
    connection.exec_driver_sql("SET LOCAL statement_timeout = '4s'")
    connection.exec_driver_sql("SET LOCAL lock_timeout = '2s'")


async def granted(session: Session, dependencies: Iterable[Dependency]) -> set[SourceRef]:
    return await granted_sources(session, [dependency.source for dependency in dependencies])


async def granted_sources(session: Session, references: Iterable[SourceRef]) -> set[SourceRef]:
    refs = list(dict.fromkeys(references))
    allowed = set()
    if not refs:
        return allowed
    if session.scope is None:
        raise ConnectorError("CHAT_UNAVAILABLE", 503)
    if session.scope.kind == "client" and any(
        ref.client_id != session.scope.client_id for ref in refs
    ):
        raise ConnectorError("CHAT_UNAVAILABLE", 503)
    for offset in range(0, len(refs), 50):
        batch = refs[offset : offset + 50]
        result = await session.connector.authorize_sources(session.token, session.scope, batch)
        decisions = getattr(result, "decisions", None)
        if (
            not isinstance(result, AuthorizationResult)
            or not isinstance(decisions, list)
            or len(decisions) != len(batch)
        ):
            raise ConnectorError()
        for ref, decision in zip(batch, decisions, strict=True):
            if (
                not isinstance(decision, Decision)
                or type(decision.allowed) is not bool
                or (decision.source_id, decision.revision, decision.client_id)
                != (ref.source_id, ref.revision, ref.client_id)
            ):
                raise ConnectorError()
            if decision.allowed:
                allowed.add(ref)
    return allowed


def _published_chunks(session: Session):
    """Chunks of each source's current publication for this index profile and scope."""
    source, publication, artifact, chunk = (
        sources.c,
        index_publications.c,
        document_artifacts.c,
        document_chunks.c,
    )
    scope_relation = relations.c
    joined = (
        sources.join(relations, scope_relation.source_id == source.id)
        .join(
            index_publications,
            and_(
                publication.source_id == source.id,
                publication.revision == source.revision,
                publication.generation == source.generation,
                publication.profile == session.index_profile,
            ),
        )
        .join(
            document_artifacts,
            and_(
                artifact.id == publication.artifact_id,
                artifact.source_id == source.id,
                artifact.revision == source.revision,
                artifact.generation == source.generation,
            ),
        )
        .join(
            document_chunks,
            and_(
                chunk.artifact_id == artifact.id,
                chunk.source_id == source.id,
                chunk.revision == source.revision,
                chunk.generation == source.generation,
                chunk.parser_version == artifact.parser_version,
                chunk.content_sha256 == artifact.content_sha256,
            ),
        )
    )
    version = (source.source_id, source.revision, source.generation, scope_relation.client_id)
    mime = source.manifest["mime_type"].as_string()
    statement = (
        select(
            *version,
            chunk.id,
            chunk.page,
            chunk.kind,
            chunk.text,
            chunk.table_fragment,
            source.manifest["title"].as_string().label("title"),
            mime.label("mime_type"),
            artifact.content_sha256,
        )
        .select_from(joined)
        .where(
            source.connector_id == session.connector_id,
            source.active.is_(True),
            or_(
                mime.in_((EMAIL_MIME, DOCX_MIME)),
                chunk.page.between(1, artifact.page_count),
            ),
            func.char_length(chunk.text).between(1, CHUNK_LIMIT),
        )
    )
    if session.scope.kind == "client":
        statement = statement.where(scope_relation.client_id == session.scope.client_id)
    return statement, version


def _evidence(rows) -> dict[Dependency, Evidence]:
    result = {}
    for row in rows:
        dependency = Dependency(
            SourceRef(
                source_id=row.source_id,
                revision=row.revision,
                client_id=row.client_id,
            ),
            row.generation,
            row.id,
        )
        page = None if row.mime_type in (EMAIL_MIME, DOCX_MIME) else row.page
        title = row.title
        if not title or len(title) > 500 or any(ord(char) < 32 for char in title):
            title = {EMAIL_MIME: "E-Mail", DOCX_MIME: "Word-Dokument"}.get(row.mime_type, "PDF")
        result[dependency] = Evidence(
            dependency,
            page,
            row.kind,
            row.text,
            title,
            row.mime_type,
            row.content_sha256,
            row.table_fragment,
        )
    return result


def current_evidence(connection, session: Session, dependencies, *, expand=(), lock=False):
    if (not dependencies and not expand) or session.scope is None:
        return {}
    statement, version = _published_chunks(session)
    source, chunk = sources.c, document_chunks.c
    if expand:
        statement = (
            statement.where(
                tuple_(*version).in_(expand),
                chunk.kind == "table",
                chunk.id.not_in([d.chunk_id for d in dependencies]),
            )
            .order_by(source.source_id, chunk.page, chunk.id, relations.c.client_id)
            .limit(RELATED_TABLE_LIMIT)
        )
    else:
        statement = statement.where(tuple_(*version, chunk.id).in_([d.key() for d in dependencies]))
    if lock:
        statement = statement.with_for_update(
            read=True,
            of=(sources, relations, index_publications, document_artifacts, document_chunks),
        )
    return _evidence(connection.execute(statement))


def _current_version(engine, session: Session, source_id: str, client_id: str):
    source, publication = sources.c, index_publications.c
    statement = (
        select(source.revision, source.generation)
        .select_from(
            sources.join(relations, relations.c.source_id == source.id).join(
                index_publications,
                and_(
                    publication.source_id == source.id,
                    publication.revision == source.revision,
                    publication.generation == source.generation,
                    publication.profile == session.index_profile,
                ),
            )
        )
        .where(
            source.connector_id == session.connector_id,
            source.active.is_(True),
            source.source_id == source_id,
            relations.c.client_id == client_id,
        )
    )
    with engine.begin() as connection:
        configure_query(connection)
        return connection.execute(statement).one_or_none()


def _document_page(engine, session: Session, version_key, offset: int, limit: int):
    statement, version = _published_chunks(session)
    statement = (
        statement.where(tuple_(*version).in_([version_key]))
        .order_by(document_chunks.c.ordinal, document_chunks.c.id)
        .offset(offset)
        .limit(limit + 1)
    )
    with engine.begin() as connection:
        configure_query(connection)
        found = list(_evidence(connection.execute(statement)).values())
    return tuple(found[:limit]), len(found) > limit


async def current_source(engine, session: Session, source_id: str, client_id: str):
    """The published version of a source the user picked, or None."""
    if session.scope is None or (
        session.scope.kind == "client" and client_id != session.scope.client_id
    ):
        return None
    row = await database_call(_current_version, engine, session, source_id, client_id)
    if row is None:
        return None
    ref = SourceRef(source_id=source_id, revision=row.revision, client_id=client_id)
    return ref, row.generation


async def read_document(
    engine, session: Session, ref: SourceRef, generation: int, offset: int, limit: int
) -> tuple[tuple[Evidence, ...], bool]:
    """Chunks of one currently authorized source version in reading order."""
    if ref not in await granted_sources(session, [ref]):
        return (), False
    await asyncio.sleep(0)
    version_key = (ref.source_id, ref.revision, generation, ref.client_id)
    return await database_call(_document_page, engine, session, version_key, offset, limit)


def read_current(engine, session, dependencies, *, expand=()):
    with engine.begin() as connection:
        configure_query(connection)
        return current_evidence(connection, session, dependencies, expand=expand)


async def checked_evidence(engine, session, dependencies) -> dict[Dependency, Evidence]:
    allowed = await granted(session, dependencies)
    # Match retrieval's ordering when an invalidation was scheduled during the grant.
    await asyncio.sleep(0)
    return await database_call(
        read_current, engine, session, [d for d in dependencies if d.source in allowed]
    )


async def retrieve_context(engine, index, session: Session, question: str) -> tuple[Evidence, ...]:
    hits = await retrieve(
        question,
        session.scope,
        session.token,
        RETRIEVAL_LIMIT,
        engine=engine,
        index=index,
        connector=session.connector,
    )
    dependencies = tuple(Dependency(hit.source, hit.generation, hit.chunk_id) for hit in hits)
    current = await checked_evidence(engine, session, dependencies)
    if len(current) != len(dependencies):
        raise ConnectorError("STALE_SOURCE", 409)
    identifiers = exact_identifiers(question)
    expandable = sorted(
        {
            dependency.version_key()
            for dependency, evidence in current.items()
            if evidence.source_kind == "pdf" and identifiers & exact_identifiers(evidence.text)
        }
    )
    # Tables may be on another page of the same authorized source version and client.
    if expandable:

        def expand():
            return read_current(engine, session, dependencies, expand=expandable)

        current.update(await database_call(expand))
    ordered = dict.fromkeys((*dependencies, *current))
    return tuple(current[dependency] for dependency in ordered)
