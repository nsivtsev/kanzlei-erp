"""Short leased claims and generation-fenced metadata staging."""

import re
import time
import uuid

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import OperationalError

from kanzlei_ai.connectors.contracts import SourceManifest
from kanzlei_ai.models import document_artifacts, document_chunks, jobs, sources

LEASE_SECONDS = 120
MAX_ATTEMPTS = 5
RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


def _error_code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value):
        raise ValueError("Invalid error code")
    return value


def _locked(conn, job_id, *, skip_locked=True):
    source_id = conn.execute(
        select(jobs.c.source_id).where(jobs.c.id == job_id)
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


def claim(engine, job_id):
    with engine.begin() as conn:
        source, job = _locked(conn, job_id)
        if (
            job is None
            or not source["active"]
            or source["generation"] != job["generation"]
            or source["revision"] != job["revision"]
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
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status="failed",
                    error_code=RETRY_EXHAUSTED,
                    lease_token=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
            conn.execute(
                update(sources)
                .where(sources.c.id == source["id"])
                .values(status="failed", error_code=RETRY_EXHAUSTED)
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
        if job is None:
            return False
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if (
            not source["active"]
            or source["generation"] != job["generation"]
            or source["revision"] != job["revision"]
            or job["status"] != "running"
            or job["lease_token"] != token
            or job["lease_until"] <= now
        ):
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


def _fail_once(engine, job_id, token, error_code="EXTRACTION_UNAVAILABLE", terminal=False):
    from datetime import timedelta

    error_code = _error_code(error_code)

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        source, job = _locked(conn, job_id, skip_locked=False)
        if job is None:
            return False
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if (
            not source["active"]
            or source["generation"] != job["generation"]
            or source["revision"] != job["revision"]
            or job["status"] != "running"
            or job["lease_token"] != token
            or job["lease_until"] <= now
        ):
            return False
        if terminal or job["attempts"] >= MAX_ATTEMPTS:
            final_code = error_code if terminal else RETRY_EXHAUSTED
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


def _publish_extraction_once(engine, job_id, token, publication):
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        source, job = _locked(conn, job_id, skip_locked=False)
        if job is None:
            return False
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if (
            not source["active"]
            or source["generation"] != job["generation"]
            or source["revision"] != job["revision"]
            or job["status"] != "running"
            or job["lease_token"] != token
            or job["lease_until"] <= now
        ):
            return False
        if not 0 <= publication["page_count"] <= 200 or len(publication["chunks"]) > 10000:
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
        return True


def publish_extraction(engine, job_id, token, publication):
    """Atomically expose artifacts only while the claimed source is current."""
    return _publish_with_lock_retry(_publish_extraction_once, engine, job_id, token, publication)


def fail(engine, job_id, token, error_code="EXTRACTION_UNAVAILABLE"):
    return _publish_with_lock_retry(_fail_once, engine, job_id, token, error_code)


def fail_terminal(engine, job_id, token, error_code):
    return _publish_with_lock_retry(_fail_once, engine, job_id, token, error_code, True)


def source_for_claim(engine, job_id, token):
    """Read the manifest only while its current claim is still live."""
    with engine.connect() as conn:
        source, job = _locked(conn, job_id, skip_locked=False)
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if (
            source is None
            or job is None
            or not source["active"]
            or source["generation"] != job["generation"]
            or source["revision"] != job["revision"]
            or job["status"] != "running"
            or job["lease_token"] != token
            or job["lease_until"] <= now
        ):
            return None
        return {
            "connector_id": source["connector_id"],
            "manifest": source["manifest"],
            "generation": source["generation"],
        }
