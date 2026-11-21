"""Operator aggregates against an isolated PostgreSQL schema."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select, text, update

from kanzlei_ai import coordinator, db, outbox, sync
from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import ChangePage, InventoryPage
from kanzlei_ai.models import connectors, document_artifacts, index_publications, jobs, sources
from kanzlei_ai.models import outbox as messages
from kanzlei_ai.search.publication import INDEX_PROFILE, MISTRAL_INDEX_PROFILE
from kanzlei_ai.settings import Settings

pytestmark = pytest.mark.integration
OLD = datetime(2025, 1, 1, tzinfo=UTC)


def _source(conn, name, *, connector="configured-private-id", active=True):
    return conn.scalar(
        insert(sources)
        .values(
            connector_id=connector,
            source_id=name,
            active=active,
            generation=2,
            revision="a" * 64,
            manifest={"title": "PRIVATE_TITLE", "locator": "/private/path"},
            status="extracted" if active else "deleted",
        )
        .returning(sources.c.id)
    )


def _artifact(conn, source, *, generation=2, revision="a" * 64):
    return conn.scalar(
        insert(document_artifacts)
        .values(
            source_id=source,
            revision=revision,
            generation=generation,
            parser_version="parser",
            content_sha256="b" * 64,
            artifact_key="private-object-key",
            page_count=1,
        )
        .returning(document_artifacts.c.id)
    )


def test_ops_migration_backfills_existing_jobs(database):
    config = Config()
    config.set_main_option("script_location", str(Path(db.__file__).parent / "migrations"))
    with database.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0009_chat_general_scope")
        conn.execute(insert(connectors).values(id="configured-private-id", epoch=0, phase="idle"))
        source = _source(conn, "legacy")
        conn.execute(
            text(
                "INSERT INTO jobs (id, source_id, revision, pipeline_version, generation, status, "
                "updated_at) VALUES ('legacy', :source, 'old', 'old', 1, 'queued', :updated_at)"
            ),
            {"source": source, "updated_at": OLD},
        )
    db.migrate(database)
    with database.connect() as conn:
        assert conn.scalar(select(jobs.c.created_at)) == OLD
        assert conn.scalar(select(connectors.c.last_successful_poll_at)) is None


def test_sync_records_empty_polls_but_rejects_stale_epochs_and_cursors(database, monkeypatch):
    class EmptyFeed:
        async def inventory(self, cursor):
            assert cursor is None
            return InventoryPage(items=[], next_cursor=None, changes_cursor="secret-cursor")

        async def changes(self, cursor):
            return ChangePage(items=[], next_cursor=cursor)

    settings = Settings(
        ingestion_enabled=True, erp_base_url="https://erp.example.invalid", ingestion_key="key"
    )
    monkeypatch.setattr(coordinator, "FrappeConnector", lambda *args, **kwargs: EmptyFeed())
    connector = settings.ingestion_connector_id
    sync.initialize(database)
    epoch = sync.start_scan(database, connector)

    def last_poll():
        with database.connect() as conn:
            return conn.scalar(select(connectors.c.last_successful_poll_at))

    assert last_poll() is None
    assert coordinator.sync_once(database, settings)
    inventory_poll = last_poll()
    assert inventory_poll > OLD
    with database.begin() as conn:
        conn.execute(update(connectors).values(last_successful_poll_at=OLD))
    assert not sync.apply_changes(
        database, connector, epoch, "stale", {"items": [], "next_cursor": "stale"}
    )
    assert last_poll() == OLD
    assert coordinator.sync_once(database, settings)
    changes_poll = last_poll()
    assert changes_poll > inventory_poll

    class UnavailableFeed(EmptyFeed):
        async def changes(self, cursor):
            raise ConnectorError()

    monkeypatch.setattr(coordinator, "FrappeConnector", lambda *args, **kwargs: UnavailableFeed())
    with pytest.raises(ConnectorError):
        coordinator.sync_once(database, settings)
    assert last_poll() == changes_poll

    sync.start_scan(database, connector)
    assert not sync.apply_inventory(
        database,
        connector,
        epoch,
        None,
        {"items": [], "next_cursor": None, "changes_cursor": "stale"},
    )
    assert last_poll() == changes_poll


def test_ops_status_cli_is_connector_scoped_bounded_and_read_only(database, monkeypatch, capsys):
    configured = "configured-private-id"
    other = "different-private-id"
    settings = Settings(ingestion_connector_id=configured)
    monkeypatch.setattr(coordinator, "Settings", lambda: settings)
    monkeypatch.setattr(coordinator, "engine_from_settings", lambda _: database)
    monkeypatch.setattr(sys, "argv", ["coordinator", "ops-status", "--json"])
    sync.initialize(database)
    for connector in (configured, other):
        sync.start_scan(database, connector)
    with database.begin() as conn:
        conn.exec_driver_sql("SET TIME ZONE 'Europe/Berlin'")
        conn.execute(
            update(connectors)
            .where(connectors.c.id == configured)
            .values(last_successful_poll_at=OLD, inventory_cursor="private-opaque-cursor")
        )
        indexed = _source(conn, "private-document-indexed")
        wrong_profile = _source(conn, "private-document-profile")
        old_generation = _source(conn, "private-document-generation")
        old_revision = _source(conn, "private-document-revision")
        inactive = _source(conn, "private-document-inactive", active=False)
        foreign = _source(conn, "private-document-foreign", connector=other)
        conn.execute(
            update(sources)
            .where(sources.c.id == old_generation)
            .values(status="failed", error_code="OCR_REQUIRED")
        )
        conn.execute(
            update(sources)
            .where(sources.c.id == old_revision)
            .values(status="PRIVATE_STATUS", error_code="PRIVATE_SOURCE_ERROR")
        )
        for source, profile, generation, revision in (
            (indexed, INDEX_PROFILE, 2, "a" * 64),
            (wrong_profile, MISTRAL_INDEX_PROFILE, 2, "a" * 64),
            (old_generation, INDEX_PROFILE, 1, "a" * 64),
            (old_revision, INDEX_PROFILE, 2, "c" * 64),
            (inactive, INDEX_PROFILE, 2, "a" * 64),
            (foreign, INDEX_PROFILE, 2, "a" * 64),
        ):
            artifact = _artifact(conn, source, generation=generation, revision=revision)
            conn.execute(
                insert(index_publications).values(
                    source_id=source,
                    profile=profile,
                    generation=generation,
                    revision=revision,
                    artifact_id=artifact,
                    publication_id=f"private-publication-{source}",
                )
            )
        for label, source, stage, profile, ocr, status, error, timing in (
            (
                "extract",
                indexed,
                "extract",
                "extract-v1",
                "paddle",
                "queued",
                "OCR_REQUIRED",
                {"retry_not_before": text("clock_timestamp() + interval '1 day'")},
            ),
            (
                "index",
                indexed,
                "index",
                INDEX_PROFILE,
                "native",
                "running",
                None,
                {"lease_until": OLD},
            ),
            (
                "index-other",
                wrong_profile,
                "index",
                "PRIVATE_PIPELINE",
                "native",
                "failed",
                "PRIVATE_ERROR_CODE",
                {},
            ),
            ("other", foreign, "extract", "extract-v1", "native", "queued", "INVALID_DOCX", {}),
        ):
            conn.execute(
                insert(jobs).values(
                    id=f"private-job-{label}",
                    source_id=source,
                    stage=stage,
                    pipeline_version=profile,
                    ocr_profile=ocr,
                    status=status,
                    error_code=error,
                    generation=2,
                    revision="a" * 64,
                    created_at=OLD,
                    **timing,
                )
            )
        for label, job, timing in (
            ("ready", "extract", {}),
            (
                "leased",
                "index",
                {
                    "lease_token": "private-lease-token",
                    "lease_until": text("clock_timestamp() + interval '1 day'"),
                },
            ),
            (
                "backoff",
                "index-other",
                {"attempts": 2, "lease_until": text("clock_timestamp() + interval '1 day'")},
            ),
            ("expired", "index", {"lease_token": "expired-private-token", "lease_until": OLD}),
            ("published", "extract", {"published_at": OLD}),
            ("foreign", "other", {}),
        ):
            conn.execute(insert(messages).values(id=label, job_id=f"private-job-{job}", **timing))

    coordinator.main()
    output = capsys.readouterr().out
    status = json.loads(output)
    for sensitive in ("private", "PRIVATE", configured, other):
        assert sensitive not in output
    assert status["feed"]["last_successful_poll_at"] == "2025-01-01T00:00:00Z"
    assert status["feed"]["poll_lag_seconds"] == int(
        (datetime.fromisoformat(status["as_of"]) - OLD).total_seconds()
    )
    assert status["jobs"]["extract"]["by_status"] == {"queued": 1}
    assert status["jobs"]["extract"]["queued_by_profile"] == {"paddle": 1}
    assert status["jobs"]["extract"]["running_by_profile"] == {}
    assert status["jobs"]["extract"]["retry_delayed"] == 1
    assert status["jobs"]["extract"]["error_codes"] == {"OCR_REQUIRED": 1}
    assert status["jobs"]["index"]["by_status"] == {"running": 1, "failed": 1}
    assert status["jobs"]["index"]["running_by_profile"] == {"configured": 1}
    assert status["jobs"]["index"]["expired_leases"] == 1
    assert status["jobs"]["index"]["error_codes"] == {"other": 1}
    assert (
        abs(
            status["jobs"]["index"]["oldest_unfinished_seconds"]
            - status["feed"]["poll_lag_seconds"]
        )
        <= 1
    )
    assert status["coverage"] == {
        "active_sources": 4,
        "current_artifacts": 2,
        "current_index_publications": 1,
        "without_current_index_publication": 3,
        "by_status": {"extracted": 2, "failed": 1, "other": 1},
        "error_codes": {"OCR_REQUIRED": 1, "other": 1},
    }
    assert status["outbox"] == {
        "pending": 4,
        "ready": 1,
        "expired_leases": 1,
        "leased": 1,
        "retry_delayed": 1,
        "with_publish_failures": 1,
    }
    assert capsys.readouterr().err == ""


def test_recovery_logs_only_aggregate_requeues(database, caplog):
    sync.initialize(database)
    sync.start_scan(database, "configured-private-id")
    with database.begin() as conn:
        source = _source(conn, "private-recovery-source")
        conn.execute(
            insert(jobs).values(
                id="private-recovery-job",
                source_id=source,
                pipeline_version="extract-v1",
                status="queued",
                generation=2,
                revision="a" * 64,
                updated_at=OLD,
            )
        )
    with caplog.at_level("INFO", logger="kanzlei_ai.outbox"):
        assert outbox.recover(database, limit=1) == 1
        assert outbox.recover(database, limit=1) == 0
    assert caplog.messages == ["Outbox recovery requeued 1 jobs"]
