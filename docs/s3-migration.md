# Disk → S3 migration and recovery

This runbook migrates File content while preserving File IDs, relations, owners,
permissions, folders and statuses. It never deletes original disk files. Suite
content documents remain in the database; their registered embedded File resources
are migrated. External HTTP URLs remain external. Permanently removed File records
do not require already deleted bytes.

The migration entry points run through `bench execute` as an authenticated
Administrator/System Manager. They are deliberately **not** whitelisted HTTP APIs.
Use a terminal inside the backend container from the bench directory. Substitute
your real site name and the returned run ID in these examples.

## Prepare a target without switching the backend

Use Drive Disk Settings to configure credentials, bucket, endpoint, region,
addressing style and site prefix while keeping **Use S3 disabled**. Use the
connection test first. Never put the secret in a command, migration journal,
repository or chat. The journal stores destination configuration but no secret.
Changing the target after inventory requires a new inventory.

Production values:

| Setting | Value |
| --- | --- |
| Bucket | `stb-golke` (private, Object Lock disabled) |
| Endpoint | `https://fsn1.your-objectstorage.com` |
| Region | `fsn1` |
| Signature | `s3v4` |
| Prefix | `sites/erp.stb-golke.de` |

Local development uses its own MinIO credentials, private bucket and prefix.
Server-side requests use the Docker service address; browser signed links use the
localhost endpoint. Do not import production credentials into a local target.

Run `bash scripts/setup-minio.sh` after the application schema migration. This
provisions a private `stb-golke-local` bucket with dedicated credentials and saves
the connection while S3 remains disabled. Its prefix is
`local/development.localhost`, internal endpoint `http://minio:9000`, browser
endpoint `http://localhost:9002`, and console `http://localhost:9001`.
The source-built MinIO and mc revisions and base-image digests are pinned in
`.devcontainer/minio.Dockerfile` and recorded in `deploy/minio.env`.

Run `python3 scripts/verify-s3-minio.py` to exercise the separate test bucket,
including browser signatures and rejected cross-bucket/out-of-prefix access.
On a prepared disposable `s3-test.localhost` site, `python3 scripts/verify-s3-erp.py
seed` then `smoke` exercises real ERP files, permissions, ZIP/email and WebDAV.

## Inventory and copy

```sh
bench --site SITE execute kanzlei_erp.s3_migration.inventory
bench --site SITE execute kanzlei_erp.s3_migration.copy --kwargs '{"run_id":"RUN_ID"}'
bench --site SITE execute kanzlei_erp.s3_migration.verify --kwargs '{"run_id":"RUN_ID"}'
```

`inventory` creates a mode-0600 JSON journal in the site's
`private/s3-migrations/` directory (mode 0700). It records each original path,
related File IDs, destination key, byte length, SHA-256 and processing state. It
covers active and trashed originals, including descendants physically moved
inside an ancestor's `.trash/ID` tree, registered embeds, native Drive thumbnails and local `File.thumbnail_url` resources.
It reports missing originals, unsafe paths, ownership/key/URL conflicts and
unregistered disk files separately.

Resolve every `missing` or `conflicts` item before proceeding; create a fresh
inventory after changing data. Unregistered files are reported, retained on disk
and included by the site's disk backup. They are not automatically published as
S3 objects because they have no File record establishing access. Decide whether
they need registering or are leftover upload/preview files before the final
window. A permanently removed file that still has leftover bytes also appears in
this report.

`copy` does not change the running backend, URLs or File rows. It can be repeated
after interruption: existing destination bytes are checked before reuse. A
nonmatching existing object causes a collision error and is never overwritten.
`verify` reads each destination body and checks SHA-256 and size; ETag is not a
content checksum. Source changes invalidate the run.

Objects use a site-specific prefix with `drive/ID` for native Drive content,
`drive/.thumbnails/ID.thumbnail` for previews and deterministic
`attachments/SHA256-of-path` keys for framework uploads. Shared framework paths
copy once; native Drive storage aliases are ownership conflicts requiring review.
Framework thumbnail bytes use `derived/SHA256-of-path` keys. Their old URLs stay unchanged and protected bindings identify an original File; shared previews copy once while retaining every File reference. A framework thumbnail that already points at a native Drive sidecar reuses that object. Legacy active URLs receive protected mappings. Old URLs remembered by trashed
native files are not claimed because a new active file may have reused that path.

## Maintenance window and activation

1. Deploy and test the integration while retaining the disk backend. Confirm a
   successful isolated MinIO backup/restore rehearsal and production-specific
   Hetzner checks (upload, download, signed URL, Range and test-object removal).
2. Enable maintenance mode, stop scheduler/workers and all integrations that write
   files, and wait for active requests/uploads to finish. Maintenance alone is not
   evidence that workers have stopped. Keep the frontend fenced throughout.
3. Create a complete, verified pre-migration backup containing the database,
   encryption configuration, remaining disk files and managed S3 objects. Retain
   this specific snapshot and originals for at least 14 days independently of
   normal seven-snapshot rotation.
4. Create a fresh inventory during the stopped-writer window, then run copy and
   verify for that run. This captures any changes made after the rehearsal.
5. Activate:

```sh
bench --site SITE execute kanzlei_erp.s3_migration.activate --kwargs '{"run_id":"RUN_ID"}'
```

Activation requires maintenance mode, unchanged storage configuration, an unchanged
full File-table snapshot and mutation epoch, no missing/conflicting originals, and
successful verification. It checks all source and destination bytes again before
switching. Bindings, native File URLs, Drive/Users folder URLs, `enabled=1`,
`flat=1`, the new Drive root and a recovery marker commit in one DB transaction.
The journal records both old and activated metadata.

While still fenced, check ordinary staff and denied/Guest access, old links,
preview/download, ZIP, new uploads, move/rename, trash/restore, Mandant documents,
FiBu exports and incoming/outgoing attachment bytes. Mail tests use a local mail
sink. Tests that write after activation intentionally make the simple rollback
ineligible: rehearse write scenarios on MinIO and create a full backup before any
production write tests. Read-only acceptance checks preserve the rollback option.
Open the frontend and resume scheduler/workers only after acceptance.

## Interrupted operations and rollback

A host-level file lock prevents simultaneous migration commands. Journals use an
atomic, fsynced replacement per completed object. A DB marker is committed with
activation, so an interrupted commit boundary can be resolved:

```sh
bench --site SITE execute kanzlei_erp.s3_migration.recover --kwargs '{"run_id":"RUN_ID"}'
```

Recovery requires maintenance mode. It resolves `activating`,
`activation_prepared` and `rolling_back` from the atomic database marker. It
rejects inconsistent state for manual investigation. Repeat `copy` or `verify`
after interruption in those phases.

Before any post-activation File writes, rollback uses retained original bytes:

```sh
bench --site SITE execute kanzlei_erp.s3_migration.rollback --kwargs '{"run_id":"RUN_ID"}'
```

Rollback requires maintenance mode, the same migration marker, unchanged File
metadata, storage settings, object mappings and mutation epoch, plus intact
original bytes. It restores the old URLs/layout and removes only mappings newly
created by this run. Uploaded S3 objects remain; no original is deleted.

After a File or storage mutation, use a separately verified reverse migration in
a new maintenance window or restore a complete consistent backup. Merely turning
Use S3 off would strand new bytes and is blocked by the integration's settings
guard. Do not edit journals or counters to bypass this check.

## Verification evidence

Host-only safety tests (temporary directories and a memory S3 transport):

```sh
python3 -m unittest kanzlei_erp.tests.test_s3_migration
```

Run application integration tests only on a disposable Frappe site backed by an
isolated MinIO prefix. Host tests verify traversal/symlink rejection, nested trash,
shared uploads and previews, protected thumbnail ownership, inventory reports, collision-safe resume, source mutation, remote corruption, private journal writes and activation/recovery/rollback guards. They do not establish real Hetzner
connectivity or database rollback correctness; those require the rehearsals above.

The opt-in `run_minio_rehearsal(access_key, secret_key)` helper in the migration test module is restricted to `s3-test.localhost` or `s3-migration.localhost`, `stb-golke-test` and a unique prefix below `tests/s3-test.localhost/`. It covers real S3 copy/resumption, corruption and collision rejection, activation, process-loss recovery, rollback, Writer embeds, nested trash, standard thumbnails and WebDAV overwrites. Supply local test credentials through protected stdin and run only while the disposable site is exclusively reserved. The helper cleans up its fixture rows and S3 objects; it deliberately rejects a nonempty or already S3-enabled test site.
