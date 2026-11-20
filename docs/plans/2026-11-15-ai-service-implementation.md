# Independent AI service implementation plan

> **For the implementing agent:** Use superpowers:executing-plans to implement this plan task by task. Complete and review the first milestone before expanding the corpus.

**Goal:** Deliver a standalone AI chat for ERP email and documents, beginning with one complete Drive PDF to cited-answer scenario.

**Architecture:** Keep the AI application in `services/kanzlei_ai/`, with an independent runtime, database, broker, workers and deployment. A small versioned ERP connector provides source changes, content and current user authorization. PostgreSQL owns durable state; Qdrant is a rebuildable projection.

**Tech Stack:** Python, FastAPI, Pydantic, Uvicorn, PostgreSQL, SQLAlchemy, Alembic, Qdrant, Celery, Redis, Docling and private S3-compatible artifacts.

**Design:** [Independent AI service architecture](2026-11-15-ai-service-design.md). The architecture and monorepo location are accepted; login details, frontend framework and production model providers remain explicit decisions within their tasks. This plan does not authorize a production rollout or processing real documents with a new provider.

---

## Execution conventions

All listed service paths are repository-relative under `services/kanzlei_ai/`; connector paths belong to the existing Frappe app. Use the current checkout and preserve unrelated work. Do not add AI dependencies to the ERP root `pyproject.toml` or modify ERP deployment by default.

For each behavior task: (1) add the named regression test; (2) run its narrow command and verify the expected assertion fails for the intended reason; (3) implement the smallest coherent behavior; (4) rerun affected checks and inspect the diff; (5) checkpoint the reviewed change in a focused commit. Split the cases into individual small steps while executing. Missing packages or broken fixtures are not a successful red test. Do not write implementation-mirroring tests for configuration-only edits.

Service commands below run from `services/kanzlei_ai/` after bootstrap. Integration tests must use real disposable PostgreSQL/Qdrant/Redis instances, not production services; queue tests must include a real worker rather than only Celery eager mode. Test commands describe future acceptance and have not been run while writing this plan.

ERP command prefix from the repository root:

```bash
docker compose --env-file .env -f .devcontainer/docker-compose.yml exec -T frappe bash -lc 'cd /workspace/development/frappe-bench && bench --site development.localhost run-tests --app kanzlei_erp --module kanzlei_erp.tests.test_ai_connector'
```

## Task 1: Service bootstrap and dependency compatibility

**Create:** `services/kanzlei_ai/pyproject.toml`, `uv.lock`, `Dockerfile`, `compose.yml`, `.env.example`, `README.md`, `.gitignore`, `src/kanzlei_ai/__init__.py`, `src/kanzlei_ai/app.py`, `src/kanzlei_ai/settings.py`, `tests/test_health.py`.

1. Define `/health/live` without dependency checks and `/health/ready` with bounded dependency probes. Add tests that distinguish startup health, unavailable database and unavailable search.
2. Run `uv run pytest tests/test_health.py -q`; verify the missing health behavior fails, then implement it with injected probes.
3. Lock mutually compatible Python/packages and container images. Smoke-test Docling and the database/search clients on the local ARM64 environment. Separate CPU-heavy worker dependencies from the API runtime where useful.
4. Run `docker compose config --quiet`, then the service's own image build and health smoke check. This is a new service build, not the full ERP CI image build. Bind local ports to loopback and keep credentials in ignored configuration.
5. Verify the service starts with ERP absent and contains no Frappe imports. Document start/stop and test dependency setup. Commit this task independently.

**Expected:** Independent application skeleton; no ingestion and no production provider calls.

**Result on 2026-11-15:** Implemented in `services/kanzlei_ai/`, including a separate health-probe module, idle Celery entry point and opt-in real-dependency test. The API and Docling worker images build on Linux ARM64; Docling and CPU PyTorch import with networking disabled. API startup without ERP or any infrastructure returns live 200 and ready 503. Real HTTP checks cover successful readiness, individual PostgreSQL/Qdrant/Redis outages and recovery; liveness remains 200 throughout. The worker answers a targeted Celery ping after broker reconnection. A real PostgreSQL connection test reproduced and fixed reserved-character password handling by passing the password separately from the URI. Runtime and document dependencies are locked; the Linux lock selects CPU wheels without CUDA dependencies. Durable tasks, ingestion, ERP authentication and model-quality evaluation remain in subsequent stages. See the service README for exact local commands.

## Task 2: Versioned connector and trusted identity contract

**Create:** `services/kanzlei_ai/src/kanzlei_ai/connectors/contracts.py`, `connectors/base.py`, `connectors/frappe.py`, `auth.py`, `tests/test_connector_contract.py`, `tests/test_auth.py`; `kanzlei_erp/ai_connector/__init__.py`, `api.py`, `policy.py`, `kanzlei_erp/tests/test_ai_connector.py`.

1. Define typed manifests, revision conflicts, cursor pages, scoped batch authorization and content responses. Reject unknown contract versions, oversized batches and user-supplied authoritative role fields.
2. Resolve the login handoff: specify trusted issuer, audience, subject mapping, expiration, replay protection, signing/key rotation and logout behavior. A signed short-lived ERP handoff is a candidate; shared OIDC is an alternative. Make the chosen flow concrete before implementing user-facing access. Keep synthetic fake identity explicitly development-only.
3. Add tests for forged/expired/wrong-audience identity, service credential misuse, unauthorized general scope, cross-client source IDs, and a revoked user. Run `uv run pytest tests/test_connector_contract.py tests/test_auth.py -q` and the ERP command above; verify intended red failures.
4. Implement bounded connector operations using current ERP permission and relationship checks. Use `object_storage.open_file` only after source membership/read checks. Return no source metadata on denied requests. Do not expose arbitrary impersonation or generic DB access.
5. Run the targeted service and ERP cases. Resolve the frontend framework before Task 7; a small independent Vite/TypeScript client is sufficient unless existing requirements justify more. Commit the contract and adapter boundary.

**Expected:** A verified identity can enumerate only permitted clients and read an eligible synthetic source at the expected revision.

**Result on 2026-11-15:** Implemented the versioned ERP adapter and independent HTTP client. The chosen handoff uses a 60-second opaque, single-use ERP ticket and a 300-second session-bound grant; every operation checks current ERP identity and permissions online. Service credentials cannot choose a user. Configuration/key rotation is read without the ordinary Frappe configuration cache. Client/document pagination, scoped batch authorization and original-file retrieval are bounded; denied sources expose no metadata. Reads verify revisions, including managed S3 bindings, and reject a replacement committed by another connection. Existing ordinary downloads retain their all-or-nothing authorization. The connector is disabled by default; the independent frontend will use Vite/TypeScript, with state/verifier binding required before automatic browser handoff. See the [implemented contract](../../services/kanzlei_ai/docs/erp-connector.md).

Acceptance: 31 service tests (including the real infrastructure check), 21 ERP connector tests, 17 existing Mandant Documents tests and 23 Posteingang document tests passed. The real HTTP scenario covers single-use exchange, client isolation, batch authorization, original bytes, permission revocation and ERP logout; temporary records/configuration are cleaned up. The API image builds and its live/ready endpoints return 200. Existing Mandant Drive tests report 19 fixture errors because Frappe rejects their `/local/.../drive/...` paths; a representative failure reproduces with the original document helper, independently of this adapter. Durable inventory/feed, unattended ingestion identity and chat remain later tasks.

## Task 3: Durable Drive inventory and change feed

**Create:** `kanzlei_erp/ai_connector/events.py`, `kanzlei_erp/ai_connector/inventory.py`, `kanzlei_erp/kanzlei_erp/doctype/ai_source_event/{__init__.py,ai_source_event.py,ai_source_event.json}`, `kanzlei_erp/tests/test_ai_connector_events.py`.

**Modify:** `kanzlei_erp/hooks.py` additively; `kanzlei_erp/suite_file.py` or `object_storage.py` only where an actual uncovered mutation path requires a hook. Preserve all existing guards and notifications.

1. Add real transaction tests for rollback, duplicate changes, deletion, trash/restore, replacement and reversed commit order. Include source bytes changed through storage rebinding and active ancestry changed without changing each descendant.
2. Run the new ERP module using the command prefix with `--module kanzlei_erp.tests.test_ai_connector_events`; expect the missing durable-feed behavior to fail.
3. Implement same-transaction event recording and commit-safe cursor ordering. Add initial inventory with watermark/overlap, opaque bounded cursors and tombstones. Audit direct `db_insert`/`db_set` paths instead of assuming `on_update` covers them.
4. Rerun the feed tests, connector tests and directly affected Drive/storage tests. Broader checks are warranted here because shared File hooks and storage mutation coverage can affect existing downloads and permissions.
5. Inspect rollback/commit behavior and ensure no network calls happen in the File transaction. Commit independently.

**Expected:** A stopped consumer can resume without skipping committed changes or seeing rolled-back sources.

**Result on 2026-11-16:** Local Drive ingestion is deferred because Suite's local
WebDAV failed-promotion repair performs a raw metadata commit outside File
hooks. Managed S3 is the background boundary; a reachable local source fails
closed. Capture remains active while ingestion access is disabled. The
[ERP connector contract](../../services/kanzlei_ai/docs/erp-connector.md)
describes configuration, the locking and alias-scan costs, and operational
limits. Worker persistence and reconciliation remain Task 4.

Focused acceptance: 18 real Bench database/Redis tests passed, including
rollback, duplicate invalidations, deletion, trash and restore, S3 replacement,
held sequence lock, reversed mutation completion, an established snapshot,
concurrent descendant movement, keyset overlap, direct `db_insert`, atomic
`commit=True`, authentication and RPC dispatch. Tests use an in-memory S3
fixture and do not test a live S3 provider or the deferred consumer/worker.
Shared regression checks passed: 17 Mandant Documents, 19 object-storage,
20 S3-migration and 23 Posteingang document cases. All 21 existing connector
cases were verified across the main run and a focused rerun of four cases
after isolating the test user-creation throttle. The shared PDF fixture now
contains a valid ASCII-safe PDF and works without a live S3 connection.

The old Drive suites retain 19 errors in Mandant Drive and 21 errors plus one
failure in Drive Storage Security. Representative path-validation and copied
folder failures reproduce with the original production files from `7ccf20f`.
These fixtures limit full Drive acceptance. Ruff and diff whitespace checks
passed; independent Astra review has no remaining blockers. Replacement's
linear alias scan remains a documented contention cost.

A failed committed test initially leaked temporary S3 settings. The six fields
were restored from ERP version history, the user confirmed S3 was enabled,
and a read-only bucket HEAD returned 200. Tests now keep durable private
configuration snapshots, roll back each case, and verify restoration. Final
acceptance restored and verified the original S3 configuration; unreferenced
synthetic bindings attributable to this task were removed without changing
File records or S3 objects.

## Task 4: Durable service ingestion and recovery

**Create:** `services/kanzlei_ai/src/kanzlei_ai/db.py`, `models.py`, `sync.py`, `jobs.py`, `worker.py`, `outbox.py`, `migrations/env.py`, `migrations/versions/0001_sources_jobs.py`, `tests/integration/test_sync.py`, `tests/integration/test_job_recovery.py`.

1. Add tests for duplicate delivery, atomic cursor advancement, a crash between DB commit and dispatch, duplicate worker execution, expired leases and reordered source revisions.
2. Run `uv run pytest tests/integration/test_sync.py tests/integration/test_job_recovery.py -q` against isolated dependencies; verify intended failures.
3. Implement source/client relationship tables, processing generations, leases and job/outbox transactions. Key idempotency by connector, source, revision and pipeline version; dispatch task IDs only.
4. Kill/restart a real worker during a synthetic task. Verify recovery and generation fencing, then rerun the affected tests. Set timeouts, bounded retries/backoff and broker persistence explicitly.
5. Document reconciliation and cleanup. Commit independently.

**Expected:** Pending work survives restarts and cannot regress a source to an older revision.

**Result on 2026-11-16:** Implemented in the standalone service. PostgreSQL stores accepted page receipts/items, the connector's scan epoch/phase/cursor, sources, client relationships, generation-fenced jobs and transactional outbox entries. Page application is serialized and checks the expected cursor; stale responses cannot overwrite later state. Full inventory completes before feed replay and reconciles unseen relationships. A tombstone removes only its client relationship. Job identity includes the source/revision/pipeline key and processing incarnation, so restoring identical content creates fresh work. The worker currently validates and stages metadata as `awaiting_extraction`; fetching bytes, PDF parsing and indexing remain later tasks.

An independent coordinator handles polling, periodic reconciliation, outbox dispatch and recovery. Messages contain only job IDs. Claims, completion and failure publication check database-time leases, tokens and source generations. Durable attempts are capped at five. Broker-loss recovery republishes unclaimed work and reuses pending outbox records; publication and acknowledgement crashes can safely cause duplicate delivery. Completion retries bounded row-lock contention within its existing lease. A broker publication failure delays polling until a successful coordinator cycle; the ERP feed and saved AI cursor preserve the backlog. Synchronization is disabled by default and requires the separate ingestion key.

Acceptance: all 62 service tests passed, including 24 integration cases against isolated PostgreSQL schemas and Redis namespaces. Cases cover atomic cursor/inbox/outbox commits, replay and concurrent acceptance, reconciliation, same-revision restoration, pipeline changes, expired/reclaimed leases, retry limits, real lock timeouts and killing/restarting the actual Celery task. API/worker images build; an installed-image scenario migrated a private schema, accepted a synthetic manifest, dispatched through Redis and completed it in the production worker, then removed its resources. The local AI database was explicitly migrated to `0001_sources_jobs`; API live/ready return 200, the worker answers ping, and ERP ingestion remains disabled. Ruff and formatting checks passed. Independent Astra review has no remaining blockers. See the [operations commands](../../services/kanzlei_ai/README.md#durable-ingestion-operations).

An initial driver traceback exposed the local AI database password. It was rotated in the database and ignored service-local configuration, and affected containers were refreshed. Subsequent integration diagnostics suppress connection tracebacks; both separate and URI-embedded passwords with reserved characters are verified. No ERP configuration was needed for these tests.

## Task 5: Extract a PDF with page provenance

**Create:** `services/kanzlei_ai/src/kanzlei_ai/documents/{__init__.py,parse.py,chunk.py,artifacts.py}`, `providers/{__init__.py,base.py,fake.py}`, `tests/test_pdf_extraction.py`, `tests/test_chunk_provenance.py`, `tests/fixtures/documents/` with synthetic source/gold fixtures.

1. Build synthetic PDFs covering native German text, a table, repeated headings, an empty page and an unsupported/encrypted input. Gold facts include page locations and values independent of parser output.
2. Run `uv run pytest tests/test_pdf_extraction.py tests/test_chunk_provenance.py -q`; verify the absent extraction/provenance behavior fails.
3. Implement Docling parsing, bounded artifact storage and page-aware chunks. Do not infer an invoice date from upload time or flatten table cells into unrelated claims. Record parser version and content hash.
4. Verify page provenance and extracted gold facts. Check limits and explicit unsupported/error states. Keep external OCR disabled in this slice.
5. Commit parser and fixtures independently.

**Expected:** A text PDF produces useful, reproducible chunks linked to its source version and pages.

## Task 6: Hybrid search and safe index publication

**Create:** `services/kanzlei_ai/src/kanzlei_ai/search/{__init__.py,index.py,retrieve.py,publication.py}`, `tests/integration/test_index_publication.py`, `tests/integration/test_scoped_search.py`.

1. Test exact identifiers and semantic retrieval separately; include two clients, a multi-client source, duplicate content with different permissions and a stale generation. Use deterministic embeddings for mechanics, not as proof of semantic quality.
2. Run `uv run pytest tests/integration/test_index_publication.py tests/integration/test_scoped_search.py -q`; verify intended failures.
3. Implement dense/BM25 indexes and initial RRF fusion, indexed filter fields, scoped retrieval and staged Qdrant generations with a conditional PostgreSQL activation. Reject candidates not matching the active manifest. Reauthorize candidates before an external reranker or LLM receives text.
4. Exercise a crash between Qdrant writes and DB activation, replacement during embedding and trash during indexing. Verify no stale generation becomes visible and index reconstruction works from stored artifacts/manifests on the isolated fixture.
5. Commit independently.

**Expected:** Search finds authorized current chunks without depending on atomic writes across two databases.

## Task 7: Scoped chat API and first browser scenario

**Create:** `services/kanzlei_ai/src/kanzlei_ai/chat/{__init__.py,api.py,context.py,citations.py,history.py}`, `migrations/versions/0005_chat_conversations.py`, `tests/test_chat.py`, `tests/integration/test_history_permissions.py`, `web/` containing the independent client and its browser tests.

1. Add tests for immutable scope, source-grounded citations, unanswerable questions, model injection in a PDF, denied source expansion, unavailable ERP authorization and revoked access to old answers/summaries.
2. Run `uv run pytest tests/test_chat.py tests/integration/test_history_permissions.py -q`; verify failures for the intended missing behavior.
3. Implement context assembly, model adapter calls, SSE, citation validation and dependency-aware history. A fake provider enables deterministic integration work; live providers remain separately configured. Check authorization before generation starts and fail closed if ERP cannot authorize. Stop known-invalidated work; do not claim already-streamed content can be recalled.
4. Add the small independent chat UI with login, selected client, source processing state and clickable citations. Exercise one browser scenario: upload a synthetic PDF to client A in Drive, wait for readiness, ask a gold question in A's chat, open the cited original, then verify client B and an unauthorized user cannot obtain the content. If the UI supports DE/RU or responsive layouts, exercise both languages and one narrow viewport.
5. Rerun affected checks after fixes, record concise results and commit. The first milestone is complete only after this scenario passes with real isolated storage/search/queue components.

**Result on 2026-11-20:** The independent German chat uses ERP browser sign-in,
an immutable Mandant scope, a deterministic extractive provider, and live ERP
authorization for cited originals and history. Native Suite uploads in managed
flat S3 storage flow through inventory without a separate attachment binding.

The isolated acceptance uploaded a synthetic PDF through Suite Drive, then
processed and indexed it through Celery, MinIO, PostgreSQL and Qdrant. Chromium
showed the expected amount and rendered page 2 of the original at desktop and
mobile widths. Mandant B and a separate B-only user could not access A's
content. A committed DocShare revocation during the S3 read withheld the
original, and the browser cleared the old answer and viewer after denial.
Focused checks passed: 44 service unit tests, 36 PostgreSQL integration tests,
8 native ERP connector tests and 3 Chromium source-refresh regressions.
Production, external model providers and real documents were outside this run.
Role and User Permission cache freshness remains a pre-pilot boundary.

## Task 8: OCR quality and model integration

**Create:** `services/kanzlei_ai/src/kanzlei_ai/providers/ocr.py`, `providers/embeddings.py`, `providers/generation.py`, `tests/test_provider_policy.py`, `tests/evaluation/test_document_answers.py` and synthetic scanned German fixtures.

1. Select concrete OCR, embedding, reranking and generation providers/models after checking deployment and processing requirements. Lock IDs and pipeline versions. Provider keys go into secret configuration, never fixtures or commands.
2. Write/run failing tests for processing-policy denial, timeout, rate limits, size limits and OCR checkpoint reuse. Implement adapters with bounded requests and independently reusable checkpoints.
3. Evaluate actual selected models on synthetic scans, tables, exact identifiers, semantic questions, contradictions and unanswerable questions. Measure retrieval separately from answer quality and citation correctness.
4. Document observed quality, latency, resource use and provider cost; do not infer quality from fake-provider checks. Commit independently.

## Task 9: Email and broader source coverage

**Modify:** `kanzlei_erp/ai_connector/{api.py,policy.py,events.py,inventory.py}`, `kanzlei_erp/email_history.py` only where explicit direct timeline-link changes require event coverage.

**Create:** `services/kanzlei_ai/src/kanzlei_ai/documents/email.py`, `tests/test_email_threads.py`, `tests/integration/test_email_sync.py`, `kanzlei_erp/tests/test_ai_connector_email.py`.

1. Add red tests for messages without attachments, incoming/outgoing mail, reply expansion, multiple Mandant links, ambiguous/unlinked mail, attachment permissions and a message whose body contains quoted material from a different scope.
2. Run `uv run pytest tests/test_email_threads.py tests/integration/test_email_sync.py -q` and the new ERP module. Implement message sources and separately authorized attachment sources, preserve provenance, and prevent automatic inclusion of an entire thread/mailbox.
3. Cover relationship changes performed by the archive/history linking code; rerun affected email-history and archive tests. Verify changing/removing a Contact alone is not misrepresented as unlinking already-linked Communication records.
4. Add Office formats after PDF/email acceptance, with format-specific gold fixtures. Do not treat search extraction of a spreadsheet as an accounting calculation engine. Commit focused increments.

## Task 10: General mode and structured ERP answers

**Create:** `services/kanzlei_ai/src/kanzlei_ai/chat/routing.py`, `tests/integration/test_general_scope.py`, `tests/integration/test_work_context.py`; `kanzlei_erp/ai_connector/work_context.py`, `kanzlei_erp/tests/test_ai_connector_work_context.py`.

1. Add red tests that distinguish a user's readable clients from permission to use general mode. Test mixed-client sources, scope changes, denied candidates and permission revocation across saved general answers.
2. Implement general retrieval with source/client labels and the same per-source authorization. Keep one-client and general histories separate.
3. Add bounded ERP queries for unresolved Questions, tasks and FiBu states. Test exhaustive enumeration/counts independently of top-k retrieval and require the verified user's existing record access.
4. Run the targeted service/ERP modules and one browser question across two synthetic clients, then switch to one-client context and verify isolation. Commit independently.

## Task 11: Pilot operations and release preparation

**Create:** `services/kanzlei_ai/docs/operations.md`, `docs/reviews/YYYY-MM-DD-ai-service-pilot.md`; update service `README.md` and root `README.md` with the boundary and local entry point.

1. Record indexing lag, processing failures, job recovery and source coverage without logging document text, credentials or private paths. Define artifact/history retention and authorized deletion behavior; no indefinite retention by accident.
2. Add service-specific CI checks and review isolation of secrets, volumes and ports. Release images and exact versions are pinned after compatibility checks. Broaden verification for changed authentication/storage/build boundaries with a stated concrete risk.
3. Document startup, migration, rebuild/reconciliation, credential rotation, failure recovery and backup requirements. Keep the existing ERP release gate; no full local ERP image build or backup/restore acceptance by default.
4. Report changed behavior, focused test/browser results and meaningful unverified areas. The task owner creates the PR and deploys. Live documents require the agreed processing configuration; synthetic model evaluation does not approve real-document processing.

## First implementation action

Implement Task 1 only as the initial code change, followed by the concrete Task 2 identity/source contract. Keep the end-to-end PDF milestone as the first integration target; email and general mode follow after its authorization and recovery boundaries pass.
