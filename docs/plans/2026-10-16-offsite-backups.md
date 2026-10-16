# Daily encrypted offsite backups implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Recover ERP after loss of the production server or working S3 bucket using an independently stored, encrypted snapshot.

**Architecture:** A host-side systemd timer runs at 03:00 Europe/Berlin. The existing deployment lock and snapshot/restore format protect a consistent DB, local files, site encryption configuration and complete managed S3 namespace. Restic stores encrypted, deduplicated snapshots in the separate private Hetzner bucket `stb-golke-backups` at `nbg1.your-objectstorage.com` (region `nbg1`). ERP resumes before remote transfer and verification. Retain seven daily, four weekly and six monthly snapshots; existing deployment retention stays unchanged.

**Tech stack:** Python 3.9+, existing Docker Compose deployment manager, pinned Restic release, systemd.

## Scope and recovery properties

- Dedicated backup credentials come from the separate Hetzner backup project. No application credential reuse. Object Lock is disabled because automatic Restic lock removal and pruning require deletion.
- Private environment/password files are excluded from Git and logs. Save a recovery password outside the production server; losing it makes the encrypted repository unrecoverable.
- One durable staging snapshot outside deployment backup retention. Failed uploads preserve it; the next run retries that snapshot before creating a fresh one.
- Preflight checks repository access, current deployment health, maintenance state and free space before stopping writers. Refuse unfinished deployment/restore operations.
- A durable maintenance marker fences deploy/restore. Normal failures restore service only after all temporary containers are confirmed stopped. Uncertain container shutdown keeps the marker and maintenance for explicit recovery.
- Verify every new external snapshot through a full restore to an isolated staging directory and the existing SHA-256 manifest verification, before local cleanup or remote retention. Preserve staging on failure. Record status and nonzero exit for monitoring.
- Do not automatically restore production data. Provide list/fetch commands that download and verify an explicit snapshot into the existing restore format. Local recovery still requires a complete explicit MinIO target.
- systemd handles scheduling independently of the desktop app; persistent timer catches missed runs. It does not retry by interrupting ERP repeatedly.

## Implementation sequence

1. Add failure-first host tests for config secrecy, snapshot placement, maintenance recovery, retry, verification before deletion, retention scope, stale status and explicit snapshot fetch.
2. Add an optional destination to the existing snapshot method while retaining default deployment behavior. Implement `deploy/offsite_backup.py` using that same backup/verify format and deployment lock; no new app backend or migration.
3. Add systemd service/timer and operator instructions, including secure credentials, password recovery, log/status checks, failures and disaster restore.
4. Pin and verify official Restic release binaries. Run unit/lint/Python compatibility checks and an encrypted repository integration test against isolated MinIO, including tampering/failure and retrieve-to-restore checks.
5. Install host tooling without an ERP image deployment. Initialize only the explicitly configured new repository. Create the first backup and verify restore, then enable the timer. Keep a clear operational record of the first production snapshot and recovery rehearsal.

## Acceptance

- Full snapshot includes DB, encryption key/config, remaining local files, release restore bundle and all managed S3 objects.
- ERP returns before network upload; failed/unreadable uploads cannot delete local staging or prune remote history.
- Deployment and backup cannot overlap during snapshot creation; existing maintenance/stopped systems are never reopened automatically.
- All verification commands pass with fresh evidence. First offsite backup and isolated restore are demonstrated before claiming operational completion.
