"""Independent operator CLI and coordinator; synchronization defaults off."""

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select

from kanzlei_ai import outbox, sync
from kanzlei_ai.connectors.frappe import FrappeConnector
from kanzlei_ai.db import engine_from_settings, migrate
from kanzlei_ai.models import connectors
from kanzlei_ai.search.publication import MAX_REINDEX_LIMIT, reindex
from kanzlei_ai.settings import Settings
from kanzlei_ai.worker import app

log = logging.getLogger(__name__)


def sync_once(engine, settings):
    if not settings.ingestion_enabled:
        raise RuntimeError("Ingestion is disabled")
    if not settings.erp_base_url or not settings.ingestion_key:
        raise RuntimeError("Ingestion origin and independent key are required")
    if (
        settings.erp_service_key
        and settings.ingestion_key.get_secret_value() == settings.erp_service_key.get_secret_value()
    ):
        raise ValueError("Background ingestion requires an independent credential")
    with engine.connect() as conn:
        state = (
            conn.execute(
                select(connectors).where(connectors.c.id == settings.ingestion_connector_id)
            )
            .mappings()
            .first()
        )
    if state is None or state["phase"] not in ("inventory", "changes"):
        raise RuntimeError("Start a scan before synchronization")
    if (
        state["phase"] == "changes"
        and state["scan_completed_at"] is not None
        and (datetime.now(UTC) - state["scan_completed_at"]).total_seconds()
        >= settings.ingestion_reconcile_seconds
    ):
        sync.start_scan(engine, state["id"], expected_epoch=state["epoch"])
        return True
    client = FrappeConnector(
        str(settings.erp_base_url),
        settings.ingestion_key,
        allow_insecure_http=settings.erp_allow_insecure_http,
        site_name=settings.erp_site_name,
        timeout_seconds=settings.erp_timeout_seconds,
    )
    if state["phase"] == "inventory":
        cursor = state["inventory_cursor"]
        page = asyncio.run(client.inventory(cursor)).model_dump(mode="json")
        return sync.apply_inventory(
            engine, state["id"], state["epoch"], cursor, page, settings.ingestion_pipeline_version
        )
    cursor = state["changes_cursor"]
    page = asyncio.run(client.changes(cursor)).model_dump(mode="json")
    return sync.apply_changes(
        engine, state["id"], state["epoch"], cursor, page, settings.ingestion_pipeline_version
    )


def _dispatch(engine, settings):
    def publish_extract(job_id):
        app.send_task("kanzlei_ai.stage_metadata", args=[job_id])

    def publish_index(job_id):
        app.send_task("kanzlei_ai.index_document", args=[job_id])

    if settings.search_index_enabled:
        return outbox.dispatch(engine, publish_extract, publish_index=publish_index)
    return outbox.dispatch(engine, publish_extract)


def _recover(engine, settings):
    if settings.search_index_enabled:
        return outbox.recover(engine, include_index=True)
    return outbox.recover(engine)


def run_cycle(engine, settings):
    _recover(engine, settings)
    _dispatch(engine, settings)
    if settings.ingestion_enabled:
        sync_once(engine, settings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "scan", "sync-once", "recover", "dispatch", "run"):
        commands.add_parser(command)
    reindex_parser = commands.add_parser("reindex")
    reindex_parser.add_argument("--connector")
    reindex_parser.add_argument("--cursor", type=int, default=0)
    reindex_parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "reindex":
        if not settings.search_index_enabled:
            parser.error("Reindex requires AI_SEARCH_INDEX_ENABLED")
        if args.cursor < 0 or not 1 <= args.limit <= MAX_REINDEX_LIMIT:
            reindex_parser.error(
                f"Cursor must be nonnegative and limit must be 1–{MAX_REINDEX_LIMIT}"
            )
    engine = engine_from_settings(settings)
    try:
        if args.command == "migrate":
            migrate(engine)
        elif args.command == "scan":
            sync.start_scan(engine, settings.ingestion_connector_id)
        elif args.command == "sync-once":
            sync_once(engine, settings)
        elif args.command == "recover":
            _recover(engine, settings)
        elif args.command == "dispatch":
            _dispatch(engine, settings)
        elif args.command == "reindex":
            result = reindex(
                engine,
                args.connector or settings.ingestion_connector_id,
                after=args.cursor,
                limit=args.limit,
            )
            print(json.dumps(asdict(result)))
        else:
            while True:
                try:
                    run_cycle(engine, settings)
                except Exception:
                    log.error("Coordinator cycle failed; retrying")
                time.sleep(settings.ingestion_poll_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.error("Coordinator command failed; check configuration and service availability")
        sys.exit(1)
