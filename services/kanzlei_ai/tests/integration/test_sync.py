"""Durability contract against a private schema in the service database."""

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def test_page_and_cursor_commit_together_and_reject_stale_page(database):
    from kanzlei_ai import sync

    assert hasattr(sync, "initialize")
    sync.initialize(database)
    sync.start_scan(database, "erp")
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
    page = {"version": 1, "items": [manifest], "next_cursor": None, "changes_cursor": "watermark"}
    assert sync.apply_inventory(database, "erp", 1, None, page)
    assert not sync.apply_inventory(database, "erp", 1, None, page)
    with database.connect() as conn:
        assert conn.execute(text("select count(*) from sources")).scalar_one() == 1
        assert conn.execute(text("select count(*) from jobs")).scalar_one() == 1
        assert conn.execute(text("select count(*) from outbox")).scalar_one() == 1
        assert (
            conn.execute(text("select changes_cursor from connectors")).scalar_one() == "watermark"
        )


def test_page_receipt_and_items_are_committed(database):
    from kanzlei_ai import sync

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
        assert conn.execute(text("select count(*) from page_receipts")).scalar_one() == 1
        assert conn.execute(text("select count(*) from inbox_items")).scalar_one() == 1


def test_delete_one_client_fences_old_job(database):
    from sqlalchemy import select

    from kanzlei_ai import jobs, sync
    from kanzlei_ai.models import jobs as job_table
    from kanzlei_ai.models import sources

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:one",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["a", "b"],
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
        job_id = conn.execute(select(job_table.c.id)).scalar_one()
    token = jobs.claim(database, job_id)
    sync.apply_changes(
        database,
        "erp",
        epoch,
        "w",
        {
            "items": [
                {"source_id": "file:one", "client_id": "a", "operation": "delete", "manifest": None}
            ],
            "next_cursor": "w2",
        },
    )
    assert not jobs.complete(database, job_id, token)
    with database.connect() as conn:
        row = conn.execute(select(sources)).mappings().one()
        assert row["active"] and row["manifest"]["client_ids"] == ["b"]


def test_pipeline_change_creates_new_generation(database):
    from kanzlei_ai import sync

    sync.initialize(database)
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
    epoch = sync.start_scan(database, "erp")
    sync.apply_inventory(
        database,
        "erp",
        epoch,
        None,
        {"items": [item], "next_cursor": None, "changes_cursor": "w"},
        "v1",
    )
    epoch = sync.start_scan(database, "erp")
    sync.apply_inventory(
        database,
        "erp",
        epoch,
        None,
        {"items": [item], "next_cursor": None, "changes_cursor": "w"},
        "v2",
    )
    with database.connect() as conn:
        assert conn.execute(text("select count(*) from jobs")).scalar_one() == 2
        assert conn.execute(text("select generation from sources")).scalar_one() == 2


def test_stale_pages_cannot_regress_and_scan_cleanup_waits_for_last_page(database):
    from kanzlei_ai import sync

    sync.initialize(database)

    def manifest(revision):
        return {
            "version": 1,
            "source_id": "file:one",
            "source_type": "file",
            "revision": revision * 64,
            "client_ids": ["client"],
            "title": "One",
            "mime_type": "text/plain",
            "source_modified_at": "2026-01-01",
            "locator": "/file",
            "byte_size": 1,
            "active": True,
        }

    epoch = sync.start_scan(database, "erp")
    sync.apply_inventory(
        database,
        "erp",
        epoch,
        None,
        {"items": [manifest("a")], "next_cursor": None, "changes_cursor": "w"},
    )
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
                    "manifest": manifest("b"),
                }
            ],
            "next_cursor": "w2",
        },
    )
    assert not sync.apply_changes(
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
                    "manifest": manifest("a"),
                }
            ],
            "next_cursor": "w2",
        },
    )
    assert not sync.apply_inventory(
        database,
        "erp",
        epoch,
        None,
        {"items": [manifest("a")], "next_cursor": None, "changes_cursor": "w"},
    )
    epoch = sync.start_scan(database, "erp")
    sync.apply_inventory(
        database, "erp", epoch, None, {"items": [], "next_cursor": "next", "changes_cursor": "new"}
    )
    with database.connect() as conn:
        assert conn.execute(text("select active from sources")).scalar_one()
        assert conn.execute(text("select revision from sources")).scalar_one() == "b" * 64
    sync.apply_inventory(
        database, "erp", epoch, "next", {"items": [], "next_cursor": None, "changes_cursor": "new"}
    )
    with database.connect() as conn:
        assert not conn.execute(text("select active from sources")).scalar_one()


def test_invalid_page_rolls_back_receipt_and_cursor(database):
    from kanzlei_ai import sync

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    with pytest.raises(ValueError):
        sync.apply_inventory(
            database,
            "erp",
            epoch,
            None,
            {"items": [{"source_id": "invalid"}], "next_cursor": "next", "changes_cursor": "w"},
        )
    with database.connect() as conn:
        assert conn.execute(text("select count(*) from page_receipts")).scalar_one() == 0
        assert conn.execute(text("select inventory_cursor from connectors")).scalar_one() is None


def test_reconciliation_removes_only_unseen_client_relation(database):
    from sqlalchemy import select

    from kanzlei_ai import sync
    from kanzlei_ai.models import sources

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:one",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["a", "b"],
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
    epoch = sync.start_scan(database, "erp")
    item["client_ids"] = ["b"]
    sync.apply_inventory(
        database, "erp", epoch, None, {"items": [item], "next_cursor": None, "changes_cursor": "w2"}
    )
    with database.connect() as conn:
        row = conn.execute(select(sources)).mappings().one()
        assert row["manifest"]["client_ids"] == ["b"] and row["active"]


def test_scheduled_scan_does_not_reset_newer_scan(database):
    from kanzlei_ai import sync

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    assert sync.start_scan(database, "erp", expected_epoch=epoch) == epoch + 1
    assert sync.start_scan(database, "erp", expected_epoch=epoch) is None


def test_deleted_then_restored_same_revision_creates_new_job(database):
    from kanzlei_ai import sync

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    item = {
        "version": 1,
        "source_id": "file:restore",
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
    sync.apply_changes(
        database,
        "erp",
        epoch,
        "w",
        {
            "items": [
                {
                    "source_id": item["source_id"],
                    "client_id": "client",
                    "operation": "delete",
                    "manifest": None,
                }
            ],
            "next_cursor": "w2",
        },
    )
    sync.apply_changes(
        database,
        "erp",
        epoch,
        "w2",
        {
            "items": [
                {
                    "source_id": item["source_id"],
                    "client_id": "client",
                    "operation": "upsert",
                    "manifest": item,
                }
            ],
            "next_cursor": "w3",
        },
    )
    with database.connect() as conn:
        assert conn.execute(text("select count(*) from jobs")).scalar_one() == 2
        assert conn.execute(text("select generation from sources")).scalar_one() == 3


def test_concurrent_duplicate_pages_only_one_wins(database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from kanzlei_ai import sync

    sync.initialize(database)
    epoch = sync.start_scan(database, "erp")
    barrier = Barrier(2)

    def apply():
        barrier.wait(timeout=5)
        return sync.apply_inventory(
            database, "erp", epoch, None, {"items": [], "next_cursor": None, "changes_cursor": "w"}
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(apply) for _ in range(2)]
        assert sorted(f.result(timeout=5) for f in results) == [False, True]
    with database.connect() as conn:
        assert conn.execute(text("select count(*) from page_receipts")).scalar_one() == 1


def test_connection_uses_password_embedded_in_database_url(database):
    from pydantic import SecretStr
    from sqlalchemy.engine import make_url

    from kanzlei_ai.db import engine_from_settings
    from kanzlei_ai.settings import Settings

    configured = Settings()
    assert configured.database_password is not None
    with database.connect() as connection:
        schema = connection.execute(text("select current_schema()")).scalar_one()
    url = (
        make_url(configured.database_url.get_secret_value())
        .set(password=configured.database_password.get_secret_value())
        .update_query_dict({"options": f"-csearch_path={schema}"})
    )
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(url.render_as_string(hide_password=False)),
        database_password=None,
    )
    engine = engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("select current_schema()")).scalar_one() == schema
            assert connection.execute(text("select 1")).scalar_one() == 1
    finally:
        engine.dispose()
