#!/usr/bin/env python3
"""Deploy an immutable Frappe image; retain recovery state across SSH interruptions."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

APP_SERVICES = ["backend", "frontend", "websocket", "queue-short", "queue-long", "scheduler"]
WEB_SERVICES = ["backend", "frontend", "websocket"]
WORKERS = ["queue-short", "queue-long", "scheduler"]
IMAGE_RE = re.compile(r"ghcr\.io/nsivtsev/kanzlei-erp@sha256:[a-f0-9]{64}\Z")
TAG_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
DOMAIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")


def atomic_json(path, value):
	path = Path(path)
	path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
	fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
	try:
		with os.fdopen(fd, "w") as handle:
			json.dump(value, handle, indent=2, sort_keys=True)
			handle.write("\n")
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary, path)
	finally:
		if os.path.exists(temporary):
			os.unlink(temporary)


def read_json(path):
	return json.loads(Path(path).read_text())


def validate_release(image, revision, tag):
	if not IMAGE_RE.fullmatch(image):
		raise ValueError("Image must be a Kanzlei GHCR digest, not a mutable tag")
	if not re.fullmatch(r"[a-f0-9]{40}", revision):
		raise ValueError("Revision must be a full Git commit SHA")
	if not TAG_RE.fullmatch(tag):
		raise ValueError("Release tag must have the form vX.Y.Z")


def read_config(root, path=None):
	path = path or root / "shared" / "production.env"
	if path.stat().st_mode & 0o077:
		raise ValueError("production.env must have permissions 600")
	config = {}
	for line in path.read_text().splitlines():
		line = line.strip()
		if not line or line.startswith("#"):
			continue
		key, separator, value = line.partition("=")
		if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
			raise ValueError("production.env accepts plain KEY=value entries only")
		if any(character.isspace() for character in value) or any(
			character in value for character in "$\"'`\\"
		):
			raise ValueError("Unsupported quoting or interpolation in configuration key " + key)
		if key in config:
			raise ValueError("Duplicate configuration key " + key)
		config[key] = value
	for key in ("DOMAIN", "SITE_NAME", "ACME_EMAIL", "DB_PASSWORD", "ADMIN_PASSWORD"):
		if not config.get(key) or config[key].startswith("REPLACE_"):
			raise ValueError("Configure " + key + " in production.env")
	if not DOMAIN_RE.fullmatch(config["DOMAIN"]) or ".." in config["DOMAIN"]:
		raise ValueError("Invalid DOMAIN")
	if config["SITE_NAME"] != config["DOMAIN"]:
		raise ValueError("SITE_NAME must equal DOMAIN for this single-site deployment")
	config.setdefault("PROJECT_NAME", "kanzlei-production")
	config.setdefault("PLATFORM", "linux/amd64")
	config.setdefault("FRONTEND_PORT", "8080")
	config.setdefault("HTTPS_ENABLED", "1")
	if not re.fullmatch(r"kanzlei-[a-z0-9-]+", config["PROJECT_NAME"]):
		raise ValueError(
			"PROJECT_NAME must start with kanzlei- and contain lowercase letters, digits or hyphens"
		)
	if config["PLATFORM"] not in ("linux/amd64", "linux/arm64"):
		raise ValueError("Unsupported PLATFORM")
	if not config["FRONTEND_PORT"].isdigit() or not 1024 <= int(config["FRONTEND_PORT"]) <= 65535:
		raise ValueError("FRONTEND_PORT must be between 1024 and 65535")
	if config["HTTPS_ENABLED"] not in ("0", "1"):
		raise ValueError("HTTPS_ENABLED must be 0 or 1")
	if config["HTTPS_ENABLED"] == "0" and not config["DOMAIN"].endswith(".localhost"):
		raise ValueError("HTTP-only mode is restricted to local .localhost verification sites")
	return config


@contextlib.contextmanager
def deployment_lock(root):
	root.mkdir(parents=True, exist_ok=True, mode=0o700)
	with (root / ".deploy.lock").open("a") as lock:
		try:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError as error:
			raise RuntimeError("Another deployment or restore is running") from error
		yield


class Commands:
	def run(self, args, *, env=None, timeout=1800):
		result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout, check=False)
		if result.returncode:
			output = (result.stdout + result.stderr)[-8000:]
			for key, value in (env or {}).items():
				if any(word in key for word in ("PASSWORD", "TOKEN", "SECRET")) and value:
					output = output.replace(value, "[REDACTED]")
			raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}\n{output}")
		return result.stdout.strip()


class UnsafeContainerError(RuntimeError):
	"""A maintenance process could still be changing persistent data."""


class Compose:
	def __init__(self, root, release, image, commands, config=None):
		self.root, self.release = Path(root), Path(release)
		self.image, self.commands = image, commands
		self.config = config or read_config(self.root)
		self.env = {**os.environ, **self.config, "APP_IMAGE": image}
		self.prefix = [
			"docker",
			"compose",
			"--project-name",
			self.config["PROJECT_NAME"],
			"--env-file",
			str(self.root / "shared" / "production.env"),
			"-f",
			str(self.release / "deploy" / "compose.yml"),
		]
		if self.config["HTTPS_ENABLED"] == "1":
			self.prefix += ["--profile", "https"]

	def call(self, *args, timeout=1800):
		args = list(args)
		container = None
		if args and args[0] == "run":
			if "--name" in args:
				container = args[args.index("--name") + 1]
			else:
				container = self.config["PROJECT_NAME"] + "-deployment-task"
				args[1:1] = ["--name", container]
			args[1:1] = ["--label", "io.kanzlei.deployment=true"]
		try:
			return self.commands.run(self.prefix + args, env=self.env, timeout=timeout)
		except BaseException:
			if container:
				try:
					# Killing the Compose client alone does not terminate its one-off container.
					self.commands.run(["docker", "rm", "-f", container], timeout=180)
				except Exception as error:
					try:
						remaining = self.commands.run(
							["docker", "ps", "-aq", "--filter", "name=^/" + container + "$"]
						)
					except Exception:
						remaining = "unknown"
					if remaining:
						raise UnsafeContainerError(
							"Cannot terminate maintenance container "
							+ container
							+ "; inspect it before recovery"
						) from error
			raise

	def clear_interrupted_tasks(self):
		ids = self.commands.run(
			[
				"docker",
				"ps",
				"-aq",
				"--filter",
				"label=io.kanzlei.deployment=true",
				"--filter",
				"label=com.docker.compose.project=" + self.config["PROJECT_NAME"],
			]
		).splitlines()
		if ids:
			self.commands.run(["docker", "rm", "-f", *ids], timeout=180)

	def bench(self, *args):
		return self.call(
			"run", "--rm", "-T", "--no-deps", "backend", "bench", "--site", self.config["SITE_NAME"], *args
		)

	def mode(self, enabled):
		self.bench("set-config", "maintenance_mode", "1" if enabled else "0")

	def stop(self):
		self.call("stop", "--timeout", "120", *APP_SERVICES)

	def web(self):
		self.call("up", "-d", *WEB_SERVICES)
		if self.config["HTTPS_ENABLED"] == "1":
			# Recreate to apply the release's Caddyfile bind mount.
			self.call("up", "-d", "--force-recreate", "caddy")

	def workers(self):
		self.call("up", "-d", *WORKERS)
		self.bench("enable-scheduler")

	def running(self):
		observations = []
		for sample in range(2):
			counts = {}
			for service in APP_SERVICES:
				ids = self.call("ps", "-q", service).splitlines()
				if len(ids) != 1:
					raise RuntimeError("Service is missing: " + service)
				status = self.commands.run(
					["docker", "inspect", "--format", "{{.State.Running}} {{.RestartCount}}", ids[0]]
				)
				fields = status.split()
				if len(fields) != 2 or fields[0] != "true":
					raise RuntimeError("Service is not running: " + service)
				counts[service] = fields[1]
			observations.append(counts)
			if sample == 0:
				time.sleep(3)
		if observations[0] != observations[1]:
			raise RuntimeError("Application services are restarting")


def smoke_check(compose):
	config = compose.config
	base = (
		"https://" + config["DOMAIN"]
		if config["HTTPS_ENABLED"] == "1"
		else "http://127.0.0.1:" + config["FRONTEND_PORT"]
	)
	opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

	def get(path):
		request = urllib.request.Request(base + path, headers={"Host": config["DOMAIN"]})
		with opener.open(request, timeout=15) as response:
			if response.status != 200:
				raise RuntimeError("Unexpected HTTP response")
			return response.read()

	last_error = None
	for _attempt in range(60):
		try:
			get("/login")
			if json.loads(get("/api/method/frappe.ping"))["message"] != "pong":
				raise RuntimeError("Frappe ping did not return pong")
			break
		except (OSError, ValueError, KeyError, RuntimeError) as error:
			last_error = error
			time.sleep(5)
	else:
		raise RuntimeError("Public HTTP/TLS check failed") from last_error
	assets = json.loads(
		compose.call(
			"exec",
			"-T",
			"backend",
			"env/bin/python",
			"-c",
			"import json; print(json.dumps(json.load(open('sites/assets/assets.json'))))",
		)
	)
	for name in ("kanzlei_navigation.bundle.js", "mandant_documents.bundle.js", "fibu_questions.bundle.js"):
		path = assets.get(name)
		if not isinstance(path, str) or not path.startswith("/assets/kanzlei_erp/"):
			raise RuntimeError("Built asset is missing: " + name)
		if not get(path):
			raise RuntimeError("Empty built asset: " + name)
	get("/assets/kanzlei_erp/js/fibu_workflow.js")
	get("/assets/kanzlei_erp/js/fibu_checklist.js")
	# Recovery may restore a pre-Suite snapshot, so inspect the restored site's apps.
	apps = json.loads(compose.call("exec", "-T", "backend", "bench", "--site", config["SITE_NAME"], "execute", "frappe.get_installed_apps"))
	if "suite" in apps:
		html = get("/drive").decode("utf-8")
		paths = re.findall(r'''(?:src|href)=["'](/assets/suite/frontend/[^"']+\.(?:js|css))["']''', html)
		if not any(path.endswith(".js") for path in paths):
			raise RuntimeError("Drive HTML does not reference its built JavaScript")
		for path in set(paths):
			if not get(path):
				raise RuntimeError("Empty Suite asset")


def publish_state(root, state):
	atomic_json(root / "shared" / "current.json", state)
	temporary = root / (".current-" + uuid.uuid4().hex)
	temporary.symlink_to(state["release"])
	os.replace(temporary, root / "current")


def checksum(path):
	digest = hashlib.sha256()
	with path.open("rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def verify_backup(backup):
	manifest = read_json(backup / "manifest.json")
	for name in ("database.sql.gz", "public.tgz", "private.tgz", "site_config.json", "production.env"):
		if name not in manifest["checksums"]:
			raise ValueError("Incomplete backup: " + name)
	for name, expected in manifest["checksums"].items():
		path = (backup / name).resolve()
		if (
			not path.is_relative_to(backup)
			or not path.is_file()
			or path.stat().st_size == 0
			or checksum(path) != expected
		):
			raise ValueError("Backup checksum mismatch: " + name)
	return manifest


class Deployer:
	def __init__(self, root, release, image, revision, tag, *, commands=None, smoke=None, resume=False):
		validate_release(image, revision, tag)
		self.root, self.release = Path(root).resolve(), Path(release).resolve()
		if not self.release.is_relative_to(self.root / "releases"):
			raise ValueError("Release must be inside the deployment root's releases directory")
		self.image, self.revision, self.tag = image, revision, tag
		self.commands, self.smoke = commands or Commands(), smoke or smoke_check
		self.resume = resume
		self.pending_path = self.root / "shared" / "pending.json"
		self.config = read_config(self.root)
		self.new = Compose(self.root, self.release, image, self.commands, self.config)

	def backup(self, previous):
		identifier = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
		backup = self.root / "backups" / identifier
		backup.mkdir(parents=True, mode=0o700)
		compose = Compose(self.root, previous["release"], previous["image"], self.commands, self.config)
		snapshot = (
			"/home/frappe/frappe-bench/sites/"
			+ self.config["SITE_NAME"]
			+ "/private/backups/deploy-"
			+ identifier
		)
		container = self.config["PROJECT_NAME"] + "-backup-" + identifier.lower()
		try:
			compose.call(
				"run",
				"--name",
				container,
				"-T",
				"--no-deps",
				"backend",
				"bench",
				"--site",
				self.config["SITE_NAME"],
				"backup",
				"--with-files",
				"--compress",
				"--ignore-backup-conf",
				"--backup-path-db",
				snapshot + "/database.sql.gz",
				"--backup-path-files",
				snapshot + "/public.tgz",
				"--backup-path-private-files",
				snapshot + "/private.tgz",
				"--backup-path-conf",
				snapshot + "/site_config.json",
			)
			self.commands.run(["docker", "cp", container + ":" + snapshot + "/.", str(backup)])
		finally:
			with contextlib.suppress(Exception):
				self.commands.run(["docker", "rm", "-f", container])
		shutil.copy2(self.root / "shared" / "production.env", backup / "production.env")
		shutil.copytree(Path(previous["release"]) / "deploy", backup / "release" / "deploy")
		for path in backup.rglob("*"):
			if path.is_file():
				path.chmod(0o600)
		atomic_json(
			backup / "manifest.json",
			{
				"previous": previous,
				"checksums": {
					str(path.relative_to(backup)): checksum(path)
					for path in backup.rglob("*")
					if path.is_file()
				},
			},
		)
		verify_backup(backup)
		# The exported host snapshot is authoritative; avoid accumulating a second copy in sites.
		compose.call("run", "--rm", "-T", "--no-deps", "backend", "rm", "-rf", snapshot)
		print("Backup complete: " + str(backup), flush=True)
		return str(backup)

	def initialize(self):
		self.new.call(
			"run",
			"--rm",
			"-T",
			"--no-deps",
			"-e",
			"SITE_NAME",
			"-e",
			"DB_PASSWORD",
			"-e",
			"ADMIN_PASSWORD",
			"backend",
			"bash",
			"-euc",
			"""
if [ ! -f "sites/$SITE_NAME/site_config.json" ]; then
  bench new-site "$SITE_NAME" --db-root-password "$DB_PASSWORD" --admin-password "$ADMIN_PASSWORD" --mariadb-user-host-login-scope=%
fi
apps="$(bench --site "$SITE_NAME" list-apps)"
if ! printf '%s\\n' "$apps" | grep -Eq '^erpnext[[:space:]]'; then
  bench --site "$SITE_NAME" install-app erpnext
fi
""",
		)

	def ensure_apps(self):
		"""Install newly bundled apps on existing sites, after the recovery snapshot."""
		self.new.call(
			"run", "--rm", "-T", "--no-deps", "-e", "SITE_NAME", "backend", "bash", "-euc",
			"""
apps="$(bench --site "$SITE_NAME" list-apps)"
suite_pending="$(env/bin/python -c 'import json, os; print(json.load(open("sites/"+os.environ["SITE_NAME"]+"/site_config.json")).get("kanzlei_suite_install_pending", 0))')"
if ! printf '%s\\n' "$apps" | grep -Eq '^suite[[:space:]]' || [ "$suite_pending" = 1 ]; then
  force=()
  if printf '%s\\n' "$apps" | grep -Eq '^suite[[:space:]]'; then
    force=(--force)
  fi
  bench --site "$SITE_NAME" set-config kanzlei_suite_install_pending 1
  bench --site "$SITE_NAME" execute '__import__("kanzlei_erp.suite_setup", fromlist=["prepare_suite_install"]).prepare_suite_install("suite")'
  bench --site "$SITE_NAME" install-app suite "${force[@]}"
  bench --site "$SITE_NAME" set-config kanzlei_suite_install_pending 0
fi
if ! printf '%s\\n' "$apps" | grep -Eq '^kanzlei_erp[[:space:]]'; then
  bench --site "$SITE_NAME" install-app kanzlei_erp
fi
bench --site "$SITE_NAME" set-config developer_mode 0
bench --site "$SITE_NAME" set-config allow_tests false
bench --site "$SITE_NAME" execute kanzlei_erp.suite_setup.configure
""",
		)

	def deploy(self):
		with deployment_lock(self.root):
			self._deploy()

	def _deploy(self):
		previous_path = self.root / "shared" / "current.json"
		previous = read_json(previous_path) if previous_path.exists() else None
		pending = read_json(self.pending_path) if self.pending_path.exists() else None
		if pending and not self.resume:
			raise RuntimeError(
				"An unfinished deployment exists; inspect pending.json and explicitly resume or restore"
			)
		if pending and (pending.get("image"), pending.get("revision"), pending.get("tag")) != (
			self.image,
			self.revision,
			self.tag,
		):
			raise RuntimeError("Resume must use the unfinished release's exact image, revision and tag")
		if self.resume and not pending:
			raise RuntimeError("No unfinished deployment to resume")
		if previous:
			validate_release(previous["image"], previous["revision"], previous["tag"])
			if previous["site"] != self.config["SITE_NAME"]:
				raise ValueError("Configured site differs from the current installation")
			if previous.get("project", self.config["PROJECT_NAME"]) != self.config["PROJECT_NAME"]:
				raise ValueError("Configured PROJECT_NAME differs from the current installation")
			if tuple(map(int, TAG_RE.fullmatch(self.tag).groups())) < tuple(
				map(int, TAG_RE.fullmatch(previous["tag"]).groups())
			):
				raise ValueError("Cannot deploy an older release; use an explicit backup restore")
			if self.tag == previous["tag"] and (self.image, self.revision) != (
				previous["image"],
				previous["revision"],
			):
				raise ValueError("An existing release tag cannot be reassigned to another image or commit")
		if pending:
			previous = pending["previous"]
		elif previous and (previous["tag"], previous["revision"], previous["image"]) == (
			self.tag, self.revision, self.image
		):
			# Tag pushes and successful CI events can both enqueue this release.
			# Check the already deployed stack without replaying database changes.
			current = Compose(self.root, previous["release"], self.image, self.commands, self.config)
			self.smoke(current)
			current.running()
			print("Already deployed and healthy: " + self.tag, flush=True)
			return
		print("Preparing " + self.tag + " (" + self.revision + ")", flush=True)
		self.new.call("config", "--quiet")
		self.commands.run(["docker", "pull", "--platform", self.config["PLATFORM"], self.image], timeout=1800)
		actual = self.commands.run(
			[
				"docker",
				"image",
				"inspect",
				"--format",
				'{{index .Config.Labels "org.opencontainers.image.revision"}}',
				self.image,
			]
		)
		if actual != self.revision:
			raise ValueError("Image revision label does not match the requested commit")
		self.new.clear_interrupted_tasks()
		old = (
			Compose(self.root, previous["release"], previous["image"], self.commands, self.config)
			if previous
			else None
		)
		stopped = False
		try:
			if not pending:
				if old:
					old.mode(True)
					stopped = True
					old.stop()
					backup = self.backup(previous)
				else:
					backup = None
				pending = {
					"image": self.image,
					"revision": self.revision,
					"tag": self.tag,
					"release": str(self.release),
					"previous": previous,
					"backup": backup,
					"operation": "deploy",
				}
				atomic_json(self.pending_path, pending)
			elif pending.get("backup"):
				verify_backup(Path(pending["backup"]).resolve())
			# Stop a partially started candidate on explicit resume as well.
			self.new.stop()
			if not previous:
				self.new.call(
					"up",
					"-d",
					"--wait",
					"--wait-timeout",
					"180",
					"db",
					"redis-cache",
					"redis-queue",
					timeout=240,
				)
			self.new.call("run", "--rm", "-T", "configurator")
			if not previous:
				self.initialize()
			self.ensure_apps()
			if not previous:
				self.new.bench("execute", "frappe.db.set_single_value", "--args", '["System Settings","time_zone","Europe/Berlin"]')
				self.new.bench("execute", "kanzlei_erp.localization.configure_german_language", "--kwargs", '{"user":"Administrator"}')
			print("Applying migrations", flush=True)
			self.new.bench("migrate")
			self.new.bench("clear-cache")
			self.new.mode(False)
			self.new.web()
			self.smoke(self.new)
			self.new.workers()
			self.new.running()
			state = {
				"image": self.image,
				"revision": self.revision,
				"tag": self.tag,
				"release": str(self.release),
				"site": self.config["SITE_NAME"],
				"project": self.config["PROJECT_NAME"],
			}
			publish_state(self.root, state)
			self.pending_path.unlink()
			# Retention errors must never roll application code back after committed migrations.
			try:
				self.prune_backups()
			except OSError as error:
				print("Deployment succeeded; backup retention needs attention: " + str(error), flush=True)
			print("Deployment complete: " + self.tag + " " + self.image, flush=True)
		except BaseException as error:
			if isinstance(error, UnsafeContainerError) and not self.pending_path.exists():
				atomic_json(self.pending_path, {"operation": "blocked", "reason": str(error)})
			if self.pending_path.exists():
				with contextlib.suppress(Exception):
					self.new.mode(True)
				with contextlib.suppress(Exception):
					self.new.stop()
				print("Deployment stopped; recovery state: " + str(self.pending_path), flush=True)
			elif old and stopped:
				old.mode(False)
				old.web()
				old.workers()
			raise

	def prune_backups(self):
		backups = []
		for path in sorted((self.root / "backups").glob("*")):
			try:
				verify_backup(path.resolve())
			except (OSError, ValueError, KeyError, TypeError):
				continue
			backups.append(path)
		for backup in backups[:-7]:
			shutil.rmtree(backup)


def restore_snapshot(compose, backup):
	# Mount the host backup read-only; passwords remain process environment values.
	compose.call(
		"run",
		"--rm",
		"-T",
		"--no-deps",
		"--user",
		"root",
		"-e",
		"DB_PASSWORD",
		"-e",
		"SITE_NAME",
		"-v",
		str(backup) + ":/restore:ro",
		"backend",
		"bash",
		"-euc",
		"""
install -d -o frappe -g frappe -m 700 /tmp/kanzlei-restore
cp /restore/database.sql.gz /restore/public.tgz /restore/private.tgz /tmp/kanzlei-restore/
chown -R frappe:frappe /tmp/kanzlei-restore
install -o frappe -g frappe -m 600 /restore/site_config.json "sites/$SITE_NAME/site_config.json"
export HOME=/home/frappe
exec su -p -s /bin/bash frappe -c '
set -euo pipefail
bench --site "$SITE_NAME" set-config maintenance_mode 1
rm -rf "sites/$SITE_NAME/public/files" "sites/$SITE_NAME/private/files"
mkdir -p "sites/$SITE_NAME/public/files" "sites/$SITE_NAME/private/files"
bench --site "$SITE_NAME" restore /tmp/kanzlei-restore/database.sql.gz --db-root-password "$DB_PASSWORD" --with-public-files /tmp/kanzlei-restore/public.tgz --with-private-files /tmp/kanzlei-restore/private.tgz --force
'
""",
	)


def restore(root, backup, confirm_site, *, commands=None, smoke=None):
	root, backup = Path(root).resolve(), Path(backup).resolve()
	commands, smoke = commands or Commands(), smoke or smoke_check
	with deployment_lock(root):
		if not backup.is_relative_to(root / "backups"):
			raise ValueError("Backup must be inside this deployment root")
		manifest = verify_backup(backup)
		previous = manifest["previous"]
		config = read_config(root)
		if confirm_site != config["SITE_NAME"] or previous["site"] != confirm_site:
			raise ValueError("Explicit site confirmation must match the backup and current site")
		saved_config = read_config(root, backup / "production.env")
		for key in ("SITE_NAME", "PROJECT_NAME", "PLATFORM", "DB_PASSWORD"):
			if saved_config[key] != config[key]:
				raise ValueError(
					"Backup and current configuration differ for " + key + "; resolve this before restore"
				)
		validate_release(previous["image"], previous["revision"], previous["tag"])
		# Use the saved configuration bundle even if the old releases directory was removed.
		recovery_release = root / "releases" / ("restore-" + uuid.uuid4().hex)
		shutil.copytree(backup / "release", recovery_release)
		compose = Compose(root, recovery_release, previous["image"], commands, config)
		commands.run(["docker", "pull", "--platform", config["PLATFORM"], previous["image"]], timeout=1800)
		compose.clear_interrupted_tasks()
		pending = {"operation": "restore", "backup": str(backup), "previous": previous}
		atomic_json(root / "shared" / "pending.json", pending)
		try:
			compose.mode(True)
			compose.stop()
			shutil.copy2(backup / "production.env", root / "shared" / "production.env")
			config = read_config(root)
			compose = Compose(root, recovery_release, previous["image"], commands, config)
			compose.call("run", "--rm", "-T", "configurator")
			restore_snapshot(compose, backup)
			# Do not execute Redis jobs that reference the discarded database state.
			for service in ("redis-cache", "redis-queue"):
				compose.call("exec", "-T", service, "redis-cli", "FLUSHALL")
			compose.bench("migrate")
			compose.bench("clear-cache")
			compose.mode(False)
			compose.web()
			smoke(compose)
			compose.workers()
			compose.running()
			publish_state(root, {**previous, "release": str(recovery_release)})
			(root / "shared" / "pending.json").unlink()
			print("Restore complete: " + previous["tag"], flush=True)
		except BaseException:
			with contextlib.suppress(Exception):
				compose.mode(True)
			with contextlib.suppress(Exception):
				compose.stop()
			raise


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", type=Path, default=Path("/opt/kanzlei-erp"))
	sub = parser.add_subparsers(dest="command", required=True)
	deploy = sub.add_parser("deploy")
	deploy.add_argument("--release", type=Path, required=True)
	deploy.add_argument("--image", required=True)
	deploy.add_argument("--revision", required=True)
	deploy.add_argument("--tag", required=True)
	deploy.add_argument(
		"--resume", action="store_true", help="Explicitly retry the exact unfinished release after diagnosis"
	)
	recovery = sub.add_parser("restore")
	recovery.add_argument("--backup", type=Path, required=True)
	recovery.add_argument("--confirm-site", required=True)
	args = parser.parse_args()
	os.umask(0o077)

	def interrupted(signum, _frame):
		raise InterruptedError("Deployment interrupted by signal " + str(signum))

	for signum in (signal.SIGTERM, signal.SIGHUP):
		signal.signal(signum, interrupted)
	try:
		if args.command == "deploy":
			Deployer(
				args.root, args.release, args.image, args.revision, args.tag, resume=args.resume
			).deploy()
		else:
			restore(args.root, args.backup, args.confirm_site)
	except Exception as error:
		parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
	main()
