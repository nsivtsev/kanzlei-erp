# Backups with S3 storage

For scheduled encrypted external copies, see [daily offsite backups](offsite-backups.md).

Deployment and offsite snapshots contain the database, local files and site
configuration, **not S3 objects**. The bucket must be protected at provider level
(Hetzner Object Storage versioning/backup). Until that is configured, a deleted or
damaged object cannot be recovered from a new snapshot.

The manifest field `s3_referenced` records whether the site stored files in S3. A
production restore reconnects to the existing bucket and leaves its objects unchanged;
objects deleted after the snapshot must be recovered from the provider backup. A local
HTTP/`.localhost` restore of such a snapshot is refused, because it would attach to
the original bucket.

## Restore older snapshots that contain S3 objects

Snapshots made before this change include an `s3/manifest.json` and object payloads
and remain fully restorable as described below. They count toward the seven retained
snapshots, so later deployments remove them.

Use the deployment manager's existing explicit restore command. It validates
local snapshot checksums before starting Docker changes, restores the database
and site configuration, and then restores S3 before migrations and smoke checks.
Any missing payload, upload failure or remote readback mismatch leaves the site
stopped in maintenance mode with the recovery marker intact. Extra objects at the
destination are retained as recovery data; the restored database controls which
objects can be accessed.

A normal production recovery uses the protected connection settings restored with
the database. To restore into another bucket, pass `--s3-target /absolute/path.json`.
For a local HTTP/`.localhost` recovery this argument is mandatory for an S3 snapshot.
The file must have permissions `600`, and must contain **every** field below. No
connection value falls back to the source site's production settings.

```json
{
  "bucket": "kanzlei-local",
  "aws_key": "LOCAL_ACCESS_KEY",
  "aws_secret": "LOCAL_SECRET_KEY",
  "endpoint_url": "http://minio:9000",
  "signature_version": "s3v4",
  "kanzlei_s3_region": "us-east-1",
  "kanzlei_s3_addressing_style": "path",
  "kanzlei_s3_public_endpoint": "http://localhost:9002",
  "kanzlei_s3_prefix": "THE_SNAPSHOT_PREFIX/"
}
```

The prefix must match the snapshot because the restored database stores object
keys. A separate target bucket and credentials provide isolation. Destination
settings are saved only after every restored object has been reread and its
SHA-256 verified. The target JSON is mounted read-only and its contents are never
put into a command argument or log.

For a direct site-side recovery, with the application and all writers stopped:

```sh
bench --site SITE execute kanzlei_erp.s3_backup.restore_snapshot \
  --kwargs '{"path":"/absolute/snapshot/s3","target":"/private/minio-target.json"}'
```

`export_snapshot(path)` creates a new directory and intentionally refuses to reuse
an existing export. Incomplete snapshots do not receive a completed manifest and
are excluded from retention. Neither backup nor restore automatically deletes
original local files or extra destination objects.
