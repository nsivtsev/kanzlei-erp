"""Stage a rebuildable search projection, then activate it under a SQL lease."""

import uuid
from dataclasses import dataclass
from struct import Struct

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kanzlei_ai import jobs as job_service
from kanzlei_ai.models import (
    document_artifacts,
    document_chunks,
    index_embedding_batches,
    index_publications,
    jobs,
    outbox,
    relations,
    sources,
)
from kanzlei_ai.outbox import pending_delivery
from kanzlei_ai.providers.mistral import MistralError
from kanzlei_ai.providers.mistral_embeddings import (
    EMBEDDING_DIMENSIONS,
    MAX_BATCH_ITEMS,
    embedding_batches,
)
from kanzlei_ai.search.index import INDEX_PROFILE, MISTRAL_INDEX_PROFILE, SearchIndex

INDEX_STAGE = "index"
INDEX_UNAVAILABLE = "INDEX_UNAVAILABLE"
INDEX_PROCESSING_DENIED = "INDEX_PROCESSING_DENIED"
MAX_REINDEX_LIMIT = 500
_EMBEDDING_ROW = Struct(f"!{EMBEDDING_DIMENSIONS}f")


class _ProcessingDenied(Exception):
    """A current source is ineligible for external processing."""


class _ClaimLost(Exception):
    """The lease or source generation changed before an embedding batch."""


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


def configured_index_profile(settings):
    if getattr(settings, "model_profile", "synthetic") == "mistral":
        return MISTRAL_INDEX_PROFILE
    return INDEX_PROFILE


def queue_index(conn, source, artifact_id, *, profile=INDEX_PROFILE):
    """Queue the current extracted artifact while holding the source row lock."""
    key = {
        "source_id": source["id"],
        "revision": source["revision"],
        "pipeline_version": profile,
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
        "index_build_id": None,
        "index_offset": 0,
        "index_repair_started": False,
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
    job_service.clear_index_batches(conn, job_id)
    superseded = conn.execute(
        update(jobs)
        .where(
            jobs.c.source_id == source["id"],
            jobs.c.generation == source["generation"],
            jobs.c.stage == INDEX_STAGE,
            jobs.c.pipeline_version != profile,
            jobs.c.status.in_(("queued", "running")),
        )
        .values(
            status="superseded",
            lease_token=None,
            lease_until=None,
            updated_at=func.clock_timestamp(),
        )
        .returning(jobs.c.id)
    ).scalars()
    for other_job_id in superseded:
        job_service.clear_index_batches(conn, other_job_id)
    pending = select(outbox.c.id).where(outbox.c.job_id == job_id, pending_delivery())
    if not conn.execute(select(pending.exists())).scalar_one():
        conn.execute(insert(outbox).values(id=str(uuid.uuid4()), job_id=job_id))
    return True


def reindex(
    engine, connector_id, *, after=0, limit=100, profile=INDEX_PROFILE, missing_only=False
) -> ReindexPage:
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
                if missing_only and _is_published(conn, source_id, artifact_id, profile):
                    continue
                queued += int(queue_index(conn, source, artifact_id, profile=profile))
    return ReindexPage(
        queued=queued, next_cursor=source_ids[-1] if len(source_ids) == limit else None
    )


def _is_published(conn, source_id, artifact_id, profile):
    publication = select(index_publications.c.source_id).where(
        index_publications.c.source_id == source_id,
        index_publications.c.artifact_id == artifact_id,
        index_publications.c.profile == profile,
    )
    return conn.execute(select(publication.exists())).scalar_one()


def _artifact_for_source(conn, source_id):
    return conn.execute(
        select(_current_artifact()).select_from(sources).where(sources.c.id == source_id)
    ).scalar_one()


def _publication_claim(conn, job_id, token, profile):
    conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    source, job = job_service._locked(conn, job_id, stage=INDEX_STAGE, skip_locked=False)
    now = conn.execute(select(func.clock_timestamp())).scalar_one()
    if not job_service._live_claim(source, job, token, now):
        return None, None
    artifact_id = job["artifact_id"]
    if (
        job["pipeline_version"] != profile
        or artifact_id is None
        or artifact_id != _artifact_for_source(conn, source["id"])
    ):
        return None, None
    return source, job


def _read_projection(engine, job_id, token, profile):
    with engine.connect() as conn:
        source, job = _publication_claim(conn, job_id, token, profile)
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
        live = profile == MISTRAL_INDEX_PROFILE
        offset = job["index_offset"] if live else 0
        limit = (
            min(MAX_BATCH_ITEMS, job_service.MAX_CHUNKS - offset + 1)
            if live
            else (job_service.MAX_CHUNKS + 1)
        )
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
                .offset(offset)
                .limit(limit)
            )
            .mappings()
            .all()
        )
        if offset + len(chunks) > job_service.MAX_CHUNKS:
            raise ValueError("Invalid index projection")
        cached_batch = None
        if live and chunks and job["index_build_id"]:
            cached_batch = conn.execute(
                select(index_embedding_batches.c.vectors).where(
                    index_embedding_batches.c.job_id == job_id,
                    index_embedding_batches.c.offset == offset,
                )
            ).scalar_one_or_none()
        return (
            source["source_id"],
            job,
            cached_batch,
            {
                "publication_id": (job["index_build_id"] or token) if live else token,
                "connector_id": source["connector_id"],
                "source_pk": source["id"],
                "revision": job["revision"],
                "generation": job["generation"],
                "client_ids": client_ids,
                "chunks": chunks,
            },
        )


def _activate_once(engine, job_id, token, profile, publication_id=None):
    with engine.begin() as conn:
        source, job = _publication_claim(conn, job_id, token, profile)
        if job is None:
            return False
        if publication_id is not None:
            if publication_id != (job["index_build_id"] or token):
                return False
            job_service.clear_index_batches(conn, job_id)
        values = {
            "publication_id": publication_id or token,
            "artifact_id": job["artifact_id"],
            "revision": job["revision"],
            "generation": job["generation"],
            "profile": profile,
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


def _index_claim_is_live(engine, job_id, token, profile):
    with engine.connect() as conn:
        _, job = _publication_claim(conn, job_id, token, profile)
        return job is not None


def _save_batch_once(engine, job_id, token, offset, encoded):
    with engine.begin() as conn:
        _, job = _publication_claim(conn, job_id, token, MISTRAL_INDEX_PROFILE)
        if job is None or job["index_offset"] != offset:
            return False
        conn.execute(
            insert(index_embedding_batches).values(job_id=job_id, offset=offset, vectors=encoded)
        )
        if job["index_build_id"] is None:
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(index_build_id=token))
        return True


def _advance_build_once(engine, job_id, token, offset, next_offset, repair):
    with engine.begin() as conn:
        _, job = _publication_claim(conn, job_id, token, MISTRAL_INDEX_PROFILE)
        if job is None or job["index_offset"] != offset:
            return False
        job_service.requeue_progress(
            conn,
            job_id,
            index_offset=next_offset,
            index_repair_started=repair or job["index_repair_started"],
        )
        return True


def _stage_live(engine, job_id, token, index, job, encoded, projection, before_embed, check_claim):
    locked = job_service._publish_with_lock_retry
    offset = job["index_offset"]
    chunks = projection["chunks"]
    if not chunks:
        check_claim()
        build_id = projection["publication_id"]
        if index.verify_staged(build_id, offset):
            return locked(_activate_once, engine, job_id, token, MISTRAL_INDEX_PROFILE, build_id)
        if job["index_repair_started"]:
            job_service.fail_terminal(
                engine, job_id, token, "INDEX_STORAGE_INCOMPLETE", stage=INDEX_STAGE
            )
        else:
            locked(_advance_build_once, engine, job_id, token, offset, 0, True)
        return False

    if encoded is None:
        batch = next(embedding_batches([chunk["text"] for chunk in chunks]))
        vectors = index.embedding.embed(batch, before_batch=before_embed)
        if len(vectors) != len(batch):
            raise ValueError("Invalid embedding count")
        encoded = b"".join(_EMBEDDING_ROW.pack(*vector) for vector in vectors)
        if not locked(_save_batch_once, engine, job_id, token, offset, encoded):
            return False
    vectors = [list(vector) for vector in _EMBEDDING_ROW.iter_unpack(encoded)]
    if not 1 <= len(vectors) <= len(chunks):
        raise ValueError("Invalid saved embedding batch")
    check_claim()
    index.stage_vectors(**{**projection, "chunks": chunks[: len(vectors)]}, vectors=vectors)
    locked(_advance_build_once, engine, job_id, token, offset, offset + len(vectors), False)
    return False


def process(engine, job_id, token, index: SearchIndex, *, authorize=None) -> bool:
    profile = getattr(index, "profile", INDEX_PROFILE)
    selection = job_service._publish_with_lock_retry(
        _read_projection, engine, job_id, token, profile
    )
    if selection is None:
        return False
    source_id, job, cached_batch, projection = selection

    def check_claim():
        if not job_service._publish_with_lock_retry(
            _index_claim_is_live, engine, job_id, token, profile
        ):
            raise _ClaimLost

    def before_embed():
        check_claim()
        if (
            authorize(
                connector_id=projection["connector_id"],
                source_id=source_id,
                revision=projection["revision"],
                client_ids=tuple(projection["client_ids"]),
            )
            is not True
        ):
            raise _ProcessingDenied
        check_claim()

    try:
        if profile != INDEX_PROFILE and authorize is None:
            raise _ProcessingDenied
        if profile == MISTRAL_INDEX_PROFILE:
            return _stage_live(
                engine,
                job_id,
                token,
                index,
                job,
                cached_batch,
                projection,
                before_embed,
                check_claim,
            )
        # The adapter confirms all bounded batches under one immutable publication ID.
        if authorize is not None:
            index.stage(**projection, before_embed=before_embed)
        else:
            index.stage(**projection)
    except _ProcessingDenied:
        job_service.fail_terminal(engine, job_id, token, INDEX_PROCESSING_DENIED, stage=INDEX_STAGE)
        return False
    except _ClaimLost:
        return False
    except MistralError as error:
        if error.retryable:
            job_service.fail(
                engine, job_id, token, error.code, stage=INDEX_STAGE, retry_after=error.retry_after
            )
        else:
            job_service.fail_terminal(engine, job_id, token, error.code, stage=INDEX_STAGE)
        return False
    except Exception:
        job_service.fail(engine, job_id, token, INDEX_UNAVAILABLE, stage=INDEX_STAGE)
        return False
    return job_service._publish_with_lock_retry(_activate_once, engine, job_id, token, profile)
