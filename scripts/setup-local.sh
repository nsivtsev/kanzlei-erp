#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  umask 077
  cat > .env <<EOF
LOCAL_DB_ROOT_PASSWORD=$(openssl rand -hex 24)
LOCAL_ADMIN_PASSWORD=$(openssl rand -hex 24)
EOF
  echo "Created local credentials in .env"
fi

compose=(docker compose --env-file .env -f .devcontainer/docker-compose.yml)
"${compose[@]}" config --quiet
"${compose[@]}" up -d

echo "Waiting for MariaDB..."
for attempt in {1..60}; do
  if "${compose[@]}" exec -T mariadb sh -lc 'mariadb-admin ping -h 127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" >/dev/null 2>&1'; then
    break
  fi
  if (( attempt == 60 )); then
    echo "MariaDB did not become ready; inspect docker compose logs." >&2
    exit 1
  fi
  sleep 2
done

"${compose[@]}" exec -T frappe bash -lc '
set -euo pipefail
cd /workspace/development
if [[ ! -d frappe-bench/apps/frappe ]]; then
  bench init --skip-redis-config-generation --frappe-branch version-16 frappe-bench
fi
cd frappe-bench
bench set-config -g db_host mariadb
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379
bench set-config -g socketio_port 9100
sed -i "/^redis_/d" Procfile
if [[ ! -d sites/development.localhost ]]; then
  bench new-site --db-root-password "$LOCAL_DB_ROOT_PASSWORD" --admin-password "$LOCAL_ADMIN_PASSWORD" --mariadb-user-host-login-scope=% development.localhost
fi
if [[ ! -d apps/erpnext ]]; then
  bench get-app --branch version-16 erpnext
fi
if ! bench --site development.localhost list-apps | grep -q "^erpnext"; then
  bench --site development.localhost install-app erpnext
fi
if [[ ! -e apps/kanzlei_erp ]]; then
  ln -s /workspace apps/kanzlei_erp
fi
if [[ "$(readlink apps/kanzlei_erp)" != "/workspace" ]]; then
  echo "apps/kanzlei_erp must link to /workspace" >&2
  exit 1
fi
grep -qxF kanzlei_erp sites/apps.txt || printf "kanzlei_erp\n" >> sites/apps.txt
uv pip install --quiet -e /workspace --python env/bin/python
if ! bench --site development.localhost list-apps | grep -q "^kanzlei_erp"; then
  bench --site development.localhost install-app kanzlei_erp
fi
bench --site development.localhost set-config developer_mode 1
bench --site development.localhost set-config allow_tests true
bench --site development.localhost migrate > /tmp/kanzlei-migrate.log 2>&1
bench --site development.localhost clear-cache
touch .kanzlei-ready
'

echo "Kanzlei ERP setup finished. Open http://development.localhost:8100"
echo "Login: Administrator; password is LOCAL_ADMIN_PASSWORD in .env"
