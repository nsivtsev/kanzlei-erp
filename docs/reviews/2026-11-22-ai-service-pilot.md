# Kanzlei AI pilot acceptance

Task 11 adds aggregate operational status, broker-loss recovery, bounded
maintenance, and a dedicated AI release workflow. Conversation history has
no expiration. Only the signed-in owner can delete a conversation through
the management dialog, including after access to its former sources ends.

## Local checks

- On an isolated Frappe site, the real connector fetched a synthetic Drive
  invoice, the worker extracted and indexed it, and a browser user signed in
  through ERP. The answer showed `1.249,50 EUR` and opened the cited PDF.
  Revoking the source permission hid the answer and closed the viewer.
  The owner then deleted the conversation, and the history request returned
  `404`. Service tests also cover deletion while history and citation reads
  are waiting for ERP.
- A stopped Celery worker left a visible reindex backlog. After restart,
  the current publication returned to ready. `ops-status` measures source
  coverage from SQL publication records. The search/index checks separately
  exercise Qdrant.
- Offline maintenance against disposable PostgreSQL, MinIO and Qdrant
  removed stale projections and kept the current artifact, publication,
  connector cursor and year-old synthetic chat. A simulated storage outage
  left a retry entry, which a later apply cleared. Service tests cover denied
  S3 HEAD responses, retired delivery cancellation, current index failures
  and reindexing after a profile change. The disposable database migrated to
  `0012_outbox_cancellation`.
- All 486 service tests and 154 deployment checks passed locally. Local
  validation also covered the web build and browser suite, Linux amd64 image
  builds, and offline Mac Paddle table OCR. The pinned native PDF table and
  answer checks passed on Linux arm64 and amd64 and are included in the AI
  workflow.

## Pilot handoff

Hosted GitHub CI, the production artifact-bucket policy, production migration
timing, and a paired backup restore remain to be verified. This stage used
synthetic documents and the synthetic model profile. The 30-day encrypted
backup-copy schedule is documented for the pilot operator. Run the rollout in
the order described in the [pilot runbook](../../services/kanzlei_ai/docs/operations.md).
