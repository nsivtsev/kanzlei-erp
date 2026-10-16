# Offsite backup acceptance — 2026-10-16

## Verified

- 116 host deployment tests passed, including offsite backup failure/retry,
  credential permissions, no secret leakage, maintenance state, disk preflight,
  deployment coordination, explicit fetch and existing backup/restore compatibility.
- Ruff lint/format, Python 3.9 syntax compatibility, shell syntax and diff checks passed.
- Independent code review findings were fixed: interrupted stop preserves maintenance;
  deploy and readback cannot exhaust disk concurrently; global maintenance is respected;
  a partial readback is removed before retry free-space checks.
- Real isolated MinIO rehearsal: encrypted Restic repository, full remote readback,
  explicit snapshot fetch, intentional database/object damage, database/files/S3
  restoration and preserved site encryption key. Test site: `s3-test.localhost`;
  4 test originals restored. Successful repeat snapshot:
  `7e901d29d8e54de4635f0bf5d92b8db297c87af0e8bce27cb0f80976a663e638`.
- Repeat rehearsal exposed Frappe's directory cleanup behavior inside
  `private/backups`. Deployment temporary snapshots now use
  `private/kanzlei-snapshots`; a regression test covers that placement.
- Official Restic 0.19.1 binaries were verified against the pinned SHA-256.

## Production acceptance

The first production offsite snapshot completed successfully on 2026-10-16 at
**12:57:48 Europe/Berlin**. The complete remote snapshot was downloaded and every
file verified against the snapshot's SHA-256 manifest before local staging was
removed. Restic retention and repository check also succeeded; the systemd
service exited with status 0. This is a full remote readback test; the separate
database/object damage-and-restore rehearsal was performed on isolated MinIO.

- Snapshot ID: `5bb4d6a625749c61e3195d0715a2f6dcc3041b61b7d51a0200e4e79ff7863f4b`
- Snapshot timestamp: `2026-10-16T12:56:18.064800416+02:00`
- Restored logical size reported by Restic: **2,352,954,167 bytes**
- Managed S3 objects: **4,919**, totaling **1,322,114,167 bytes**
- Outer manifest: **4,944** checksummed files
- ERP resumed: **12:55:47 Europe/Berlin**, before the external transfer/readback
- First capture maintenance: approximately **9 minutes**
- Timer: **enabled and active**, daily at **03:00 Europe/Berlin**
- Next scheduled trigger at acceptance: **2026-10-17 03:00 Europe/Berlin**
- Retention: 7 daily, 4 weekly, 6 monthly; existing deployment retention unchanged
- Anonymous request to the existing repository config: **HTTP 403**
- ERP ping after completion: **pong**; no maintenance marker remains
- Local snapshot and verification staging were removed after successful validation

Installed host tooling matches the reviewed implementation. The ERP image is
unchanged (`v0.5.1`). The independent recovery password has a private local copy
outside production. Backup credentials are separate from application keys.

Destination: private `stb-golke-backups`, `nbg1.your-objectstorage.com`, region `nbg1`,
Hetzner project ID `16338956`. Owner and key project confirmed through signed
bucket metadata and ListBuckets. Bucket Object Lock is disabled, versioning is
disabled, and there was initially no bucket policy. A private Allow policy was
added for the replacement backup key: ListBucket/GetBucketLocation and
GetObject/PutObject/DeleteObject under `erp/*`; no public principal.

## Earlier Hetzner access failures — no longer blocking

Initial repository initialization failed. Signed calls received inconsistent
403 AccessDenied responses across object LIST, PUT, HEAD and DELETE; other calls
with the same key succeed. Both original and replacement keys were tested.
Metadata calls confirm the new key and bucket belong to the same project.
Virtual/path addressing and explicit `nbg1` region did not resolve Restic init.
Disabling optional SDK checksum headers did not resolve object access either.
Alternative signing regions were rejected and are not used in configuration.
An independent curl SigV4 probe from production returned PUT 200, GET 403 and
DELETE 204 consecutively for the same test object. A ready-to-send provider
report is in [the support draft](hetzner-backup-support-2026-10-16.md); it was not
sent externally. The root cause was not established.

Example failed request on 2026-10-16:

- operation: DeleteObject, region: nbg1
- request ID: `tx00000dbd7d7f67feb02be-006ad1f1e3-2b7979a0-nbg1-prod1-ceph5`
- response: HTTP 403, `AccessDenied`

A later recheck succeeded for PUT, HEAD, GET and DELETE. A failed initialization
had left one encrypted Restic key without a config, data packs or snapshots. That
key was copied to protected recovery storage and reread/compared before removing
it from the active key directory. Initialization then succeeded using the same
independent recovery password, followed by the successful production backup above.
The archived incomplete key is retained under `erp/recovery/initialization-20261016/`
and `/opt/kanzlei-erp/offsite/initialization-recovery/`.

ERP was not stopped during access diagnostics. Existing deployment snapshots and
original files were not deleted. Some tiny non-sensitive access-test objects may
remain under `erp/verification-*` or `erp/.verification-*` where early deletion
was refused; their body is only the literal `backup access test`.
