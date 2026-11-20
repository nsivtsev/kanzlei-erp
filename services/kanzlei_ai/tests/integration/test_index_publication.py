"""Durable indexing and activation against an isolated PostgreSQL schema."""

import json
import sys
import time
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, inspect, select, text, update

from kanzlei_ai import jobs as job_service
from kanzlei_ai import outbox, sync
from kanzlei_ai.models import document_artifacts, document_chunks, jobs, sources
from kanzlei_ai.settings import Settings

pytestmark = pytest.mark.integration


def _manifest(revision="a" * 64):
    return {
        "version": 1,
        "source_id": "file:synthetic",
        "source_type": "file",
        "revision": revision,
        "client_ids": ["synthetic-client"],
        "title": "Synthetic",
        "mime_type": "application/pdf",
        "source_modified_at": "2026-01-01",
        "locator": "/synthetic",
        "byte_size": 1,
        "active": True,
    }


def _seed(database, connector="synthetic", *, initialize=True):
    if initialize:
        sync.initialize(database)
    epoch = sync.start_scan(database, connector)
    sync.apply_inventory(
        database,
        connector,
        epoch,
        None,
        {"items": [_manifest()], "next_cursor": None, "changes_cursor": "w"},
    )
    with database.connect() as conn:
        return conn.execute(
            select(jobs.c.id).join(sources).where(sources.c.connector_id == connector)
        ).scalar_one()


def _extract(database, connector="synthetic", *, enabled=True, initialize=True, chunks=1):
    job_id = _seed(database, connector, initialize=initialize)
    token = job_service.claim(database, job_id)
    data = {
        "parser_version": "synthetic-parser",
        "content_sha256": "b" * 64,
        "artifact_key": "synthetic-artifact",
        "page_count": 1,
        "chunks": [
            {"id": f"{job_id}-{n}", "page": 1, "kind": "text", "ordinal": n, "text": "Test"}
            for n in range(chunks)
        ],
    }
    if enabled:
        assert job_service.publish_extraction(database, job_id, token, data, enqueue_index=True)
    else:
        assert job_service.publish_extraction(database, job_id, token, data)
    return job_id


def _rows(database, table):
    with database.connect() as conn:
        return conn.execute(select(table)).mappings().all()


def _index_job(database):
    with database.connect() as conn:
        return conn.execute(select(jobs.c.id).where(jobs.c.stage == "index")).scalar_one()


def _expire(database, job_id):
    with database.begin() as conn:
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(lease_until=text("clock_timestamp() - interval '1 second'"))
        )


class Projection:
    """Capture confirmed external writes and allow changes during the write window."""

    profile = "synthetic-v1"

    def __init__(self, during_stage=lambda: None):
        self.during_stage = during_stage
        self.staged = []

    def stage(self, **values):
        self.staged.append(values)
        self.during_stage()


def test_migration_keeps_seeded_extraction_rows(database):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[2] / "src/kanzlei_ai/migrations")
    )
    with database.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0003_processing_error_codes")
        conn.execute(text("INSERT INTO connectors (id, epoch, phase) VALUES ('test', 0, 'idle')"))
        conn.execute(
            insert(sources).values(
                id=1,
                connector_id="test",
                source_id="file:seed",
                revision="a" * 64,
                manifest=_manifest(),
                generation=1,
                active=True,
                status="queued",
            )
        )
        conn.execute(
            text(
                "INSERT INTO jobs (id, source_id, revision, generation, pipeline_version, status, "
                "attempts) VALUES ('seed', 1, :revision, 1, 'parser', 'queued', 2)"
            ),
            {"revision": "a" * 64},
        )
        conn.execute(text("INSERT INTO outbox (id, job_id) VALUES ('message', 'seed')"))
        command.upgrade(config, "head")
        row = conn.execute(text("SELECT stage, attempts FROM jobs WHERE id = 'seed'")).one()
        assert row == ("extract", 2)
        assert conn.execute(text("SELECT job_id FROM outbox")).scalar_one() == "seed"
        assert conn.execute(text("SELECT count(*) FROM index_publications")).scalar_one() == 0
        # Same job key may be used by a separate stage.
        conn.execute(
            text(
                "INSERT INTO jobs (id, source_id, revision, generation, pipeline_version, status, "
                "stage) VALUES ('other', 1, :revision, 1, 'parser', 'queued', 'index')"
            ),
            {"revision": "a" * 64},
        )


def test_downgrade_preserves_extraction_and_resets_published_source(database):
    from kanzlei_ai.models import index_publications

    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[2] / "src/kanzlei_ai/migrations")
    )
    previous_revision = "0003_processing_error_codes"
    with database.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, previous_revision)
        conn.execute(text("INSERT INTO connectors (id, epoch, phase) VALUES ('test', 0, 'idle')"))
        conn.execute(
            insert(sources),
            [
                {
                    "id": source_id,
                    "connector_id": "test",
                    "source_id": f"file:seed-{source_id}",
                    "revision": "a" * 64,
                    "manifest": _manifest(),
                    "generation": 1,
                    "active": status != "deleted",
                    "status": status,
                    "error_code": error_code,
                }
                for source_id, status, error_code in [
                    (1, "extracted", None),
                    (2, "extracted", None),
                    (3, "failed", "OCR_REQUIRED"),
                    (4, "deleted", None),
                ]
            ],
        )
        artifact = {
            "source_id": 1,
            "revision": "a" * 64,
            "generation": 1,
            "parser_version": "synthetic-parser",
            "content_sha256": "b" * 64,
        }
        conn.execute(
            insert(document_artifacts).values(
                id=1, artifact_key="synthetic-artifact", page_count=1, **artifact
            )
        )
        conn.execute(
            insert(document_chunks).values(
                id="chunk", artifact_id=1, page=1, kind="text", ordinal=0, text="Test", **artifact
            )
        )
        conn.execute(
            text(
                "INSERT INTO jobs (id, source_id, revision, generation, pipeline_version, "
                "status, attempts) VALUES ('extract', 1, :revision, 1, 'parser', 'completed', 2)"
            ),
            {"revision": artifact["revision"]},
        )
        conn.execute(
            text(
                "INSERT INTO outbox (id, job_id, published_at) VALUES ('extract', 'extract', now())"
            )
        )

        def snapshot():
            return {
                table: conn.execute(text(f"SELECT * FROM {table} ORDER BY id")).all()
                for table in ("sources", "jobs", "outbox", "document_artifacts", "document_chunks")
            }

        extracted = snapshot()
        command.upgrade(config, "0004_index_publications")
        conn.execute(update(sources).where(sources.c.id == 1).values(status="ready"))
        conn.execute(
            insert(jobs).values(
                id="index",
                stage="index",
                status="completed",
                source_id=1,
                revision=artifact["revision"],
                generation=1,
                artifact_id=1,
                pipeline_version="synthetic-v1",
            )
        )
        conn.execute(
            insert(outbox.outbox).values(id="index", job_id="index", published_at=text("now()"))
        )
        conn.execute(
            insert(index_publications).values(
                source_id=1,
                revision=artifact["revision"],
                generation=1,
                artifact_id=1,
                publication_id=str(uuid.uuid4()),
                profile="synthetic-v1",
            )
        )
        command.downgrade(config, previous_revision)

        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            previous_revision
        )
        assert not inspect(conn).has_table("index_publications")
        assert conn.execute(select(sources.c.status).where(sources.c.id == 1)).scalar_one() == (
            "extracted"
        )
        assert snapshot() == extracted


def test_extraction_enqueues_index_atomically_and_routes_by_stage(database):
    extraction_id = _extract(database)
    index_id = _index_job(database)
    assert Settings(_env_file=None).search_collection == "kanzlei_pdf_synthetic_v1"
    assert len(_rows(database, document_chunks)) == 1
    assert job_service.claim(database, index_id) is None
    assert job_service.claim(database, extraction_id, stage="index") is None
    assert (len(_rows(database, jobs)), len(_rows(database, outbox.outbox))) == (2, 2)

    extract_messages = []
    index_messages = []
    assert outbox.dispatch(database, extract_messages.append) == 1
    assert extract_messages == [extraction_id]
    assert (
        outbox.dispatch(database, extract_messages.append, publish_index=index_messages.append) == 1
    )
    assert index_messages == [index_id]


def test_crash_after_staging_and_stale_lease_recover_without_reparse(database):
    from kanzlei_ai.models import index_publications
    from kanzlei_ai.search import publication

    _extract(database)
    index_id = _index_job(database)
    first = job_service.claim(database, index_id, stage="index")
    assert first and job_service.claim(database, index_id, stage="index") is None

    def crash():
        raise SystemExit

    projection = Projection(crash)
    with pytest.raises(SystemExit):
        publication.process(database, index_id, first, projection)
    assert projection.staged[0]["publication_id"] == first
    assert _rows(database, index_publications) == []
    _expire(database, index_id)
    assert outbox.recover(database) == 0
    assert (
        next(row for row in _rows(database, jobs) if row["id"] == index_id)["status"] == "running"
    )
    outbox.recover(database, include_index=True)
    second = job_service.claim(database, index_id, stage="index")
    assert second and second != first
    assert not publication.process(database, index_id, first, projection)

    rebuilt = Projection()
    assert publication.process(database, index_id, second, rebuilt)
    assert _rows(database, index_publications)[0]["publication_id"] == second
    assert rebuilt.staged[0]["publication_id"] == second
    assert len(_rows(database, document_artifacts)) == len(_rows(database, document_chunks)) == 1


@pytest.mark.parametrize(
    "operation, expected_status", [("upsert", "queued"), ("delete", "deleted")]
)
def test_source_change_during_confirmed_write_prevents_activation(
    database, operation, expected_status
):
    from kanzlei_ai.models import index_publications
    from kanzlei_ai.search import publication

    _extract(database)
    index_id = _index_job(database)
    token = job_service.claim(database, index_id, stage="index")

    def change():
        item = {
            "source_id": "file:synthetic",
            "client_id": "synthetic-client",
            "operation": operation,
            "manifest": _manifest("b" * 64) if operation == "upsert" else None,
        }
        assert sync.apply_changes(
            database, "synthetic", 1, "w", {"items": [item], "next_cursor": "w2"}
        )

    assert not publication.process(database, index_id, token, Projection(change))
    assert not job_service.fail(database, index_id, token, "INDEX_UNAVAILABLE", stage="index")
    assert not _rows(database, index_publications)
    source = _rows(database, sources)[0]
    assert source["status"] == expected_status
    assert source["error_code"] is None


def test_reindex_without_reparse_preserves_active_snapshot_until_replacement(database):
    from kanzlei_ai.models import index_publications
    from kanzlei_ai.search import publication

    _extract(database, enabled=False)
    _extract(database, "another", enabled=False, initialize=False)
    result = publication.reindex(database, "synthetic", limit=1)
    assert result.queued == 1 and result.next_cursor is not None
    assert publication.reindex(database, "synthetic", after=result.next_cursor).queued == 0
    assert publication.reindex(database, "synthetic").queued == 0
    index_id = _index_job(database)
    first = job_service.claim(database, index_id, stage="index")
    assert publication.reindex(database, "synthetic").queued == 0
    assert publication.process(database, index_id, first, Projection())

    assert publication.reindex(database, "synthetic").queued == 1
    second = job_service.claim(database, index_id, stage="index")
    assert _rows(database, index_publications)[0]["publication_id"] == first

    def fail():
        raise OSError("synthetic stage failure")

    assert not publication.process(database, index_id, second, Projection(fail))
    assert _rows(database, index_publications)[0]["publication_id"] == first
    index = next(row for row in _rows(database, jobs) if row["id"] == index_id)
    assert (index["status"], index["error_code"]) == ("queued", "INDEX_UNAVAILABLE")
    with database.begin() as conn:
        conn.execute(
            update(jobs)
            .where(jobs.c.id == index_id)
            .values(retry_not_before=text("clock_timestamp() - interval '1 second'"))
        )
    third = job_service.claim(database, index_id, stage="index")
    assert publication.process(database, index_id, third, Projection())
    assert _rows(database, index_publications)[0]["publication_id"] == third


def test_index_chunks_come_from_sql_and_activate_only_after_confirmed_write(database):
    from kanzlei_ai.models import index_publications
    from kanzlei_ai.search import publication

    _extract(database, chunks=3)
    index_id = _index_job(database)
    token = job_service.claim(database, index_id, stage="index")

    def verify_staged_only():
        assert not _rows(database, index_publications)

    projection = Projection(verify_staged_only)
    assert publication.process(database, index_id, token, projection)
    assert [len(batch["chunks"]) for batch in projection.staged] == [3]
    assert all(batch["client_ids"] == ["synthetic-client"] for batch in projection.staged)
    assert _rows(database, sources)[0]["status"] == "ready"


def test_exhausted_index_job_can_requeue_without_losing_extract(database):
    from kanzlei_ai.search import publication

    _extract(database)
    index_id = _index_job(database)
    with database.begin() as conn:
        conn.execute(
            update(jobs).where(jobs.c.id == index_id).values(attempts=job_service.MAX_ATTEMPTS - 1)
        )
    token = job_service.claim(database, index_id, stage="index")
    assert job_service.fail(database, index_id, token, "INDEX_UNAVAILABLE", stage="index")
    index = next(row for row in _rows(database, jobs) if row["id"] == index_id)
    assert (index["status"], index["error_code"]) == ("failed", "INDEX_RETRY_EXHAUSTED")
    assert _rows(database, sources)[0]["status"] == "extracted"
    assert publication.reindex(database, "synthetic").queued == 1
    index = next(row for row in _rows(database, jobs) if row["id"] == index_id)
    assert (index["status"], index["attempts"], index["error_code"]) == ("queued", 0, None)


def test_real_worker_rebuilds_deleted_collection_via_reindex_cli(database, monkeypatch, capsys):
    from celery.contrib.testing.worker import start_worker
    from redis import Redis

    from kanzlei_ai import coordinator, worker
    from kanzlei_ai.models import index_publications
    from kanzlei_ai.search.index import SearchIndex

    unique = "task6_index_test_" + uuid.uuid4().hex
    settings = Settings(
        ingestion_enabled=False,
        ingestion_connector_id="synthetic",
        search_index_enabled=True,
        search_collection=unique,
    )
    projection = SearchIndex.from_settings(settings)
    redis_url = settings.redis_url.get_secret_value()
    redis = Redis.from_url(redis_url)
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "engine_from_settings", lambda _: database)
    monkeypatch.setattr(coordinator, "Settings", lambda: settings)
    monkeypatch.setattr(coordinator, "engine_from_settings", lambda _: database)
    monkeypatch.setitem(worker.app.conf, "task_default_queue", unique)
    monkeypatch.setitem(worker.app.conf, "broker_url", redis_url)
    monkeypatch.setitem(
        worker.app.conf, "broker_transport_options", {"global_keyprefix": unique + "_"}
    )
    _extract(database)

    def wait_for_activation(previous=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rows = _rows(database, index_publications)
            if rows and rows[0]["publication_id"] != previous:
                return rows[0]["publication_id"]
            time.sleep(0.1)
        pytest.fail("Timed out waiting for index activation")

    try:
        with start_worker(
            worker.app,
            queues=[unique],
            perform_ping_check=False,
            loglevel="CRITICAL",
            shutdown_timeout=10,
        ):
            coordinator.run_cycle(database, settings)
            first = wait_for_activation()
            assert projection.client.count(unique, exact=True).count == 1
            assert projection.client.delete_collection(unique)
            monkeypatch.setattr(sys, "argv", ["coordinator", "reindex", "--limit", "1"])
            coordinator.main()
            assert json.loads(capsys.readouterr().out)["queued"] == 1
            assert _rows(database, index_publications)[0]["publication_id"] == first
            coordinator.run_cycle(database, settings)
            rebuilt = wait_for_activation(first)
            candidates = projection.candidates(
                query="Test", connector_id="synthetic", client_ids=["synthetic-client"], limit=1
            )
            assert candidates["dense"][0].publication_id == rebuilt
            assert (
                len(_rows(database, document_artifacts))
                == len(_rows(database, document_chunks))
                == 1
            )
    finally:
        if projection.client.collection_exists(unique):
            projection.client.delete_collection(unique)
        projection.client.close()
        for key in redis.scan_iter(match=unique + "*"):
            redis.delete(key)
        redis.close()
