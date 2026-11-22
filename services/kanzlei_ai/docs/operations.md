# Kanzlei AI pilot operations

Production starts with `AI_ENABLED=0`. The existing ERP release pipeline
continues to publish the ERP image. Test the separate AI `runtime` and
`documents` targets for the deployment platform and publish their digests
through the approved registry process. AI Compose services share the ERP
project. PostgreSQL, Redis and Qdrant use dedicated volumes and an internal
network with no host ports. The AI API binds to host loopback on
`AI_API_PORT` (default 8180). Keep the artifact bucket and its endpoint private
and separate from ERP Drive storage.

## Bring up a synthetic stand

Use the [local startup instructions](../README.md#start-locally) and fictional
fixtures with independent stores and a synthetic ERP connector. Leave
`AI_MODEL_PROFILE=synthetic` and `AI_MISTRAL_ENABLED=false`. Enable ingestion,
search and chat only for the fixture paths under test. Browser CI runs the
synthetic stub in an isolated loopback network. Python CI provisions pinned
public Docling layout and table weights before running tests on an internal
Docker network. The Paddle unit tests use local test doubles for OCR models.
Both test paths use synthetic data and disposable credentials. CI includes
the native German fixture's exact table rows, cited invoice answer, and
service-level permissions, publication and recovery checks.

## Pilot prerequisites

Use the deployment account and the private, mode-600
`/opt/kanzlei-erp/shared/production.env`. Start with `deploy/production.env.example`.
Set `AI_API_IMAGE` and `AI_WORKER_IMAGE` to tested immutable digests.
Use a separate `AI_POSTGRES_PASSWORD` and configure the ERP delegation
identity and `AI_ERP_SERVICE_KEY`. For ingestion, provision the independent
`AI_INGESTION_KEY` and approved Mandant allowlist described in the
[ERP connector contract](erp-connector.md).

| Setting | Pilot use |
| --- | --- |
| `AI_INGESTION_ENABLED`, `AI_INGESTION_DOCX_ENABLED` | Opt in to background ingestion and, separately, DOCX ingestion. |
| `AI_ARTIFACT_S3_ENDPOINT`, `AI_ARTIFACT_S3_BUCKET`, `AI_ARTIFACT_S3_PREFIX` | Use the private HTTPS endpoint and the dedicated unversioned AI bucket and prefix. |
| `AI_ARTIFACT_S3_ACCESS_KEY`, `AI_ARTIFACT_S3_SECRET_KEY` | Worker credentials scoped to `PutObject` and `GetObject` for writes and metadata checks. Give the separate maintenance principal prefix-scoped `ListBucket`, `GetBucketVersioning` on the bucket, and `GetObject` and `DeleteObject` on canonical artifact keys. |
| `AI_DOCLING_MODELS_DIR` | Absolute host path with verified native PDF weights, readable by container UID 10001. Compose mounts it read-only and the worker loads weights offline. |
| `AI_INGESTION_OCR_PROFILE` | Set `paddle` for the Mac archive pass and `native` for server fallback configuration. The saved connector override controls Mistral OCR after cutover. |
| `AI_MISTRAL_ENABLED`, `AI_MODEL_PROFILE`, `AI_MISTRAL_ENDPOINT` | Enable Mistral only for the approved pilot and endpoint, using the explicit `mistral` profile. |
| `AI_WORKER_MISTRAL_API_KEY`, `AI_API_MISTRAL_API_KEY` | Distinct provider keys for the worker and for chat. The API receives its key only when Mistral chat is enabled. The coordinator routes jobs without either key. |
| `AI_SEARCH_INDEX_ENABLED`, `AI_CHAT_ENABLED`, `AI_CHAT_BROWSER_ERP_ORIGIN`, `AI_CHAT_WEB_DIR` | Enable search for reindexing and enable chat after index verification. Set the exact public HTTPS ERP origin and the absolute path to the built chat bundle on the deployment host. |

Stage the pinned weights on an internet-connected build machine from the AI
service directory. Copy the verified directory to the deployment host.

```bash
uv sync --locked --extra documents
uv run --locked --extra documents python scripts/provision_docling_models.py ./docling-models
```

The server API receives the ERP delegation key. The coordinator and workers
receive the independent ingestion key. Only workers receive the operational S3
credentials. Give any remote worker its own private environment with only its
required settings. Protect Docker access and configuration files as access to
the secrets they hold. Use Compose `config --quiet` for validation.

When ready to start or change the AI services, set `AI_ENABLED=1` and run the
current release's manager. It takes the deployment lock, starts the AI stores,
migrates AI PostgreSQL, and starts the API, worker and coordinator.

```bash
python3 /opt/kanzlei-erp/current/deploy/manage.py ai-reconcile
```

## Locked maintenance commands

Run the following once to open an exclusive maintenance shell on the host.
Keep the shell open through the whole maintenance procedure. A pending deploy
or restore must be resolved first. Close the shell before calling
`ai-reconcile`, which acquires the same lock itself.

```bash
flock --exclusive --nonblock /opt/kanzlei-erp/.deploy.lock bash --noprofile --norc
set -e
root=/opt/kanzlei-erp
test ! -e "$root/shared/pending.json"
cd "$root/current"
APP_IMAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image"])' "$root/shared/current.json")"
export APP_IMAGE
ai_compose() {
  docker compose --env-file "$root/shared/production.env" -f "$root/current/deploy/compose.yml" --profile ai "$@"
}
coordinator() {
  ai_compose run --rm --no-deps -T ai-coordinator python -m kanzlei_ai.coordinator "$@"
}
```

Use only the deployment's project and environment. Do not export other
`AI_*` variables into this shell. Use `ai-reconcile` to recreate application
containers with the validated settings.

## Paddle archive and Mistral cutover

1. Prepare the Mac using the [Paddle provisioning instructions](ocr-rollout.md).
   Use the existing approved private connections from the Mac to AI PostgreSQL,
   Redis, the ERP ingestion endpoint and the private AI artifact bucket. Keep
   store ports unpublished. With an existing private tunnel, use its loopback
   listeners for the Mac's database and Redis URLs. The Mac needs no Qdrant
   connection during the archive pass. Keep its owner-only service environment
   set to `AI_INGESTION_ENABLED=true`, `AI_MODEL_PROFILE=synthetic`,
   `AI_MISTRAL_ENABLED=false`, and search and chat disabled. Supply the
   ingestion and artifact credentials, the Docling cache and the verified
   Paddle weights through that private environment. Run the Mac worker only
   on `ocr-paddle` and keep it offline from model services.
2. On the server enable ingestion and the Paddle OCR profile. Keep search,
   chat and Mistral off. Use a locked shell to run `coordinator scan` for the
   initial inventory. Track progress via `coordinator ops-status --json`.
   When the inventory and feed are current, stop coordinator polling. Use
   `coordinator recover` and `coordinator dispatch` to drain committed jobs
   while the Mac is still running. Confirm the archive results and resolve
   failed OCR work before stopping the Mac worker.
3. Obtain approval for external processing for each pilot Mandant. Configure
   the explicit ERP `external_processing` entitlements for `ocr`, `embed`
   and `generate` as documented in the [OCR cutover guide](ocr-rollout.md).
   Stop the coordinator, API and all workers before changing the profile.
   Set the approved Mistral endpoint and worker key in the private environment,
   opt in to Mistral and search using the settings above, set the OCR
   fallback to `native`, and keep chat off. In the locked shell run
   `coordinator cutover-ocr`. This records the OCR override for new revisions
   and preserves the feed cursor and published Paddle artifacts.
4. Still under the lock, run `coordinator reindex --missing-only` and
   continue with `--cursor <next_cursor>` until the cursor is null. Close
   the maintenance shell and reconcile. The production server worker consumes
   `celery,ocr-mistral`. Check index coverage and failures before adding the
   API's independent key and enabling chat. Reconcile after changing chat settings.

## Browser UI on the ERP domain

Open the production chat at `https://erp.stb-golke.de/chat`. Caddy serves
the UI under `/chat/` and proxies its `/chat/v1/` requests to the AI API on
the Compose network. The ERP login, handoff and original downloads use the
existing ERP routes. The chat paths return `404` while AI or chat is disabled.

From `services/kanzlei_ai/web`, run `npm ci` and
`KANZLEI_AI_PROXY_TARGET=http://127.0.0.1:8180 npm run build`. Copy `dist/`
to a versioned directory on the deployment host and set `AI_CHAT_WEB_DIR`
to the absolute path to that directory containing `index.html` and `assets/`.
Use a directory dedicated to the bundle. Caddy mounts it read-only.

In the ERP site's private connector settings, set `allowed_users` to the
pilot accounts and `browser_chat_origin` to `https://erp.stb-golke.de`.
Set `AI_CHAT_BROWSER_ERP_ORIGIN=https://erp.stb-golke.de` in the private AI
production environment. Both origin settings contain the scheme and host
without `/chat`. Apply the configuration with `ai-reconcile`, which brings
up the AI API before applying the Caddy service configuration.

Verify `/health/ready` from loopback and fetch `/chat/v1/auth/browser/config`
on the ERP domain to check the configured origin. With a synthetic pilot
account, open the UI, sign in through the ERP popup, select an authorized
Mandant and send a fictional question. Confirm that the stream and citation
viewer work, that the cited original uses the ERP browser session, and that
revoking the account's source access removes access to the original and the
affected history. Retest after an origin or proxy change. Keep diagnostic
traces and browser tokens out of shared logs.

## Status and retention

Use `coordinator ops-status --json` to monitor connector progress, lag,
source coverage, job failures and recovery. Treat a stale feed or sustained
backlog as an incident. Check dependency readiness and worker queue selection
before retrying. If synchronization reports an invalid cursor, inspect the
connector and run a deliberate `coordinator scan` to reconcile the corpus.
Keep logs private. Avoid dumping receipts, source data, keys or raw responses.

Operator retention covers eligible stale artifacts, index projections and
completed operational records. A projection needs 30 days from its first
confirmed stale observation. Terminal bookkeeping becomes eligible after 90
days when recovery and reconciliation no longer need it. The pilot operator
runs `retention mark` daily and reviews `last_mark_at`, `ready`, `pending`
and `retry_pending`. Schedule a weekly maintenance window to apply eligible
batches. Conversation history has no expiration and is outside operator
retention. The signed-in owner can use
**Gespräche verwalten** → **Löschen** → **Gespräch löschen** in the chat.

Use the dedicated, prefix-scoped maintenance S3 principal and pass its keys
from a restricted local secret file or prompt. The principal must be able
to list the artifact namespace and an exact key under that prefix to verify
absence when a missing object returns a denied HEAD. Mark and plan can run
while the service is online.

```bash
read -rsp "Maintenance S3 access key: " AI_ARTIFACT_S3_ACCESS_KEY
printf '\n'
read -rsp "Maintenance S3 secret key: " AI_ARTIFACT_S3_SECRET_KEY
printf '\n'
export AI_ARTIFACT_S3_ACCESS_KEY AI_ARTIFACT_S3_SECRET_KEY
retention() {
  ai_compose run --rm --no-deps -T -e AI_ARTIFACT_S3_ACCESS_KEY \
    -e AI_ARTIFACT_S3_SECRET_KEY ai-maintenance retention "$@"
}
retention mark
retention plan
```

Before applying, stop the remote Paddle worker and any other external AI
consumer. In the locked maintenance shell, stop the local writers and take
a consistent AI backup with the stores still up. Wait for any live leases
to expire, then review the plan in the offline window.

```bash
ai_compose stop --timeout 120 ai-coordinator ai-api ai-worker
running_ai="$(ai_compose ps --status running -q ai-coordinator ai-api ai-worker)"
test -z "$running_ai"
# Take the paired backup described below.
retention plan
retention apply --confirm-offline
unset AI_ARTIFACT_S3_ACCESS_KEY AI_ARTIFACT_S3_SECRET_KEY
```

Use `retention apply --confirm-offline --limit 100` for explicit bounded
batches. The `cancelled` count includes superseded deliveries for retired
index profiles. A subsequent `mark` starts the 30-day window for any
artifacts that become eligible after cancellation. Review a fresh plan before
resuming after a partial failure, and keep the pending cleanup state for
retry. Keep all writers stopped throughout the maintenance window. Exit the
shell before reconciling and restarting any authorized remote worker.

## Back up and recover

- With all AI writers stopped under the deployment lock, capture a
  PostgreSQL custom-format dump and a matching inventory and copy of the
  private S3 artifact objects. Encrypt and restrict both backups. Record the
  AI image digests, migration level, connector and artifact namespace in the
  private restore metadata. Validate the dump with `pg_restore --list` and
  verify the copied objects against a private manifest. ERP backups are
  independent. AI PostgreSQL includes conversation history and should be
  handled as private data. Expire the encrypted pilot backup copies after 30
  days. A conversation deleted by its owner can remain in a prior snapshot
  until that snapshot expires. Review removed history before exposing any
  restored database to users.
  Set `backup_dir` to an existing private backup directory.

  ```bash
  umask 077
  ai_compose exec -T ai-postgres pg_dump -U kanzlei_ai -d kanzlei_ai -Fc > "$backup_dir/ai-postgres.dump"
  ai_compose exec -T ai-postgres pg_restore --list < "$backup_dir/ai-postgres.dump" > /dev/null
  ```

- Restore the paired database and artifact snapshot in an isolated recovery
  environment using the matching image versions. Restore PostgreSQL into an
  empty dedicated database and S3 into the private AI namespace. Check artifact
  references before reopening ingestion. After an ERP restore, keep AI
  disabled while reconciling against restored source permissions and start
  a full scan before enabling chat.
- Rebuild a lost Qdrant projection from PostgreSQL's saved chunks. Keep chat
  off, use the collection for the configured profile, and run
  `coordinator reindex --limit 100`. Advance via `--cursor <next_cursor>`
  until null. A full rebuild needs every current publication, including rows
  PostgreSQL previously marked indexed. Verify index coverage and citations
  after the queue drains.
- To correct table rows in previously extracted native PDFs, run
  `coordinator requeue-extract SOURCE_ID` for the affected sources. Let
  extraction and indexing finish before checking the new citations.
- Redis AOF is useful for restart recovery. PostgreSQL retains durable jobs,
  leases and the outbox. If the broker is lost, bring up the dedicated AI Redis,
  allow stale leases to expire, then run `coordinator recover` and
  `coordinator dispatch`, or restart the coordinator to do both. Check for
  remaining queue lag before reopening the pilot.

## Rotate credentials or stop AI

In a locked window, stop consumers, update the credentials at the issuer and
in the owner-only environment, and restart only after verification. For the
PostgreSQL password use `psql` and its interactive `\password kanzlei_ai`
command on the existing database; changing `production.env` alone does not
change a role in an existing volume. Rotate ERP delegation and ingestion
keys separately and update the corresponding private ERP connector
configuration. Rotate the S3 worker and maintenance principals separately,
and distribute new Mac credentials through its private connection. For Mistral
rotate the API and worker keys independently. Close the lock shell, reconcile,
and verify readiness, authorized browser handoff and queue progress before
revoking the old credentials.

For emergency stop, revoke the affected ERP external-processing permissions,
stop and disable restart of remote workers, and stop the API, server worker
and coordinator in the locked shell. If needed revoke the Mistral keys at
the provider. Set `AI_ENABLED=0` in the private environment to keep subsequent
reconciliation from bringing the AI consumers up. Preserve volumes, artifacts
and cursors for investigation and recovery.
