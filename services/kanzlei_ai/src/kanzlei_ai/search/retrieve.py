"""Retrieve current SQL chunks under a live ERP grant."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import and_, select, tuple_
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from kanzlei_ai.connectors.base import ConnectorError, ERPConnector
from kanzlei_ai.connectors.contracts import (
    AuthorizationResult,
    ClientPage,
    Decision,
    DocumentPage,
    Identity,
    Scope,
    SourceRef,
)
from kanzlei_ai.models import (
    document_artifacts,
    document_chunks,
    index_publications,
    relations,
    sources,
)
from kanzlei_ai.search.index import (
    INDEX_PROFILE,
    Candidate,
    SearchIndex,
    exact_identifiers,
    point_id,
)

_MAX_QUERY_CHARACTERS = 1000
_SEARCH_TIMEOUT_SECONDS = 15
_CLIENT_PAGE_SIZE = 50
_MAX_CLIENT_PAGES = 20
_MAX_CLIENTS = 1000
_MAX_SOURCE_REFS = 2000
_AUTH_BATCH_SIZE = 50
_BRANCHES = ("exact", "dense", "sparse")
_RRF_K = 60


@dataclass(frozen=True)
class SearchHit:
    source: SourceRef
    generation: int
    page: int
    chunk_id: str
    text: str = field(repr=False)


async def _scope_clients(
    scope: Scope, connector: ERPConnector, access_token: str, identity: Identity
) -> tuple[str, ...]:
    if scope.kind == "client":
        assert scope.client_id is not None
        probe = await connector.list_documents(access_token, scope.client_id, limit=1)
        if not isinstance(probe, DocumentPage):
            raise ConnectorError()
        return (scope.client_id,)
    if not identity.general_access:
        raise ConnectorError("FORBIDDEN", 403)

    clients: set[str] = set()
    seen_cursors: set[str] = set()
    cursor = None
    for _ in range(_MAX_CLIENT_PAGES):
        page = await connector.list_clients(access_token, limit=_CLIENT_PAGE_SIZE, cursor=cursor)
        if not isinstance(page, ClientPage) or len(page.items) > _CLIENT_PAGE_SIZE:
            raise ConnectorError()
        clients.update(client.id for client in page.items)
        cursor = page.next_cursor
        if len(clients) > _MAX_CLIENTS or cursor in seen_cursors or cursor == "":
            break
        if cursor is None:
            return tuple(sorted(clients))
        seen_cursors.add(cursor)
    raise ConnectorError("SEARCH_SCOPE_INCOMPLETE")


def _current_chunks(connector_id: str, client_ids: Sequence[str], candidates: Sequence[Candidate]):
    source, publication = sources.c, index_publications.c
    artifact, chunk, relation = document_artifacts.c, document_chunks.c, relations.c
    rows = (
        sources.join(
            index_publications,
            and_(
                publication.source_id == source.id,
                publication.revision == source.revision,
                publication.generation == source.generation,
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
        .join(relations, relation.source_id == source.id)
    )
    return (
        select()
        .select_from(rows)
        .where(
            source.active.is_(True),
            source.connector_id == connector_id,
            publication.profile == INDEX_PROFILE,
            relation.client_id.in_(client_ids),
            tuple_(
                publication.publication_id, source.id, source.revision, source.generation, chunk.id
            ).in_(
                [
                    (c.publication_id, c.source_pk, c.revision, c.generation, c.chunk_id)
                    for c in candidates
                ]
            ),
        )
    )


def _read_rows(engine: Engine, statement):
    # A new transaction and one SELECT provide a fresh snapshot for each check.
    with engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
        return connection.execute(statement).all()


def _source_refs(engine: Engine, statement) -> list[SourceRef]:
    source_id, revision, client_id = sources.c.source_id, sources.c.revision, relations.c.client_id
    refs = (
        statement.with_only_columns(source_id, revision, client_id)
        .distinct()
        .order_by(source_id, revision, client_id)
        .limit(_MAX_SOURCE_REFS + 1)
    )
    rows = _read_rows(engine, refs)
    if len(rows) > _MAX_SOURCE_REFS:
        raise ConnectorError("SEARCH_SCOPE_INCOMPLETE")
    return [
        SourceRef(source_id=source_id, revision=revision, client_id=client_id)
        for source_id, revision, client_id in rows
    ]


def _ref_values(ref: SourceRef) -> tuple[str, str, str]:
    return ref.source_id, ref.revision, ref.client_id


async def _authorize(
    connector: ERPConnector, access_token: str, scope: Scope, refs: list[SourceRef]
) -> list[SourceRef]:
    allowed = []
    for start in range(0, len(refs), _AUTH_BATCH_SIZE):
        batch = refs[start : start + _AUTH_BATCH_SIZE]
        result = await connector.authorize_sources(access_token, scope, list(batch))
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
                or _ref_values(ref) != _ref_values(decision)
            ):
                raise ConnectorError()
            if decision.allowed:
                allowed.append(ref)
    return allowed


def _authorized_hits(
    engine: Engine, statement, allowed: list[SourceRef]
) -> dict[Candidate, SearchHit]:
    source, chunk, relation = sources.c, document_chunks.c, relations.c
    hits = (
        statement.with_only_columns(
            index_publications.c.publication_id,
            source.id.label("source_pk"),
            source.source_id,
            source.revision,
            relation.client_id,
            chunk.generation,
            chunk.page,
            chunk.id.label("chunk_id"),
            chunk.text,
        )
        .where(
            tuple_(source.source_id, source.revision, relation.client_id).in_(
                [_ref_values(ref) for ref in allowed]
            )
        )
        .distinct(chunk.id)
        .order_by(chunk.id, relation.client_id)
    )
    authorized = {}
    for row in _read_rows(engine, hits):
        candidate = Candidate(
            point_id=point_id(row.publication_id, row.chunk_id),
            publication_id=row.publication_id,
            source_pk=row.source_pk,
            revision=row.revision,
            generation=row.generation,
            chunk_id=row.chunk_id,
        )
        authorized[candidate] = SearchHit(
            source=SourceRef(
                source_id=row.source_id, revision=row.revision, client_id=row.client_id
            ),
            generation=row.generation,
            page=row.page,
            chunk_id=row.chunk_id,
            text=row.text,
        )
    return authorized


def _rank(
    branches: Mapping[str, Sequence[Candidate]], hits: dict[Candidate, SearchHit], query: str
) -> list[SearchHit]:
    ranked = {
        branch: dict.fromkeys(candidate for candidate in candidates if candidate in hits)
        for branch, candidates in branches.items()
    }
    identifiers = exact_identifiers(query)
    exact = [
        candidate
        for candidate in ranked["exact"]
        if identifiers & exact_identifiers(hits[candidate].text)
    ]
    exact_matches = set(exact)
    scores: dict[Candidate, float] = defaultdict(float)
    for branch in ("dense", "sparse"):
        remaining = (candidate for candidate in ranked[branch] if candidate not in exact_matches)
        for rank, candidate in enumerate(remaining, start=1):
            scores[candidate] += 1 / (_RRF_K + rank)
    fused = sorted(
        scores,
        key=lambda candidate: (
            -scores[candidate],
            hits[candidate].source.source_id,
            hits[candidate].page,
            candidate.chunk_id,
        ),
    )
    return [hits[candidate] for candidate in [*exact, *fused]]


def _index_candidates(
    index: SearchIndex, query: str, connector_id: str, client_ids: Sequence[str], limit: int
) -> dict[str, list[Candidate]]:
    try:
        candidates = index.candidates(
            query=query, connector_id=connector_id, client_ids=client_ids, limit=limit
        )
        return {
            branch: [
                c
                for c in candidates.get(branch, [])[:limit]
                if c.point_id == point_id(c.publication_id, c.chunk_id)
            ]
            for branch in _BRANCHES
        }
    except Exception:
        # Provider/transport exceptions can contain query or transport details.
        raise RuntimeError("SEARCH_INDEX_UNAVAILABLE") from None


async def retrieve(
    query: str,
    scope: Scope,
    access_token: str,
    limit: int = 10,
    *,
    engine: Engine,
    index: SearchIndex,
    connector: ERPConnector,
) -> list[SearchHit]:
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("SEARCH_LIMIT_INVALID")
    if len(query) > _MAX_QUERY_CHARACTERS or not query.strip():
        raise ValueError("SEARCH_QUERY_INVALID")

    try:
        async with asyncio.timeout(_SEARCH_TIMEOUT_SECONDS):
            identity = await connector.identity(access_token)
            if not isinstance(identity, Identity):
                raise ConnectorError()
            client_ids = await _scope_clients(scope, connector, access_token, identity)
            if not client_ids:
                return []
            for pool_size in (max(50, limit * 4), 200):
                branches = await asyncio.to_thread(
                    _index_candidates, index, query, identity.connector_id, client_ids, pool_size
                )
                candidates = tuple(dict.fromkeys(c for branch in branches.values() for c in branch))
                if not candidates:
                    continue
                statement = _current_chunks(identity.connector_id, client_ids, candidates)
                refs = await asyncio.to_thread(_source_refs, engine, statement)
                allowed = await _authorize(connector, access_token, scope, refs)
                if not allowed:
                    continue
                # Let invalidations scheduled during the grant reach the event loop
                # before dispatching the final PostgreSQL read to a worker thread.
                await asyncio.sleep(0)
                hits = await asyncio.to_thread(_authorized_hits, engine, statement, allowed)
                ranked = _rank(branches, hits, query)
                if len(ranked) >= limit or pool_size == 200:
                    return ranked[:limit]
            return []
    except (TimeoutError, ValidationError):
        raise ConnectorError() from None
    except SQLAlchemyError:
        raise RuntimeError("SEARCH_DATABASE_UNAVAILABLE") from None
