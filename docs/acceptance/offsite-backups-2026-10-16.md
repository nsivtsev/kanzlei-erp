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

## Production preparation

Installed host tooling and inactive systemd service/timer; the ERP image is
unchanged (`v0.5.1`). The independent recovery password also has a private local
copy outside production. Backup credentials are separate from application keys.

Destination: private `stb-golke-backups`, `nbg1.your-objectstorage.com`, region `nbg1`,
Hetzner project ID `16338956`. Owner and key project confirmed through signed
bucket metadata and ListBuckets. Bucket Object Lock is disabled, versioning is
disabled, and there was initially no bucket policy. A private Allow policy was
added for the replacement backup key: ListBucket/GetBucketLocation and
GetObject/PutObject/DeleteObject under `erp/*`; no public principal.

## Pending: Hetzner access failures

Repository initialization has not succeeded. Signed calls receive inconsistent
403 AccessDenied responses across object LIST, PUT, HEAD and DELETE; other calls
with the same key succeed. Both original and replacement keys were tested.
Metadata calls confirm the new key and bucket belong to the same project.
Virtual/path addressing and explicit `nbg1` region did not resolve Restic init.
Disabling optional SDK checksum headers did not resolve object access either.
Alternative signing regions were rejected and are not used in configuration.

Example failed request on 2026-10-16:

- operation: DeleteObject, region: nbg1
- request ID: `tx00000dbd7d7f67feb02be-006ad1f1e3-2b7979a0-nbg1-prod1-ceph5`
- response: HTTP 403, `AccessDenied`

No production snapshot has been created by this new service and the daily timer
is **not enabled**. ERP was not stopped during these S3 access probes. Existing
deployment snapshots and working file storage were not changed.

Do not mark daily offsite protection operational until the provider access issue
is resolved, the first full production snapshot has passed remote readback, and
the timer is enabled and its next trigger checked. Some tiny access-test objects
may remain under `erp/verification-*` or `erp/.verification-*` where DeleteObject
was refused; their body is only the literal non-sensitive `backup access test`.
