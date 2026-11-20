"""At-least-once outbox and independent broker-loss recovery."""

import uuid
from datetime import timedelta

from sqlalchemy import and_, func, insert, or_, select, text, update

from kanzlei_ai.jobs import MAX_ATTEMPTS, exhausted_code
from kanzlei_ai.models import jobs, outbox, sources


def dispatch(
    engine, publish, limit=50, *, publish_index=None, publish_ocr=None, index_profile=None
):
    routes = {"native": publish}
    if publish_ocr is not None:
        routes.update(
            paddle=lambda job_id: publish_ocr(job_id, "paddle"),
            mistral=lambda job_id: publish_ocr(job_id, "mistral"),
        )
    eligible = and_(jobs.c.stage == "extract", jobs.c.ocr_profile.in_(routes))
    if publish_index is not None:
        index_route = jobs.c.stage == "index"
        if index_profile is not None:
            index_route = and_(index_route, jobs.c.pipeline_version == index_profile)
        eligible = or_(eligible, index_route)
    count = 0
    for _ in range(limit):
        token = str(uuid.uuid4())
        with engine.begin() as conn:
            now = func.clock_timestamp()
            row = (
                conn.execute(
                    select(outbox, jobs.c.stage, jobs.c.ocr_profile)
                    .join(jobs, outbox.c.job_id == jobs.c.id)
                    .where(
                        and_(
                            outbox.c.published_at.is_(None),
                            eligible,
                            or_(outbox.c.lease_until.is_(None), outbox.c.lease_until < now),
                        )
                    )
                    .order_by(outbox.c.id)
                    .limit(1)
                    .with_for_update(of=outbox, skip_locked=True)
                )
                .mappings()
                .first()
            )
            if row is None:
                break
            conn.execute(
                update(outbox)
                .where(outbox.c.id == row["id"])
                .values(lease_token=token, lease_until=now + text("interval '30 seconds'"))
            )
        try:
            publisher = publish_index if row["stage"] == "index" else routes[row["ocr_profile"]]
            publisher(row["job_id"])
        except Exception:
            delay = min(300, 2 ** min(row["attempts"] + 1, 8))
            with engine.begin() as conn:
                conn.execute(
                    update(outbox)
                    .where(and_(outbox.c.id == row["id"], outbox.c.lease_token == token))
                    .values(
                        attempts=row["attempts"] + 1,
                        lease_token=None,
                        lease_until=func.clock_timestamp() + timedelta(seconds=delay),
                    )
                )
            raise
        with engine.begin() as conn:
            result = conn.execute(
                update(outbox)
                .where(
                    and_(
                        outbox.c.id == row["id"],
                        outbox.c.lease_token == token,
                        outbox.c.published_at.is_(None),
                    )
                )
                .values(published_at=func.clock_timestamp(), lease_until=None, lease_token=None)
            )
            if result.rowcount:
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == row["job_id"])
                    .values(updated_at=func.clock_timestamp())
                )
                count += 1
    return count


def recover(engine, limit=100, *, include_index=False, index_profile=None):
    count = 0
    eligible_stage = jobs.c.stage == "extract"
    if include_index:
        index_stage = jobs.c.stage == "index"
        if index_profile is not None:
            index_stage = and_(index_stage, jobs.c.pipeline_version == index_profile)
        eligible_stage = or_(eligible_stage, index_stage)
    with engine.connect() as conn:
        candidates = conn.execute(
            select(jobs.c.id, jobs.c.source_id)
            .where(
                eligible_stage,
                or_(
                    and_(
                        jobs.c.status == "queued",
                        jobs.c.updated_at < func.clock_timestamp() - text("interval '60 seconds'"),
                    ),
                    and_(jobs.c.status == "running", jobs.c.lease_until < func.clock_timestamp()),
                ),
            )
            .limit(limit)
        ).all()
    for job_id, source_id in candidates:
        with engine.begin() as conn:
            source = (
                conn.execute(
                    select(sources)
                    .where(sources.c.id == source_id)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .first()
            )
            if source is None:
                continue
            job = (
                conn.execute(
                    select(jobs).where(jobs.c.id == job_id).with_for_update(skip_locked=True)
                )
                .mappings()
                .first()
            )
            if job is None:
                continue
            now = conn.execute(select(func.clock_timestamp())).scalar_one()
            eligible = (
                job["status"] == "queued" and (now - job["updated_at"]).total_seconds() >= 60
            ) or (job["status"] == "running" and job["lease_until"] < now)
            if not eligible or (
                job["retry_not_before"] is not None and job["retry_not_before"] > now
            ):
                continue
            if (
                not source["active"]
                or source["generation"] != job["generation"]
                or source["revision"] != job["revision"]
            ):
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id)
                    .values(status="superseded", lease_token=None, lease_until=None, updated_at=now)
                )
                continue
            if job["attempts"] >= MAX_ATTEMPTS:
                error_code = exhausted_code(job["stage"])
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id)
                    .values(
                        status="failed",
                        error_code=error_code,
                        lease_token=None,
                        lease_until=None,
                        updated_at=now,
                    )
                )
                if job["stage"] == "extract":
                    conn.execute(
                        update(sources)
                        .where(sources.c.id == source_id)
                        .values(status="failed", error_code=error_code)
                    )
                continue
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(status="queued", lease_token=None, lease_until=None, updated_at=now)
            )
            pending = conn.execute(
                select(outbox.c.id)
                .where(and_(outbox.c.job_id == job_id, outbox.c.published_at.is_(None)))
                .limit(1)
            ).first()
            if pending is None:
                conn.execute(insert(outbox).values(id=str(uuid.uuid4()), job_id=job_id))
                count += 1
    return count
