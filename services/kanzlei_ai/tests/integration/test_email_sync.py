"""Typed feed pages enqueue email natively and preserve old PDF generations."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from kanzlei_ai import jobs as job_service
from kanzlei_ai import sync, worker
from kanzlei_ai.models import document_artifacts, document_chunks, jobs, sources

pytestmark = pytest.mark.integration


def _file():
    return {
        "version": 1,
        "source_type": "file",
        "source_id": "file:managed-pdf",
        "revision": "a" * 64,
        "client_ids": ["client-a", "client-b"],
        "title": "Document.pdf",
        "mime_type": "application/pdf",
        "source_modified_at": "2026-11-15T00:00:00",
        "locator": "/file",
        "byte_size": 40,
    }


def _inventory(engine, items):
    epoch = sync.start_scan(engine, "erp")
    assert sync.apply_inventory(
        engine, "erp", epoch, None, {"items": items, "next_cursor": None, "changes_cursor": "w"}
    )
    return epoch


def test_email_native_with_mistral_pdf_cutover_and_scope_removal(database):
    sync.initialize(database)
    pdf = _file()
    _inventory(database, [pdf])
    with database.connect() as conn:
        old_pdf_job = conn.execute(select(jobs.c.id)).scalar_one()
    assert job_service.publish_extraction(
        database,
        old_pdf_job,
        job_service.claim(database, old_pdf_job),
        {
            "parser_version": "docling-native-v1",
            "content_sha256": "d" * 64,
            "artifact_key": "synthetic/managed-pdf",
            "page_count": 1,
            "chunks": [{"id": "d" * 64, "page": 1, "kind": "text", "ordinal": 0, "text": "PDF 42"}],
        },
    )
    assert sync.cutover_ocr(database, "erp")
    email = {
        **pdf,
        "source_type": "email",
        "source_id": "email:message",
        "mime_type": "application/vnd.kanzlei.email+json",
        "title": "Information",
    }
    attachment = {**pdf, "source_id": "file:attachment", "parent_message_id": email["source_id"]}

    epoch = _inventory(database, [pdf, email, attachment])
    assert sync.apply_changes(
        database,
        "erp",
        epoch,
        "w",
        {
            "items": [
                {
                    "operation": "delete",
                    "source_id": email["source_id"],
                    "client_id": "client-b",
                    "manifest": None,
                }
            ],
            "next_cursor": "w2",
        },
    )
    with database.connect() as conn:
        rows = conn.execute(
            select(
                sources.c.source_id,
                jobs.c.id,
                jobs.c.ocr_profile,
                sources.c.manifest,
                sources.c.status,
            )
            .join(jobs, jobs.c.source_id == sources.c.id)
            .order_by(sources.c.source_id, jobs.c.generation)
        ).all()
        artifact_key = conn.execute(
            select(document_artifacts.c.artifact_key)
            .join(sources, document_artifacts.c.source_id == sources.c.id)
            .where(sources.c.active, sources.c.generation == document_artifacts.c.generation)
        ).scalar_one()
    assert [(r.source_id, r.ocr_profile) for r in rows] == [
        ("email:message", "native"),
        ("email:message", "native"),
        ("file:attachment", "mistral"),
        ("file:managed-pdf", "native"),
    ]
    assert rows[1].manifest["client_ids"] == ["client-a"]
    assert rows[2].manifest["parent_message_id"] == email["source_id"]
    assert rows[-1].id == old_pdf_job
    assert "parent_message_id" not in rows[-1].manifest
    assert rows[-1].status == "extracted"
    assert artifact_key == "synthetic/managed-pdf"


def test_mail_worker_publishes_nonpaginated_chunks_and_queues_index(database, monkeypatch):
    sync.initialize(database)
    source_id = "email:outgoing"
    content = json.dumps(
        {
            "version": 1,
            "subject": "Bestätigung",
            "sender": "ana@example.invalid",
            "recipients": ["archive@example.invalid"],
            "direction": "outgoing",
            "sent_at": None,
            "reply_to": "email:foreign",
            "body": "Freigegeben unter AZ-00615.\n> Fremde Akte AZ-00111",
            "body_format": "text",
        },
        ensure_ascii=False,
    ).encode()
    manifest = {
        **_file(),
        "source_id": source_id,
        "source_type": "email",
        "mime_type": "application/vnd.kanzlei.email+json",
        "byte_size": len(content),
    }
    _inventory(database, [manifest])
    with database.connect() as conn:
        job_id = conn.execute(select(jobs.c.id)).scalar_one()

    class Connector:
        async def read_background_source(self, source):
            assert source.source_id == source_id
            return content

    class Store:
        def put(self, key, artifact):
            assert json.loads(artifact)["blocks"] == [
                {"page": None, "kind": "text", "text": "Freigegeben unter AZ-00615."}
            ]
            return key

    monkeypatch.setattr(worker, "_background_connector", lambda _: Connector())
    monkeypatch.setattr(worker, "_artifact_store", lambda _: Store())
    token = job_service.claim(database, job_id, ocr_profile="native")
    assert worker._extract_or_stage(
        database,
        job_id,
        token,
        SimpleNamespace(
            ingestion_enabled=True, search_index_enabled=True, model_profile="synthetic"
        ),
    )
    with database.connect() as conn:
        assert conn.execute(select(document_artifacts.c.page_count)).scalar_one() == 0
        assert conn.execute(select(document_chunks.c.page, document_chunks.c.text)).one() == (
            None,
            "Freigegeben unter AZ-00615.",
        )
        assert conn.execute(select(jobs.c.status).where(jobs.c.stage == "index")).scalar_one() == (
            "queued"
        )
