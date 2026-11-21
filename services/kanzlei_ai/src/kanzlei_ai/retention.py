"""Manual retention for private projections and completed ingestion records."""

from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
from functools import cached_property
from itertools import batched

from qdrant_client import QdrantClient
from sqlalchemy import and_, case, delete, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError

from kanzlei_ai.db import engine_from_settings
from kanzlei_ai.documents.artifacts import S3ArtifactStore
from kanzlei_ai.models import (
    connectors,
    document_artifacts,
    document_chunks,
    extraction_plans,
    inbox_items,
    index_embedding_batches,
    index_publications,
    jobs,
    ocr_page_checkpoints,
    outbox,
    page_receipts,
    retention_candidates,
    retention_mark,
    sources,
)
from kanzlei_ai.retention_storage import (
    ArtifactInventory,
    StorageError,
    UnownedObject,
    VectorInventory,
    canonical_uuid,
    scope_id,
)
from kanzlei_ai.search.index import INDEX_PROFILE, MISTRAL_INDEX_PROFILE
from kanzlei_ai.settings import Settings

RETENTION_LOCK_ID = 513_619_681_753_094_339
MAX_APPLY_LIMIT = 500
PROJECT_AGE = timedelta(days=30)
RECORD_AGE = timedelta(days=90)
_TERMINAL = ("completed", "failed", "superseded")
_PROJECTS = ("points", "publications", "artifacts", "objects")
_RECORDS = ("outbox", "jobs", "receipts")
_COUNTS = dict.fromkeys((*_PROJECTS, *_RECORDS), 0)
_C = retention_candidates.c


class RetentionError(RuntimeError):
    """An operator retention command could not proceed safely."""


def _clock(conn):
    return conn.scalar(select(func.clock_timestamp()))


@contextmanager
def _require_backend_confirmation():
    try:
        yield
    except Exception:
        raise StorageError("Backend operation was not confirmed") from None


@contextmanager
def _exclusive(engine):
    with engine.connect() as conn:
        if not conn.scalar(select(func.pg_try_advisory_lock(RETENTION_LOCK_ID))):
            raise RetentionError("Another retention maintenance command is running")
        try:
            conn.commit()
            yield conn
        finally:
            # The session lock covers all batch commits, including retry records.
            try:
                conn.rollback()
                if not conn.scalar(select(func.pg_advisory_unlock(RETENTION_LOCK_ID))):
                    conn.invalidate()
            except DBAPIError:
                conn.invalidate()


def _offline_guard(conn):
    try:
        # Fence writers between checking references and obtaining confirmation
        # from storage, including writers that do not use the maintenance lock.
        conn.execute(
            text(
                "LOCK TABLE connectors, sources, jobs, outbox, document_artifacts, "
                "document_chunks, index_publications, extraction_plans, ocr_page_checkpoints, "
                "index_embedding_batches, page_receipts, inbox_items "
                "IN SHARE ROW EXCLUSIVE MODE NOWAIT"
            )
        )
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "55P03":
            raise RetentionError("Retention apply requires idle ingestion tables") from None
        raise
    now = _clock(conn)
    for table in (jobs, outbox):
        if conn.scalar(
            select(
                select(table.c.id)
                .where(
                    or_(
                        table.c.lease_until > now,
                        and_(table.c.lease_token.is_not(None), table.c.lease_until.is_(None)),
                    )
                )
                .exists()
            )
        ):
            raise RetentionError("Retention apply refused because ingestion has live leases")


def _current(version):
    return (
        select(sources.c.id)
        .where(
            sources.c.id == version.c.source_id,
            sources.c.active,
            sources.c.revision == version.c.revision,
            sources.c.generation == version.c.generation,
        )
        .correlate(version)
        .exists()
    )


def _terminal():
    return and_(
        jobs.c.status.in_(_TERMINAL),
        jobs.c.stage.in_(("extract", "index")),
        jobs.c.lease_token.is_(None),
        jobs.c.lease_until.is_(None),
    )


def _unconfirmed_outbox():
    return (
        select(outbox.c.id)
        .where(
            outbox.c.job_id == jobs.c.id,
            or_(
                outbox.c.published_at.is_(None),
                outbox.c.lease_token.is_not(None),
                outbox.c.lease_until.is_not(None),
            ),
        )
        .correlate(jobs)
        .exists()
    )


def _stale_artifacts(*, for_apply):
    blocked = (
        select(jobs.c.id)
        .where(
            jobs.c.artifact_id == document_artifacts.c.id,
            or_(~_terminal(), _current(jobs), _unconfirmed_outbox()),
        )
        .correlate(document_artifacts)
        .exists()
    )
    query = select(document_artifacts).where(~_current(document_artifacts), ~blocked)
    if for_apply:
        query = query.where(
            ~select(index_publications.c.source_id)
            .where(index_publications.c.artifact_id == document_artifacts.c.id)
            .correlate(document_artifacts)
            .exists()
        )
    return query


def _stale_publications():
    return select(index_publications).where(~_current(index_publications))


def _protected_builds():
    # Synthetic builds use the running token; resumable builds have a durable ID.
    return select(jobs.c.index_build_id, jobs.c.lease_token).where(
        jobs.c.stage == "index", ~_terminal()
    )


def _protected_publications():
    return select(index_publications.c.publication_id).where(_current(index_publications))


def _sql_records(now):
    cutoff = now - RECORD_AGE
    terminal_job = (
        select(jobs.c.id)
        .where(jobs.c.id == outbox.c.job_id, _terminal())
        .correlate(outbox)
        .exists()
    )
    job_outbox = select(outbox.c.id).where(outbox.c.job_id == jobs.c.id).correlate(jobs).exists()
    phase, receipt = connectors.c, page_receipts.c
    outbox_ready = select(outbox.c.id).where(
        outbox.c.published_at <= cutoff,
        outbox.c.lease_token.is_(None),
        outbox.c.lease_until.is_(None),
        terminal_job,
    )
    jobs_ready = select(jobs.c.id).where(
        jobs.c.updated_at <= cutoff,
        _terminal(),
        or_(jobs.c.stage != "extract", ~_current(jobs)),
        ~job_outbox,
    )
    receipts_ready = (
        select(receipt.id)
        .join(connectors, phase.id == receipt.connector_id)
        .where(
            receipt.created_at <= cutoff,
            receipt.phase.in_(("inventory", "changes")),
            or_(
                receipt.epoch < phase.epoch,
                and_(
                    receipt.epoch == phase.epoch,
                    phase.phase == "changes",
                    or_(
                        and_(receipt.phase == "inventory", receipt.next_cursor.is_not(None)),
                        and_(
                            receipt.phase == "changes",
                            receipt.next_cursor.is_distinct_from(phase.changes_cursor),
                        ),
                    ),
                ),
            ),
        )
    )
    return {"outbox": outbox_ready, "jobs": jobs_ready, "receipts": receipts_ready}


def _candidate_key(kind, scope, target):
    return and_(_C.kind == kind, _C.scope == scope, _C.target == target)


def _latest_mark():
    return select(retention_mark.c.marked_at).scalar_subquery()


def _observed():
    return or_(_C.last_observed_at == _latest_mark(), _C.failure_code.is_not(None))


def _due_projects(now, scopes):
    query = select(retention_candidates).where(
        _observed(), _C.first_observed_at <= now - PROJECT_AGE
    )
    if scopes is not None:
        query = query.where(_C.scope.in_(scopes))
    # Counts and the batch itself use the same current SQL eligibility predicates.
    return query.where(
        or_(
            and_(
                _C.kind == "artifacts",
                _C.target.in_(
                    _stale_artifacts(for_apply=True).with_only_columns(
                        document_artifacts.c.id.cast(_C.target.type)
                    )
                ),
            ),
            and_(
                _C.kind == "publications",
                _C.target.in_(
                    _stale_publications().with_only_columns(index_publications.c.publication_id)
                ),
            ),
            _C.kind.in_(("objects", "points")),
        )
    )


def _plan(conn, scopes=None):
    now = _clock(conn)
    due = _due_projects(now, scopes).subquery()
    ready = _COUNTS.copy()
    ready.update(dict(conn.execute(select(due.c.kind, func.count()).group_by(due.c.kind)).all()))
    for kind, query in _sql_records(now).items():
        ready[kind] = conn.scalar(select(func.count()).select_from(query.subquery()))
    observed = select(_C.kind, func.count()).where(_observed()).group_by(_C.kind)
    retry = (
        select(func.count()).select_from(retention_candidates).where(_C.failure_code.is_not(None))
    )
    if scopes is not None:
        observed = observed.where(_C.scope.in_(scopes))
        retry = retry.where(_C.scope.in_(scopes))
    pending = dict.fromkeys(_PROJECTS, 0)
    for kind, count in conn.execute(observed):
        pending[kind] = count - ready[kind]
    marked_at = conn.scalar(select(retention_mark.c.marked_at))
    return {
        "last_mark_at": marked_at.isoformat() if marked_at else None,
        "ready": ready,
        "pending": pending,
        "retry_pending": conn.scalar(retry),
    }


def _identity(row, remote):
    sql = scope_id(str(row.source_id), str(row.generation), row.revision, row.artifact_key)
    return f"{sql}:{remote or ''}"


class Retention:
    def __init__(self, settings, engine, *, store=None, qdrant=None):
        self.settings = settings
        self.engine = engine
        self._store = store
        self._qdrant = qdrant
        self.connector = settings.ingestion_connector_id
        self._collections = {}
        for profile, name in (
            (INDEX_PROFILE, settings.search_collection),
            (MISTRAL_INDEX_PROFILE, settings.mistral_search_collection),
        ):
            self._collections[scope_id(str(settings.qdrant_url), self.connector, profile, name)] = (
                name,
                profile,
            )
        self._artifact_scope = None
        if settings.artifact_s3_bucket:
            self._artifact_scope = scope_id(
                str(settings.artifact_s3_endpoint),
                settings.artifact_s3_bucket,
                settings.artifact_s3_prefix.strip("/"),
            )
        self._scopes = {*self._collections}
        if self._artifact_scope:
            self._scopes.add(self._artifact_scope)

    @cached_property
    def _artifacts(self):
        return ArtifactInventory(self._store or S3ArtifactStore.from_settings(self.settings))

    @cached_property
    def _vectors(self):
        key = self.settings.qdrant_api_key
        client = self._qdrant or QdrantClient(
            url=str(self.settings.qdrant_url),
            api_key=key.get_secret_value() if key else None,
            timeout=3,
            check_compatibility=False,
        )
        return {
            scope: VectorInventory(client, name, profile, self.connector)
            for scope, (name, profile) in self._collections.items()
        }

    def _observations(self, conn):
        protected = set(conn.scalars(_protected_publications()))
        for build, token in conn.execute(_protected_builds()):
            protected.update((build, token))
        publications = conn.execute(
            _stale_publications().join(sources).where(sources.c.connector_id == self.connector)
        ).all()
        for scope, vector in self._vectors.items():
            for target, publication in vector.points():
                if publication not in protected:
                    yield "points", scope, target, publication
            for row in publications:
                if row.profile == vector.profile and canonical_uuid(row.publication_id):
                    yield "publications", scope, row.publication_id, ""
        if self._artifact_scope is not None:
            scope = self._artifact_scope
            stale = conn.execute(_stale_artifacts(for_apply=False)).all()
            for row in stale:
                if not self._artifacts.contains(row.artifact_key):
                    continue
                try:
                    remote = self._artifacts.identity(row.artifact_key)
                except UnownedObject:
                    continue
                yield "artifacts", scope, str(row.id), _identity(row, remote)
            referenced = set(conn.scalars(select(document_artifacts.c.artifact_key)))
            for target, identity in self._artifacts.objects(referenced):
                yield "objects", scope, target, identity

    def mark(self):
        """Record obsolete observations without deleting application data."""
        with _exclusive(self.engine) as conn, conn.begin():
            now = _clock(conn)
            try:
                for rows in batched(self._observations(conn), 250, strict=False):
                    statement = pg_insert(retention_candidates).values(
                        [
                            dict(
                                kind=kind,
                                scope=scope,
                                target=target,
                                identity=identity,
                                first_observed_at=now,
                                last_observed_at=now,
                            )
                            for kind, scope, target, identity in rows
                        ]
                    )
                    proposed = statement.excluded.identity
                    # An absent object can confirm a previous deletion whose
                    # response was lost. A different present object starts a new clock.
                    still_obsolete = and_(
                        _C.last_observed_at == _latest_mark(),
                        or_(
                            _C.identity == proposed,
                            and_(
                                _C.kind == "artifacts",
                                proposed.endswith(":"),
                                func.starts_with(_C.identity, proposed),
                            ),
                        ),
                    )
                    conn.execute(
                        statement.on_conflict_do_update(
                            index_elements=[_C.kind, _C.scope, _C.target],
                            set_={
                                "identity": case((still_obsolete, _C.identity), else_=proposed),
                                "first_observed_at": case(
                                    (still_obsolete, _C.first_observed_at), else_=now
                                ),
                                "last_observed_at": now,
                                "last_attempt_at": case((still_obsolete, _C.last_attempt_at)),
                                "failure_code": case((still_obsolete, _C.failure_code)),
                            },
                        )
                    )
            except DBAPIError:
                raise
            except Exception:
                raise RetentionError("Could not inventory private retention projections") from None
            conn.execute(
                pg_insert(retention_mark)
                .values(id=1, marked_at=now)
                .on_conflict_do_update(
                    index_elements=[retention_mark.c.id], set_={"marked_at": now}
                )
            )
            return _plan(conn, self._scopes)

    def plan(self):
        """Return SQL aggregates from the latest observations without backend calls."""
        return _read_plan(self.engine, self._scopes)

    def _apply_project(self, conn, row):
        if row.kind == "points":
            if (
                conn.scalar(
                    _protected_publications().where(
                        index_publications.c.publication_id == row.identity
                    )
                )
                or conn.execute(
                    _protected_builds()
                    .where(
                        or_(
                            jobs.c.index_build_id == row.identity,
                            jobs.c.lease_token == row.identity,
                        )
                    )
                    .limit(1)
                ).first()
            ):
                return "skipped"
            with _require_backend_confirmation():
                deleted = self._vectors[row.scope].delete(row.target, row.identity)
            return "completed" if deleted else "skipped"
        if row.kind == "publications":
            with _require_backend_confirmation():
                vector = self._vectors[row.scope]
            stale = conn.execute(
                _stale_publications()
                .join(sources)
                .where(
                    index_publications.c.publication_id == row.target,
                    index_publications.c.profile == vector.profile,
                    sources.c.connector_id == self.connector,
                )
            ).first()
            if not stale:
                return "skipped"
            with _require_backend_confirmation():
                empty = vector.publication_empty(row.target)
            if not empty:
                return "blocked"
            conn.execute(
                delete(index_publications).where(index_publications.c.publication_id == row.target)
            )
            return "completed"
        if row.kind == "objects":
            if conn.scalar(
                select(document_artifacts.c.id)
                .where(document_artifacts.c.artifact_key == row.target)
                .limit(1)
            ):
                return "skipped"
            with _require_backend_confirmation():
                deleted = self._artifacts.delete(row.target, row.identity)
            return "completed" if deleted else "skipped"
        return self._apply_artifact(conn, row)

    def _apply_artifact(self, conn, row):
        artifact = conn.execute(
            _stale_artifacts(for_apply=True).where(document_artifacts.c.id == int(row.target))
        ).first()
        if not artifact:
            return "blocked"
        sql_identity, remote_identity = row.identity.split(":")
        with _require_backend_confirmation():
            in_namespace = self._artifacts.contains(artifact.artifact_key)
        if not in_namespace or _identity(artifact, None) != f"{sql_identity}:":
            return "skipped"
        shared = conn.scalar(
            select(document_artifacts.c.id)
            .where(
                document_artifacts.c.artifact_key == artifact.artifact_key,
                document_artifacts.c.id != artifact.id,
            )
            .limit(1)
        )
        if not shared:
            with _require_backend_confirmation():
                deleted = self._artifacts.delete(artifact.artifact_key, remote_identity)
            if not deleted:
                return "skipped"
        # Only obsolete terminal jobs with confirmed dispatches can reach here.
        conn.execute(update(jobs).where(jobs.c.artifact_id == artifact.id).values(artifact_id=None))
        conn.execute(delete(document_chunks).where(document_chunks.c.artifact_id == artifact.id))
        conn.execute(delete(document_artifacts).where(document_artifacts.c.id == artifact.id))
        return "completed"

    def apply(self, *, confirm_offline, limit=100):
        """Apply a bounded batch while the external runtime and Paddle are offline."""
        if confirm_offline is not True:
            raise RetentionError("Retention apply requires --confirm-offline")
        if type(limit) is not int or not 1 <= limit <= MAX_APPLY_LIMIT:
            raise RetentionError(f"Retention limit must be between 1 and {MAX_APPLY_LIMIT}")
        counts = Counter(completed=0, failed=0, skipped=0, blocked=0)
        with _exclusive(self.engine) as conn:
            with conn.begin():
                _offline_guard(conn)
                counts["completed"] = _apply_sql(conn, limit)
                due = (
                    _due_projects(_clock(conn), self._scopes)
                    .order_by(
                        _C.last_attempt_at.asc().nulls_first(),
                        case(
                            dict(zip(_PROJECTS, range(len(_PROJECTS)), strict=True)), value=_C.kind
                        ),
                        _C.first_observed_at,
                        _C.scope,
                        _C.target,
                    )
                    .limit(limit - counts["completed"])
                )
                projects = conn.execute(due).all()
            for row in projects:
                with conn.begin():
                    _offline_guard(conn)
                    key = _candidate_key(row.kind, row.scope, row.target)
                    try:
                        outcome = self._apply_project(conn, row)
                    except StorageError:
                        outcome = "failed"
                    counts[outcome] += 1
                    if outcome in {"completed", "skipped"}:
                        conn.execute(delete(retention_candidates).where(key))
                    else:
                        conn.execute(
                            update(retention_candidates)
                            .where(key)
                            .values(
                                last_attempt_at=_clock(conn),
                                failure_code="BACKEND_UNCONFIRMED" if outcome == "failed" else None,
                            )
                        )
            remaining = limit - sum(counts.values())
            with conn.begin():
                if remaining:
                    key = tuple_(_C.kind, _C.scope, _C.target)
                    obsolete_marks = (
                        select(_C.kind, _C.scope, _C.target)
                        .where(~_observed())
                        .order_by(_C.last_observed_at, _C.kind, _C.scope, _C.target)
                        .limit(remaining)
                    )
                    counts["skipped"] += conn.execute(
                        delete(retention_candidates).where(key.in_(obsolete_marks))
                    ).rowcount
                return {**_plan(conn, self._scopes), **counts}


def _apply_sql(conn, limit):
    completed = 0
    for kind, query in _sql_records(_clock(conn)).items():
        ids = conn.scalars(query.order_by(query.selected_columns.id).limit(limit - completed)).all()
        if not ids:
            continue
        if kind == "outbox":
            conn.execute(delete(outbox).where(outbox.c.id.in_(ids)))
        elif kind == "jobs":
            for table in (ocr_page_checkpoints, extraction_plans, index_embedding_batches):
                conn.execute(delete(table).where(table.c.job_id.in_(ids)))
            conn.execute(delete(jobs).where(jobs.c.id.in_(ids)))
        else:
            conn.execute(delete(inbox_items).where(inbox_items.c.receipt_id.in_(ids)))
            conn.execute(delete(page_receipts).where(page_receipts.c.id.in_(ids)))
        completed += len(ids)
        if completed == limit:
            break
    return completed


def summary(engine):
    """Return lightweight SQL-only status without opening any external clients."""
    return _read_plan(engine)


def _read_plan(engine, scopes=None):
    with engine.begin() as conn:
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        return _plan(conn, scopes)


@contextmanager
def _open(settings):
    engine = engine_from_settings(settings)
    try:
        yield Retention(settings, engine)
    finally:
        engine.dispose()


def mark(settings: Settings) -> dict:
    """Record first observations in the configured private storage scope."""
    with _open(settings) as retention:
        return retention.mark()


def plan(settings: Settings) -> dict:
    """Return an aggregate dry run from PostgreSQL without backend calls."""
    with _open(settings) as retention:
        return retention.plan()


def apply(settings: Settings, *, confirm_offline: bool, limit: int = 100) -> dict:
    """Apply up to limit candidates after attesting that AI and Paddle are stopped."""
    with _open(settings) as retention:
        return retention.apply(confirm_offline=confirm_offline, limit=limit)
