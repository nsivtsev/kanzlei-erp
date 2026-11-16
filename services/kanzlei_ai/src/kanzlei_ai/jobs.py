"""Short leased claims and generation-fenced metadata staging."""

import time
import uuid

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError

from kanzlei_ai.connectors.contracts import SourceManifest
from kanzlei_ai.models import jobs, sources

LEASE_SECONDS = 120
MAX_ATTEMPTS = 5


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
                .values(status="failed", lease_token=None, lease_until=None, updated_at=now)
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
            update(sources).where(sources.c.id == source["id"]).values(status="awaiting_extraction")
        )
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                status="completed",
                lease_token=None,
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )
        )
        return True


def _fail_once(engine, job_id, token):
    from datetime import timedelta

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        source, job = _locked(conn, job_id, skip_locked=False)
        if job is None:
            return
        now = conn.execute(select(func.clock_timestamp())).scalar_one()
        if (
            not source["active"]
            or source["generation"] != job["generation"]
            or source["revision"] != job["revision"]
            or job["status"] != "running"
            or job["lease_token"] != token
            or job["lease_until"] <= now
        ):
            return
        if job["attempts"] >= MAX_ATTEMPTS:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(status="failed", lease_token=None, lease_until=None, updated_at=now)
            )
        else:
            delay = min(300, 2 ** job["attempts"])
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status="queued",
                    lease_token=None,
                    lease_until=None,
                    retry_not_before=now + timedelta(seconds=delay),
                    updated_at=now,
                )
            )


def _publish_with_lock_retry(operation, engine, job_id, token):
    # Bound contention well below the task soft deadline and the 120s lease.
    deadline = time.monotonic() + 20
    while True:
        try:
            return operation(engine, job_id, token)
        except OperationalError as error:
            if getattr(error.orig, "sqlstate", None) != "55P03" or time.monotonic() >= deadline:
                raise


def complete(engine, job_id, token):
    return _publish_with_lock_retry(_complete_once, engine, job_id, token)


def fail(engine, job_id, token):
    return _publish_with_lock_retry(_fail_once, engine, job_id, token)
