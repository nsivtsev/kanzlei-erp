"""Durable extraction routing and page checkpoints against PostgreSQL."""

from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update

from kanzlei_ai import coordinator, outbox, sync, worker
from kanzlei_ai import jobs as job_service
from kanzlei_ai.documents.parse import Block, RetryableExtractionError
from kanzlei_ai.models import document_chunks, jobs, sources
from kanzlei_ai.search import publication

pytestmark = pytest.mark.integration


def _manifest(source="file:ocr", revision="a"):
    return {
        "version": 1,
        "source_id": source,
        "source_type": "file",
        "revision": revision * 64,
        "client_ids": ["synthetic-client"],
        "title": "Synthetic",
        "mime_type": "application/pdf",
        "source_modified_at": "2026-01-01",
        "locator": "/synthetic",
        "byte_size": 1,
        "active": True,
    }


def _inventory(database, *manifests, profile="paddle", pipeline_version=sync.PIPELINE_VERSION):
    epoch = sync.start_scan(database, "synthetic")
    assert sync.apply_inventory(
        database,
        "synthetic",
        epoch,
        None,
        {"items": list(manifests), "next_cursor": None, "changes_cursor": "w"},
        pipeline_version,
        ocr_profile=profile,
    )
    return epoch


def _jobs(database, stage="extract"):
    with database.connect() as conn:
        return (
            conn.execute(
                select(jobs, sources.c.source_id.label("external_id"))
                .join(sources)
                .where(jobs.c.stage == stage)
                .order_by(sources.c.source_id, jobs.c.generation)
            )
            .mappings()
            .all()
        )


def test_persisted_cutover_routes_only_new_revisions_and_explicit_requeue(database, monkeypatch):
    sync.initialize(database)
    original = _manifest()
    other = _manifest("file:other")
    _inventory(database, original, other)
    assert sync.cutover_ocr(database, "synthetic")
    assert not sync.cutover_ocr(database, "synthetic")

    _inventory(database, original, _manifest("file:other", "b"), profile="native")
    archive, previous, new = _jobs(database)
    assert [(item["generation"], item["ocr_profile"]) for item in (archive, previous, new)] == [
        (1, "paddle"),
        (1, "paddle"),
        (2, "mistral"),
    ]

    sent = []
    monkeypatch.setattr(
        coordinator.app, "send_task", lambda task, **kwargs: sent.append((task, kwargs))
    )
    assert coordinator._dispatch(database, SimpleNamespace(search_index_enabled=False)) == 3
    routes = {kwargs["args"][0]: (task, kwargs["queue"]) for task, kwargs in sent}
    assert routes[archive["id"]] == ("kanzlei_ai.extract_paddle", "ocr-paddle")
    assert routes[new["id"]] == ("kanzlei_ai.extract_mistral", "ocr-mistral")
    assert job_service.claim(database, archive["id"], ocr_profile="mistral") is None

    assert sync.requeue_extract(database, "synthetic", original["source_id"]) is not None
    assert sync.requeue_extract(database, "synthetic", original["source_id"]) is None
    _inventory(database, original, _manifest("file:other", "b"))
    archive, replacement, previous, new = _jobs(database)
    assert archive["status"] == "superseded"
    assert (replacement["generation"], replacement["ocr_profile"]) == (2, "mistral")
    assert (previous["generation"], new["generation"]) == (1, 2)


def test_explicit_pipeline_version_change_still_creates_new_generation(database):
    sync.initialize(database)
    original = _manifest()
    _inventory(database, original, profile="native")
    assert sync.cutover_ocr(database, "synthetic")
    _inventory(database, original, pipeline_version="pdf-explicit-v2")

    assert [(item["generation"], item["ocr_profile"]) for item in _jobs(database)] == [
        (1, "native"),
        (2, "native"),
    ]


def test_bootstrap_picks_up_previous_ocr_required_and_cutover_handles_reverted_revision(database):
    sync.initialize(database)
    original = _manifest()
    _inventory(database, original, profile="native")
    native = _jobs(database)[0]
    token = job_service.claim(database, native["id"], ocr_profile="native")
    assert job_service.fail_terminal(database, native["id"], token, "OCR_REQUIRED")
    _inventory(database, original, profile="paddle")
    assert sync.cutover_ocr(database, "synthetic")
    _inventory(database, _manifest(revision="b"))
    _inventory(database, original)
    assert [(row["generation"], row["ocr_profile"]) for row in _jobs(database)] == [
        (1, "native"),
        (2, "paddle"),
        (3, "mistral"),
        (4, "mistral"),
    ]


@pytest.fixture
def extraction(monkeypatch):
    from kanzlei_ai.documents import parse

    content = b"synthetic"
    parse_calls = []
    ocr_calls = []
    uploads = []

    @dataclass(frozen=True)
    class PlannedPdf(parse.ParsedPdf):
        ocr_pages: tuple[int, ...] = (2, 3)

    class Connector:
        async def read_background_source(self, _source):
            return content

    def parse_pdf(pdf, *, allow_missing_text=False):
        assert pdf == content
        assert allow_missing_text
        parse_calls.append(pdf)
        return PlannedPdf(
            "synthetic-parser",
            sha256(pdf).hexdigest(),
            3,
            [Block(1, "text", "Native text")],
            ocr_pages=(2, 3),
        )

    class Provider:
        profile = "paddle"

        def recognize_page(self, pdf, page):
            assert pdf == content
            ocr_calls.append(page)
            if page == 3 and ocr_calls.count(page) == 1:
                raise RetryableExtractionError()
            return [Block(page, "table", "| Betrag |\n| --- |\n| 120 EUR |")]

    def put(_namespace, payload):
        uploads.append(payload)
        return "synthetic/artifact"

    monkeypatch.setattr(worker, "_background_connector", lambda _settings: Connector())
    monkeypatch.setattr(parse, "parse_pdf", parse_pdf)
    monkeypatch.setattr(worker, "_artifact_store", lambda _settings: SimpleNamespace(put=put))
    settings = SimpleNamespace(
        ingestion_enabled=True, search_index_enabled=False, artifact_max_bytes=32_768
    )
    return SimpleNamespace(
        parse_calls=parse_calls,
        ocr_calls=ocr_calls,
        uploads=uploads,
        settings=settings,
        provider=lambda _settings, _profile: Provider(),
    )


def test_resume_after_page_failure_reuses_completed_pages_without_duplicate_publish(
    database,
    extraction,
):
    sync.initialize(database)
    _inventory(database, _manifest())
    job_id = _jobs(database)[0]["id"]

    def deliver():
        token = job_service.claim(database, job_id, ocr_profile="paddle")
        assert token
        return worker._extract_or_stage(
            database,
            job_id,
            token,
            extraction.settings,
            ocr_provider_factory=extraction.provider,
        )

    assert not deliver()  # Native planning is bounded independently of OCR.
    assert not deliver()  # Page 2 durably commits and queues the next portion.
    assert not deliver()  # Page 3 retry.
    assert job_service.claim(database, job_id, ocr_profile="paddle") is None
    with database.begin() as conn:
        conn.execute(update(jobs).values(retry_not_before=text("clock_timestamp()")))
    assert not deliver()  # Page 3 commits before a separate publication claim.
    assert deliver()
    assert not job_service.claim(database, job_id, ocr_profile="paddle")

    assert len(extraction.parse_calls) == 1
    assert extraction.ocr_calls == [2, 3, 3]
    assert len(extraction.uploads) == 1
    with database.connect() as conn:
        chunks = conn.execute(
            select(document_chunks.c.page, document_chunks.c.kind).order_by(document_chunks.c.page)
        ).all()
    assert chunks == [(1, "text"), (2, "table"), (3, "table")]


@pytest.mark.parametrize("invalidate", ["lease", "revision"])
def test_ocr_during_stale_claim_cannot_checkpoint_or_publish(database, extraction, invalidate):
    from kanzlei_ai import models

    sync.initialize(database)
    original = _manifest()
    _inventory(database, original)
    job_id = _jobs(database)[0]["id"]
    token = job_service.claim(database, job_id, ocr_profile="paddle")
    assert not worker._extract_or_stage(
        database,
        job_id,
        token,
        extraction.settings,
        ocr_provider_factory=extraction.provider,
    )
    token = job_service.claim(database, job_id, ocr_profile="paddle")

    def recognize_page(_content, page):
        if invalidate == "revision":
            _inventory(database, _manifest(revision="b"))
        else:
            with database.begin() as conn:
                conn.execute(
                    update(jobs).values(lease_until=text("clock_timestamp() - interval '1s'"))
                )
        return [Block(page, "text", "stale")]

    assert not worker._extract_or_stage(
        database,
        job_id,
        token,
        extraction.settings,
        ocr_provider_factory=lambda *_: SimpleNamespace(
            profile="paddle", recognize_page=recognize_page
        ),
    )
    with database.connect() as conn:
        assert conn.execute(select(models.ocr_page_checkpoints)).all() == []
        assert conn.execute(select(document_chunks)).all() == []
    assert extraction.uploads == []


def test_profile_reindex_uses_saved_chunks_and_can_skip_current_publications(database):
    import json

    import httpx
    from qdrant_client import QdrantClient

    from kanzlei_ai.providers.mistral_embeddings import MistralEmbeddingProvider
    from kanzlei_ai.search.index import SearchIndex

    sync.initialize(database)
    _inventory(database, _manifest())
    extract = _jobs(database)[0]
    token = job_service.claim(database, extract["id"], ocr_profile="paddle")
    assert job_service.publish_extraction(
        database,
        extract["id"],
        token,
        {
            "parser_version": "synthetic",
            "content_sha256": "c" * 64,
            "artifact_key": "synthetic/artifact",
            "page_count": 1,
            "chunks": [{"id": "chunk", "page": 1, "kind": "text", "ordinal": 0, "text": "saved"}],
        },
    )
    live = "mistral-embed-v1"
    assert publication.reindex(database, "synthetic", profile=live, missing_only=True).queued == 1
    assert publication.reindex(database, "synthetic", profile=live, missing_only=True).queued == 0
    index_job = _jobs(database, "index")[0]
    staged = []
    token = job_service.claim(database, index_job["id"], stage="index")
    assert not publication.process(
        database,
        index_job["id"],
        token,
        SimpleNamespace(profile="synthetic-v1", stage=lambda **data: staged.append(data)),
    )
    assert staged == []

    authorized = []

    def request_embedding(request):
        assert authorized
        payload = json.loads(request.content)
        assert payload["input"] == ["saved"]
        staged.append(payload)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1] * 1024}]})

    def authorize(**source):
        authorized.append(source)
        return True

    index = SearchIndex(
        client=QdrantClient(":memory:"),
        collection="mistral_fixture",
        profile=live,
        dimensions=1024,
        embedding=MistralEmbeddingProvider(
            endpoint="https://mistral.invalid",
            api_key="fixture-key",
            transport=httpx.MockTransport(request_embedding),
        ),
    )

    assert not publication.process(database, index_job["id"], token, index, authorize=authorize)
    token = job_service.claim(database, index_job["id"], stage="index")
    assert publication.process(database, index_job["id"], token, index, authorize=authorize)
    assert len(staged) == 1
    assert authorized[0]["source_id"] == "file:ocr"
    assert publication.reindex(database, "synthetic", profile=live, missing_only=True).queued == 0
    assert len(_jobs(database)) == 1


def test_native_default_routes_original_task_and_recovery_routes_ocr_profile(database):
    sync.initialize(database)
    _inventory(database, _manifest("file:native"), profile="native")
    epoch = sync.start_scan(database, "paddle-archive")
    assert sync.apply_inventory(
        database,
        "paddle-archive",
        epoch,
        None,
        {"items": [_manifest("file:paddle")], "next_cursor": None, "changes_cursor": "w"},
        ocr_profile="paddle",
    )
    native, paddle = _jobs(database)
    native_sent, ocr_sent = [], []
    assert (
        outbox.dispatch(
            database, native_sent.append, publish_ocr=lambda *args: ocr_sent.append(args)
        )
        == 2
    )

    with database.begin() as conn:
        conn.execute(update(jobs).values(updated_at=text("clock_timestamp() - interval '61s'")))
    assert outbox.recover(database) == 2
    assert (
        outbox.dispatch(
            database, native_sent.append, publish_ocr=lambda *args: ocr_sent.append(args)
        )
        == 2
    )
    assert native_sent == [native["id"], native["id"]]
    assert ocr_sent == [(paddle["id"], "paddle"), (paddle["id"], "paddle")]


def test_usable_native_pdf_never_constructs_selected_ocr_provider(
    database, extraction, monkeypatch
):
    from kanzlei_ai.documents import parse

    sync.initialize(database)
    _inventory(database, _manifest())
    job_id = _jobs(database)[0]["id"]
    token = job_service.claim(database, job_id, ocr_profile="paddle")

    @dataclass(frozen=True)
    class NativePdf(parse.ParsedPdf):
        ocr_pages: tuple[int, ...] = ()

    def parse_pdf(content, *, allow_missing_text):
        assert allow_missing_text
        return NativePdf("native-v1", sha256(content).hexdigest(), 1, [Block(1, "text", "native")])

    def reject_ocr(*_):
        pytest.fail("Usable native text triggered OCR")

    monkeypatch.setattr(parse, "parse_pdf", parse_pdf)
    assert worker._extract_or_stage(
        database, job_id, token, extraction.settings, ocr_provider_factory=reject_ocr
    )
    assert extraction.ocr_calls == []
    assert len(extraction.uploads) == 1


def test_embedding_policy_denial_fails_closed_before_staging(database):
    sync.initialize(database)
    _inventory(database, _manifest())
    extract = _jobs(database)[0]
    token = job_service.claim(database, extract["id"])
    live = "mistral-embed-v1"
    assert job_service.publish_extraction(
        database,
        extract["id"],
        token,
        {
            "parser_version": "synthetic",
            "content_sha256": "c" * 64,
            "artifact_key": "synthetic/artifact",
            "page_count": 1,
            "chunks": [],
        },
        enqueue_index=True,
        index_profile=live,
    )
    job_id = _jobs(database, "index")[0]["id"]
    token = job_service.claim(database, job_id, stage="index", pipeline_version=live)

    def reject_stage(**_):
        pytest.fail("Unauthorized index attempted to stage chunks")

    assert not publication.process(
        database,
        job_id,
        token,
        SimpleNamespace(profile=live, stage=reject_stage),
    )
    assert _jobs(database, "index")[0]["error_code"] == "INDEX_PROCESSING_DENIED"


def test_old_index_profile_cannot_activate_after_new_profile_queued(database):
    sync.initialize(database)
    _inventory(database, _manifest())
    extract = _jobs(database)[0]
    token = job_service.claim(database, extract["id"])
    assert job_service.publish_extraction(
        database,
        extract["id"],
        token,
        {
            "parser_version": "synthetic",
            "content_sha256": "c" * 64,
            "artifact_key": "synthetic/artifact",
            "page_count": 1,
            "chunks": [],
        },
        enqueue_index=True,
    )
    job_id = _jobs(database, "index")[0]["id"]
    token = job_service.claim(database, job_id, stage="index")

    def stage(**_):
        assert publication.reindex(database, "synthetic", profile="mistral-embed-v1").queued == 1

    assert not publication.process(
        database,
        job_id,
        token,
        SimpleNamespace(profile="synthetic-v1", stage=stage),
    )
    assert {item["pipeline_version"]: item["status"] for item in _jobs(database, "index")} == {
        "synthetic-v1": "superseded",
        "mistral-embed-v1": "queued",
    }


def test_embedding_authorization_runs_before_each_provider_batch(database):
    from kanzlei_ai.models import index_embedding_batches

    sync.initialize(database)
    _inventory(database, _manifest())
    extract = _jobs(database)[0]
    token = job_service.claim(database, extract["id"])
    live = "mistral-embed-v1"
    assert job_service.publish_extraction(
        database,
        extract["id"],
        token,
        {
            "parser_version": "synthetic",
            "content_sha256": "c" * 64,
            "artifact_key": "synthetic/artifact",
            "page_count": 1,
            "chunks": [
                {"id": f"chunk-{n}", "page": 1, "kind": "text", "ordinal": n, "text": "saved"}
                for n in range(33)
            ],
        },
        enqueue_index=True,
        index_profile=live,
    )
    job_id = _jobs(database, "index")[0]["id"]
    policy_checks, batches = [], []

    def authorize(**source):
        policy_checks.append(source)
        return len(policy_checks) == 1

    def embed(inputs, *, before_batch):
        before_batch()
        batches.append(len(inputs))
        return [[1] * 1024 for _ in inputs]

    index = SimpleNamespace(
        profile=live, embedding=SimpleNamespace(embed=embed), stage_vectors=lambda **_: None
    )
    for _ in range(2):
        token = job_service.claim(database, job_id, stage="index")
        assert token
        assert not publication.process(database, job_id, token, index, authorize=authorize)
    assert (
        policy_checks
        == [
            {
                "connector_id": "synthetic",
                "source_id": "file:ocr",
                "revision": "a" * 64,
                "client_ids": ("synthetic-client",),
            }
        ]
        * 2
    )
    assert batches == [32]
    assert _jobs(database, "index")[0]["error_code"] == "INDEX_PROCESSING_DENIED"
    with database.connect() as conn:
        assert conn.execute(select(index_embedding_batches)).all() == []


def test_mistral_ocr_denial_happens_before_external_page_call(database, extraction, monkeypatch):
    sync.initialize(database)
    sync.start_scan(database, "synthetic")
    sync.cutover_ocr(database, "synthetic")
    manifest = _manifest()
    manifest["client_ids"] = ["a-allowed", "b-denied"]
    _inventory(database, manifest)
    job_id = _jobs(database)[0]["id"]
    authorize_calls = []

    def deny(_settings, source):
        authorize_calls.append((source.source_id, source.client_id))
        return source.client_id == "a-allowed"

    monkeypatch.setattr(worker, "_authorize_ocr", deny, raising=False)

    for _ in range(2):  # Plan, then the first Mistral page.
        token = job_service.claim(database, job_id, ocr_profile="mistral")
        assert token
        assert not worker._extract_or_stage(
            database,
            job_id,
            token,
            extraction.settings,
            ocr_provider_factory=extraction.provider,
        )
    assert authorize_calls == [("file:ocr", "a-allowed"), ("file:ocr", "b-denied")]
    assert extraction.ocr_calls == []
    assert _jobs(database)[0]["error_code"] == "OCR_PROCESSING_DENIED"


def test_provider_retry_after_is_retained_in_the_durable_job(database):
    sync.initialize(database)
    _inventory(database, _manifest())
    job_id = _jobs(database)[0]["id"]
    token = job_service.claim(database, job_id, ocr_profile="paddle")
    assert job_service.fail(database, job_id, token, "OCR_RATE_LIMITED", retry_after=75)

    job = _jobs(database)[0]
    assert job["status"] == "queued"
    assert (job["retry_not_before"] - job["updated_at"]).total_seconds() == 75
    assert job_service.claim(database, job_id, ocr_profile="paddle") is None


def test_recovery_exhaustion_fails_current_ocr_source(database):
    sync.initialize(database)
    _inventory(database, _manifest())
    job_id = _jobs(database)[0]["id"]
    assert job_service.claim(database, job_id, ocr_profile="paddle")
    with database.begin() as conn:
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                attempts=job_service.MAX_ATTEMPTS,
                lease_until=text("clock_timestamp() - interval '1s'"),
            )
        )
    assert outbox.recover(database) == 0
    with database.connect() as conn:
        assert conn.execute(select(sources.c.status, sources.c.error_code)).one() == (
            "failed",
            "RETRY_EXHAUSTED",
        )
    assert _jobs(database)[0]["status"] == "failed"


def test_paddle_worker_consumes_only_its_queue(database, extraction, monkeypatch):
    import time
    from uuid import uuid4

    from celery.contrib.testing.worker import start_worker
    from redis import Redis

    from kanzlei_ai.settings import Settings

    sync.initialize(database)
    _inventory(database, _manifest("file:archive"))
    sync.cutover_ocr(database, "synthetic")
    _inventory(database, _manifest("file:archive"), _manifest("file:new"))
    redis_url = Settings().redis_url.get_secret_value()
    prefix = f"ocr_test_{uuid4().hex}_"
    redis = Redis.from_url(redis_url)
    monkeypatch.setitem(worker.app.conf, "broker_url", redis_url)
    monkeypatch.setitem(worker.app.conf, "broker_transport_options", {"global_keyprefix": prefix})
    monkeypatch.setattr(worker, "Settings", lambda: extraction.settings)
    monkeypatch.setattr(worker, "engine_from_settings", lambda _: database)
    handled = []

    def recognize_page(_content, page):
        handled.append(page)
        return [Block(page, "text", f"Synthetic {page}")]

    def provider(_settings, profile):
        assert profile == "paddle"
        return SimpleNamespace(profile="paddle-test", recognize_page=recognize_page)

    monkeypatch.setattr(worker, "_ocr_provider", provider)
    try:
        with (
            worker.app.connection_for_write() as connection,
            start_worker(
                worker.app, queues=["ocr-paddle"], perform_ping_check=False, loglevel="CRITICAL"
            ),
        ):
            # Other tests used the same app with a different broker key prefix.
            # Use this connection instead of reusing the cached producer pool.
            producer = worker.app.amqp.Producer(connection)
            send_task = worker.app.send_task
            monkeypatch.setattr(
                worker.app,
                "send_task",
                lambda *args, **kw: send_task(*args, producer=producer, **kw),
            )
            archive, cloud = _jobs(database)
            worker.app.send_task(
                "kanzlei_ai.extract_paddle", args=[archive["id"]], queue="ocr-paddle"
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                coordinator._dispatch(database, extraction.settings)
                archive, cloud = _jobs(database)
                if archive["status"] == "completed":
                    break
                time.sleep(0.05)
            assert archive["status"] == "completed"
            assert cloud["status"] == "queued"
            assert cloud["attempts"] == 0
            assert handled == [2, 3]
            assert len(extraction.uploads) == 1
    finally:
        for key in redis.scan_iter(match=prefix + "*"):
            redis.delete(key)
        redis.close()


def test_native_mode_keeps_text_available_on_a_mixed_pdf(database, extraction):
    sync.initialize(database)
    _inventory(database, _manifest(), profile="native")
    job_id = _jobs(database)[0]["id"]
    token = job_service.claim(database, job_id, ocr_profile="native")
    assert worker._extract_or_stage(
        database, job_id, token, extraction.settings, ocr_provider_factory=extraction.provider
    )
    assert extraction.ocr_calls == []
    with database.connect() as conn:
        assert conn.execute(select(document_chunks.c.page)).scalar_one() == 1


def test_long_embedding_resumes_paid_batches_and_activates_only_complete_projection(
    database, monkeypatch
):
    import json

    import httpx
    from qdrant_client import QdrantClient

    from kanzlei_ai.models import index_embedding_batches, index_publications
    from kanzlei_ai.providers.mistral_embeddings import MistralEmbeddingProvider
    from kanzlei_ai.search.index import SearchIndex

    sync.initialize(database)
    _inventory(database, _manifest())
    source_job = _jobs(database)[0]["id"]
    profile = "mistral-embed-v1"
    texts = [f"Synthetic chunk {n}" for n in range(225)]
    assert job_service.publish_extraction(
        database,
        source_job,
        job_service.claim(database, source_job),
        {
            "parser_version": "synthetic",
            "content_sha256": "c" * 64,
            "artifact_key": "synthetic/artifact",
            "page_count": 1,
            "chunks": [
                {"id": f"chunk-{n}", "page": 1, "kind": "text", "ordinal": n, "text": value}
                for n, value in enumerate(texts)
            ],
        },
        enqueue_index=True,
        index_profile=profile,
    )
    job_id = _jobs(database, "index")[0]["id"]
    elapsed = 0
    requests = []
    policy = []

    def authorize(**ref):
        policy.append(ref)
        return True

    def request_embedding(request):
        nonlocal elapsed
        elapsed += 9
        assert elapsed < 60
        items = json.loads(request.content)["input"]
        requests.append(items)
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [1] * 1024} for i in range(len(items))]}
        )

    client = QdrantClient(":memory:")
    index = SearchIndex(
        client=client,
        collection="long_fixture",
        profile=profile,
        dimensions=1024,
        embedding=MistralEmbeddingProvider(
            endpoint="https://mistral.invalid",
            api_key="fixture-key",
            transport=httpx.MockTransport(request_embedding),
        ),
    )
    upsert = client.upsert
    failures = [True]
    collection_lost = False

    def write(*args, **kwargs):
        result = upsert(*args, **kwargs)
        if failures:
            failures.pop()
            raise OSError("synthetic lost acknowledgement after Qdrant write")
        return result

    monkeypatch.setattr(client, "upsert", write)
    for _ in range(30):
        elapsed = 0
        with database.begin() as conn:
            conn.execute(update(jobs).values(retry_not_before=None))
        lease = job_service.claim(database, job_id, stage="index", pipeline_version=profile)
        assert lease
        completed = publication.process(database, job_id, lease, index, authorize=authorize)
        assert elapsed <= 9
        if completed:
            break
        with database.connect() as conn:
            assert conn.execute(select(index_publications)).all() == []
        if _jobs(database, "index")[0]["index_offset"] == len(texts) and not collection_lost:
            assert client.delete_collection("long_fixture")
            collection_lost = True
    else:
        pytest.fail("Long artifact never activated")

    assert [item for batch in requests for item in batch] == texts
    assert len(requests) == len(policy) == 8
    assert collection_lost
    assert client.count("long_fixture", exact=True).count == len(texts)
    with database.connect() as conn:
        assert conn.execute(select(index_embedding_batches)).all() == []


def test_embedding_response_cannot_checkpoint_a_replaced_source(database):
    from kanzlei_ai.models import index_embedding_batches, index_publications

    sync.initialize(database)
    _inventory(database, _manifest())
    extract_id = _jobs(database)[0]["id"]
    profile = "mistral-embed-v1"
    assert job_service.publish_extraction(
        database,
        extract_id,
        job_service.claim(database, extract_id),
        {
            "parser_version": "synthetic",
            "content_sha256": "c" * 64,
            "artifact_key": "synthetic/artifact",
            "page_count": 1,
            "chunks": [{"id": "chunk", "page": 1, "kind": "text", "ordinal": 0, "text": "saved"}],
        },
        enqueue_index=True,
        index_profile=profile,
    )

    def embed(texts, *, before_batch):
        before_batch()
        _inventory(database, _manifest(revision="b"))
        return [[1] * 1024 for _ in texts]

    def reject_stage(**_):
        pytest.fail("A stale embedding claimant wrote to the projection")

    index = SimpleNamespace(
        profile=profile, embedding=SimpleNamespace(embed=embed), stage_vectors=reject_stage
    )
    job_id = _jobs(database, "index")[0]["id"]
    token = job_service.claim(database, job_id, stage="index")
    assert not publication.process(database, job_id, token, index, authorize=lambda **_: True)
    with database.connect() as conn:
        assert conn.execute(select(index_embedding_batches)).all() == []
        assert conn.execute(select(index_publications)).all() == []


@pytest.mark.parametrize("action", ["replacement", "exhaustion", "requeue"])
def test_abandoned_index_jobs_delete_temporary_vectors(database, action):
    from kanzlei_ai.models import index_embedding_batches

    sync.initialize(database)
    _inventory(database, _manifest())
    extract = _jobs(database)[0]
    assert job_service.publish_extraction(
        database,
        extract["id"],
        job_service.claim(database, extract["id"]),
        {
            "parser_version": "synthetic",
            "content_sha256": "c" * 64,
            "artifact_key": "synthetic/artifact",
            "page_count": 1,
            "chunks": [],
        },
        enqueue_index=True,
        index_profile="mistral-embed-v1",
    )
    job_id = _jobs(database, "index")[0]["id"]
    with database.begin() as conn:
        conn.execute(index_embedding_batches.insert().values(job_id=job_id, offset=0, vectors=b"v"))
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                updated_at=text("clock_timestamp() - interval '2 minutes'"),
                attempts=job_service.MAX_ATTEMPTS,
            )
        )

    if action == "requeue":
        assert sync.requeue_extract(database, "synthetic", "file:ocr")
    else:
        if action == "replacement":
            _inventory(database, _manifest(revision="b"))
        outbox.recover(database, include_index=True)

    with database.connect() as conn:
        assert conn.execute(select(index_embedding_batches)).all() == []
    assert _jobs(database, "index")[0]["status"] == (
        "failed" if action == "exhaustion" else "superseded"
    )


@pytest.fixture
def failed_table_job(database, request):
    from kanzlei_ai.documents.parse import PARSER_VERSION
    from kanzlei_ai.providers.paddle_ocr import PADDLE_OCR_PROFILE

    sync.initialize(database)
    _inventory(database, _manifest())
    job_id = _jobs(database)[0]["id"]
    assert job_service.save_extraction_plan(
        database,
        job_id,
        job_service.claim(database, job_id),
        dict(
            parser_version=PARSER_VERSION,
            content_sha256="a" * 64,
            page_count=2,
            native_blocks=[dict(page=1, kind="text", text="Synthetischer Test")],
            ocr_pages=[2],
        ),
    )
    case = getattr(request, "param", "long")
    columns = ("Rechnung", "Beschreibung", "Betrag")
    rows = [(f"RE-2026-{i:04}", ("Prüfung " * 24).strip(), f"{i},00 EUR") for i in range(125)]
    error_code = "TABLE_LIMIT_EXCEEDED"
    padding = 1
    divider = 3
    if case in {"padded-row", "header-only"}:
        rows = rows[:1] if case == "padded-row" else []
        padding = 1000
        divider = 500
    elif case == "empty-header":
        columns = ("", "", "")
        error_code = "UNSUPPORTED_TABLE_STRUCTURE"
    divider_line = f"| {'-' * divider} | {'-' * divider} | {'-' * divider}: |"
    lines = [
        "| " + " | ".join(cell.ljust(padding) for cell in cells) + " |"
        for cells in [columns, *rows]
    ]
    lines.insert(1, divider_line)
    assert job_service.save_ocr_page(
        database,
        job_id,
        job_service.claim(database, job_id),
        dict(
            page=2,
            provider_profile=PADDLE_OCR_PROFILE,
            blocks=[dict(page=2, kind="table", text="\n".join(lines))],
        ),
    )
    assert job_service.fail_terminal(
        database, job_id, job_service.claim(database, job_id), error_code
    )
    return job_id, error_code, columns, rows


@pytest.mark.parametrize(
    "failed_table_job", ["long", "padded-row", "header-only", "empty-header"], indirect=True
)
def test_retry_table_publication_reuses_completed_pages_once(
    database, failed_table_job, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from kanzlei_ai.documents.pdf_table import parse_pdf_table
    from kanzlei_ai.models import document_artifacts

    job_id, error_code, columns, rows = failed_table_job

    def retry():
        return sync.retry_table_publication(database, "synthetic", job_id, ocr_profile="paddle")

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(retry), workers.submit(retry)]
        assert sorted(task.result() for task in results) == ["queued", "stale_job"]
    old, new = _jobs(database)
    assert old["status"] == "failed" and old["error_code"] == error_code
    assert new["generation"] == old["generation"] + 1

    deliveries = []
    outbox.dispatch(
        database,
        lambda _: pytest.fail("A Paddle retry must use the Paddle route"),
        publish_ocr=lambda job_id, profile: deliveries.append((job_id, profile)),
    )
    assert deliveries.count((new["id"], "paddle")) == 1

    def unexpected(*_):
        pytest.fail("A completed plan must publish without reading the PDF or running OCR")

    monkeypatch.setattr(worker, "_background_connector", unexpected)
    monkeypatch.setattr(
        worker, "_artifact_store", lambda _: SimpleNamespace(put=lambda *_: "synthetic/artifact")
    )
    assert worker._extract_or_stage(
        database,
        new["id"],
        job_service.claim(database, new["id"], ocr_profile="paddle"),
        SimpleNamespace(ingestion_enabled=True, search_index_enabled=False),
        ocr_provider_factory=unexpected,
    )
    assert retry() == "stale_job"
    with database.connect() as conn:
        assert (
            conn.execute(select(document_artifacts.c.generation)).scalar_one() == new["generation"]
        )
        chunks = (
            conn.execute(
                select(document_chunks)
                .where(document_chunks.c.kind == "table")
                .order_by(document_chunks.c.ordinal)
            )
            .mappings()
            .all()
        )
    assert len(chunks) > 1 if len(rows) > 1 else len(chunks) == 1
    reconstructed = []
    for chunk in chunks:
        assert chunk["page"] == 2 and len(chunk["text"]) <= 1800
        table = parse_pdf_table(chunk["text"])
        assert table.columns == columns
        reconstructed.extend(table.rows)
    assert reconstructed == rows


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "adapter",
        "parser",
        "pipeline",
        "profile",
        "revision",
        "mismatched-error",
        "other-error",
    ],
)
def test_retry_table_publication_skips_incompatible_job(database, failed_table_job, change):
    from kanzlei_ai.models import extraction_plans, ocr_page_checkpoints

    job_id, *_ = failed_table_job
    with database.begin() as conn:
        if change == "missing":
            conn.execute(ocr_page_checkpoints.delete())
        elif change == "adapter":
            conn.execute(ocr_page_checkpoints.update().values(provider_profile="old"))
        elif change == "parser":
            conn.execute(extraction_plans.update().values(parser_version="old"))
        elif change == "pipeline":
            conn.execute(jobs.update().values(pipeline_version="old"))
        elif change == "revision":
            conn.execute(sources.update().values(revision="b" * 64))
        elif change == "mismatched-error":
            conn.execute(jobs.update().values(error_code="UNSUPPORTED_TABLE_STRUCTURE"))
        elif change == "other-error":
            conn.execute(jobs.update().values(error_code="OCR_REQUIRED"))
            conn.execute(sources.update().values(error_code="OCR_REQUIRED"))
    result = sync.retry_table_publication(
        database, "synthetic", job_id, ocr_profile="native" if change == "profile" else "paddle"
    )
    assert (
        result
        == {
            "missing": "incomplete_checkpoints",
            "adapter": "incompatible_checkpoints",
            "parser": "incompatible_checkpoints",
            "pipeline": "incompatible_job",
            "profile": "incompatible_job",
            "revision": "stale_job",
            "mismatched-error": "incompatible_job",
            "other-error": "incompatible_job",
        }[change]
    )
    assert len(_jobs(database)) == 1


def test_operator_retry_command_reports_only_status(
    database, failed_table_job, monkeypatch, capsys
):
    import json
    import sys

    from kanzlei_ai.settings import Settings

    job_id, *_ = failed_table_job
    monkeypatch.setattr(sys, "argv", ["coordinator", "retry-table-publication", job_id])
    monkeypatch.setattr(coordinator, "engine_from_settings", lambda _: database)
    monkeypatch.setattr(
        coordinator,
        "Settings",
        lambda: Settings(
            ingestion_enabled=True,
            ingestion_connector_id="synthetic",
            ingestion_ocr_profile="paddle",
            model_profile="synthetic",
            mistral_enabled=False,
            chat_enabled=False,
            search_index_enabled=False,
        ),
    )
    coordinator.main()
    assert json.loads(capsys.readouterr().out) == {"status": "queued"}
    coordinator.main()
    assert json.loads(capsys.readouterr().out) == {"status": "skipped", "reason": "stale_job"}
