"""Short leased claims and generation-fenced metadata staging."""

import re
import time
import uuid
from datetime import timedelta

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import OperationalError

from kanzlei_ai.connectors.contracts import SourceManifest
from kanzlei_ai.models import document_artifacts, document_chunks, jobs, sources

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


def claim(engine, job_id, *, stage="extract"):
    with engine.begin() as conn:
        source, job = _locked(conn, job_id, stage=stage)
        if not _current(source, job):
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


def _fail_once(engine, job_id, token, error_code, terminal, stage):
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
        else:
            delay = min(300, 2 ** job["attempts"])
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


def _publish_extraction_once(engine, job_id, token, publication, enqueue_index):
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        source, job = _locked(conn, job_id, skip_locked=False)
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if not _live_claim(source, job, token, now):
            return False
        if not 0 <= publication["page_count"] <= 200 or len(publication["chunks"]) > MAX_CHUNKS:
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
                page_count=publication["page_count"],
            )
            .returning(document_artifacts.c.id)
        ).scalar_one()
        for chunk in publication["chunks"]:
            if not 1 <= chunk["page"] <= publication["page_count"]:
                raise ValueError("Invalid chunk page")
            conn.execute(
                insert(document_chunks).values(
                    id=chunk["id"],
                    artifact_id=artifact_id,
                    source_id=source["id"],
                    revision=job["revision"],
                    generation=job["generation"],
                    parser_version=publication["parser_version"],
                    content_sha256=publication["content_sha256"],
                    page=chunk["page"],
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

            queue_index(conn, source, artifact_id)
        return True


def publish_extraction(engine, job_id, token, publication, *, enqueue_index=False):
    """Atomically expose artifacts only while the claimed source is current."""
    return _publish_with_lock_retry(
        _publish_extraction_once, engine, job_id, token, publication, enqueue_index
    )


def fail(engine, job_id, token, error_code="EXTRACTION_UNAVAILABLE", *, stage="extract"):
    return _publish_with_lock_retry(_fail_once, engine, job_id, token, error_code, False, stage)


def fail_terminal(engine, job_id, token, error_code, *, stage="extract"):
    return _publish_with_lock_retry(_fail_once, engine, job_id, token, error_code, True, stage)


def source_for_claim(engine, job_id, token):
    """Read the manifest only while its current claim is still live."""
    with engine.connect() as conn:
        source, job = _locked(conn, job_id, skip_locked=False)
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if not _live_claim(source, job, token, now):
            return None
        return {
            "connector_id": source["connector_id"],
            "manifest": source["manifest"],
            "generation": source["generation"],
        }
