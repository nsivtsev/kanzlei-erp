"""Atomic application of ordered, opaque ERP feed pages."""

import uuid

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kanzlei_ai.connectors.contracts import SourceManifest
from kanzlei_ai.db import migrate
from kanzlei_ai.models import (
    connectors,
    inbox_items,
    jobs,
    outbox,
    page_receipts,
    relations,
    sources,
)

PIPELINE_VERSION = "metadata-v1"


def initialize(engine):
    migrate(engine)


def start_scan(engine, connector_id, expected_epoch=None):
    with engine.begin() as conn:
        conn.execute(
            pg_insert(connectors)
            .values(id=connector_id, epoch=0, phase="idle")
            .on_conflict_do_nothing()
        )
        row = (
            conn.execute(
                select(connectors).where(connectors.c.id == connector_id).with_for_update()
            )
            .mappings()
            .one()
        )
        if expected_epoch is not None and row["epoch"] != expected_epoch:
            return None
        epoch = row["epoch"] + 1
        conn.execute(
            update(connectors)
            .where(connectors.c.id == connector_id)
            .values(epoch=epoch, phase="inventory", inventory_cursor=None, watermark=None)
        )
        return epoch


def _enqueue(conn, source_id, revision, generation, pipeline_version):
    job_id = str(uuid.uuid4())
    conn.execute(
        insert(jobs).values(
            id=job_id,
            source_id=source_id,
            revision=revision,
            pipeline_version=pipeline_version,
            generation=generation,
            status="queued",
        )
    )
    conn.execute(insert(outbox).values(id=str(uuid.uuid4()), job_id=job_id))


def _receipt(conn, connector_id, epoch, phase, expected_cursor, page):
    receipt_id = str(uuid.uuid4())
    conn.execute(
        insert(page_receipts).values(
            id=receipt_id,
            connector_id=connector_id,
            epoch=epoch,
            phase=phase,
            expected_cursor=expected_cursor,
            next_cursor=page["next_cursor"],
        )
    )
    for ordinal, item in enumerate(page["items"]):
        conn.execute(
            insert(inbox_items).values(receipt_id=receipt_id, ordinal=ordinal, payload=item)
        )


def _upsert(conn, connector_id, epoch, manifest, pipeline_version):
    manifest = SourceManifest.model_validate(manifest).model_dump(mode="json")
    row = (
        conn.execute(
            select(sources)
            .where(
                and_(
                    sources.c.connector_id == connector_id,
                    sources.c.source_id == manifest["source_id"],
                )
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    current_clients = (
        set()
        if row is None
        else set(
            conn.execute(
                select(relations.c.client_id).where(relations.c.source_id == row["id"])
            ).scalars()
        )
    )
    existing_pipeline = (
        None
        if row is None
        else conn.execute(
            select(jobs.c.pipeline_version)
            .where(and_(jobs.c.source_id == row["id"], jobs.c.generation == row["generation"]))
            .limit(1)
        ).scalar_one_or_none()
    )
    changed = (
        row is None
        or not row["active"]
        or row["revision"] != manifest["revision"]
        or not set(manifest["client_ids"]).issubset(current_clients)
        or existing_pipeline != pipeline_version
    )
    incoming_clients = manifest["client_ids"]
    manifest["client_ids"] = sorted(current_clients | set(incoming_clients))
    if row is None:
        source_id = conn.execute(
            insert(sources)
            .values(
                connector_id=connector_id,
                source_id=manifest["source_id"],
                revision=manifest["revision"],
                manifest=manifest,
                generation=1,
                active=True,
                status="queued",
            )
            .returning(sources.c.id)
        ).scalar_one()
        generation = 1
    else:
        source_id = row["id"]
        generation = row["generation"] + int(changed)
        conn.execute(
            update(sources)
            .where(sources.c.id == source_id)
            .values(
                revision=manifest["revision"],
                manifest=manifest,
                generation=generation,
                active=True,
                status="queued" if changed else row["status"],
            )
        )
    for client in incoming_clients:
        conn.execute(
            pg_insert(relations)
            .values(source_id=source_id, client_id=client, seen_epoch=epoch)
            .on_conflict_do_update(
                index_elements=["source_id", "client_id"], set_={"seen_epoch": epoch}
            )
        )
    if changed:
        _enqueue(conn, source_id, manifest["revision"], generation, pipeline_version)


def _remove_relation(conn, source_id, client_id, pipeline_version):
    result = conn.execute(
        delete(relations).where(
            and_(relations.c.source_id == source_id, relations.c.client_id == client_id)
        )
    )
    if not result.rowcount:
        return
    source = conn.execute(select(sources).where(sources.c.id == source_id)).mappings().one()
    clients = sorted(
        conn.execute(
            select(relations.c.client_id).where(relations.c.source_id == source_id)
        ).scalars()
    )
    manifest = dict(source["manifest"])
    manifest["client_ids"] = clients
    generation = source["generation"] + 1
    conn.execute(
        update(sources)
        .where(sources.c.id == source_id)
        .values(
            active=bool(clients),
            manifest=manifest,
            generation=generation,
            status="queued" if clients else "deleted",
        )
    )
    if clients:
        _enqueue(conn, source_id, source["revision"], generation, pipeline_version)


def apply_inventory(
    engine, connector_id, epoch, expected_cursor, page, pipeline_version=PIPELINE_VERSION
):
    with engine.begin() as conn:
        state = (
            conn.execute(
                select(connectors).where(connectors.c.id == connector_id).with_for_update()
            )
            .mappings()
            .one()
        )
        if (
            state["phase"] != "inventory"
            or state["epoch"] != epoch
            or state["inventory_cursor"] != expected_cursor
        ):
            return False
        _receipt(conn, connector_id, epoch, "inventory", expected_cursor, page)
        watermark = page["changes_cursor"]
        if state["watermark"] is not None and state["watermark"] != watermark:
            raise ValueError("Inventory watermark changed")
        for manifest in page["items"]:
            _upsert(conn, connector_id, epoch, manifest, pipeline_version)
        next_cursor = page["next_cursor"]
        if next_cursor is None:
            unseen = conn.execute(
                select(relations.c.source_id, relations.c.client_id)
                .join(sources)
                .where(
                    and_(sources.c.connector_id == connector_id, relations.c.seen_epoch != epoch)
                )
                .order_by(relations.c.source_id)
            ).all()
            for source_id, client_id in unseen:
                conn.execute(
                    select(sources.c.id).where(sources.c.id == source_id).with_for_update()
                )
                _remove_relation(conn, source_id, client_id, pipeline_version)
        conn.execute(
            update(connectors)
            .where(connectors.c.id == connector_id)
            .values(
                inventory_cursor=next_cursor,
                watermark=watermark,
                phase="changes" if next_cursor is None else "inventory",
                changes_cursor=watermark if next_cursor is None else None,
                scan_completed_at=func.clock_timestamp()
                if next_cursor is None
                else state["scan_completed_at"],
            )
        )
        return True


def apply_changes(
    engine, connector_id, epoch, expected_cursor, page, pipeline_version=PIPELINE_VERSION
):
    with engine.begin() as conn:
        state = (
            conn.execute(
                select(connectors).where(connectors.c.id == connector_id).with_for_update()
            )
            .mappings()
            .one()
        )
        if (
            state["phase"] != "changes"
            or state["epoch"] != epoch
            or state["changes_cursor"] != expected_cursor
        ):
            return False
        _receipt(conn, connector_id, epoch, "changes", expected_cursor, page)
        for item in page["items"]:
            if item["operation"] == "upsert":
                if (
                    item["manifest"]["source_id"] != item["source_id"]
                    or item["client_id"] not in item["manifest"]["client_ids"]
                ):
                    raise ValueError("Mismatched change")
                _upsert(conn, connector_id, epoch, item["manifest"], pipeline_version)
            elif item["operation"] == "delete" and item["manifest"] is None:
                source = conn.execute(
                    select(sources.c.id)
                    .where(
                        and_(
                            sources.c.connector_id == connector_id,
                            sources.c.source_id == item["source_id"],
                        )
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if source is not None:
                    _remove_relation(conn, source, item["client_id"], pipeline_version)
            else:
                raise ValueError("Invalid change")
        conn.execute(
            update(connectors)
            .where(connectors.c.id == connector_id)
            .values(changes_cursor=page["next_cursor"])
        )
        return True
