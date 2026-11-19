"""Stage a rebuildable search projection, then activate it under a SQL lease."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kanzlei_ai import jobs as job_service
from kanzlei_ai.models import (
    document_artifacts,
    document_chunks,
    index_publications,
    jobs,
    outbox,
    relations,
    sources,
)
from kanzlei_ai.search.index import INDEX_PROFILE, SearchIndex

INDEX_STAGE = "index"
INDEX_UNAVAILABLE = "INDEX_UNAVAILABLE"
MAX_REINDEX_LIMIT = 500


@dataclass(frozen=True)
class ReindexPage:
    queued: int
    next_cursor: int | None


def _current_artifact():
    return (
        select(document_artifacts.c.id)
        .where(
            document_artifacts.c.source_id == sources.c.id,
            document_artifacts.c.revision == sources.c.revision,
            document_artifacts.c.generation == sources.c.generation,
        )
        .order_by(document_artifacts.c.id.desc())
        .limit(1)
        .correlate(sources)
        .scalar_subquery()
    )


def queue_index(conn, source, artifact_id):
    """Queue the current extracted artifact while holding the source row lock."""
    key = {
        "source_id": source["id"],
        "revision": source["revision"],
        "pipeline_version": INDEX_PROFILE,
        "generation": source["generation"],
        "stage": INDEX_STAGE,
    }
    values = {
        **key,
        "artifact_id": artifact_id,
        "status": "queued",
        "attempts": 0,
        "lease_token": None,
        "lease_until": None,
        "retry_not_before": None,
        "error_code": None,
        "updated_at": func.clock_timestamp(),
    }
    command = pg_insert(jobs).values(id=str(uuid.uuid4()), **values)
    job_id = conn.execute(
        command.on_conflict_do_update(
            index_elements=[jobs.c[name] for name in key],
            set_=values,
            where=jobs.c.status.in_(("completed", "failed", "superseded")),
        ).returning(jobs.c.id)
    ).scalar_one_or_none()
    if job_id is None:
        return False
    pending = select(outbox.c.id).where(outbox.c.job_id == job_id, outbox.c.published_at.is_(None))
    if not conn.execute(select(pending.exists())).scalar_one():
        conn.execute(insert(outbox).values(id=str(uuid.uuid4()), job_id=job_id))
    return True


def reindex(engine, connector_id, *, after=0, limit=100) -> ReindexPage:
    """Queue one page of current extracted sources using a numeric source cursor."""
    if after < 0 or not 1 <= limit <= MAX_REINDEX_LIMIT:
        raise ValueError("Invalid reindex page")
    with engine.connect() as conn:
        source_ids = (
            conn.execute(
                select(sources.c.id)
                .where(
                    sources.c.connector_id == connector_id,
                    sources.c.active,
                    sources.c.id > after,
                    _current_artifact().is_not(None),
                )
                .order_by(sources.c.id)
                .limit(limit)
            )
            .scalars()
            .all()
        )
    queued = 0
    # Lock one source per transaction, in the same order as the worker.
    for source_id in source_ids:
        with engine.begin() as conn:
            source = (
                conn.execute(select(sources).where(sources.c.id == source_id).with_for_update())
                .mappings()
                .one_or_none()
            )
            if source is None or not source["active"]:
                continue
            artifact_id = _artifact_for_source(conn, source_id)
            if artifact_id is not None:
                queued += int(queue_index(conn, source, artifact_id))
    return ReindexPage(
        queued=queued, next_cursor=source_ids[-1] if len(source_ids) == limit else None
    )


def _artifact_for_source(conn, source_id):
    return conn.execute(
        select(_current_artifact()).select_from(sources).where(sources.c.id == source_id)
    ).scalar_one()


def _publication_claim(conn, job_id, token):
    conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    source, job = job_service._locked(conn, job_id, stage=INDEX_STAGE, skip_locked=False)
    now = conn.execute(select(func.clock_timestamp())).scalar_one()
    if not job_service._live_claim(source, job, token, now):
        return None, None
    artifact_id = job["artifact_id"]
    if (
        job["pipeline_version"] != INDEX_PROFILE
        or artifact_id is None
        or artifact_id != _artifact_for_source(conn, source["id"])
    ):
        return None, None
    return source, job


def _read_projection(engine, job_id, token):
    with engine.connect() as conn:
        source, job = _publication_claim(conn, job_id, token)
        if job is None:
            return None
        client_ids = (
            conn.execute(
                select(relations.c.client_id)
                .where(relations.c.source_id == source["id"])
                .order_by(relations.c.client_id)
            )
            .scalars()
            .all()
        )
        if not client_ids:
            return None
        chunks = (
            conn.execute(
                select(document_chunks.c.id, document_chunks.c.text)
                .where(
                    document_chunks.c.artifact_id == job["artifact_id"],
                    document_chunks.c.source_id == source["id"],
                    document_chunks.c.revision == job["revision"],
                    document_chunks.c.generation == job["generation"],
                )
                .order_by(document_chunks.c.ordinal)
                .limit(job_service.MAX_CHUNKS + 1)
            )
            .mappings()
            .all()
        )
        if len(chunks) > job_service.MAX_CHUNKS:
            raise ValueError("Invalid index projection")
        return {
            "publication_id": token,
            "connector_id": source["connector_id"],
            "source_pk": source["id"],
            "revision": job["revision"],
            "generation": job["generation"],
            "client_ids": client_ids,
            "chunks": chunks,
        }


def _activate_once(engine, job_id, token):
    with engine.begin() as conn:
        source, job = _publication_claim(conn, job_id, token)
        if job is None:
            return False
        values = {
            "publication_id": token,
            "artifact_id": job["artifact_id"],
            "revision": job["revision"],
            "generation": job["generation"],
            "profile": INDEX_PROFILE,
        }
        conn.execute(
            pg_insert(index_publications)
            .values(source_id=source["id"], **values)
            .on_conflict_do_update(index_elements=[index_publications.c.source_id], set_=values)
        )
        conn.execute(
            update(sources)
            .where(sources.c.id == source["id"])
            .values(status="ready", error_code=None)
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


def process(engine, job_id, token, index: SearchIndex) -> bool:
    projection = job_service._publish_with_lock_retry(_read_projection, engine, job_id, token)
    if projection is None:
        return False
    try:
        # The adapter confirms all bounded batches under one immutable publication ID.
        index.stage(**projection)
    except Exception:
        job_service.fail(engine, job_id, token, INDEX_UNAVAILABLE, stage=INDEX_STAGE)
        return False
    return job_service._publish_with_lock_retry(_activate_once, engine, job_id, token)
