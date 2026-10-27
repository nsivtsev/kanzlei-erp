#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077
if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: bash scripts/ci-image.sh LOCAL_IMAGE_TAG [all|application|upgrade]" >&2
  exit 1
fi
image=$1
scenario=${2:-all}
case "$scenario" in
  all) exec python3 "$(dirname "$0")/ci_scenarios.py" "$image" ;;
  application|upgrade) ;;
  *) echo "Unknown CI scenario: $scenario" >&2; exit 1 ;;
esac
task_root=$(mktemp -d)
project="kanzlei-ci-$scenario-$(date +%s)-$RANDOM"
frontend_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
mkdir -p "$task_root/shared"
export DB_PASSWORD ADMIN_PASSWORD
DB_PASSWORD=$(openssl rand -hex 32)
ADMIN_PASSWORD=$(openssl rand -hex 32)
cat > "$task_root/shared/production.env" <<EOF
DOMAIN=ci.localhost
SITE_NAME=ci.localhost
ACME_EMAIL=ci@example.invalid
DB_PASSWORD=$DB_PASSWORD
ADMIN_PASSWORD=$ADMIN_PASSWORD
PROJECT_NAME=$project
PLATFORM=${PLATFORM:-linux/amd64}
FRONTEND_PORT=$frontend_port
HTTPS_ENABLED=0
EOF
export APP_IMAGE=$image
compose=(docker compose --project-name "$project" --env-file "$task_root/shared/production.env" -f deploy/compose.yml)
cleanup() {
  result=$?
  if (( result != 0 )); then
    # Test data only. Do not dump site config or process environments.
    "${compose[@]}" logs --tail 80 backend frontend websocket queue-short queue-long scheduler >&2 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null || true
  rm -rf "$task_root"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
"${compose[@]}" config --quiet
"${compose[@]}" up -d --wait --wait-timeout 180 db redis-cache redis-queue
"${compose[@]}" run --rm -T configurator
if [[ $scenario == upgrade ]]; then
  # Variables expand inside this scenario's isolated container.
  # shellcheck disable=SC2016
  "${compose[@]}" run --rm -T --no-deps -v "$PWD/scripts/ci-suite.py:/tmp/ci-suite.py:ro" -e DB_PASSWORD -e ADMIN_PASSWORD backend bash -euc '
bench new-site suite-upgrade.localhost --db-root-password "$DB_PASSWORD" --admin-password "$ADMIN_PASSWORD" --mariadb-user-host-login-scope=%
bench --site suite-upgrade.localhost install-app erpnext
bench --site suite-upgrade.localhost install-app kanzlei_erp
env/bin/python /tmp/ci-suite.py legacy-seed --site suite-upgrade.localhost
bench --site suite-upgrade.localhost backup --with-files --compress
bench --site suite-upgrade.localhost install-app suite
bench --site suite-upgrade.localhost execute kanzlei_erp.suite_setup.configure
bench --site suite-upgrade.localhost migrate
bench --site suite-upgrade.localhost migrate
env/bin/python /tmp/ci-suite.py legacy-verify --site suite-upgrade.localhost
bench --site suite-upgrade.localhost set-config kanzlei_suite_install_pending 1
bench --site suite-upgrade.localhost execute "__import__(\"kanzlei_erp.suite_setup\", fromlist=[\"prepare_suite_install\"]).prepare_suite_install(\"suite\")"
bench --site suite-upgrade.localhost install-app suite --force
bench --site suite-upgrade.localhost set-config kanzlei_suite_install_pending 0
bench --site suite-upgrade.localhost execute kanzlei_erp.suite_setup.configure
env/bin/python /tmp/ci-suite.py legacy-verify --site suite-upgrade.localhost
'
  echo "Suite upgrade and interrupted installation verification passed."
  exit 0
fi
# Variables in this script expand inside the disposable container.
# shellcheck disable=SC2016
"${compose[@]}" run --rm -T --no-deps -v "$PWD/scripts/ci-site.py:/tmp/ci-site.py:ro" -v "$PWD/scripts/ci-suite.py:/tmp/ci-suite.py:ro" -v "$PWD/deploy/verify_drive.py:/tmp/verify_drive.py:ro" -e SITE_NAME=ci.localhost -e DB_PASSWORD -e ADMIN_PASSWORD backend bash -euc '
bench new-site ci.localhost --db-root-password "$DB_PASSWORD" --admin-password "$ADMIN_PASSWORD" --mariadb-user-host-login-scope=%
bench --site ci.localhost install-app erpnext
bench --site ci.localhost install-app suite
bench --site ci.localhost set-config kanzlei_suite_install_pending 1
bench --site ci.localhost execute "__import__(\"kanzlei_erp.suite_setup\", fromlist=[\"prepare_suite_install\"]).prepare_suite_install(\"suite\")"
bench --site ci.localhost install-app suite --force
bench --site ci.localhost set-config kanzlei_suite_install_pending 0
bench --site ci.localhost install-app kanzlei_erp
bench --site ci.localhost execute kanzlei_erp.suite_setup.configure
bench --site ci.localhost execute frappe.db.set_single_value --args "[\"System Settings\",\"time_zone\",\"Europe/Berlin\"]"
bench --site ci.localhost execute kanzlei_erp.localization.configure_german_language --kwargs "{\"user\":\"Administrator\"}"
bench --site ci.localhost migrate
bench --site ci.localhost migrate
bench --site ci.localhost set-config allow_tests true
bench --site ci.localhost set-config posteingang_acceptance_site true
# Only the ephemeral test container receives the framework test dependencies.
env/bin/python -c '\''import tomllib, subprocess; data=tomllib.load(open("apps/frappe/pyproject.toml", "rb")); deps=data["tool"]["bench"]["dev-dependencies"]; subprocess.run(["bench", "pip", "install", *[name+version for name,version in deps.items()]], check=True)'\''
env/bin/python /tmp/ci-site.py
bench --site ci.localhost run-tests --app kanzlei_erp --junit-xml-output /home/frappe/frappe-bench/sites/ci.localhost/private/test-results.xml
env/bin/python -c '\''import re, xml.etree.ElementTree as E; data=open("/home/frappe/frappe-bench/sites/ci.localhost/private/test-results.xml").read(); root=E.fromstring("<reports>"+re.sub(r"<\?xml[^>]*\?>", "", data)+"</reports>"); cases=list(root.iter("testcase")); assert any(case.find("skipped") is None for case in cases), "No application tests executed"; assert not list(root.iter("failure")) and not list(root.iter("error")), "Application tests failed"; print(f"Verified {len(cases)} application test results")'\''
bench --site ci.localhost set-config posteingang_acceptance_site false
bench --site ci.localhost set-config allow_tests false

'
"${compose[@]}" up -d backend frontend websocket queue-short queue-long scheduler
python3 - "$task_root" "$image" <<'PY'
import importlib.util
from pathlib import Path
import sys
import subprocess
spec = importlib.util.spec_from_file_location("manage", "deploy/manage.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
compose = module.Compose(Path(sys.argv[1]), Path.cwd(), sys.argv[2], module.Commands())
module.smoke_check(compose)
compose.running()
try:
    compose.call("run", "--rm", "-T", "--no-deps", "backend", "sleep", "30", timeout=2)
except subprocess.TimeoutExpired:
    pass
else:
    raise AssertionError("Timeout test did not time out")
remaining = compose.commands.run(["docker", "ps", "-aq", "--filter", "label=io.kanzlei.deployment=true",
    "--filter", "label=com.docker.compose.project=" + compose.config["PROJECT_NAME"]])
assert not remaining, "Timed-out maintenance container is still present"
PY
"${compose[@]}" run --rm -T --no-deps \
  -v "$PWD/scripts/verify-drive-security.py:/tmp/verify-drive-security.py:ro" \
  -v "$PWD/scripts/verify-drive-security.cjs:/tmp/verify-drive-security.cjs:ro" \
  -v "$PWD/tests:/home/frappe/frappe-bench/apps/kanzlei_erp/tests:ro" \
  backend bash -euc '
bench --site ci.localhost set-config allow_tests true
trap "bench --site ci.localhost set-config allow_tests false" EXIT
env/bin/python /tmp/verify-drive-security.py --site ci.localhost --fixtures --base http://frontend:8080 --socket http://websocket:9000
cd apps/kanzlei_erp
node --test tests/*.test.cjs
'
"${compose[@]}" run --rm -T --no-deps -v "$PWD/scripts/ci-suite.py:/tmp/ci-suite.py:ro" -v "$PWD/deploy/verify_drive.py:/tmp/verify_drive.py:ro" -e ADMIN_PASSWORD backend env/bin/python /tmp/ci-suite.py upload
"${compose[@]}" run --rm -T --no-deps -v "$PWD/scripts/ci-suite.py:/tmp/ci-suite.py:ro" -v "$PWD/deploy/verify_drive.py:/tmp/verify_drive.py:ro" backend env/bin/python /tmp/ci-suite.py verify
# Real backup/restore on the disposable site verifies both file archives and schema restoration.
"${compose[@]}" stop --timeout 120 backend frontend websocket queue-short queue-long scheduler
backup_container="$project-acceptance-backup"
"${compose[@]}" run --name "$backup_container" -T --no-deps backend bash -euc '
mkdir -p sites/ci.localhost/public/files sites/ci.localhost/private/files
printf "public snapshot" > sites/ci.localhost/public/files/deploy-check.txt
printf "private snapshot" > sites/ci.localhost/private/files/deploy-check.txt
bench --site ci.localhost execute frappe.db.set_single_value --args "[\"System Settings\",\"app_name\",\"CI snapshot\"]"
bench --site ci.localhost backup --with-files --compress --ignore-backup-conf --backup-path-db /home/frappe/frappe-bench/sites/ci.localhost/private/backups/acceptance/database.sql.gz --backup-path-files /home/frappe/frappe-bench/sites/ci.localhost/private/backups/acceptance/public.tgz --backup-path-private-files /home/frappe/frappe-bench/sites/ci.localhost/private/backups/acceptance/private.tgz --backup-path-conf /home/frappe/frappe-bench/sites/ci.localhost/private/backups/acceptance/site_config.json
bench --site ci.localhost execute frappe.db.set_single_value --args "[\"System Settings\",\"app_name\",\"CI newer state\"]"
printf "changed" > sites/ci.localhost/public/files/deploy-check.txt
rm sites/ci.localhost/private/files/deploy-check.txt
touch sites/ci.localhost/public/files/after-snapshot.txt sites/ci.localhost/private/files/after-snapshot.txt
'
mkdir -m 700 "$task_root/restore"
docker cp "$backup_container:/home/frappe/frappe-bench/sites/ci.localhost/private/backups/acceptance/." "$task_root/restore"
docker rm "$backup_container" >/dev/null
"${compose[@]}" run --rm -T --no-deps -v "$PWD/scripts/ci-suite.py:/tmp/ci-suite.py:ro" -v "$PWD/deploy/verify_drive.py:/tmp/verify_drive.py:ro" backend env/bin/python /tmp/ci-suite.py damage
chmod 600 "$task_root/restore/"*
python3 - "$task_root" "$image" <<'PY'
import importlib.util
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location("manage", "deploy/manage.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
compose = module.Compose(Path(sys.argv[1]), Path.cwd(), sys.argv[2], module.Commands())
module.restore_snapshot(compose, Path(sys.argv[1]) / "restore")
compose.bench("migrate")
compose.mode(False)
import json
value = compose.bench("execute", "frappe.db.get_single_value", "--args", json.dumps(["System Settings", "app_name"]))
assert value.splitlines()[-1] == "CI snapshot", "Database snapshot was not restored"
PY
# The command substitutions read files inside the container.
# shellcheck disable=SC2016
"${compose[@]}" run --rm -T --no-deps backend bash -euc '
test "$(cat sites/ci.localhost/public/files/deploy-check.txt)" = "public snapshot"
test "$(cat sites/ci.localhost/private/files/deploy-check.txt)" = "private snapshot"
test ! -e sites/ci.localhost/public/files/after-snapshot.txt
test ! -e sites/ci.localhost/private/files/after-snapshot.txt
'
"${compose[@]}" up -d backend frontend websocket queue-short queue-long scheduler
"${compose[@]}" run --rm -T --no-deps -v "$PWD/scripts/ci-suite.py:/tmp/ci-suite.py:ro" -v "$PWD/deploy/verify_drive.py:/tmp/verify_drive.py:ro" backend env/bin/python /tmp/ci-suite.py verify
python3 - "$task_root" "$image" <<'PY'
import importlib.util
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location("manage", "deploy/manage.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
compose = module.Compose(Path(sys.argv[1]), Path.cwd(), sys.argv[2], module.Commands())
module.smoke_check(compose)
compose.running()
import json
saved = json.loads((Path(sys.argv[1]) / "restore/site_config.json").read_text())
current = json.loads(compose.call("exec", "-T", "backend", "env/bin/python", "-c",
    "import json; print(json.dumps(json.load(open('sites/ci.localhost/site_config.json'))))"))
assert current.get("encryption_key") == saved.get("encryption_key"), "Site encryption key was not restored"
PY
echo "Image verification and disposable backup/restore passed."
