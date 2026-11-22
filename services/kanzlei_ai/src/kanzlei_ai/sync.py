"""Atomic application of ordered, opaque ERP feed pages."""

import uuid

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kanzlei_ai.connectors.contracts import SourceManifest
from kanzlei_ai.db import migrate
from kanzlei_ai.documents.docx import DOCX_MIME_TYPE
from kanzlei_ai.jobs import clear_index_batches
from kanzlei_ai.models import (
    connectors,
    inbox_items,
    jobs,
    outbox,
    page_receipts,
    relations,
    sources,
)

PIPELINE_VERSION = "pdf-native-v1"
ARCHIVE_OCR_PROFILES = ("native", "paddle")


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


def _enqueue(conn, source_id, revision, generation, pipeline_version, ocr_profile):
    job_id = str(uuid.uuid4())
    conn.execute(
        insert(jobs).values(
            id=job_id,
            source_id=source_id,
            revision=revision,
            pipeline_version=pipeline_version,
            generation=generation,
            ocr_profile=ocr_profile,
            status="queued",
        )
    )
    conn.execute(insert(outbox).values(id=str(uuid.uuid4()), job_id=job_id))
    return job_id


def _effective_ocr_profile(connector, ocr_profile):
    if ocr_profile not in ARCHIVE_OCR_PROFILES:
        raise ValueError("Initial OCR profile must be native or paddle")
    return connector["ocr_profile_override"] or ocr_profile


def cutover_ocr(engine, connector_id):
    """Opt a connector into Mistral for newly discovered source revisions."""
    with engine.begin() as conn:
        row = conn.execute(
            select(connectors.c.ocr_profile_override)
            .where(connectors.c.id == connector_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise ValueError("Unknown connector")
        if row.ocr_profile_override == "mistral":
            return False
        conn.execute(
            update(connectors)
            .where(connectors.c.id == connector_id)
            .values(ocr_profile_override="mistral")
        )
        return True


def _latest_extract(conn, source_id, revision):
    return (
        conn.execute(
            select(jobs.c.pipeline_version, jobs.c.ocr_profile, jobs.c.generation, jobs.c.status)
            .where(
                jobs.c.source_id == source_id,
                jobs.c.revision == revision,
                jobs.c.stage == "extract",
            )
            .order_by(jobs.c.generation.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )


def requeue_extract(
    engine, connector_id, source_id, pipeline_version=PIPELINE_VERSION, *, ocr_profile="native"
):
    """Explicitly re-extract one active PDF under the connector's current profile."""
    with engine.begin() as conn:
        connector = (
            conn.execute(
                select(connectors).where(connectors.c.id == connector_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if connector is None:
            raise ValueError("Unknown connector")
        profile = _effective_ocr_profile(connector, ocr_profile)
        source = (
            conn.execute(
                select(sources)
                .where(sources.c.connector_id == connector_id, sources.c.source_id == source_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if (
            source is None
            or not source["active"]
            or source["manifest"]["mime_type"] != "application/pdf"
        ):
            raise ValueError("Active PDF source required")
        pk, revision, generation = source["id"], source["revision"], source["generation"]
        existing = _latest_extract(conn, pk, revision)
        if (
            existing
            and existing["generation"] == generation
            and existing["status"] in ("queued", "running")
            and existing["pipeline_version"] == pipeline_version
            and existing["ocr_profile"] == profile
        ):
            return None
        superseded = conn.execute(
            update(jobs)
            .where(
                jobs.c.source_id == pk,
                jobs.c.generation == generation,
                jobs.c.status.in_(("queued", "running")),
            )
            .values(
                status="superseded",
                lease_token=None,
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )
            .returning(jobs.c.id, jobs.c.stage)
        ).all()
        for job_id, stage in superseded:
            if stage == "index":
                clear_index_batches(conn, job_id)
        conn.execute(
            update(sources)
            .where(sources.c.id == pk)
            .values(generation=generation + 1, status="queued", error_code=None)
        )
        return _enqueue(conn, pk, revision, generation + 1, pipeline_version, profile)


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


def _upsert(conn, connector_id, epoch, manifest, pipeline_version, ocr_profile, *, docx_enabled):
    manifest = SourceManifest.model_validate(manifest).model_dump(mode="json", exclude_none=True)
    is_email = manifest["source_type"] == "email"
    is_docx = manifest["mime_type"] == DOCX_MIME_TYPE
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
    existing = None if row is None else _latest_extract(conn, row["id"], manifest["revision"])
    same_revision = row is not None and row["active"] and row["revision"] == manifest["revision"]
    needs_local_ocr = (
        same_revision
        and manifest["mime_type"] == "application/pdf"
        and ocr_profile == "paddle"
        and existing is not None
        and existing["ocr_profile"] == "native"
        and row["error_code"] == "OCR_REQUIRED"
    )
    needs_docx = (
        is_docx
        and docx_enabled
        and same_revision
        and existing is not None
        and (
            row["error_code"] in {"UNSUPPORTED_MEDIA", "INVALID_EXTRACTION_PROFILE"}
            or (existing["ocr_profile"] != "native" and existing["status"] in {"queued", "running"})
        )
    )
    changed = (
        not same_revision
        or not set(manifest["client_ids"]).issubset(current_clients)
        or existing is None
        or existing["generation"] != row["generation"]
        or existing["pipeline_version"] != pipeline_version
        or (is_email and existing["ocr_profile"] != "native")
        or needs_local_ocr
        or needs_docx
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
                error_code=None if changed else row["error_code"],
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
        selected_profile = (
            existing["ocr_profile"]
            if same_revision and existing and not needs_local_ocr
            else ocr_profile
        )
        if is_email or is_docx:
            selected_profile = "native"
        _enqueue(
            conn, source_id, manifest["revision"], generation, pipeline_version, selected_profile
        )


def _remove_relation(conn, source_id, client_id, pipeline_version, ocr_profile):
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
            error_code=None,
        )
    )
    if clients:
        existing = _latest_extract(conn, source_id, source["revision"])
        selected_profile = existing["ocr_profile"] if existing else ocr_profile
        if manifest["source_type"] == "email" or manifest["mime_type"] == DOCX_MIME_TYPE:
            selected_profile = "native"
        _enqueue(
            conn, source_id, source["revision"], generation, pipeline_version, selected_profile
        )


def apply_inventory(
    engine,
    connector_id,
    epoch,
    expected_cursor,
    page,
    pipeline_version=PIPELINE_VERSION,
    *,
    ocr_profile="native",
    docx_enabled=False,
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
        profile = _effective_ocr_profile(state, ocr_profile)
        for manifest in page["items"]:
            _upsert(
                conn,
                connector_id,
                epoch,
                manifest,
                pipeline_version,
                profile,
                docx_enabled=docx_enabled,
            )
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
                _remove_relation(conn, source_id, client_id, pipeline_version, profile)
        conn.execute(
            update(connectors)
            .where(connectors.c.id == connector_id)
            .values(
                inventory_cursor=next_cursor,
                watermark=watermark,
                phase="changes" if next_cursor is None else "inventory",
                changes_cursor=watermark if next_cursor is None else None,
                last_successful_poll_at=func.clock_timestamp(),
                scan_completed_at=func.clock_timestamp()
                if next_cursor is None
                else state["scan_completed_at"],
            )
        )
        return True


def apply_changes(
    engine,
    connector_id,
    epoch,
    expected_cursor,
    page,
    pipeline_version=PIPELINE_VERSION,
    *,
    ocr_profile="native",
    docx_enabled=False,
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
        profile = _effective_ocr_profile(state, ocr_profile)
        for item in page["items"]:
            if item["operation"] == "upsert":
                if (
                    item["manifest"]["source_id"] != item["source_id"]
                    or item["client_id"] not in item["manifest"]["client_ids"]
                ):
                    raise ValueError("Mismatched change")
                _upsert(
                    conn,
                    connector_id,
                    epoch,
                    item["manifest"],
                    pipeline_version,
                    profile,
                    docx_enabled=docx_enabled,
                )
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
                    _remove_relation(conn, source, item["client_id"], pipeline_version, profile)
            else:
                raise ValueError("Invalid change")
        conn.execute(
            update(connectors)
            .where(connectors.c.id == connector_id)
            .values(
                changes_cursor=page["next_cursor"],
                last_successful_poll_at=func.clock_timestamp(),
            )
        )
        return True
