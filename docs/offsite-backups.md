# Daily external recovery copies

The host timer `kanzlei-offsite-backup.timer` runs daily at **03:00 Europe/Berlin**
(including daylight saving changes). A missed run starts when the timer becomes
active again. It does not depend on a developer computer or Codex automation.

Production uses a **separate Hetzner project**, private bucket
`stb-golke-backups`, endpoint `https://nbg1.your-objectstorage.com`, region `nbg1`.
The working bucket remains in `fsn1`. Restic encrypts data and metadata before
upload and deduplicates unchanged content. Object Lock is disabled: repository
locks and retention need deletion. This is encrypted off-server storage, **not
immutable or ransomware-proof storage**; a server administrator can use its
backup credentials to delete copies.

## Contents and retention

The existing [S3 snapshot/restore format](s3-backups.md) includes the database,
site encryption key/config, remaining local files, complete managed S3 namespace,
production environment and release restore bundle. A deployment lock and durable
maintenance marker prevent overlap with deploy/restore during capture. The tool
refuses an already stopped or maintained site. ERP is stopped only during capture;
remote upload, full download/readback and retention happen after ERP resumes.

Every remote snapshot is downloaded and verified against all SHA-256 checksums.
Only then may retention prune old remote snapshots or clean local staging. Keep
7 daily, 4 weekly and 6 monthly snapshots (these sets can overlap). Retention is
restricted to this site's `kanzlei-erp` snapshots. The existing seven completed
deployment snapshots and protected pre-migration originals are independent.

Only one unpublished snapshot is staged in `/opt/kanzlei-erp/offsite/snapshot`.
A failed transfer preserves it; the next run publishes it before taking a fresh
snapshot. Incomplete captures need operator inspection. Space preflight estimates
two full copies plus 512 MiB from the latest deployment snapshot. Unexpected
growth can still exhaust space; capture then fails rather than producing a
successful incomplete backup.

The deployment lock also covers upload and readback while ERP is online, so a
simultaneous deployment cannot start another full snapshot on the same disk.
Readback/fetch recheck space, and an interrupted readback's temporary copy is
cleared only after validating the authoritative local snapshot.

## Installation and credentials

Run commands as the deployment user unless stated otherwise. Never place keys
in Git, command arguments or tickets. Create these private files with mode 600,
owned by `deploy:deploy`:

`/opt/kanzlei-erp/shared/offsite-backup.env`:

```dotenv
AWS_ACCESS_KEY_ID=REPLACE_BACKUP_PROJECT_KEY
AWS_SECRET_ACCESS_KEY=REPLACE_BACKUP_PROJECT_SECRET
AWS_DEFAULT_REGION=nbg1
RESTIC_REPOSITORY=s3:https://nbg1.your-objectstorage.com/stb-golke-backups/erp
RESTIC_PASSWORD_FILE=/opt/kanzlei-erp/shared/offsite-backup.password
```

`offsite-backup.password` contains an independently generated random password.
**Save it in an independent password manager/recovery location.** The password
is not included in the snapshot. Losing the server and its only password copy
makes the encrypted repository unrecoverable. Save the separate S3 credentials
and GitHub access needed to download the private recovery image as well.

As root, run `bash deploy/install-offsite-backup.sh` from the reviewed checkout.
This installs host tooling without changing the running ERP image. The official
Restic version and compressed binary SHA-256 are pinned in
`deploy/restic-release.json`; installation verifies them before replacement.
The installer neither initializes a repository nor enables scheduling.

```sh
python3 -B /opt/kanzlei-erp/backup-tools/offsite_backup.py init
sudo systemctl start kanzlei-offsite-backup.service
python3 -B /opt/kanzlei-erp/backup-tools/offsite_backup.py status
# Enable only after the first backup and recovery rehearsal succeed:
sudo systemctl enable --now kanzlei-offsite-backup.timer
```

The recovery password has an additional private local copy at
`.secrets/offsite-backup.password` during initial setup. `.secrets/` is excluded
from Git; it is not a replacement for an independent password manager.

## Status and failures

```sh
systemctl list-timers kanzlei-offsite-backup.timer
journalctl -u kanzlei-offsite-backup.service --since yesterday
python3 -B /opt/kanzlei-erp/backup-tools/offsite_backup.py status
python3 -B /opt/kanzlei-erp/backup-tools/offsite_backup.py list
```

`status` exits nonzero if no verified copy exists within 36 hours or if the latest
operation needs attention. `/opt/kanzlei-erp/offsite/status.json` contains stage,
timestamps and snapshot ID, without secrets. Service failures remain visible in
systemd/journal. **No email/chat alert recipient is configured**; integrate the
status command into the chosen external monitoring system for unattended alerts.
Third-party command errors are deliberately summarized to prevent key leakage.

If the server/process died during capture, inspect `shared/pending.json`, the
backup journal and temporary Docker containers. Only an unchanged deployment
whose marker belongs to this backup may be reopened using:

```sh
python3 -B /opt/kanzlei-erp/backup-tools/offsite_backup.py recover
```

Recovery first terminates leftover maintenance containers, then resumes and
checks the site. It cannot clear a deploy/restore marker. Inspect incomplete
staging before moving it aside; never delete retained originals or unrelated
deployment snapshots to force a run.

## Disaster recovery

On a prepared host with recovery credentials and the independent password:

1. Install the pinned Restic and host tool, and configure a recovery deployment
   root and its private `shared/offsite-backup.env` / password.
2. Run `list`, select the **full** snapshot ID, then download and verify:

   ```sh
   python3 -B /opt/kanzlei-erp/backup-tools/offsite_backup.py fetch \
     --snapshot FULL_64_CHARACTER_ID \
     --destination /opt/kanzlei-erp/backups/offsite-recovery
   ```

3. Provision Docker/Compose and the site's matching database credentials/project
   configuration. Pull the exact immutable image recorded in `manifest.json`;
   preserve GitHub package access outside the lost server.
4. Follow [the existing explicit restore procedure](s3-backups.md). Use the
   retrieved `release/deploy/manage.py`, or a compatible reviewed newer manager.
   Confirm the site explicitly. Recovery to local HTTP/`.localhost` **requires**
   a complete private `--s3-target` MinIO JSON, including its own keys and endpoint;
   production S3 credentials must not be reused locally.
5. Keep users and workers stopped until DB, object readback, permissions and
   HTTP checks succeed. Restore failure retains maintenance and the recovery
   marker. Do not merely turn S3 off after recovery.

For a test repository, `--allow-local-repository` allows HTTP only on `localhost`
or `127.0.0.1`. It does not permit an arbitrary unencrypted remote endpoint.
