"""Lease and generation fencing against PostgreSQL, without eager workers."""

import pytest
from sqlalchemy import select, text, update

from kanzlei_ai import sync
from kanzlei_ai.models import jobs, sources

pytestmark = pytest.mark.integration


def test_expired_worker_cannot_publish_over_new_generation(database):
    from kanzlei_ai import jobs as job_service

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    manifest = {
        "version": 1,
        "source_id": "file:one",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "One",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database,
        "erp",
        epoch,
        None,
        {"items": [manifest], "next_cursor": None, "changes_cursor": "w"},
    )
    with database.connect() as conn:
        job_id = conn.execute(select(jobs.c.id)).scalar_one()
    token = job_service.claim(database, job_id)
    assert token
    assert not job_service.claim(database, job_id)
    manifest["revision"] = "b" * 64
    sync.apply_changes(
        database,
        "erp",
        epoch,
        "w",
        {
            "items": [
                {
                    "source_id": "file:one",
                    "client_id": "client",
                    "operation": "upsert",
                    "manifest": manifest,
                }
            ],
            "next_cursor": "w2",
        },
    )
    assert not job_service.complete(database, job_id, token)
    with database.connect() as conn:
        assert conn.execute(select(sources.c.status)).scalar_one() == "queued"


def test_extraction_publication_is_fenced_and_keeps_page_chunks(database):
    """A replaced source, stale worker, or duplicate delivery cannot publish chunks."""
    from kanzlei_ai import jobs as job_service
    from kanzlei_ai.models import document_chunks

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    manifest = {
        "version": 1,
        "source_id": "file:extract",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "Synthetic",
        "mime_type": "application/pdf",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database,
        "erp",
        epoch,
        None,
        {"items": [manifest], "next_cursor": None, "changes_cursor": "w"},
    )
    with database.connect() as conn:
        job_id = conn.execute(select(jobs.c.id)).scalar_one()
    token = job_service.claim(database, job_id)
    publication = {
        "parser_version": "docling-native-v1",
        "content_sha256": "b" * 64,
        "artifact_key": "private/hash",
        "page_count": 2,
        "chunks": [
            {"id": "c" * 64, "page": 2, "kind": "table", "ordinal": 0, "text": "A | B\n1 | 2"}
        ],
    }
    assert job_service.publish_extraction(database, job_id, token, publication)
    assert not job_service.publish_extraction(database, job_id, token, publication)
    with database.connect() as conn:
        chunk = conn.execute(select(document_chunks)).mappings().one()
        assert chunk["page"] == 2 and chunk["kind"] == "table"


def test_outbox_republishes_unclaimed_job_and_expired_lease(database):
    from kanzlei_ai import outbox

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    manifest = {
        "version": 1,
        "source_id": "file:one",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "One",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database,
        "erp",
        epoch,
        None,
        {"items": [manifest], "next_cursor": None, "changes_cursor": "w"},
    )
    sent = []
    assert outbox.dispatch(database, sent.append) == 1
    assert len(sent) == 1
    with database.begin() as conn:
        conn.execute(
            update(jobs).values(
                updated_at=__import__("sqlalchemy").text("clock_timestamp() - interval '5 minutes'")
            )
        )
    assert outbox.recover(database) == 1
    assert outbox.dispatch(database, sent.append) == 1
    assert sent[0] == sent[1]


def test_worker_kill_restart_recovers_job(database, tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time
    import uuid

    from celery import Celery
    from redis import Redis
    from sqlalchemy import text

    from kanzlei_ai import outbox
    from kanzlei_ai.settings import Settings

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    manifest = {
        "version": 1,
        "source_id": "file:kill",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "Kill test",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database,
        "erp",
        epoch,
        None,
        {"items": [manifest], "next_cursor": None, "changes_cursor": "w"},
    )
    with database.connect() as conn:
        job_id = conn.execute(select(jobs.c.id)).scalar_one()
        schema = conn.execute(text("select current_schema()")).scalar_one()
    prefix = "ingestion_test_" + uuid.uuid4().hex + "_"
    queue = prefix + "queue"
    marker = prefix + "marker"
    redis_url = Settings().redis_url.get_secret_value()
    redis = Redis.from_url(redis_url)
    broker = Celery("test", broker=redis_url)
    broker.conf.broker_transport_options = {"global_keyprefix": prefix, "visibility_timeout": 300}
    (tmp_path / "synthetic_worker.py").write_text(
        """
import os
import time
from redis import Redis
import kanzlei_ai.worker as worker
app = worker.app
app.conf.broker_transport_options = {
    "global_keyprefix": os.environ["TEST_PREFIX"], "visibility_timeout": 300,
}
original_complete = worker.complete
def barrier(engine, job_id, token):
    count = Redis.from_url(os.environ["TEST_REDIS_URL"]).incr(os.environ["TEST_MARKER"])
    if count == 1:
        time.sleep(120)
    return original_complete(engine, job_id, token)
worker.complete = barrier
"""
    )
    env = os.environ.copy()
    settings = Settings()
    env.update(
        {
            "TEST_REDIS_URL": redis_url,
            "TEST_PREFIX": prefix,
            "TEST_MARKER": marker,
            "AI_DATABASE_URL": settings.database_url.get_secret_value()
            + "?options=-csearch_path%3D"
            + schema,
            "AI_DATABASE_PASSWORD": settings.database_password.get_secret_value(),
            "PYTHONPATH": str(tmp_path) + os.pathsep + str(os.getcwd() + "/src"),
        }
    )
    process = None

    def start_worker():
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "synthetic_worker:app",
                "worker",
                "-Q",
                queue,
                "--pool=solo",
                "--loglevel=WARNING",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def wait_for(predicate):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        pytest.fail("Timed out waiting for synthetic worker")

    try:
        process = start_worker()
        outbox.dispatch(
            database,
            lambda ident: broker.send_task("kanzlei_ai.stage_metadata", args=[ident], queue=queue),
        )
        wait_for(lambda: redis.get(marker) == b"1")
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        process = None
        with database.begin() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(lease_until=text("clock_timestamp() - interval '1 second'"))
            )
        assert outbox.recover(database) == 1
        process = start_worker()
        outbox.dispatch(
            database,
            lambda ident: broker.send_task("kanzlei_ai.stage_metadata", args=[ident], queue=queue),
        )

        def completed():
            with database.connect() as conn:
                return (
                    conn.execute(select(jobs.c.status).where(jobs.c.id == job_id)).scalar_one()
                    == "completed"
                )

        wait_for(completed)
        assert redis.get(marker) == b"2"
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for key in redis.scan_iter(match=prefix + "*"):
            redis.delete(key)
        redis.close()


def test_failed_publish_is_backed_off(database):
    from kanzlei_ai import outbox

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:one",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "One",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database, "erp", epoch, None, {"items": [item], "next_cursor": None, "changes_cursor": "w"}
    )

    def fail(_):
        raise OSError("broker unavailable")

    with pytest.raises(OSError):
        outbox.dispatch(database, fail)
    with database.connect() as conn:
        from kanzlei_ai.models import outbox as records

        row = conn.execute(select(records)).mappings().one()
        assert row["attempts"] == 1 and row["lease_until"] is not None


def test_job_attempts_are_durable_and_bounded(database):
    from sqlalchemy import text

    from kanzlei_ai import jobs as job_service

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:one",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "One",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database, "erp", epoch, None, {"items": [item], "next_cursor": None, "changes_cursor": "w"}
    )
    with database.connect() as conn:
        job_id = conn.execute(select(jobs.c.id)).scalar_one()
    for _ in range(job_service.MAX_ATTEMPTS):
        assert job_service.claim(database, job_id)
        with database.begin() as conn:
            conn.execute(
                update(jobs).values(lease_until=text("clock_timestamp() - interval '1 second'"))
            )
    assert job_service.claim(database, job_id) is None
    with database.connect() as conn:
        row = conn.execute(select(jobs)).mappings().one()
        assert row["status"] == "failed" and row["attempts"] == job_service.MAX_ATTEMPTS


@pytest.mark.parametrize("locked_table", ["sources", "jobs"])
def test_completion_waits_for_transient_row_lock(database, locked_table):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text

    from kanzlei_ai import jobs as job_service

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:lock",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "One",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database, "erp", epoch, None, {"items": [item], "next_cursor": None, "changes_cursor": "w"}
    )
    with database.connect() as conn:
        job_id = conn.execute(select(jobs.c.id)).scalar_one()
    token = job_service.claim(database, job_id)
    entered = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.begin() as blocker:
            blocker.execute(text(f"SELECT id FROM {locked_table} FOR UPDATE"))

            def publish():
                entered.set()
                return job_service.complete(database, job_id, token)

            future = executor.submit(publish)
            assert entered.wait(3)
            # Keep the lock long enough for the competing call to reach PostgreSQL.
            import time

            time.sleep(0.3)
        assert future.result(timeout=5) is True
    with database.connect() as conn:
        job = conn.execute(select(jobs)).mappings().one()
        assert job["status"] == "completed" and job["attempts"] == 1


def test_recovery_reuses_unpublished_outbox(database):
    from sqlalchemy import text

    from kanzlei_ai import outbox
    from kanzlei_ai.models import outbox as outbox_table

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:outage",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "One",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database, "erp", epoch, None, {"items": [item], "next_cursor": None, "changes_cursor": "w"}
    )
    with database.begin() as conn:
        conn.execute(
            update(jobs).values(updated_at=text("clock_timestamp() - interval '5 minutes'"))
        )
        conn.execute(
            update(outbox_table).values(
                attempts=3, lease_until=text("clock_timestamp() + interval '5 minutes'")
            )
        )
    outbox.recover(database)
    with database.connect() as conn:
        rows = conn.execute(select(outbox_table)).mappings().all()
        assert len(rows) == 1 and rows[0]["attempts"] == 3


def test_expired_token_cannot_publish_same_generation(database):
    from sqlalchemy import text

    from kanzlei_ai import jobs as job_service

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:lease",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "One",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database, "erp", epoch, None, {"items": [item], "next_cursor": None, "changes_cursor": "w"}
    )
    with database.connect() as conn:
        job_id = conn.execute(select(jobs.c.id)).scalar_one()
    old_token = job_service.claim(database, job_id)
    with database.begin() as conn:
        conn.execute(
            update(jobs).values(lease_until=text("clock_timestamp() - interval '1 second'"))
        )
    new_token = job_service.claim(database, job_id)
    assert new_token and new_token != old_token
    assert not job_service.complete(database, job_id, old_token)
    assert job_service.complete(database, job_id, new_token)


def test_expired_failure_cannot_requeue_current_job(database):
    from sqlalchemy import text

    from kanzlei_ai import jobs as job_service

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:fail",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "One",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database, "erp", epoch, None, {"items": [item], "next_cursor": None, "changes_cursor": "w"}
    )
    with database.connect() as conn:
        job_id = conn.execute(select(jobs.c.id)).scalar_one()
    token = job_service.claim(database, job_id)
    with database.begin() as conn:
        conn.execute(
            update(jobs).values(lease_until=text("clock_timestamp() - interval '1 second'"))
        )
    job_service.fail(database, job_id, token)
    with database.connect() as conn:
        assert conn.execute(select(jobs.c.status)).scalar_one() == "running"


def test_failure_publication_waits_for_transient_lock(database):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text

    from kanzlei_ai import jobs as job_service

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:failwait",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "One",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database, "erp", epoch, None, {"items": [item], "next_cursor": None, "changes_cursor": "w"}
    )
    with database.connect() as conn:
        job_id = conn.execute(select(jobs.c.id)).scalar_one()
    token = job_service.claim(database, job_id)
    entered = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.begin() as blocker:
            blocker.execute(text("select id from jobs for update"))

            def publish():
                entered.set()
                job_service.fail(database, job_id, token)

            future = executor.submit(publish)
            assert entered.wait(3)
            time.sleep(0.3)
        future.result(timeout=5)
    with database.connect() as conn:
        job = conn.execute(select(jobs)).mappings().one()
        assert job["status"] == "queued" and job["attempts"] == 1


def test_completion_retries_postgres_lock_timeout_with_same_lease(database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event, text

    from kanzlei_ai import jobs as job_service

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:timeout",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["client"],
        "title": "One",
        "mime_type": "text/plain",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }
    sync.apply_inventory(
        database, "erp", epoch, None, {"items": [item], "next_cursor": None, "changes_cursor": "w"}
    )
    with database.connect() as conn:
        job_id = conn.execute(select(jobs.c.id)).scalar_one()
    token = job_service.claim(database, job_id)
    timed_out = Event()

    def record_timeout(context):
        if getattr(context.original_exception, "sqlstate", None) == "55P03":
            timed_out.set()

    event.listen(database, "handle_error", record_timeout)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with database.begin() as blocker:
                blocker.execute(text("select id from sources for update"))
                future = executor.submit(job_service.complete, database, job_id, token)
                assert timed_out.wait(6), "expected actual PostgreSQL lock timeout"
            assert future.result(timeout=6) is True
        with database.connect() as conn:
            job = conn.execute(select(jobs)).mappings().one()
            assert job["status"] == "completed" and job["attempts"] == 1
    finally:
        event.remove(database, "handle_error", record_timeout)


def _pdf_manifest(source_id="file:error", revision="a" * 64):
    return {
        "version": 1,
        "source_id": source_id,
        "source_type": "file",
        "revision": revision,
        "client_ids": ["client"],
        "title": "Synthetic",
        "mime_type": "application/pdf",
        "source_modified_at": "2026-01-01",
        "locator": "/file",
        "byte_size": 1,
        "active": True,
    }


def _queued_pdf_job(database, source_id="file:error"):
    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    sync.apply_inventory(
        database,
        "erp",
        epoch,
        None,
        {"items": [_pdf_manifest(source_id)], "next_cursor": None, "changes_cursor": "w"},
    )
    with database.connect() as conn:
        return conn.execute(
            select(jobs.c.id).join(sources).where(sources.c.source_id == source_id)
        ).scalar_one()


def test_error_code_migration_upgrades_existing_ingestion_schema(database):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    def upgrade(revision):
        with database.begin() as conn:
            config = Config()
            config.set_main_option(
                "script_location",
                str(Path(__file__).parents[2] / "src" / "kanzlei_ai" / "migrations"),
            )
            config.attributes["connection"] = conn
            command.upgrade(config, revision)

    upgrade("0001_sources_jobs")
    with database.connect() as conn:
        assert "error_code" not in {column["name"] for column in inspect(conn).get_columns("jobs")}

    upgrade("head")
    with database.connect() as conn:
        for table in ("sources", "jobs"):
            columns = {column["name"]: column for column in inspect(conn).get_columns(table)}
            assert columns["error_code"]["nullable"]
            assert columns["error_code"]["type"].length == 64


@pytest.mark.parametrize(
    "error_code", ["OCR_REQUIRED", "UNSUPPORTED_MEDIA", "TABLE_LIMIT_EXCEEDED"]
)
def test_terminal_failure_persists_safe_code_for_current_source(database, error_code):
    from kanzlei_ai import jobs as job_service

    job_id = _queued_pdf_job(database, f"file:{error_code.lower()}")
    token = job_service.claim(database, job_id)

    assert job_service.fail_terminal(database, job_id, token, error_code)

    with database.connect() as conn:
        job = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
        source = conn.execute(select(sources)).mappings().one()
    assert job["status"] == source["status"] == "failed"
    assert job["error_code"] == source["error_code"] == error_code


def test_transient_exhaustion_persists_retry_exhausted_and_successful_retry_clears_code(database):
    from kanzlei_ai import jobs as job_service

    job_id = _queued_pdf_job(database, "file:transient")
    token = job_service.claim(database, job_id)
    assert job_service.fail(database, job_id, token, "CONNECTOR_UNAVAILABLE")
    with database.connect() as conn:
        job = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
        assert job["status"] == "queued"
        assert job["error_code"] == "CONNECTOR_UNAVAILABLE"

    with database.begin() as conn:
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(retry_not_before=text("clock_timestamp() - interval '1 second'"))
        )
    token = job_service.claim(database, job_id)
    assert job_service.complete(database, job_id, token)
    with database.connect() as conn:
        job = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
        source = conn.execute(select(sources)).mappings().one()
    assert job["status"] == "completed" and job["error_code"] is None
    assert source["status"] == "awaiting_extraction" and source["error_code"] is None

    job_id = _queued_pdf_job(database, "file:exhausted")
    for _ in range(job_service.MAX_ATTEMPTS):
        token = job_service.claim(database, job_id)
        assert token
        assert job_service.fail(database, job_id, token, "CONNECTOR_UNAVAILABLE")
        with database.begin() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(retry_not_before=text("clock_timestamp() - interval '1 second'"))
            )
    with database.connect() as conn:
        job = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
        source = (
            conn.execute(select(sources).where(sources.c.source_id == "file:exhausted"))
            .mappings()
            .one()
        )
    assert job["status"] == source["status"] == "failed"
    assert job["error_code"] == source["error_code"] == "RETRY_EXHAUSTED"


def test_stale_terminal_failure_cannot_set_current_source_error(database):
    from kanzlei_ai import jobs as job_service

    job_id = _queued_pdf_job(database, "file:stale-error")
    token = job_service.claim(database, job_id)
    epoch = 1
    changed = _pdf_manifest("file:stale-error", "b" * 64)
    sync.apply_changes(
        database,
        "erp",
        epoch,
        "w",
        {
            "items": [
                {
                    "source_id": "file:stale-error",
                    "client_id": "client",
                    "operation": "upsert",
                    "manifest": changed,
                }
            ],
            "next_cursor": "w2",
        },
    )

    assert not job_service.fail_terminal(database, job_id, token, "OCR_REQUIRED")
    with database.connect() as conn:
        source = conn.execute(select(sources)).mappings().one()
    assert source["status"] == "queued"
    assert source["error_code"] is None
