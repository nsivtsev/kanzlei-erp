"""Read-only operator aggregates for one connector's durable SQL state."""

from datetime import UTC

from sqlalchemy import case, func, or_, select

from kanzlei_ai.models import (
    connectors,
    document_artifacts,
    index_publications,
    jobs,
    outbox,
    sources,
)
from kanzlei_ai.outbox import pending_delivery

_JOB_STATUSES = ("queued", "running", "completed", "failed", "superseded")
_SOURCE_STATUSES = ("queued", "awaiting_extraction", "extracted", "ready", "failed")
_OCR_PROFILES = ("native", "paddle", "mistral")
_CONNECTOR_PHASES = ("idle", "inventory", "changes")

# The failure writers validate the code's shape. Only known labels may leave SQL here,
# so legacy or unexpected values cannot leak private content or grow the report.
_ERROR_CODES = (
    "ARTIFACT_LIMIT_EXCEEDED",
    "CHUNK_LIMIT_EXCEEDED",
    "CONNECTOR_UNAVAILABLE",
    "DOCX_NO_TEXT",
    "DOCX_PACKAGE_LIMIT_EXCEEDED",
    "DOCX_TEXT_LIMIT_EXCEEDED",
    "DOCX_XML_LIMIT_EXCEEDED",
    "EMAIL_NO_TEXT",
    "ENCRYPTED_PDF",
    "EXTRACTION_UNAVAILABLE",
    "FORBIDDEN",
    "INDEX_PROCESSING_DENIED",
    "INDEX_RETRY_EXHAUSTED",
    "INDEX_STORAGE_INCOMPLETE",
    "INDEX_UNAVAILABLE",
    "INVALID_DOCX",
    "INVALID_EMAIL",
    "INVALID_EXTRACTION_PROFILE",
    "INVALID_PDF_PAGE",
    "INVALID_REQUEST",
    "MISTRAL_HTTP_ERROR",
    "MISTRAL_INPUT_LIMIT_EXCEEDED",
    "MISTRAL_INVALID_INPUT",
    "MISTRAL_INVALID_RESPONSE",
    "MISTRAL_RATE_LIMITED",
    "MISTRAL_REQUEST_GATE_UNAVAILABLE",
    "MISTRAL_RESPONSE_LIMIT_EXCEEDED",
    "MISTRAL_TIMEOUT",
    "MISTRAL_UNAVAILABLE",
    "OCR_IMAGE_LIMIT_EXCEEDED",
    "OCR_INVALID_PAGE",
    "OCR_INVALID_RESULT",
    "OCR_NO_TEXT",
    "OCR_PROCESSING_DENIED",
    "OCR_PROFILE_CHANGED",
    "OCR_PROVIDER_UNCONFIGURED",
    "OCR_REJECTED",
    "OCR_REQUIRED",
    "PAGE_LIMIT_EXCEEDED",
    "PARSE_INCOMPLETE",
    "PARSE_TIMEOUT",
    "RETRY_EXHAUSTED",
    "SOURCE_CONTENT_CHANGED",
    "SOURCE_LIMIT_EXCEEDED",
    "STALE_SOURCE",
    "TABLE_LIMIT_EXCEEDED",
    "UNAUTHENTICATED",
    "UNSUPPORTED_DOCX_STRUCTURE",
    "UNSUPPORTED_MEDIA",
    "UNSUPPORTED_TABLE_STRUCTURE",
)


def _utc(timestamp):
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z") if timestamp else None


def _age_seconds(now, timestamp):
    return max(0, int((now - timestamp).total_seconds())) if timestamp else None


def _known(column, values):
    return case((column.in_(values), column), else_="other")


def _count_by(conn, query, label):
    return dict(conn.execute(query.with_only_columns(label, func.count()).group_by(label)).all())


def _job_status(conn, query, profile, now):
    queued = jobs.c.status == "queued"
    running = jobs.c.status == "running"
    timing = (
        conn.execute(
            query.with_only_columns(
                func.min(jobs.c.created_at).filter(or_(queued, running)).label("oldest"),
                func.count().filter(running & (jobs.c.lease_until > now)).label("leased"),
                func.count().filter(running & (jobs.c.lease_until <= now)).label("expired_leases"),
                func.count()
                .filter(queued & (jobs.c.retry_not_before > now))
                .label("retry_delayed"),
            )
        )
        .mappings()
        .one()
    )
    profiles = conn.execute(
        query.with_only_columns(jobs.c.status, profile, func.count())
        .where(or_(queued, running))
        .group_by(jobs.c.status, profile)
    )
    by_profile = {"queued": {}, "running": {}}
    for status, route, count in profiles:
        by_profile[status][route] = count
    return {
        "by_status": _count_by(conn, query, _known(jobs.c.status, _JOB_STATUSES)),
        "queued_by_profile": by_profile["queued"],
        "running_by_profile": by_profile["running"],
        "error_codes": _count_by(
            conn,
            query.where(jobs.c.error_code.is_not(None)),
            _known(jobs.c.error_code, _ERROR_CODES),
        ),
        "oldest_unfinished_seconds": _age_seconds(now, timing["oldest"]),
        "leased": timing["leased"],
        "expired_leases": timing["expired_leases"],
        "retry_delayed": timing["retry_delayed"],
    }


def _outbox_status(conn, connector_id, now):
    available = or_(outbox.c.lease_until.is_(None), outbox.c.lease_until <= now)
    leased = outbox.c.lease_token.is_not(None)
    return dict(
        conn.execute(
            select(
                func.count().label("pending"),
                func.count().filter(available & ~leased).label("ready"),
                func.count().filter(available & leased).label("expired_leases"),
                func.count().filter(~available & leased).label("leased"),
                func.count().filter(~available & ~leased).label("retry_delayed"),
                func.count().filter(outbox.c.attempts > 0).label("with_publish_failures"),
            )
            .select_from(outbox.join(jobs).join(sources))
            .where(sources.c.connector_id == connector_id, pending_delivery())
        )
        .mappings()
        .one()
    )


def _coverage(conn, connector_id, index_profile):
    active_sources = select(sources.c.id).where(
        sources.c.active.is_(True), sources.c.connector_id == connector_id
    )
    current_artifacts = select(document_artifacts.c.id).where(
        document_artifacts.c.source_id == sources.c.id,
        document_artifacts.c.revision == sources.c.revision,
        document_artifacts.c.generation == sources.c.generation,
    )
    current_publication = (
        current_artifacts.join(
            index_publications, index_publications.c.artifact_id == document_artifacts.c.id
        )
        .where(
            index_publications.c.source_id == sources.c.id,
            index_publications.c.revision == sources.c.revision,
            index_publications.c.generation == sources.c.generation,
            index_publications.c.profile == index_profile,
        )
        .correlate(sources)
    )
    counts = dict(
        conn.execute(
            active_sources.with_only_columns(
                func.count().label("active_sources"),
                func.count().filter(current_artifacts.exists()).label("current_artifacts"),
                func.count()
                .filter(current_publication.exists())
                .label("current_index_publications"),
            )
        )
        .mappings()
        .one()
    )
    counts["without_current_index_publication"] = (
        counts["active_sources"] - counts["current_index_publications"]
    )
    counts["by_status"] = _count_by(
        conn, active_sources, _known(sources.c.status, _SOURCE_STATUSES)
    )
    counts["error_codes"] = _count_by(
        conn,
        active_sources.where(sources.c.error_code.is_not(None)),
        _known(sources.c.error_code, _ERROR_CODES),
    )
    return counts


def collect_status(conn, connector_id, *, index_profile):
    """Collect bounded aggregates on the caller's read-only snapshot connection."""
    now = conn.scalar(select(func.transaction_timestamp()))
    state = conn.execute(
        select(
            _known(connectors.c.phase, _CONNECTOR_PHASES),
            connectors.c.last_successful_poll_at,
            connectors.c.scan_completed_at,
        ).where(connectors.c.id == connector_id)
    ).one_or_none()
    phase, last_poll, last_scan = state or ("uninitialized", None, None)
    scoped_jobs = (
        select(jobs.c.id)
        .select_from(jobs.join(sources))
        .where(sources.c.connector_id == connector_id)
    )
    return {
        "as_of": _utc(now),
        "feed": {
            "phase": phase,
            "last_successful_poll_at": _utc(last_poll),
            "poll_lag_seconds": _age_seconds(now, last_poll),
            "last_scan_completed_at": _utc(last_scan),
        },
        "jobs": {
            "extract": _job_status(
                conn,
                scoped_jobs.where(jobs.c.stage == "extract"),
                _known(jobs.c.ocr_profile, _OCR_PROFILES),
                now,
            ),
            "index": _job_status(
                conn,
                scoped_jobs.where(jobs.c.stage == "index"),
                case((jobs.c.pipeline_version == index_profile, "configured"), else_="other"),
                now,
            ),
        },
        "outbox": _outbox_status(conn, connector_id, now),
        "coverage": _coverage(conn, connector_id, index_profile),
    }
