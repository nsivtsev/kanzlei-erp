"""Short leased claims and generation-fenced extraction."""

import re
import time
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import OperationalError

from kanzlei_ai.connectors.contracts import SourceManifest
from kanzlei_ai.models import (
    document_artifacts,
    document_chunks,
    extraction_plans,
    index_embedding_batches,
    jobs,
    ocr_page_checkpoints,
    outbox,
    sources,
)

LEASE_SECONDS = 120
MAX_ATTEMPTS = 5
MAX_CHUNKS = 10000
RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


def _error_code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value):
        raise ValueError("Invalid error code")
    return value


def _locked(conn, job_id, *, stage="extract", skip_locked=True):
    source_id = conn.execute(
        select(jobs.c.source_id).where(jobs.c.id == job_id, jobs.c.stage == stage)
    ).scalar_one_or_none()
    if source_id is None:
        return None, None
    source = (
        conn.execute(
            select(sources)
            .where(sources.c.id == source_id)
            .with_for_update(skip_locked=skip_locked)
        )
        .mappings()
        .first()
    )
    if source is None:
        return None, None
    job = (
        conn.execute(
            select(jobs).where(jobs.c.id == job_id).with_for_update(skip_locked=skip_locked)
        )
        .mappings()
        .first()
    )
    return source, job


def _current(source, job):
    return (
        job is not None
        and source["active"]
        and source["generation"] == job["generation"]
        and source["revision"] == job["revision"]
    )


def _live_claim(source, job, token, now):
    return (
        _current(source, job)
        and job["status"] == "running"
        and job["lease_token"] == token
        and job["lease_until"] > now
    )


def exhausted_code(stage):
    return "INDEX_RETRY_EXHAUSTED" if stage == "index" else RETRY_EXHAUSTED


def clear_index_batches(conn, job_id):
    conn.execute(delete(index_embedding_batches).where(index_embedding_batches.c.job_id == job_id))


def claim(engine, job_id, *, stage="extract", ocr_profile=None, pipeline_version=None):
    with engine.begin() as conn:
        source, job = _locked(conn, job_id, stage=stage)
        if (
            not _current(source, job)
            or (ocr_profile is not None and job["ocr_profile"] != ocr_profile)
            or (pipeline_version is not None and job["pipeline_version"] != pipeline_version)
        ):
            return None
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if job["status"] != "queued" and not (
            job["status"] == "running" and job["lease_until"] <= now
        ):
            return None
        if job["retry_not_before"] is not None and job["retry_not_before"] > now:
            return None
        if job["attempts"] >= MAX_ATTEMPTS:
            code = exhausted_code(stage)
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status="failed",
                    error_code=code,
                    lease_token=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
            if stage == "extract":
                conn.execute(
                    update(sources)
                    .where(sources.c.id == source["id"])
                    .values(status="failed", error_code=code)
                )
            elif stage == "index":
                clear_index_batches(conn, job_id)
            return None
        token = str(uuid.uuid4())
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                status="running",
                attempts=job["attempts"] + 1,
                retry_not_before=None,
                lease_token=token,
                lease_until=func.clock_timestamp() + text("interval '120 seconds'"),
                updated_at=func.clock_timestamp(),
            )
        )
        return token


def _complete_once(engine, job_id, token):
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        source, job = _locked(conn, job_id, skip_locked=False)
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if not _live_claim(source, job, token, now):
            return False
        SourceManifest.model_validate(source["manifest"])
        conn.execute(
            update(sources)
            .where(sources.c.id == source["id"])
            .values(status="awaiting_extraction", error_code=None)
        )
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                status="completed",
                error_code=None,
                lease_token=None,
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )
        )
        return True


def _fail_once(engine, job_id, token, error_code, terminal, stage, retry_after=None):
    error_code = _error_code(error_code)

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        source, job = _locked(conn, job_id, stage=stage, skip_locked=False)
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if not _live_claim(source, job, token, now):
            return False
        if terminal or job["attempts"] >= MAX_ATTEMPTS:
            final_code = error_code if terminal else exhausted_code(stage)
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status="failed",
                    error_code=final_code,
                    lease_token=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
            if stage == "extract":
                conn.execute(
                    update(sources)
                    .where(sources.c.id == source["id"])
                    .values(status="failed", error_code=final_code)
                )
            elif stage == "index":
                clear_index_batches(conn, job_id)
        else:
            delay = min(300, 2 ** job["attempts"])
            if retry_after is not None:
                delay = max(delay, min(retry_after, 86400))
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status="queued",
                    error_code=error_code,
                    lease_token=None,
                    lease_until=None,
                    retry_not_before=now + timedelta(seconds=delay),
                    updated_at=now,
                )
            )
        return True


def _publish_with_lock_retry(operation, engine, job_id, token, *args):
    # Bound contention well below the task soft deadline and the 120s lease.
    deadline = time.monotonic() + 20
    while True:
        try:
            return operation(engine, job_id, token, *args)
        except OperationalError as error:
            if getattr(error.orig, "sqlstate", None) != "55P03" or time.monotonic() >= deadline:
                raise


def complete(engine, job_id, token):
    return _publish_with_lock_retry(_complete_once, engine, job_id, token)


def _publish_extraction_once(engine, job_id, token, publication, enqueue_index, index_profile):
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        source, job = _locked(conn, job_id, skip_locked=False)
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if not _live_claim(source, job, token, now):
            return False
        page_count = publication["page_count"]
        if not 0 <= page_count <= 200 or len(publication["chunks"]) > MAX_CHUNKS:
            raise ValueError("Invalid extraction publication")
        artifact_id = conn.execute(
            insert(document_artifacts)
            .values(
                source_id=source["id"],
                revision=job["revision"],
                generation=job["generation"],
                parser_version=publication["parser_version"],
                content_sha256=publication["content_sha256"],
                artifact_key=publication["artifact_key"],
                page_count=page_count,
            )
            .returning(document_artifacts.c.id)
        ).scalar_one()
        for chunk in publication["chunks"]:
            page = chunk["page"]
            if page_count and (type(page) is not int or not 1 <= page <= page_count):
                raise ValueError("Invalid chunk page")
            if not page_count and page is not None:
                raise ValueError("A nonpaginated chunk must not have a page")
            conn.execute(
                insert(document_chunks).values(
                    id=chunk["id"],
                    artifact_id=artifact_id,
                    source_id=source["id"],
                    revision=job["revision"],
                    generation=job["generation"],
                    parser_version=publication["parser_version"],
                    content_sha256=publication["content_sha256"],
                    page=page,
                    kind=chunk["kind"],
                    ordinal=chunk["ordinal"],
                    text=chunk["text"],
                )
            )
        conn.execute(
            update(sources)
            .where(sources.c.id == source["id"])
            .values(status="extracted", error_code=None)
        )
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                status="completed",
                error_code=None,
                lease_token=None,
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )
        )
        if enqueue_index:
            from kanzlei_ai.search.publication import queue_index

            if index_profile is None:
                queue_index(conn, source, artifact_id)
            else:
                queue_index(conn, source, artifact_id, profile=index_profile)
        return True


def publish_extraction(
    engine, job_id, token, publication, *, enqueue_index=False, index_profile=None
):
    """Atomically expose artifacts only while the claimed source is current."""
    return _publish_with_lock_retry(
        _publish_extraction_once, engine, job_id, token, publication, enqueue_index, index_profile
    )


def fail(
    engine, job_id, token, error_code="EXTRACTION_UNAVAILABLE", *, stage="extract", retry_after=None
):
    return _publish_with_lock_retry(
        _fail_once, engine, job_id, token, error_code, False, stage, retry_after
    )


def fail_terminal(engine, job_id, token, error_code, *, stage="extract"):
    return _publish_with_lock_retry(_fail_once, engine, job_id, token, error_code, True, stage)


def _extraction_claim(conn, job_id, token):
    conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    source, job = _locked(conn, job_id, skip_locked=False)
    now = conn.execute(select(func.clock_timestamp())).scalar_one()
    if not _live_claim(source, job, token, now):
        return None, None
    return source, job


def _checkpoint_once(engine, job_id, token, table, values):
    with engine.begin() as conn:
        _, job = _extraction_claim(conn, job_id, token)
        if job is None:
            return False
        conn.execute(insert(table).values(job_id=job_id, **values))
        requeue_progress(conn, job_id)
        return True


def requeue_progress(conn, job_id, **progress):
    """Release a fenced claim and publish the next durable portion."""
    conn.execute(
        update(jobs)
        .where(jobs.c.id == job_id)
        .values(
            status="queued",
            attempts=0,
            lease_token=None,
            lease_until=None,
            error_code=None,
            updated_at=func.clock_timestamp(),
            **progress,
        )
    )
    # The preceding delivery may still be marking its outbox row sent.
    conn.execute(insert(outbox).values(id=str(uuid.uuid4()), job_id=job_id))


def save_extraction_plan(engine, job_id, token, plan):
    """Checkpoint the native parse and release the claim before OCR starts."""
    return _publish_with_lock_retry(_checkpoint_once, engine, job_id, token, extraction_plans, plan)


def save_ocr_page(engine, job_id, token, page):
    """Commit a normalized page only while the extraction claim is current."""
    return _publish_with_lock_retry(
        _checkpoint_once, engine, job_id, token, ocr_page_checkpoints, page
    )


def extraction_claim_is_live(engine, job_id, token):
    with engine.connect() as conn:
        _, job = _extraction_claim(conn, job_id, token)
        return job is not None


def _source_for_claim_once(engine, job_id, token):
    with engine.connect() as conn:
        source, job = _extraction_claim(conn, job_id, token)
        if job is None:
            return None
        plan = (
            conn.execute(select(extraction_plans).where(extraction_plans.c.job_id == job_id))
            .mappings()
            .first()
        )
        checkpoints = (
            conn.execute(
                select(ocr_page_checkpoints)
                .where(ocr_page_checkpoints.c.job_id == job_id)
                .order_by(ocr_page_checkpoints.c.page)
            )
            .mappings()
            .all()
            if plan
            else []
        )
        return {
            "connector_id": source["connector_id"],
            "manifest": source["manifest"],
            "generation": source["generation"],
            "ocr_profile": job["ocr_profile"],
            "plan": plan,
            "checkpoints": checkpoints,
        }


def source_for_claim(engine, job_id, token):
    """Read the current manifest and completed OCR portions."""
    return _publish_with_lock_retry(_source_for_claim_once, engine, job_id, token)
