# Kanzlei AI service

Independent FastAPI service in the ERP monorepo. It has its own Python environment,
Docker Compose project, PostgreSQL, Qdrant and Redis. It does not import Frappe,
read ERP database tables or join the ERP Docker network.

This bootstrap provides health endpoints and an idle Celery worker. Document
ingestion, authentication, chat, ERP connectors and application migrations are
subsequent implementation stages. No model API keys or real documents are needed
to run this skeleton.

## Start locally

Requirements: Docker Compose with BuildKit and ARM64 or x86-64 Linux containers.
Run commands from this directory, not the repository root:

```bash
cp .env.example .env
docker compose up -d --build --wait --wait-timeout 180
curl --fail http://127.0.0.1:8180/health/live
curl --fail http://127.0.0.1:8180/health/ready
```

The example password is for this loopback-only local stack. Keep `.env` private
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

```bash
docker compose ps
docker compose logs --tail 100 api worker
docker compose exec worker celery -A kanzlei_ai.worker:app inspect ping --destination worker@ai-worker --timeout 5
docker compose stop
docker compose start
docker compose down
```

`down` keeps this project's database/search/broker volumes. Do not use `down -v`
unless intentionally discarding this service's local data. Commands above do not
stop, migrate, rebuild or remove the existing ERP environment.

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
Readiness currently checks infrastructure, not migrations, collections, model
availability or future ingestion workers. It will expand with those features.

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
docker compose stop api
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
docker compose run --rm --no-deps worker python -c 'from docling.document_converter import DocumentConverter; import torch; print("Docling import OK; torch", torch.__version__)'
```

This checks binary/import compatibility, not OCR or model quality. A future
document-processing stage will configure model artifacts and evaluate synthetic
German documents before live data is used.

## Architecture and next stage

See the [accepted design](../../docs/plans/2026-11-15-ai-service-design.md) and
[implementation plan](../../docs/plans/2026-11-15-ai-service-implementation.md).
The next stage defines the versioned ERP source/identity contract before adding
ingestion. PostgreSQL remains authoritative; Redis is a transport and Qdrant a
rebuildable projection. Durable jobs and recovery are not provided by this
bootstrap worker.
