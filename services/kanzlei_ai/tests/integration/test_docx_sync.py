import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from sqlalchemy import select, update

from kanzlei_ai import coordinator, sync, worker
from kanzlei_ai import jobs as job_service
from kanzlei_ai.connectors.contracts import ChangePage, InventoryPage
from kanzlei_ai.models import document_artifacts, document_chunks, jobs, sources
from kanzlei_ai.settings import Settings

pytestmark = pytest.mark.integration

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _manifest(source_id, mime_type, **fields):
    return {
        "source_id": source_id,
        "source_type": source_id.split(":")[0],
        "revision": "a" * 64,
        "client_ids": ["fixture-client"],
        "title": "Fixture source",
        "mime_type": mime_type,
        "source_modified_at": "2026-11-01T12:00:00",
        "locator": "/fixture",
        "byte_size": 32,
        **fields,
    }


def _jobs(database):
    with database.connect() as conn:
        return {
            source: (job, profile, generation)
            for source, job, profile, generation in conn.execute(
                select(sources.c.source_id, jobs.c.id, jobs.c.ocr_profile, jobs.c.generation)
                .join(jobs, jobs.c.source_id == sources.c.id)
                .where(jobs.c.stage == "extract", jobs.c.generation == sources.c.generation)
            )
        }


def test_coordinator_opt_in_requeues_current_unsupported_docx_only(database, monkeypatch):
    sync.initialize(database)
    items = [
        _manifest("file:document", DOCX_MIME),
        _manifest("file:invalid-document", DOCX_MIME),
        _manifest("file:legacy-pdf", "application/pdf"),
        _manifest("email:legacy", "application/vnd.kanzlei.email+json"),
    ]

    class Connector:
        async def inventory(self, cursor):
            assert cursor is None
            return InventoryPage(items=items, next_cursor=None, changes_cursor="current-watermark")

    # Construct typed records as the real HTTP adapter does.
    items = InventoryPage.model_validate(
        {"items": items, "next_cursor": None, "changes_cursor": "current-watermark"}
    ).items
    monkeypatch.setattr(coordinator, "FrappeConnector", lambda *_args, **_kw: Connector())
    settings = Settings(
        _env_file=None,
        ingestion_enabled=True,
        ingestion_connector_id="fixture",
        ingestion_key="s" * 43,
        erp_base_url="https://erp.example.invalid",
        ingestion_ocr_profile="paddle",
    )
    sync.start_scan(database, "fixture")
    assert coordinator.sync_once(database, settings)
    original_jobs = _jobs(database)
    docx_job = original_jobs["file:document"][0]
    # Represent a pre-rollout DOCX job that was assigned an OCR profile.
    with database.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == docx_job).values(ocr_profile="paddle"))
    assert job_service.fail_terminal(
        database,
        docx_job,
        job_service.claim(database, docx_job, ocr_profile="paddle"),
        "UNSUPPORTED_MEDIA",
    )
    invalid_job = original_jobs["file:invalid-document"][0]
    assert job_service.fail_terminal(
        database,
        invalid_job,
        job_service.claim(database, invalid_job),
        "INVALID_DOCX",
    )
    sync.start_scan(database, "fixture")
    assert coordinator.sync_once(database, settings)
    assert _jobs(database)["file:document"] == (docx_job, "paddle", 1)

    assert sync.cutover_ocr(database, "fixture")
    monkeypatch.setenv("AI_INGESTION_DOCX_ENABLED", "true")
    enabled = Settings(
        _env_file=None,
        ingestion_enabled=True,
        ingestion_connector_id="fixture",
        ingestion_key="s" * 43,
        erp_base_url="https://erp.example.invalid",
        ingestion_ocr_profile="paddle",
    )
    sync.start_scan(database, "fixture")
    assert coordinator.sync_once(database, enabled)
    after = _jobs(database)
    assert after["file:document"][0] != docx_job
    assert after["file:document"][1:] == ("native", 2)
    for source in ("file:legacy-pdf", "email:legacy", "file:invalid-document"):
        assert after[source] == original_jobs[source]
    sync.start_scan(database, "fixture")
    assert coordinator.sync_once(database, enabled)
    assert _jobs(database) == after


@pytest.mark.parametrize(
    ("enabled", "oversize", "error"),
    [
        (True, False, None),
        (True, True, "ARTIFACT_LIMIT_EXCEEDED"),
        (False, False, "UNSUPPORTED_MEDIA"),
    ],
)
def test_docx_from_changes_has_native_routing_and_bounded_publish(
    database, monkeypatch, enabled, oversize, error
):
    sync.initialize(database)
    epoch = sync.start_scan(database, "fixture")
    assert sync.apply_inventory(
        database,
        "fixture",
        epoch,
        None,
        {"items": [], "next_cursor": None, "changes_cursor": "current"},
    )
    assert sync.cutover_ocr(database, "fixture")
    source = _manifest("file:attachment", DOCX_MIME, parent_message_id="email:parent")
    text = "Проверка 1.249,50 EUR"
    if oversize:
        text = " | ".join([text] * 60)
    doc = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = BytesIO()
    with ZipFile(buf, "w") as zip_file:
        zip_file.writestr("word/document.xml", doc)
    content = buf.getvalue()
    source["byte_size"] = len(content)

    class Connector:
        async def changes(self, cursor):
            assert cursor == "current"
            return ChangePage.model_validate(
                {
                    "items": [
                        {
                            "source_id": source["source_id"],
                            "client_id": "fixture-client",
                            "operation": "upsert",
                            "manifest": source,
                        }
                    ],
                    "next_cursor": "after",
                }
            )

        async def read_background_source(self, ref):
            assert enabled
            assert ref.source_id == source["source_id"]
            return content

    class Store:
        def put(self, key, artifact):
            assert error is None
            assert json.loads(artifact)["blocks"][0]["text"] == text
            return key

    def reject_pdf(*_args, **_kwargs):
        pytest.fail("DOCX entered the PDF pipeline")

    monkeypatch.setattr(coordinator, "FrappeConnector", lambda *_args, **_kw: Connector())
    monkeypatch.setattr(worker, "_background_connector", lambda _: Connector())
    monkeypatch.setattr(worker, "_artifact_store", lambda _: Store())
    monkeypatch.setattr(worker, "_process_extraction_portion", reject_pdf)
    settings = Settings(
        _env_file=None,
        ingestion_enabled=True,
        ingestion_docx_enabled=enabled,
        artifact_max_bytes=1024,
        search_index_enabled=True,
        ingestion_connector_id="fixture",
        ingestion_key="s" * 43,
        erp_base_url="https://erp.example.invalid",
    )
    assert coordinator.sync_once(database, settings)
    job, profile, generation = _jobs(database)[source["source_id"]]
    assert (profile, generation) == ("native", 1)
    token = job_service.claim(database, job, ocr_profile="native")
    assert worker._extract_or_stage(database, job, token, settings) is (error is None)
    with database.connect() as conn:
        if error:
            assert conn.execute(select(jobs.c.status, jobs.c.error_code)).one() == ("failed", error)
            assert conn.execute(select(document_artifacts.c.id)).all() == []
            return
        assert conn.execute(select(document_artifacts.c.page_count)).scalar_one() == 0
        assert conn.execute(select(document_chunks.c.page, document_chunks.c.text)).one() == (
            None,
            text,
        )
        assert conn.execute(select(jobs.c.status).where(jobs.c.stage == "index")).scalar_one() == (
            "queued"
        )
