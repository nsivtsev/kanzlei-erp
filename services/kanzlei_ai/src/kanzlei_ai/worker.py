"""Celery tasks for fenced source extraction and indexing."""

import asyncio
import logging
from dataclasses import asdict, replace
from functools import lru_cache
from hashlib import sha256

from celery import Celery

from kanzlei_ai.db import engine_from_settings
from kanzlei_ai.jobs import (
    MAX_CHUNKS,
    claim,
    complete,
    extraction_claim_is_live,
    fail,
    fail_terminal,
    publish_extraction,
    save_extraction_plan,
    save_ocr_page,
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
    _run_extraction(job_id, "native")


@app.task(name="kanzlei_ai.extract_paddle")
def extract_paddle(job_id):
    _run_extraction(job_id, "paddle")


@app.task(name="kanzlei_ai.extract_mistral")
def extract_mistral(job_id):
    _run_extraction(job_id, "mistral")


def _run_extraction(job_id, profile):
    engine = None
    token = None
    try:
        settings = Settings()
        if profile != "native" and not settings.ingestion_enabled:
            return
        if profile == "mistral" and not settings.mistral_enabled:
            return
        engine = engine_from_settings(settings)
        token = claim(engine, job_id, ocr_profile=profile)
        if token:
            _extract_or_stage(engine, job_id, token, settings)
    except Exception:
        if token and engine is not None:
            try:
                fail(engine, job_id, token)
            except Exception:
                pass  # Expired-lease recovery handles an unavailable database.
        logging.getLogger(__name__).error("Extraction task failed; recovery will retry")
    finally:
        if engine is not None:
            engine.dispose()


@app.task(name="kanzlei_ai.index_document")
def index_document(job_id):
    from kanzlei_ai.search.index import SearchIndex
    from kanzlei_ai.search.publication import (
        INDEX_STAGE,
        INDEX_UNAVAILABLE,
        configured_index_profile,
        process,
    )

    engine = None
    token = None
    try:
        settings = Settings()
        if not settings.search_index_enabled:
            return
        engine = engine_from_settings(settings)
        token = claim(
            engine, job_id, stage=INDEX_STAGE, pipeline_version=configured_index_profile(settings)
        )
        if token:
            process(
                engine,
                job_id,
                token,
                SearchIndex.from_settings(settings),
                authorize=_index_authorizer(settings),
            )
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


def _ocr_provider(settings, profile):
    from kanzlei_ai.documents.parse import TerminalExtractionError

    if profile == "paddle" and settings.paddle_model_dir and settings.paddle_docling_artifacts_dir:
        return _paddle_provider(settings.paddle_model_dir, settings.paddle_docling_artifacts_dir)
    if profile == "mistral" and settings.mistral_enabled:
        from kanzlei_ai.providers.mistral import MistralRequestGate
        from kanzlei_ai.providers.mistral_ocr import MistralOcrProvider

        return MistralOcrProvider(
            endpoint=settings.mistral_endpoint,
            api_key=settings.mistral_api_key.get_secret_value(),
            gate=MistralRequestGate.from_settings(settings),
        )
    raise TerminalExtractionError("OCR_PROVIDER_UNCONFIGURED")


@lru_cache(maxsize=1)
def _paddle_provider(model_dir, artifacts_dir):
    from kanzlei_ai.providers.paddle_ocr import PaddleOcrProvider

    return PaddleOcrProvider(model_dir, artifacts_path=artifacts_dir)


async def _allow_processing(connector, operation, refs):
    from kanzlei_ai.connectors.base import ConnectorError
    from kanzlei_ai.connectors.contracts import Scope

    if not refs:
        return False
    try:
        for ref in refs:
            await connector.check_processing_policy(
                operation, Scope(kind="client", client_id=ref.client_id), [ref]
            )
    except ConnectorError as error:
        if error.status in {401, 403, 404, 409}:
            return False
        raise
    return True


def _authorize_ocr(settings, source):
    return settings.mistral_enabled and asyncio.run(
        _allow_processing(_background_connector(settings), "ocr", [source])
    )


def _index_authorizer(settings):
    if settings.model_profile != "mistral":
        return None

    from kanzlei_ai.connectors.contracts import SourceRef

    def authorize(*, connector_id, source_id, revision, client_ids):
        if connector_id != settings.ingestion_connector_id:
            return False
        refs = [
            SourceRef(client_id=client_id, source_id=source_id, revision=revision)
            for client_id in client_ids
        ]
        return asyncio.run(_allow_processing(_background_connector(settings), "embed", refs))

    return authorize


def _artifact_store(settings):
    from kanzlei_ai.documents.artifacts import S3ArtifactStore

    return S3ArtifactStore.from_settings(settings)


def _process_extraction_portion(engine, job_id, token, settings, source, context, factory):
    from kanzlei_ai.connectors.base import ConnectorError
    from kanzlei_ai.documents.artifacts import LimitExceeded
    from kanzlei_ai.documents.parse import Block, ParsedPdf, TerminalExtractionError, parse_pdf

    plan = context["plan"]
    profile = context["ocr_profile"]
    if plan is None:
        content = asyncio.run(_background_connector(settings).read_background_source(source))
        parsed = parse_pdf(content, allow_missing_text=True)
        if profile == "native":
            if not parsed.blocks:
                raise TerminalExtractionError("OCR_REQUIRED")
            return parsed
        if not parsed.ocr_pages:
            return parsed
        if len(parsed.artifact_bytes()) > settings.artifact_max_bytes:
            raise LimitExceeded
        save_extraction_plan(
            engine,
            job_id,
            token,
            {
                "parser_version": parsed.parser_version,
                "content_sha256": parsed.content_sha256,
                "page_count": parsed.page_count,
                "native_blocks": [asdict(block) for block in parsed.blocks],
                "ocr_pages": list(parsed.ocr_pages),
            },
        )
        return None

    checkpoints = context["checkpoints"]
    blocks = [Block(**block) for block in plan["native_blocks"]]
    for checkpoint in checkpoints:
        blocks.extend(Block(**block) for block in checkpoint["blocks"])
    parsed = ParsedPdf(plan["parser_version"], plan["content_sha256"], plan["page_count"], blocks)
    completed = {item["page"] for item in checkpoints}
    pending = [page for page in plan["ocr_pages"] if page not in completed]
    if pending:
        content = asyncio.run(_background_connector(settings).read_background_source(source))
        if sha256(content).hexdigest() != parsed.content_sha256:
            raise TerminalExtractionError("SOURCE_CONTENT_CHANGED")
        if not extraction_claim_is_live(engine, job_id, token):
            return None
        provider = factory(settings, profile)
        adapter_profile = provider.profile
        if not isinstance(adapter_profile, str) or not 1 <= len(adapter_profile) <= 100:
            raise TerminalExtractionError("OCR_PROVIDER_UNCONFIGURED")
        if checkpoints and adapter_profile != checkpoints[0]["provider_profile"]:
            raise TerminalExtractionError("OCR_PROFILE_CHANGED")
        page = pending[0]
        if profile == "mistral":

            def authorize_page():
                for client_id in context["manifest"]["client_ids"]:
                    ref = source.model_copy(update={"client_id": client_id})
                    if _authorize_ocr(settings, ref) is not True:
                        raise TerminalExtractionError("OCR_PROCESSING_DENIED")
                if not extraction_claim_is_live(engine, job_id, token):
                    raise ConnectorError("STALE_SOURCE", 409)

            authorize_page()
            recognized = provider.recognize_page(content, page, before_request=authorize_page)
        else:
            recognized = provider.recognize_page(content, page)
        if not isinstance(recognized, list) or any(
            not isinstance(block, Block)
            or block.page != page
            or block.kind not in ("text", "table")
            or not isinstance(block.text, str)
            or "\x00" in block.text
            for block in recognized
        ):
            raise TerminalExtractionError("OCR_INVALID_RESULT")
        page_blocks = [block for block in recognized if block.text.strip()]
        parsed = replace(parsed, blocks=blocks + page_blocks)
        if len(parsed.artifact_bytes()) > settings.artifact_max_bytes:
            raise LimitExceeded
        save_ocr_page(
            engine,
            job_id,
            token,
            {
                "page": page,
                "provider_profile": adapter_profile,
                "blocks": [asdict(block) for block in page_blocks],
            },
        )
        return None
    return replace(parsed, blocks=sorted(parsed.blocks, key=lambda block: block.page))


def _extract_or_stage(engine, job_id, token, settings, *, ocr_provider_factory=None):
    # Keep the previous metadata-only mode available for recovery tests. A
    # configured ingestion worker uses only its fixed private key, never a grant.
    if not settings.ingestion_enabled:
        return complete(engine, job_id, token)
    from kanzlei_ai.connectors.base import ConnectorError
    from kanzlei_ai.connectors.contracts import EMAIL_MIME_TYPE, SourceRef
    from kanzlei_ai.documents.artifacts import LimitExceeded
    from kanzlei_ai.documents.chunk import ChunkInput, OversizedTable, deterministic_chunks
    from kanzlei_ai.documents.docx import DOCX_MIME_TYPE, parse_docx
    from kanzlei_ai.documents.parse import ExtractionError, TerminalExtractionError
    from kanzlei_ai.search.publication import configured_index_profile

    context = source_for_claim(engine, job_id, token)
    if context is None:
        return False
    manifest = context["manifest"]
    mime_type = manifest["mime_type"]
    is_email = manifest.get("source_type") == "email" and mime_type == EMAIL_MIME_TYPE
    is_docx = mime_type == DOCX_MIME_TYPE and getattr(settings, "ingestion_docx_enabled", False)
    if not (is_email or is_docx) and mime_type != "application/pdf":
        fail_terminal(engine, job_id, token, "UNSUPPORTED_MEDIA")
        return False
    source = SourceRef(
        source_id=manifest["source_id"],
        revision=manifest["revision"],
        client_id=sorted(manifest["client_ids"])[0],
    )
    try:
        artifact = None
        if is_email or is_docx:
            from kanzlei_ai.documents.email import parse_email

            if context["ocr_profile"] != "native":
                raise TerminalExtractionError("INVALID_EXTRACTION_PROFILE")
            content = asyncio.run(_background_connector(settings).read_background_source(source))
            parsed = parse_docx(content) if is_docx else parse_email(content)
            if is_docx:
                artifact = parsed.artifact_bytes()
                if len(artifact) > settings.artifact_max_bytes:
                    raise LimitExceeded
        else:
            parsed = _process_extraction_portion(
                engine,
                job_id,
                token,
                settings,
                source,
                context,
                ocr_provider_factory or _ocr_provider,
            )
        if parsed is None:
            return False
        if not parsed.blocks:
            raise TerminalExtractionError("OCR_NO_TEXT")
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
            ),
            split_pdf_tables=mime_type == "application/pdf",
        )
        if len(chunks) > MAX_CHUNKS:
            raise TerminalExtractionError("CHUNK_LIMIT_EXCEEDED")
        if not extraction_claim_is_live(engine, job_id, token):
            return False
        store = _artifact_store(settings)
        key = store.put(
            f"{context['connector_id']}/{source.source_id}/{source.revision}/{context['generation']}",
            parsed.artifact_bytes() if artifact is None else artifact,
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
                        "table_fragment": chunk.table_fragment,
                    }
                    for chunk in chunks
                ],
            },
            enqueue_index=settings.search_index_enabled,
            index_profile=configured_index_profile(settings)
            if settings.search_index_enabled
            else None,
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
            retry_after = getattr(error, "retry_after", None)
            if retry_after is None:
                fail(engine, job_id, token, error.code)
            else:
                fail(engine, job_id, token, error.code, retry_after=retry_after)
        else:
            fail_terminal(engine, job_id, token, error.code)
        return False
