# ERP connector contract v1

This stage implements delegated user identity, permitted clients and documents,
batch authorization and bounded retrieval of original stored files. Email bodies
and the chat UI are later stages. Background ingestion uses the separate
credential described below. The independent frontend will use Vite and TypeScript;
this stage adds no
browser login shortcut or automatic redirect.

## Identity and trust boundary

1. An active staff user calls ERP `issue_ticket` with their existing ERP session
   and normal CSRF protection. The user cannot supply a subject, role or audience.
2. ERP issues a random one-time ticket, valid for 60 seconds, bound to that ERP
   session, configured audience and integration key generation.
3. The browser explicitly posts the ticket to AI `POST /v1/auth/exchange`. The AI
   backend exchanges it with ERP using its private integration key. Redis GETDEL
   ensures only one exchange succeeds. Raw tickets/grants are not Redis key names.
   The ERP Redis server must support GETDEL (Redis 6.2 or later).
4. ERP returns an opaque grant valid for 300 seconds. The browser uses
   `Authorization: Bearer <grant>` for AI API requests. The ERP session ID and
   integration key are never returned to the browser.
5. Each ERP operation checks the service key, audience, grant lifetime, active
   staff account and the continued existence/expiry of the original ERP session.
   Document operations run under that user's current permissions and restore the
   original ERP request principal afterward. Service credentials alone cannot
   select or impersonate a user.

Logout from ERP, disabling the account, revoking the grant or rotating the
integration key invalidates access. Connector security settings are read without
Frappe's ordinary request configuration cache. Grants do not extend ERP session
lifetime. Redis failure closes access; there is no offline authorization fallback.

Keep tickets/grants out of URLs, logs and persisted browser storage. Before adding
an automatic browser redirect/handoff, implement state and verifier binding (for
example PKCE) and review the exact-origin/callback policy. The current explicit
API exchange is not a complete redirect login flow. No fake identity bypass is
enabled in production code.

## Configuration

The ERP connector is disabled by default. Configure the following object in the
site's private `site_config.json` through the normal operator workflow:

```json
{
  "kanzlei_ai_connector": {
    "enabled": true,
    "connector_id": "kanzlei-erp",
    "audience": "kanzlei-ai",
    "service_key_sha256": "SHA256_OF_A_RANDOM_INDEPENDENT_SERVICE_KEY",
    "allowed_users": ["pilot-user@example.invalid"],
    "general_roles": []
  }
}
```

Use a random key of at least 32 characters. ERP stores its SHA256 digest; only the
AI backend receives the raw key in secret configuration. Do not place keys in git,
chat, process arguments or HTTP query strings. `allowed_users` may be omitted to
allow active staff sessions generally; an empty list permits nobody. Start with
an explicit pilot allowlist. `general_roles` grants the separate general-mode
permission; it never replaces source-level access checks. No role is created or
assigned automatically, including for Administrator.

Set these service-local environment variables:

| Variable | Meaning |
| --- | --- |
| `AI_ERP_BASE_URL` | HTTPS origin of the ERP, with no path/query/credentials |
| `AI_ERP_SERVICE_KEY` | Independent private integration key |
| `AI_ERP_SITE_NAME` | Optional explicit Frappe site routing header |
| `AI_ERP_TIMEOUT_SECONDS` | Whole RPC deadline, default 5 seconds |
| `AI_ERP_ALLOW_INSECURE_HTTP` | Explicit local HTTP opt-in, default false |

Blank optional variables leave the connector disabled and do not affect health
endpoints. Containers reach the host's local ERP using a suitable host address
and explicit site header; `127.0.0.1` inside a container refers to that container.
There is no automatic ERP network attachment. The HTTP client validates TLS,
does not follow redirects or use environment proxy settings, and does not forward
the browser's ERP cookies.

## API operations

ERP endpoints use `POST /api/method/kanzlei_erp.ai_connector.api.<operation>` and
the standard Frappe JSON `message` envelope. `read_source` returns binary content.
All operations except session-authenticated `issue_ticket` require the private
`X-Kanzlei-AI-Service-Key` header. Delegated operations also require `access_token`
in their backend JSON body. Request bodies have `version: 1`; unknown versions,
unexpected privilege fields and invalid batches are rejected.

| ERP operation | AI API |
| --- | --- |
| `issue_ticket` | Called directly from the active ERP session |
| `exchange_ticket` | `POST /v1/auth/exchange` |
| `identity` | `GET /v1/auth/me` |
| `revoke` | `POST /v1/auth/logout` |
| `list_clients` | `POST /v1/connector/clients` |
| `list_documents` | `POST /v1/connector/documents` |
| `authorize_sources` | `POST /v1/connector/authorize` |
| `read_source` | `POST /v1/connector/read` |

Document listing takes `client_id`, `limit` (1–100) and optional `cursor`. Cursors
are signed, expire after five minutes, and bind to the user and operation/client.
An empty eligible page can still have a next cursor. Listings use existing ERP
document membership, File permissions and active Drive ancestry. Remote links and
native Suite editor content are not fetched. Metadata excludes physical paths
and other clients' relationships.

Source manifests contain `source_id`, `source_type`, `revision`, `client_ids`,
`title`, `mime_type`, `source_modified_at`, `locator`, `byte_size` and `active`.
`client_ids` is an array; the user-facing listing reports the verified client
context, not every potentially confidential association. The future ingestion
inventory must supply its own authorized full relationship set. `locator` is an
ERP-relative link to the existing original-download endpoint.

A source reference is `{source_id, revision, client_id}`. Scope is either
`{kind: "client", client_id}` or `{kind: "general"}`. General batch/read access
requires an explicitly configured role and verifies every supplied client hint
against actual source membership and current rights. General search and
aggregation are not implemented by these connector endpoints.

Batch authorization accepts 1–50 references. Denied decisions echo only the
supplied reference and `allowed: false`; they do not reveal labels, paths or
whether another source exists. Per-client membership graphs are built once per
batch. Existing ordinary downloads retain their all-or-nothing behavior.

File retrieval is bounded to 20 MiB and requires the expected revision. Revisions
include File metadata, attachment context and managed storage revision, or local
file stat information. Before releasing bytes, the connector uses a current
locking File read and locks managed S3 bindings. A snapshot conflict or changed
revision returns `STALE_SOURCE` without bytes. Authorization is checked for each
request; already delivered bytes cannot be recalled on later access revocation.
ERP returns a SHA256 and revision header, which the AI client verifies.

Public failures are content-free codes: `UNAUTHENTICATED`, `FORBIDDEN`,
`STALE_SOURCE`, `INVALID_REQUEST`, `LIMIT_EXCEEDED`, `METHOD_NOT_ALLOWED` and
`CONNECTOR_UNAVAILABLE`. Browser validation errors omit rejected input. Responses
disable caching and MIME sniffing. The client bounds JSON to 2 MiB and binary
responses to 20 MiB, including streamed/chunked responses.

## Local acceptance

Run service tests using the service environment. ERP tests use the Bench runtime:

```bash
uv run --locked pytest tests/test_connector_contract.py tests/test_connector_client.py tests/test_auth.py -q
```

From the repository root, with the local ERP and the service Python environment
available, run:

```bash
services/kanzlei_ai/.venv/bin/python scripts/verify-ai-connector.py
```

The HTTP check temporarily enables the connector only for its synthetic staff
user, starts a temporary AI API on loopback port 8181, and verifies handoff,
single-use redemption, client isolation, batch authorization, original bytes, permission revocation
and ERP logout. It restores connector configuration and removes its records in
`finally`. Do not run it concurrently with ERP tests: both can contend on shared
ERP storage bookkeeping. It does not send email, call models or index real data.

The real transaction test in `test_ai_connector` covers concurrent File revision
changes from another DB connection. Existing Mandant Documents, Drive storage and
Posteingang checks remain relevant because the connector reuses their policy.

## Background Drive inventory and changes

After migrating the ERP schema, configure `kanzlei_ai_connector.ingestion` in
private site configuration with `enabled: true`, a fixed enabled System User
in `user` (never Administrator), an explicit nonempty `client_ids` array,
`generation` (an operator-controlled string), and `key_sha256` (the SHA-256
hex digest of a separate private ingestion key). Send the original ingestion key
in `X-Kanzlei-AI-Service-Key`; the browser delegation key does not grant this
background access. The connector itself must also be enabled. The fixed user
must currently have Customer read permission on every configured client or the
whole request fails with 403; current File and attachment permissions govern
manifests. A dedicated credential authorizes minimal source IDs and client IDs
in invalidations for the allowlisted clients, including IDs that the fixed user
cannot currently read. Such tombstones include no title, path or bytes. Keep the
allowlist and credential restricted to the trusted ingestion service.

Call `inventory` with an optional `cursor` and `limit` (1–100). Apply all pages
before replaying changes: a later inventory page can otherwise overwrite a newer
change. Empty pages can have a continuation. Every page carries the same opaque
`changes_cursor` watermark; retain it through the scan. After the final page,
call `changes` with that cursor and save each returned `next_cursor` durably
with the applied results. Empty changes pages also return a resumable cursor.
Changes are invalidations projected to the current state: `operation` is
`upsert` with a current manifest, or `delete` with `manifest: null`. They are
not historical versions and duplicates are expected. Apply each operation
idempotently and remove indexed content on delete. Current permissions must also
be checked online before content is served. Cursor signatures bind the subject,
client allowlist, connector, audience, key digest and generation. Changes to
any of these require a fresh full inventory; do not reuse an old cursor.

Capture begins after schema installation and remains active even while ingestion
is disabled. A first deployment, a period without capture, lost event history,
or a generation change requires a full inventory and reconciliation that
removes previously indexed sources absent from the completed scan. No event
retention or pruning is implemented. The feed currently supports managed S3
Drive files only: a readable active local Drive source reachable during a scan
or change projection causes `UNSUPPORTED_STORAGE` (409). Migrate local files
before enabling ingestion; the existing delegated document API still supports
local files. This restriction is necessary because Suite's failed local WebDAV
promotion repair can commit raw metadata updates without File hooks. Future
legacy support needs reliable capture or reconciliation for that path.

File writes append private invalidations in the same transaction, without
network calls or content processing. A locked sequence row serializes event
allocation through commit, and the initial watermark is read from the same
consistent database snapshot as the first inventory page. Parent and root
changes invalidate descendants, including unchanged files. S3 migration
activation and rollback explicitly invalidate affected File IDs. Locking reads
can conflict with concurrent writers; retry a deadlocked operation as a whole
transaction. Binding replacement scans and locks managed File IDs and URLs to
match every canonical alias; its cost grows with the number of managed Files,
but capture does not read file content or call object storage. The AI service now
provides a durable consumer, metadata-staging worker and periodic reconciliation;
extraction and indexing remain later stages.
