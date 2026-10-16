#!/usr/bin/env bash
# Install host tooling; credentials must already be placed in shared/ with mode 600.
set -euo pipefail
umask 077
if [[ "$EUID" != 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_root=/opt/kanzlei-erp
test -f "$deploy_root/shared/offsite-backup.env"
test -f "$deploy_root/shared/offsite-backup.password"
install -d -m 755 "$deploy_root/backup-tools"
for name in manage.py offsite_backup.py install_restic.py restic-release.json; do
  install -m 644 "$source_dir/$name" "$deploy_root/backup-tools/$name"
done
python3 -B "$deploy_root/backup-tools/install_restic.py"
install -m 644 "$source_dir/systemd/kanzlei-offsite-backup.service" /etc/systemd/system/
install -m 644 "$source_dir/systemd/kanzlei-offsite-backup.timer" /etc/systemd/system/
chown deploy:deploy "$deploy_root/shared/offsite-backup.env" "$deploy_root/shared/offsite-backup.password"
chmod 600 "$deploy_root/shared/offsite-backup.env" "$deploy_root/shared/offsite-backup.password"
install -d -o deploy -g deploy -m 700 "$deploy_root/offsite"
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/kanzlei-offsite-backup.service /etc/systemd/system/kanzlei-offsite-backup.timer
echo "Host backup tooling installed. Initialize, run and verify the first backup before enabling the timer."
