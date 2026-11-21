# Kanzlei AI service

Independent FastAPI service in the ERP monorepo. It has its own Python environment,
PostgreSQL, Qdrant and Redis. Local and production services share one Compose
project with ERP. The production AI stores use a dedicated internal network.
The service accesses ERP data through the connector.

The service provides health endpoints, durable PDF extraction and indexing jobs,
and an optional ERP connector with delegated identity and online permissions.
The optional local chat uses internal hybrid search with source citations. No
model API keys or real documents are needed for the synthetic fixture.
For production prerequisites, rollout, retention and recovery, see the
[pilot operations runbook](docs/operations.md).

## Start locally

Requirements: Docker Compose with BuildKit and ARM64 or x86-64 Linux containers.
From the repository root, run `bash scripts/setup-local.sh` to create credentials
only when missing, provision the named volumes, migrate AI PostgreSQL and start
all services. The Compose entry point is `docker compose --env-file .env -f compose.yml`.
If upgrading an existing standalone AI project, first stop that project's containers
without deleting its volumes. Never start two PostgreSQL containers on the same volume.
The legacy Compose file paths are wrappers for the same project and require both
credential files created by setup. From this directory the AI wrapper can also be
used with `docker compose`; it loads the root ERP and service-local settings.

```bash
curl --fail http://127.0.0.1:8180/health/live
curl --fail http://127.0.0.1:8180/health/ready
```

The example password is for local development only. Keep `.env` private
and use independent credentials; do not copy the ERP's root `.env`. Image contents
are pinned by digest and Python dependencies by `uv.lock`. The Qdrant `latest`
label is also digest-pinned, so it does not track new releases automatically.

Default ports are API `8180`, PostgreSQL `55432`, Qdrant HTTP `56333` and Redis
`56379`, all bound to `127.0.0.1`. Containers use service DNS names internally.
Change `AI_*_PORT` in `.env` for port conflicts. If running the API on the host,
also update its `AI_DATABASE_URL`, `AI_QDRANT_URL` and `AI_REDIS_URL` to match.
When changing the PostgreSQL password, update both `POSTGRES_PASSWORD` and the
host `AI_DATABASE_PASSWORD`. The password is passed separately from the URL, so
characters such as `@`, `:` and `/` do not require URI encoding. Changing an environment variable does not rotate an
existing database volume's password.

The following commands run from `services/kanzlei_ai`:

```bash
docker compose --env-file ../../.env -f ../../compose.yml ps
docker compose --env-file ../../.env -f ../../compose.yml logs --tail 100 ai-api ai-worker
docker compose --env-file ../../.env -f ../../compose.yml exec ai-worker celery -A kanzlei_ai.worker:app inspect ping --destination worker@ai-worker --timeout 5
docker compose --env-file ../../.env -f ../../compose.yml stop
docker compose --env-file ../../.env -f ../../compose.yml start
docker compose --env-file ../../.env -f ../../compose.yml down
```

`down` stops the shared project, including ERP. AI volumes retain their existing
external names; never use volume deletion unless intentionally discarding data.

## Health contract

| Endpoint | Meaning |
| --- | --- |
| `GET /health/live` | HTTP 200 while the API can handle requests; no dependency calls |
| `GET /health/ready` | HTTP 200 only if PostgreSQL, Qdrant and Redis respond; otherwise HTTP 503 |

Readiness runs dependency probes concurrently, each with a deadline configured by
`AI_HEALTH_TIMEOUT_SECONDS` (default 2 seconds, maximum 30). It executes PostgreSQL
`SELECT 1`, calls Qdrant `/readyz` and sends Redis `PING`. Both endpoints disable
response caching. Responses contain component names and `ok`/`unavailable`, never
raw driver errors, connection URLs or credentials.

The API can start with all dependencies absent; liveness still works and readiness
returns 503. Compose waits for PostgreSQL and Redis health before starting the API
and worker. API readiness also covers Qdrant. The worker uses the separate AI
Redis broker; its availability is checked with Celery's targeted ping above.
Readiness covers infrastructure connectivity. Use the operations runbook to
check migrations, ingestion workers and index coverage.

## Local Python development and tests

Use Python 3.13 and uv. The root ERP Python environment is unrelated.

```bash
uv sync --locked
uv run --locked pytest tests/test_health.py -q
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked uvicorn kanzlei_ai.app:create_app --factory --host 127.0.0.1 --port 8180
```

Stop Compose's API first when running Uvicorn on the same port:

```bash
docker compose --env-file ../../.env -f ../../compose.yml stop ai-api
```

Tests inject asynchronous probes and need no running ERP, database, broker,
search server or external model. They cover dependency-independent liveness,
success/failure of every readiness probe, private-error suppression, cancellation
on timeout and concurrent execution. Use the real Compose stack for the dependency
smoke check; passing injected-probe tests alone does not establish connectivity.

With this service's dependencies running, opt into the actual connection test:

```bash
uv run --locked pytest -m integration tests/integration/test_readiness.py -q
```

The default test selection excludes integration tests, so it remains usable
without running services. The integration test uses the host URLs/password from
this directory's `.env`; use only this disposable local AI stack.

## Document worker dependency boundary

The `runtime` Docker target contains the API and infrastructure clients. The
`documents` target adds Docling and CPU PyTorch plus native runtime libraries.
The optional `documents` extra keeps this weight out of API installations.
Linux PyTorch packages resolve from the explicit official CPU wheel index;
macOS packages use PyPI. No document model weights are downloaded in the build.

```bash
docker compose --env-file ../../.env -f ../../compose.yml run --rm --no-deps ai-worker python -c 'from docling.document_converter import DocumentConverter; import torch; print("Docling import OK; torch", torch.__version__)'
```

This checks binary/import compatibility. Stage the pinned native PDF weights
as described in [pilot operations](docs/operations.md#pilot-prerequisites).

## Architecture and next stage

See the [accepted design](../../docs/plans/2026-11-15-ai-service-design.md) and
[implementation plan](../../docs/plans/2026-11-15-ai-service-implementation.md).
The [ERP connector contract](docs/erp-connector.md) describes optional setup,
identity, source access, managed S3 Drive inventory/change feed and local acceptance.
Durable consumption, ingestion jobs and recovery use PostgreSQL as the authority;
Redis transports job IDs and Qdrant holds a rebuildable search projection.
Native PDF extraction and indexing use separate leased jobs.

### Durable ingestion operations

Ingestion is opt-in. The coordinator starts independently from the worker and
recovers committed work even when ERP polling is disabled. First initialize the
service database explicitly (`docker compose --env-file ../../.env -f ../../compose.yml run --rm ai-api python -m
kanzlei_ai.coordinator migrate`); migrations are packaged in both images. This
never changes the ERP database. Configure the ERP background ingestion identity,
client allowlist, and **separate** ingestion key as described in
[the ERP connector contract](docs/erp-connector.md). Set `AI_INGESTION_KEY`,
`AI_INGESTION_ENABLED=true`, and the existing ERP origin/site settings only when
ready to ingest. Start a full scan with `docker compose --env-file ../../.env -f ../../compose.yml run --rm ai-api python -m
kanzlei_ai.coordinator scan`, then start the coordinator and worker. A new scan
can also be initiated with the same command after changing bindings or repairing
an invalid cursor; cursor errors do not silently skip feed history. The coordinator
periodically starts a full reconciliation (`AI_INGESTION_RECONCILE_SECONDS`,
default one day) to account for permission changes not represented in the feed.
One page per cycle is fetched with `AI_INGESTION_POLL_SECONDS` (default 30s).

For operations, `sync-once`, `recover`, and `dispatch` are also available as
`python -m kanzlei_ai.coordinator` commands. The database stores page receipts,
items, cursor, source relations, jobs, and outbox records atomically. A processing
generation fences stale workers when a revision, membership, or pipeline version
changes; a restored source receives a new generation even if its revision is
unchanged. Change `AI_INGESTION_PIPELINE_VERSION` and initiate a scan to restage
existing sources. Jobs have five durable attempts and end in `failed` after the
limit; this native-PDF upgrade uses pipeline version `pdf-native-v1`, so start
a new scan (or let reconciliation start one) after deployment to create a new
generation for existing metadata-only sources. Changing the pipeline version
and initiating a scan creates a new
generation. A broker publication failure defers ERP polling until a successful
coordinator cycle. The AI service retains its cursor and ERP retains feed history.
Outbox publication is at least once and queued jobs are periodically republished after
broker loss. Broker persistence is enabled in Compose (Redis AOF, one-second
fsync). Outbox leases are 30s; worker leases are 120s, task soft/hard limits are
60/90s, and broker visibility is 300s. With ingestion configured, the worker
fetches a current PDF only through the fixed-ingestion ERP identity, with the
expected revision and the existing 20 MiB connector bound. Docling extracts
native text first. The optional [OCR rollout](docs/ocr-rollout.md) adds local
PaddleOCR and a controlled Mistral cutover for scanned pages. Page-aware chunks
and private derived artifacts publish only while the source generation and
worker lease remain current. Encrypted,
unsupported, and size/page-limited input is terminal; connector and transient
parser failures retry through the durable job policy.
Configure the separate private S3-compatible artifact endpoint, bucket,
credentials and `AI_ARTIFACT_S3_PREFIX` before enabling ingestion; the worker
refuses extraction without all of them. Protect the database, artifacts and
receipt contents as private data.

The Task 5 background byte read has an isolated READ COMMITTED final check for
SQL-backed DocShare, Customer-root, File and Drive ancestry state. Frappe role
and User Permission changes can remain cache-dependent, so this is explicitly a
pre-pilot boundary rather than a user-facing retrieval guarantee; any later
query/serving feature must authorize again at query time.

Start the standalone coordinator after migration with `docker compose --env-file ../../.env -f ../../compose.yml up -d
ai-coordinator ai-worker`. A stopped worker does not stop polling or recovery.
Operators run eligible artifact, index and operational record cleanup during a
locked offline window. Conversation history has no expiration and stays
outside operator retention. See [status and retention](docs/operations.md#status-and-retention).

The ingestion integration tests create their own PostgreSQL schema and Redis
namespace, start only their own worker, and clean those resources afterward:

```bash
uv run --locked pytest -m integration tests/integration/test_sync.py tests/integration/test_job_recovery.py -q --tb=no
```

These checks use synthetic metadata and a real broker/worker. They do not poll
the live ERP, fetch document bytes or prove extraction quality. Keep connection
diagnostics private; driver tracebacks can include connection parameters.

### Hybrid search on synthetic data

On an isolated local stand with synthetic inputs, migrate the AI database
before enabling `AI_SEARCH_INDEX_ENABLED` for the coordinator and worker.
Set `AI_SEARCH_COLLECTION`
to a collection reserved for that stand. `AI_INGESTION_ENABLED` can stay off
when indexing previously extracted fixtures.

To enqueue stored chunks or rebuild a missing test collection, run
`python -m kanzlei_ai.coordinator reindex --limit 100` against that stand.
It defaults to `AI_INGESTION_CONNECTOR_ID`; use `--connector` for a different
fixture connector. Continue with `--cursor <next_cursor>` until `next_cursor`
is `null`. The coordinator and worker publish the queued jobs normally; the
command does not reread originals or parse PDFs.

The index job reads current chunks from PostgreSQL and stages dense, BM25 and
exact identifier data in Qdrant. The dense provider is deterministic test code;
it establishes retrieval mechanics without measuring semantic quality. PostgreSQL
activates the staged publication only after all points are written. A replacement
or deletion makes old points ineligible immediately, and an index failure does
not require extracting the PDF again. Qdrant storage is private derived data;
it can be rebuilt from the persisted chunks. Use the offline retention
procedure to clean up eligible stale points.

Internal retrieval filters every query by connector and allowed client IDs. It
checks the current PostgreSQL publication and delegates authorization to the ERP
under the caller's grant before returning text. A failed ERP check cannot produce
partial output. There is no public search endpoint in this stage.

### Chat on an isolated synthetic stand

On the isolated stand, migrate the AI database to the current head, enable
the index and chat, and set
`AI_CHAT_BROWSER_ERP_ORIGIN` to the exact ERP origin visible to the browser.
Continue to configure the API-to-ERP origin and service key separately. The
isolated ERP site's `kanzlei_ai_connector.browser_chat_origin` must match the
exact web origin (scheme, host and port). See the [ERP connector contract](docs/erp-connector.md)
and [web startup instructions](web/README.md).

The UI uses the ERP browser session to sign in and keeps the short-lived grant
in memory. A conversation stays in its selected Mandant. History and original
PDF bytes are reauthorized on access; a revoked or replaced source is not
shown from stored history. A new question searches current sources rather than
reusing prior answers. The bundled synthetic answer provider exercises this
flow without an external model and does not measure answer quality.
