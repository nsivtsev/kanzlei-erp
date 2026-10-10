#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

if [[ $EUID -ne 0 || $# -ne 3 ]]; then
  echo "Usage (as root): bash scripts/bootstrap-production.sh DOMAIN ACME_EMAIL SSH_PUBLIC_KEY_FILE" >&2
  exit 1
fi
domain=$1
acme_email=$2
public_key=$3
deploy_root=/opt/kanzlei-erp
deploy_user=deploy
if [[ ! $domain =~ ^[a-z0-9][a-z0-9.-]+[a-z0-9]$ || $domain == *..* || ! $acme_email =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+$ ]]; then
  echo "Provide a lowercase domain and a valid ACME email." >&2
  exit 1
fi
ssh-keygen -l -f "$public_key" >/dev/null
# shellcheck source=/dev/null
source /etc/os-release
case "$ID" in
  ubuntu|debian) ;;
  *) echo "Only Ubuntu and Debian are supported." >&2; exit 1 ;;
esac
if [[ $(dpkg --print-architecture) != amd64 ]]; then
  echo "This production bootstrap requires an x86-64 server." >&2
  exit 1
fi
if [[ -f $deploy_root/shared/production.env ]]; then
  if ! grep -qxF "DOMAIN=$domain" "$deploy_root/shared/production.env"; then
    echo "An existing installation uses another domain; refusing to overwrite it." >&2
    exit 1
  fi
fi
apt-get update
apt-get install -y ca-certificates curl openssl python3
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Deployment requires Python 3.9 or newer")'
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl --fail --silent --show-error --location "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  codename=${UBUNTU_CODENAME:-$VERSION_CODENAME}
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $codename
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
docker compose version >/dev/null
systemctl enable --now docker
if ! id "$deploy_user" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$deploy_user"
fi
usermod -aG docker "$deploy_user"
deploy_home=$(getent passwd "$deploy_user" | cut -d: -f6)
install -d -m 0700 -o "$deploy_user" -g "$deploy_user" "$deploy_home/.ssh"
touch "$deploy_home/.ssh/authorized_keys"
while IFS= read -r key || [[ -n $key ]]; do
  [[ -z $key || $key == \#* ]] && continue
  if ! grep -qxF "$key" "$deploy_home/.ssh/authorized_keys"; then
    printf '%s\n' "$key" >> "$deploy_home/.ssh/authorized_keys"
  fi
done < "$public_key"
chmod 0600 "$deploy_home/.ssh/authorized_keys"
chown "$deploy_user:$deploy_user" "$deploy_home/.ssh/authorized_keys"
for directory in "$deploy_root" "$deploy_root/shared" "$deploy_root/releases" "$deploy_root/backups"; do
  install -d -m 0700 -o "$deploy_user" -g "$deploy_user" "$directory"
done
if [[ ! -f $deploy_root/shared/production.env ]]; then
  db_password=$(openssl rand -hex 32)
  admin_password=$(openssl rand -hex 32)
  cat > "$deploy_root/shared/production.env" <<EOF
DOMAIN=$domain
SITE_NAME=$domain
ACME_EMAIL=$acme_email
DB_PASSWORD=$db_password
ADMIN_PASSWORD=$admin_password
PROJECT_NAME=kanzlei-production
PLATFORM=linux/amd64
FRONTEND_PORT=8080
HTTPS_ENABLED=1
EOF
  chmod 0600 "$deploy_root/shared/production.env"
  chown "$deploy_user:$deploy_user" "$deploy_root/shared/production.env"
fi
echo "Server prepared. Credentials are in $deploy_root/shared/production.env; they are not printed."
echo "Next: DNS A/AAAA, ports 80/443, GHCR read-only login as deploy, and GitHub production secrets."
echo "The deploy user has Docker access, which is equivalent to root access."
