"""Celery tasks for fenced native-PDF extraction and indexing."""

import asyncio
import logging

from celery import Celery

from kanzlei_ai.db import engine_from_settings
from kanzlei_ai.jobs import (
    claim,
    complete,
    fail,
    fail_terminal,
    publish_extraction,
    source_for_claim,
)
from kanzlei_ai.settings import Settings

app = Celery("kanzlei_ai", broker=Settings().redis_url.get_secret_value())
app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=60,
    task_time_limit=90,
    broker_transport_options={
        "visibility_timeout": 300,
        "socket_connect_timeout": 3,
        "socket_timeout": 5,
        "retry_on_timeout": False,
    },
    broker_connection_timeout=3,
    task_publish_retry=False,
)


@app.task(name="kanzlei_ai.stage_metadata")
def stage_metadata(job_id):
    engine = None
    token = None
    try:
        settings = Settings()
        engine = engine_from_settings(settings)
        token = claim(engine, job_id)
        if token:
            _extract_or_stage(engine, job_id, token, settings)
    except Exception:
        if token and engine is not None:
            try:
                fail(engine, job_id, token)
            except Exception:
                pass  # Expired-lease recovery handles an unavailable database.
        logging.getLogger(__name__).error("Metadata staging failed; recovery will retry")
    finally:
        if engine is not None:
            engine.dispose()


@app.task(name="kanzlei_ai.index_document")
def index_document(job_id):
    from kanzlei_ai.search.index import SearchIndex
    from kanzlei_ai.search.publication import INDEX_STAGE, INDEX_UNAVAILABLE, process

    engine = None
    token = None
    try:
        settings = Settings()
        if not settings.search_index_enabled:
            return
        engine = engine_from_settings(settings)
        token = claim(engine, job_id, stage=INDEX_STAGE)
        if token:
            process(engine, job_id, token, SearchIndex.from_settings(settings))
    except Exception:
        if token and engine is not None:
            try:
                fail(engine, job_id, token, INDEX_UNAVAILABLE, stage=INDEX_STAGE)
            except Exception:
                pass  # Expired-lease recovery handles an unavailable database.
        logging.getLogger(__name__).error("Index staging failed; recovery will retry")
    finally:
        if engine is not None:
            engine.dispose()


def _background_connector(settings):
    from kanzlei_ai.connectors.frappe import FrappeConnector

    if not settings.erp_base_url or not settings.ingestion_key:
        raise RuntimeError("Background connector is not configured")
    return FrappeConnector(
        str(settings.erp_base_url),
        settings.ingestion_key,
        allow_insecure_http=settings.erp_allow_insecure_http,
        site_name=settings.erp_site_name,
        timeout_seconds=settings.erp_timeout_seconds,
    )


def _artifact_store(settings):
    from boto3 import client
    from botocore.config import Config

    from kanzlei_ai.documents.artifacts import S3ArtifactStore

    if not all(
        (
            settings.artifact_s3_endpoint,
            settings.artifact_s3_bucket,
            settings.artifact_s3_access_key,
            settings.artifact_s3_secret_key,
        )
    ):
        raise RuntimeError("Private artifact storage is not configured")
    return S3ArtifactStore(
        client(
            "s3",
            endpoint_url=str(settings.artifact_s3_endpoint),
            aws_access_key_id=settings.artifact_s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.artifact_s3_secret_key.get_secret_value(),
            region_name=settings.artifact_s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=3,
                read_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        ),
        bucket=settings.artifact_s3_bucket,
        prefix=settings.artifact_s3_prefix,
        max_bytes=settings.artifact_max_bytes,
    )


def _extract_or_stage(engine, job_id, token, settings):
    # Keep the previous metadata-only mode available for recovery tests. A
    # configured ingestion worker uses only its fixed private key, never a grant.
    if not settings.ingestion_enabled:
        return complete(engine, job_id, token)
    from kanzlei_ai.connectors.base import ConnectorError
    from kanzlei_ai.connectors.contracts import SourceRef
    from kanzlei_ai.documents.artifacts import LimitExceeded
    from kanzlei_ai.documents.chunk import ChunkInput, OversizedTable, deterministic_chunks
    from kanzlei_ai.documents.parse import ExtractionError, parse_pdf

    context = source_for_claim(engine, job_id, token)
    if context is None:
        return False
    manifest = context["manifest"]
    if manifest["mime_type"] != "application/pdf":
        fail_terminal(engine, job_id, token, "UNSUPPORTED_MEDIA")
        return False
    source = SourceRef(
        source_id=manifest["source_id"],
        revision=manifest["revision"],
        client_id=sorted(manifest["client_ids"])[0],
    )
    try:
        content = asyncio.run(_background_connector(settings).read_background_source(source))
        parsed = parse_pdf(content)
        chunks = deterministic_chunks(
            ChunkInput(
                connector_id=context["connector_id"],
                source_id=source.source_id,
                revision=source.revision,
                generation=context["generation"],
                parser_version=parsed.parser_version,
                content_sha256=parsed.content_sha256,
                blocks=[
                    {"page": item.page, "kind": item.kind, "text": item.text}
                    for item in parsed.blocks
                ],
            )
        )
        store = _artifact_store(settings)
        key = store.put(
            f"{context['connector_id']}/{source.source_id}/{source.revision}/{context['generation']}",
            parsed.artifact_bytes(),
        )
        published = publish_extraction(
            engine,
            job_id,
            token,
            {
                "parser_version": parsed.parser_version,
                "content_sha256": parsed.content_sha256,
                "artifact_key": key,
                "page_count": parsed.page_count,
                "chunks": [
                    {
                        "id": chunk.id,
                        "page": chunk.page,
                        "kind": chunk.kind,
                        "ordinal": chunk.ordinal,
                        "text": chunk.text,
                    }
                    for chunk in chunks
                ],
            },
            enqueue_index=settings.search_index_enabled,
        )
        # Immutable unreferenced objects are left for a retention sweep.  A
        # stale claimant must never delete an object another claimant published.
        return published
    except LimitExceeded:
        fail_terminal(engine, job_id, token, "ARTIFACT_LIMIT_EXCEEDED")
        return False
    except OversizedTable:
        fail_terminal(engine, job_id, token, "TABLE_LIMIT_EXCEEDED")
        return False
    except ConnectorError as error:
        if error.code == "LIMIT_EXCEEDED" and error.status == 413:
            fail_terminal(engine, job_id, token, "SOURCE_LIMIT_EXCEEDED")
        else:
            # 403 and 409 remain retryable: a scan/reconciliation can replace
            # the generation before a later delivery observes current state.
            fail(engine, job_id, token, error.code)
        return False
    except ExtractionError as error:
        if error.retryable:
            fail(engine, job_id, token, error.code)
        else:
            fail_terminal(engine, job_id, token, error.code)
        return False
