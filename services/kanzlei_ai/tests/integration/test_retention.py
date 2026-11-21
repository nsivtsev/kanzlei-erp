"""Exercise retention against isolated SQL, private object and vector fixtures."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient, models
from sqlalchemy import delete, event, insert, select, text, update

from kanzlei_ai.db import migrate
from kanzlei_ai.documents.artifacts import S3ArtifactStore
from kanzlei_ai.models import (
    chat_conversations,
    chat_turns,
    connectors,
    document_artifacts,
    document_chunks,
    inbox_items,
    index_publications,
    jobs,
    outbox,
    page_receipts,
    sources,
)
from kanzlei_ai.search.index import INDEX_PROFILE, SearchIndex
from kanzlei_ai.settings import Settings

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=100)


class Objects:
    def __init__(self):
        self.objects = {}
        self.delete_attempts = []
        self.fail = False
        self.versioning = {}

    def put_object(self, *, Key, Body, Metadata, **kwargs):
        self.objects[Key] = {
            "Key": Key,
            "Metadata": Metadata,
            "ContentLength": len(Body),
            "ETag": '"fixture"',
            "LastModified": OLD,
        }

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, *, Prefix, **kwargs):
        yield {"Contents": [r for k, r in self.objects.items() if k.startswith(Prefix)]}

    def get_bucket_versioning(self, **kwargs):
        return self.versioning

    def head_object(self, *, Key, **kwargs):
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return self.objects[Key]

    def delete_object(self, *, Key, **kwargs):
        self.delete_attempts.append(Key)
        if self.fail:
            raise OSError("private details must not enter the journal")
        self.objects.pop(Key, None)


@pytest.fixture
def maintenance(database):
    from kanzlei_ai.retention import Retention

    migrate(database)
    settings = Settings(
        ingestion_connector_id="synthetic",
        artifact_s3_endpoint="http://127.0.0.1:9000",
        artifact_s3_bucket="private-derived",
        artifact_s3_prefix="derived/ai",
    )
    qdrant = QdrantClient(":memory:")
    objects = Objects()
    store = S3ArtifactStore(objects, bucket="private-derived", prefix="derived/ai", max_bytes=200)
    retention = Retention(settings, database, store=store, qdrant=qdrant)
    index = SearchIndex(client=qdrant, collection=settings.search_collection)
    with database.begin() as conn:
        conn.execute(
            insert(connectors).values(id="synthetic", epoch=2, phase="changes", changes_cursor="c2")
        )
        conn.execute(
            insert(sources).values(
                id=1,
                connector_id="synthetic",
                source_id="file:synthetic",
                revision="current",
                manifest={},
                generation=2,
                active=True,
                status="ready",
            )
        )
    yield retention, index, store
    qdrant.close()


def _artifact(conn, artifact_id, key, *, current=False):
    fields = dict(
        source_id=1,
        revision="current" if current else "old",
        generation=2 if current else 1,
        parser_version="fixture",
        content_sha256="a" * 64,
    )
    conn.execute(
        insert(document_artifacts).values(id=artifact_id, **fields, artifact_key=key, page_count=1)
    )
    conn.execute(
        insert(document_chunks).values(
            id=f"chunk-{artifact_id}",
            artifact_id=artifact_id,
            **fields,
            page=1,
            kind="text",
            ordinal=0,
            text="Synthetic",
        )
    )


def _stage(index, *, revision="old", connector="synthetic"):
    publication = str(uuid4())
    index.stage(
        publication_id=publication,
        connector_id=connector,
        source_pk=1,
        revision=revision,
        generation=2 if revision == "current" else 1,
        client_ids=["synthetic-client"],
        chunks=[{"id": "chunk-1", "text": "Synthetic projection"}],
    )
    return publication


def _age(database, days):
    from kanzlei_ai.models import retention_candidates

    with database.begin() as conn:
        conn.execute(
            update(retention_candidates).values(first_observed_at=NOW - timedelta(days=days))
        )


def _ids(database, table, column="id"):
    with database.connect() as conn:
        return set(conn.execute(select(table.c[column])).scalars())


def test_mark_delays_stale_and_orphan_deletion_and_plan_is_read_only(database, maintenance):
    retention, index, store = maintenance
    stale_key = store.put("stale", b"stale")
    orphan_key = store.put("orphan", b"orphan")
    live_key = store.put("active", b"live")
    old_projection = _stage(index)
    foreign_projection = _stage(index, connector="other")
    with database.begin() as conn:
        _artifact(conn, 1, stale_key)
        _artifact(conn, 2, live_key, current=True)
        conn.execute(
            insert(jobs).values(
                id="old-index",
                source_id=1,
                revision="old",
                generation=1,
                stage="index",
                status="completed",
                pipeline_version=INDEX_PROFILE,
                artifact_id=1,
                updated_at=NOW - timedelta(days=45),
            )
        )
        conn.execute(
            insert(index_publications).values(
                source_id=1,
                publication_id=old_projection,
                artifact_id=1,
                revision="old",
                generation=1,
                profile=INDEX_PROFILE,
            )
        )
    from kanzlei_ai.retention import summary

    assert retention.plan()["last_mark_at"] is None

    def deny_delete(conn, cursor, statement, parameters, context, executemany):
        assert not statement.lstrip().startswith("DELETE")

    event.listen(database, "before_cursor_execute", deny_delete)
    try:
        mark = retention.mark()
    finally:
        event.remove(database, "before_cursor_execute", deny_delete)
    assert mark["last_mark_at"]
    assert sum(mark["ready"].values()) == 0
    assert retention.plan() == mark
    assert summary(database) == mark
    assert retention.apply(confirm_offline=True)["completed"] == 0
    assert set(store.client.objects) == {orphan_key, stale_key, live_key}
    assert index._point_count(old_projection) == 1
    _age(database, 31)
    assert retention.mark()["ready"]["points"] == 1
    result = retention.apply(confirm_offline=True, limit=1)
    assert result["completed"] == 1
    assert index._point_count(old_projection) == 0
    assert index._point_count(foreign_projection) == 1
    while retention.apply(confirm_offline=True)["completed"]:
        pass
    assert set(store.client.objects) == {live_key}
    assert _ids(database, document_artifacts) == {2}
    assert _ids(database, document_chunks) == {"chunk-2"}
    assert _ids(database, index_publications, "publication_id") == set()
    assert _ids(database, jobs) == {"old-index"}
    assert _ids(database, jobs, "artifact_id") == {None}


def test_apply_rechecks_shared_objects_and_requeueable_builds(database, maintenance):
    retention, index, store = maintenance
    shared = store.put("shared", b"shared")
    build = _stage(index)
    with database.begin() as conn:
        _artifact(conn, 1, shared)
    retention.mark()
    _age(database, 31)
    with database.begin() as conn:
        _artifact(conn, 2, shared, current=True)
        conn.execute(
            insert(jobs).values(
                id="queued",
                source_id=1,
                revision="old",
                generation=1,
                stage="index",
                status="queued",
                pipeline_version=INDEX_PROFILE,
                index_build_id=build,
                updated_at=OLD,
            )
        )
    assert retention.apply(confirm_offline=True)["completed"] == 1
    assert _ids(database, document_artifacts) == {2}
    assert _ids(database, jobs) == {"queued"}
    assert index._point_count(build) == 1
    assert shared in store.client.objects


def test_only_old_confirmed_sql_bookkeeping_is_pruned(database, maintenance):
    retention, index, store = maintenance
    live_key = store.put("current", b"current")
    live_projection = _stage(index, revision="current")
    with database.begin() as conn:
        _artifact(conn, 2, live_key, current=True)
        conn.execute(
            insert(index_publications).values(
                source_id=1,
                publication_id=live_projection,
                artifact_id=2,
                revision="current",
                generation=2,
                profile=INDEX_PROFILE,
            )
        )
        for job, generation, status, changed in [
            ("stale", 1, "superseded", OLD),
            ("current", 2, "completed", OLD),
            ("unconfirmed", 3, "superseded", OLD),
            ("recent", 4, "failed", NOW),
        ]:
            conn.execute(
                insert(jobs).values(
                    id=job,
                    source_id=1,
                    revision="current",
                    generation=generation,
                    stage="extract",
                    status=status,
                    pipeline_version="fixture",
                    updated_at=changed,
                )
            )
            conn.execute(
                insert(outbox).values(
                    id=job,
                    job_id=job,
                    published_at=None if job == "unconfirmed" else changed,
                )
            )
        for receipt, epoch in [("old", 1), ("current", 2)]:
            conn.execute(
                insert(page_receipts).values(
                    id=receipt,
                    connector_id="synthetic",
                    epoch=epoch,
                    phase="changes",
                    created_at=OLD,
                    next_cursor="c2",
                )
            )
            conn.execute(insert(inbox_items).values(receipt_id=receipt, ordinal=0, payload={}))
        conn.execute(
            insert(chat_conversations).values(
                id="chat",
                connector_id="synthetic",
                subject="owner",
                client_id="client",
                created_at=OLD,
            )
        )
        conn.execute(
            insert(chat_turns).values(
                id="turn",
                conversation_id="chat",
                created_at=OLD,
                status="no_evidence",
                question="Synthetic",
                dependencies=[],
                citations=[],
            )
        )

    def apply_with_chat_trap():
        # Inspect statements as well as survivors to ensure operator retention
        # never reads or writes the history tables.
        def deny_chat(conn, cursor, statement, parameters, context, executemany):
            assert "chat_" not in statement.lower()

        event.listen(database, "before_cursor_execute", deny_chat)
        try:
            retention.mark()
            while retention.apply(confirm_offline=True)["completed"]:
                pass
        finally:
            event.remove(database, "before_cursor_execute", deny_chat)

    apply_with_chat_trap()
    assert _ids(database, jobs) == {"current", "unconfirmed", "recent"}
    assert _ids(database, outbox) == {"unconfirmed", "recent"}
    assert _ids(database, page_receipts) == {"current"}
    assert _ids(database, inbox_items, "receipt_id") == {"current"}
    assert _ids(database, connectors, "changes_cursor") == {"c2"}
    assert _ids(database, chat_conversations) == {"chat"}
    assert _ids(database, chat_turns) == {"turn"}
    assert _ids(database, document_artifacts) == {2}
    assert _ids(database, index_publications, "publication_id") == {live_projection}
    assert index._point_count(live_projection) == 1
    assert live_key in store.client.objects


@pytest.mark.parametrize("leased_table", [jobs, outbox])
def test_apply_requires_attestation_and_refuses_live_leases(database, maintenance, leased_table):
    from kanzlei_ai.retention import RetentionError

    retention, _, _ = maintenance
    with pytest.raises(RetentionError, match="--confirm-offline"):
        retention.apply(confirm_offline=False)
    with database.begin() as conn:
        conn.execute(
            insert(jobs).values(
                id="live",
                source_id=1,
                revision="current",
                generation=2,
                status="running",
                pipeline_version="fixture",
            )
        )
        conn.execute(insert(outbox).values(id="live", job_id="live"))
        conn.execute(
            update(leased_table).values(lease_token="live", lease_until=NOW + timedelta(hours=1))
        )
    with pytest.raises(RetentionError, match="live leases"):
        retention.apply(confirm_offline=True)


def test_exclusive_lock_prevents_overlapping_maintenance(database, maintenance):
    from kanzlei_ai.retention import RETENTION_LOCK_ID, RetentionError

    retention, _, _ = maintenance
    with database.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": RETENTION_LOCK_ID})
        retention.plan()
        with pytest.raises(RetentionError, match="maintenance"):
            retention.apply(confirm_offline=True)
        with pytest.raises(RetentionError, match="maintenance"):
            retention.mark()


def test_failed_and_missing_remote_deletes_resume_safely(database, maintenance, monkeypatch):
    retention, index, store = maintenance
    stale_key = store.put("stale", b"stale")
    publication = _stage(index)
    with database.begin() as conn:
        _artifact(conn, 1, stale_key)
    retention.mark()
    _age(database, 30)
    store.client.fail = True
    real_delete = index.client.delete

    def unconfirmed(*args, **kwargs):
        return models.UpdateResult(operation_id=1, status=models.UpdateStatus.ACKNOWLEDGED)

    monkeypatch.setattr(index.client, "delete", unconfirmed)
    result = retention.apply(confirm_offline=True)
    assert result["failed"] == 2
    assert retention.plan()["retry_pending"] == 2
    assert "private details" not in str(result)
    assert _ids(database, document_artifacts) == {1}
    assert index._point_count(publication) == 1

    # Both stores lost their responses after a previous successful operation.
    del store.client.objects[stale_key]
    index.client.delete_collection(index.collection)
    monkeypatch.setattr(index.client, "delete", real_delete)
    assert retention.mark()["retry_pending"] == 2
    assert retention.apply(confirm_offline=True)["completed"] == 2
    assert retention.plan()["retry_pending"] == 0
    assert _ids(database, document_artifacts) == set()


def test_s3_preserves_replaced_and_versioned_objects(database, maintenance):
    retention, _, store = maintenance
    key = store.put("orphan", b"synthetic")
    retention.mark()
    _age(database, 31)
    store.client.objects[key]["LastModified"] = NOW
    assert retention.apply(confirm_offline=True)["completed"] == 0
    assert key in store.client.objects
    retention.mark()
    _age(database, 31)
    store.client.versioning = {"Status": "Enabled"}
    assert retention.apply(confirm_offline=True)["failed"] == 1
    assert key in store.client.objects


def test_orphan_cleanup_rechecks_that_no_artifact_references_key(database, maintenance):
    retention, _, store = maintenance
    key = store.put("orphan", b"synthetic")
    retention.mark()
    _age(database, 31)
    with database.begin() as conn:
        _artifact(conn, 2, key, current=True)
    assert retention.apply(confirm_offline=True)["completed"] == 0
    assert key in store.client.objects
    with database.begin() as conn:
        conn.execute(delete(document_chunks))
        conn.execute(delete(document_artifacts))
    assert retention.mark()["ready"]["objects"] == 0


def test_inventory_skips_objects_without_private_provenance(database, maintenance):
    retention, _, store = maintenance
    owned = store.put("owned", b"synthetic fixture")
    unknown = store.put("unknown", b"synthetic unowned fixture")
    store.client.objects[unknown]["Metadata"] = {}
    outside = f"{store.prefix}/original.pdf"
    store.client.objects[outside] = {**store.client.objects[owned], "Key": outside}
    retention.mark()
    _age(database, 31)
    assert retention.apply(confirm_offline=True)["completed"] == 1
    assert set(store.client.objects) == {unknown, outside}
