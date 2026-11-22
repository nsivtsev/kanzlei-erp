import asyncio
import hashlib
import os
import uuid
from contextlib import closing
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import (
    AuthorizationResult,
    ClientPage,
    ClientRecord,
    Decision,
    Identity,
    Scope,
)
from kanzlei_ai.db import migrate
from kanzlei_ai.models import connectors, document_artifacts, document_chunks, relations, sources

pytestmark = pytest.mark.integration
_REVISION = "a" * 64
_CLIENT_A = Scope(kind="client", client_id="A")
_GENERAL = Scope(kind="general")


class RankedIndex:
    def __init__(self, **branches):
        self.branches = branches
        self.calls = []

    def candidates(self, *, query, connector_id, client_ids, limit, lexical_only=False):
        self.calls.append((connector_id, tuple(client_ids), limit))
        return {branch: candidates[:limit] for branch, candidates in self.branches.items()}


class DelegatedConnector:
    def __init__(self, *, denied=(), on_authorize=None, mode=None):
        self.denied = set(denied)
        self.on_authorize = on_authorize
        self.mode = mode
        self.batches = []

    async def identity(self, access_token):
        assert access_token == "synthetic"
        return Identity(subject="person", connector_id="origin", expires_at=1, general_access=True)

    async def check_scope(self, access_token, scope):
        assert access_token == "synthetic" and (
            scope.kind == "general" or scope.client_id in {"A", "B"}
        )

    async def list_clients(self, access_token, limit=50, cursor=None):
        assert access_token == "synthetic"
        return ClientPage(
            items=[ClientRecord(id="A", label="A"), ClientRecord(id="B", label="B")],
            next_cursor=None,
        )

    async def authorize_sources(self, access_token, scope, sources):
        assert access_token == "synthetic" and 1 <= len(sources) <= 50
        self.batches.append(list(sources))
        if self.on_authorize is not None:
            self.on_authorize()
            self.on_authorize = None
        if self.mode == "outage":
            raise ConnectorError()
        decisions = [
            Decision(**ref.model_dump(), allowed=(ref.source_id, ref.client_id) not in self.denied)
            for ref in sources
        ]
        if self.mode == "missing":
            decisions.pop()
        if self.mode == "reordered":
            decisions.reverse()
        if self.mode == "invalid":
            return AuthorizationResult.model_construct(decisions=None)
        return AuthorizationResult(decisions=decisions)


@pytest.fixture
def search_db(database):
    from kanzlei_ai.search.retrieve import retrieve

    migrate(database)
    with database.begin() as connection:
        connection.execute(
            connectors.insert(),
            [{"id": "origin", "phase": "steady"}, {"id": "other", "phase": "steady"}],
        )

    def run(index, connector, *, query="synthetic subject", scope=_CLIENT_A, limit=10):
        return asyncio.run(
            retrieve(
                query, scope, "synthetic", limit, engine=database, index=index, connector=connector
            )
        )

    return database, run


def add_source(
    engine,
    name,
    clients,
    text="same synthetic content",
    *,
    active=True,
    published=True,
    connector_id="origin",
    profile=None,
):
    from kanzlei_ai.models import index_publications
    from kanzlei_ai.search.index import INDEX_PROFILE, Candidate, point_id

    chunk_id = hashlib.sha256(name.encode()).hexdigest()
    publication_id = str(uuid.uuid4())
    document_fields = {"revision": _REVISION, "generation": 2}
    artifact_fields = {"parser_version": "synthetic", "content_sha256": "b" * 64}
    with engine.begin() as connection:
        source_pk = connection.execute(
            sources.insert()
            .values(
                connector_id=connector_id,
                source_id=f"file:{name}",
                manifest={"mime_type": "application/pdf"},
                active=active,
                status="ready",
                **document_fields,
            )
            .returning(sources.c.id)
        ).scalar_one()
        connection.execute(
            relations.insert(),
            [{"source_id": source_pk, "client_id": client, "seen_epoch": 1} for client in clients],
        )
        artifact_id = connection.execute(
            document_artifacts.insert()
            .values(
                source_id=source_pk,
                artifact_key="synthetic",
                page_count=2,
                **document_fields,
                **artifact_fields,
            )
            .returning(document_artifacts.c.id)
        ).scalar_one()
        connection.execute(
            document_chunks.insert().values(
                id=chunk_id,
                source_id=source_pk,
                artifact_id=artifact_id,
                page=2,
                kind="text",
                ordinal=0,
                text=text,
                **document_fields,
                **artifact_fields,
            )
        )
        if published:
            connection.execute(
                index_publications.insert().values(
                    source_id=source_pk,
                    artifact_id=artifact_id,
                    publication_id=publication_id,
                    profile=profile or INDEX_PROFILE,
                    **document_fields,
                )
            )
    return Candidate(
        point_id=point_id(publication_id, chunk_id),
        publication_id=publication_id,
        source_pk=source_pk,
        chunk_id=chunk_id,
        **document_fields,
    )


def test_dense_retrieval_is_scope_bound_with_shared_and_duplicate_sources(search_db):
    engine, run = search_db
    a = add_source(engine, "duplicate-a", ["A"])
    b = add_source(engine, "duplicate-b", ["B"])
    shared = add_source(engine, "shared", ["A", "B"])
    foreign = add_source(engine, "foreign", ["A"], connector_id="other")
    index = RankedIndex(dense=[foreign, b, a, shared])
    erp = DelegatedConnector(denied={("file:duplicate-a", "A")})

    hits_a = run(index, erp, limit=2)
    assert [(hit.source.source_id, hit.source.client_id) for hit in hits_a] == [
        ("file:shared", "A")
    ]
    hits_b = run(index, erp, scope=Scope(kind="client", client_id="B"), limit=2)
    assert [(hit.source.source_id, hit.source.client_id) for hit in hits_b] == [
        ("file:duplicate-b", "B"),
        ("file:shared", "B"),
    ]
    assert [(hit.generation, hit.page, hit.chunk_id, hit.text) for hit in hits_b] == [
        (2, 2, b.chunk_id, "same synthetic content"),
        (2, 2, shared.chunk_id, "same synthetic content"),
    ]
    assert {(connector, clients) for connector, clients, _ in index.calls} == {
        ("origin", ("A",)),
        ("origin", ("B",)),
    }
    assert all(ref.source_id != "file:foreign" for batch in erp.batches for ref in batch)


def test_general_search_selects_a_deterministic_authorized_membership(search_db):
    engine, run = search_db
    shared = add_source(engine, "shared", ["B", "A"])
    dense_only = add_source(engine, "dense-only", ["B"])
    index = RankedIndex(exact=[shared], sparse=[shared], dense=[dense_only])
    hits = run(index, DelegatedConnector(), scope=_GENERAL, limit=2)
    assert [(hit.chunk_id, hit.source.client_id) for hit in hits] == [(shared.chunk_id, "A")]
    erp = DelegatedConnector(denied={("file:shared", "A")})
    assert run(index, erp, scope=_GENERAL, limit=1)[0].source.client_id == "B"
    assert [ref.client_id for ref in erp.batches[0]] == ["A", "B"]


def test_general_search_uses_exact_and_sparse_queries_without_outbound_embeddings(search_db):
    from qdrant_client import QdrantClient

    from kanzlei_ai.search.index import MISTRAL_INDEX_PROFILE, SearchIndex

    class ExternalEmbedding:
        def embed(self, texts):
            pytest.fail("A general question reached the external embedding provider")

    class GeneralConnector(DelegatedConnector):
        async def check_processing_policy(self, operation, scope, sources, *, access_token):
            assert operation == "embed"
            pytest.fail("General search attempted to authorize question embedding")

    engine, run = search_db
    documents = [
        ("exact", ["A", "B"], ["A", "B"], "RE-2026-0042"),
        ("sparse", ["A"], ["A"], "synthetic"),
        ("outside", ["C"], ["A", "C"], "RE-2026-0042 synthetic"),
    ]
    url = os.environ.get("TASK6_QDRANT_TEST_URL")
    collection = "task10_lexical_" + uuid.uuid4().hex
    with closing(QdrantClient(url=url, timeout=2) if url else QdrantClient(":memory:")) as client:
        index = SearchIndex(
            client=client,
            collection=collection,
            profile=MISTRAL_INDEX_PROFILE,
            embedding=ExternalEmbedding(),
        )
        try:
            for name, clients, indexed_clients, text in documents:
                source = add_source(engine, name, clients, text, profile=MISTRAL_INDEX_PROFILE)
                index.stage_vectors(
                    publication_id=source.publication_id,
                    connector_id="origin",
                    source_pk=source.source_pk,
                    revision=source.revision,
                    generation=source.generation,
                    client_ids=indexed_clients,
                    chunks=[{"id": source.chunk_id, "text": text}],
                    vectors=[[1.0] + [0.0] * 63],
                )
            erp = GeneralConnector(denied={("file:exact", "A")})
            hits = run(index, erp, scope=_GENERAL, query="synthetic RE-2026-0042", limit=2)
        finally:
            client.delete_collection(collection)
    assert [(hit.source.source_id, hit.source.client_id) for hit in hits] == [
        ("file:exact", "B"),
        ("file:sparse", "A"),
    ]
    assert [(ref.source_id, ref.client_id) for ref in erp.batches[0]] == [
        ("file:exact", "A"),
        ("file:exact", "B"),
        ("file:sparse", "A"),
    ]


def test_general_search_cannot_fall_back_to_denied_alias_after_relation_revocation(search_db):
    engine, run = search_db
    shared = add_source(engine, "shared", ["A", "B"])

    def revoke():
        with engine.begin() as connection:
            connection.execute(
                relations.delete().where(
                    relations.c.source_id == shared.source_pk, relations.c.client_id == "B"
                )
            )

    erp = DelegatedConnector(denied={("file:shared", "A")}, on_authorize=revoke)
    assert run(RankedIndex(sparse=[shared]), erp, scope=_GENERAL, limit=1) == []
    assert [ref.client_id for ref in erp.batches[0]] == ["A", "B"]


def test_general_context_expands_with_the_chosen_client_relation(search_db):
    from kanzlei_ai.chat.context import Session, retrieve_context

    engine, _ = search_db
    shared = add_source(engine, "shared", ["A", "B"], "Belegnummer RE-2026-0042")
    with engine.begin() as connection:
        original = (
            connection.execute(
                select(document_chunks).where(document_chunks.c.id == shared.chunk_id)
            )
            .mappings()
            .one()
        )
        connection.execute(
            document_chunks.insert().values(
                **{**original, "id": "table", "kind": "table", "ordinal": 1, "page": 1}
            )
        )

    index = RankedIndex(exact=[shared], sparse=[shared])
    erp = DelegatedConnector(denied={("file:shared", "A")})
    session = Session(erp, "synthetic", "origin", "person", _GENERAL)

    evidence = asyncio.run(retrieve_context(engine, index, session, "RE-2026-0042"))

    assert [(item.dependency.chunk_id, item.dependency.source.client_id) for item in evidence] == [
        (shared.chunk_id, "B"),
        ("table", "B"),
    ]
    assert [ref.client_id for ref in erp.batches[-1]] == ["B"]


def test_general_context_locks_only_the_chosen_client_relation(search_db):
    from kanzlei_ai.chat.context import Dependency, Session, current_evidence
    from kanzlei_ai.connectors.contracts import SourceRef

    engine, _ = search_db
    shared = add_source(engine, "shared", ["A", "B"])
    chosen = Dependency(
        SourceRef(source_id="file:shared", revision=shared.revision, client_id="B"),
        shared.generation,
        shared.chunk_id,
    )
    session = Session(DelegatedConnector(), "synthetic", "origin", "person", _GENERAL)
    delete = relations.delete().where(relations.c.source_id == shared.source_pk)
    with engine.begin() as reader:
        assert set(current_evidence(reader, session, [chosen], lock=True)) == {chosen}
        with pytest.raises(OperationalError) as locked, engine.begin() as writer:
            writer.exec_driver_sql("SET LOCAL lock_timeout = '100ms'")
            writer.execute(delete.where(relations.c.client_id == "B"))
        assert locked.value.orig.sqlstate == "55P03"
        with engine.begin() as writer:
            writer.exec_driver_sql("SET LOCAL lock_timeout = '100ms'")
            writer.execute(delete.where(relations.c.client_id == "A"))
    with engine.begin() as connection:
        connection.execute(delete.where(relations.c.client_id == "B"))
        connection.execute(
            relations.insert().values(source_id=shared.source_pk, client_id="A", seen_epoch=1)
        )
        assert current_evidence(connection, session, [chosen], lock=True) == {}


def test_exact_branch_checks_whole_identifiers_in_authoritative_sql_text(search_db):
    engine, run = search_db
    match = add_source(engine, "match", ["A"], "Belegnummer RE-2026-0042")
    partial = add_source(engine, "partial", ["A"], "Belegnummer RE-2026-00421")
    dense = add_source(engine, "dense", ["A"], "Fiktive Buchhaltung")
    index = RankedIndex(exact=[partial, match], dense=[dense])
    hits = run(index, DelegatedConnector(), query="re-2026-0042", limit=2)
    assert [hit.chunk_id for hit in hits] == [match.chunk_id, dense.chunk_id]


def test_rrf_uses_authorized_rankings_and_stable_ties(search_db):
    engine, run = search_db
    a = add_source(engine, "a", ["A"])
    b = add_source(engine, "b", ["A"])
    c = add_source(engine, "c", ["A"])
    denied = add_source(engine, "denied", ["A"])
    index = RankedIndex(dense=[denied, b, a, c], sparse=[b, c, a])
    erp = DelegatedConnector(denied={("file:denied", "A")})
    assert [hit.source.source_id for hit in run(index, erp, limit=3)] == [
        "file:b",
        "file:a",
        "file:c",
    ]


def test_only_matching_active_publications_and_exact_chunks_are_visible(search_db):
    from kanzlei_ai.search.index import point_id

    engine, run = search_db
    current = add_source(engine, "current", ["A"])
    unpublished = add_source(engine, "unpublished", ["A"], published=False)
    trashed = add_source(engine, "trashed", ["A"], active=False)
    foreign_profile = add_source(engine, "foreign-profile", ["A"], profile="other-v1")
    old = replace(current, generation=1)
    wrong_id = replace(current, point_id=str(uuid.uuid4()))
    wrong_chunk = replace(
        current,
        chunk_id=unpublished.chunk_id,
        point_id=point_id(current.publication_id, unpublished.chunk_id),
    )
    wrong_publication_id = str(uuid.uuid4())
    wrong_publication = replace(
        current,
        publication_id=wrong_publication_id,
        point_id=point_id(wrong_publication_id, current.chunk_id),
    )
    index = RankedIndex(
        dense=[
            old,
            unpublished,
            trashed,
            foreign_profile,
            wrong_id,
            wrong_chunk,
            wrong_publication,
            current,
        ]
    )
    erp = DelegatedConnector()
    assert [hit.chunk_id for hit in run(index, erp, limit=2)] == [current.chunk_id]
    assert {ref.source_id for batch in erp.batches for ref in batch} == {"file:current"}


@pytest.mark.parametrize("mode", ["outage", "missing", "reordered", "invalid"])
@pytest.mark.parametrize("scope", [_CLIENT_A, _GENERAL], ids=["client", "general"])
def test_bad_erp_reply_fails_without_partial_hits(search_db, mode, scope):
    engine, run = search_db
    a = add_source(engine, "a", ["A"])
    b = add_source(engine, "b", ["A"])
    with pytest.raises(ConnectorError):
        run(RankedIndex(sparse=[a, b]), DelegatedConnector(mode=mode), scope=scope, limit=2)


@pytest.mark.parametrize("change", ["trash", "generation", "revision", "relation", "publication"])
def test_change_scheduled_during_erp_grant_is_visible_to_final_sql_read(search_db, change):
    from kanzlei_ai.models import index_publications

    engine, run = search_db
    source = add_source(engine, "changing", ["A"])

    def invalidate():
        with engine.begin() as connection:
            if change == "relation":
                connection.execute(
                    relations.delete().where(relations.c.source_id == source.source_pk)
                )
            elif change == "publication":
                connection.execute(
                    index_publications.delete().where(
                        index_publications.c.source_id == source.source_pk
                    )
                )
            else:
                values = {
                    "trash": {"active": False},
                    "generation": {"generation": 3},
                    "revision": {"revision": "c" * 64},
                }
                connection.execute(
                    sources.update()
                    .where(sources.c.id == source.source_pk)
                    .values(**values[change])
                )

    def schedule():
        asyncio.get_running_loop().call_soon(invalidate)

    assert (
        run(RankedIndex(dense=[source]), DelegatedConnector(on_authorize=schedule), limit=1) == []
    )


def test_expansion_is_bounded_and_authorization_is_batched_by_exact_ref(search_db):
    engine, run = search_db
    stale = add_source(engine, "stale", ["A"], published=False)
    valid = [add_source(engine, f"candidate-{i}", ["A"]) for i in range(51)]
    index = RankedIndex(dense=[stale] * 80 + valid, sparse=[valid[0]])
    erp = DelegatedConnector()

    assert len(run(index, erp, limit=20)) == 20
    assert [limit for _, _, limit in index.calls] == [80, 200]
    assert [len(batch) for batch in erp.batches[-2:]] == [50, 1]
    assert len(set(ref for batch in erp.batches[-2:] for ref in batch)) == 51


def test_stale_payload_cannot_promote_a_current_dense_candidate_to_exact(search_db):
    engine, run = search_db
    current = add_source(engine, "current", ["A"], "Belegnummer RE-2026-0042")
    best = add_source(engine, "best", ["A"], "Ein fiktiver Beleg")
    malformed = replace(current, generation=1)
    index = RankedIndex(exact=[malformed], dense=[best, current])

    hits = run(index, DelegatedConnector(), query="re-2026-0042", limit=2)
    assert [hit.chunk_id for hit in hits] == [best.chunk_id, current.chunk_id]


def test_dense_lookup_with_real_adapter_has_no_exact_or_lexical_match(search_db):
    from qdrant_client import QdrantClient

    from kanzlei_ai.search.index import SearchIndex

    class FixtureEmbedding:
        def embed(self, texts):
            return [[1.0] + [0.0] * 63 for _ in texts]

    engine, run = search_db
    doc = "Überweisung"
    current = add_source(engine, "dense-current", ["A"], doc)
    with closing(QdrantClient(":memory:")) as client:
        index = SearchIndex(
            client=client, collection="task6_retrieval", embedding=FixtureEmbedding()
        )
        index.stage(
            publication_id=current.publication_id,
            connector_id="origin",
            source_pk=current.source_pk,
            revision=current.revision,
            generation=current.generation,
            client_ids=["A"],
            chunks=[{"id": current.chunk_id, "text": doc}],
        )
        branches = index.candidates(
            query="Zahlung", connector_id="origin", client_ids=["A"], limit=1
        )
        assert not branches["exact"] and not branches["sparse"]
        hits = run(index, DelegatedConnector(), query="Zahlung", limit=1)
        assert [hit.chunk_id for hit in hits] == [current.chunk_id]
