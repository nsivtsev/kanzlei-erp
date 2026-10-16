#!/usr/bin/env python3
"""Consistent ERP snapshots in an independently encrypted Restic repository."""

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import manage

ENV_KEYS = {
	"AWS_ACCESS_KEY_ID",
	"AWS_SECRET_ACCESS_KEY",
	"AWS_DEFAULT_REGION",
	"RESTIC_REPOSITORY",
	"RESTIC_PASSWORD_FILE",
}
SNAPSHOT_ID = re.compile(r"[a-f0-9]{64}\Z")


def private_file(path):
	path = Path(path)
	if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:
		raise ValueError("Secret files must be regular private files with permissions 600")
	return path


class OffsiteBackup:
	def __init__(self, root, *, env_file=None, restic="/usr/local/bin/restic", allow_local=False):
		self.root = Path(root).resolve()
		self.directory = self.root / "offsite"
		self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
		self.staging = self.directory / "snapshot"
		self.verification = self.directory / "verification"
		self.status_path = self.directory / "status.json"
		self.pending = self.root / "shared/pending.json"
		self.binary = str(restic)
		config = {}
		for line in (
			private_file(env_file or self.root / "shared/offsite-backup.env").read_text().splitlines()
		):
			if not line.strip() or line.lstrip().startswith("#"):
				continue
			key, separator, value = line.partition("=")
			if (
				not separator
				or key not in ENV_KEYS
				or key in config
				or not value
				or value.startswith("REPLACE_")
			):
				raise ValueError("Invalid or incomplete offsite configuration")
			config[key] = value
		if config.keys() != ENV_KEYS:
			raise ValueError("Offsite configuration must supply all five documented keys")
		repository = config["RESTIC_REPOSITORY"]
		parsed = urlparse(repository.removeprefix("s3:"))
		if (
			not repository.startswith("s3:")
			or not parsed.hostname
			or parsed.username
			or parsed.password
			or parsed.query
			or parsed.fragment
			or not parsed.path.strip("/")
			or (
				parsed.scheme != "https"
				and not (
					allow_local and parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
				)
			)
		):
			raise ValueError(
				"Use an explicit HTTPS S3 repository; local HTTP requires --allow-local-repository"
			)
		password = private_file(config["RESTIC_PASSWORD_FILE"]).resolve()
		if (
			not password.is_absolute()
			or password.is_relative_to(self.directory)
			or password.stat().st_size < 20
		):
			raise ValueError("Keep a strong recovery password outside the snapshot and staging directories")
		self.environment = {
			key: os.environ[key]
			for key in ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR")
			if key in os.environ
		}
		self.environment.update(config)
		self.environment.update({"TZ": "Europe/Berlin", "RESTIC_CACHE_DIR": str(self.directory / "cache")})

	@contextlib.contextmanager
	def lock(self):
		with (self.directory / ".lock").open("a") as handle:
			try:
				fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError as error:
				raise RuntimeError("Another offsite operation is running") from error
			yield

	def restic(self, *args):
		# Do not log captured output: third-party errors may echo credential material.
		result = subprocess.run(
			[self.binary, *map(str, args)],
			env=self.environment,
			capture_output=True,
			text=True,
			timeout=7200,
			check=False,
		)
		if result.returncode:
			raise RuntimeError("Restic " + args[0] + " failed (exit " + str(result.returncode) + ")")
		return result.stdout

	def status(self, **changes):
		current = manage.read_json(self.status_path) if self.status_path.exists() else {}
		manage.atomic_json(self.status_path, {**current, **changes})

	def health(self):
		status = manage.read_json(self.status_path) if self.status_path.exists() else {}
		if time.time() - status.get("last_success", 0) > 36 * 3600:
			raise RuntimeError("No successful verified offsite backup in the last 36 hours")
		if status.get("phase") != "complete":
			raise RuntimeError("Latest offsite operation requires attention; see status.json and journal")
		return status

	def current(self):
		previous = manage.read_json(self.root / "shared/current.json")
		manage.validate_release(previous["image"], previous["revision"], previous["tag"])
		config = manage.read_config(self.root)
		if previous["site"] != config["SITE_NAME"]:
			raise ValueError("Current deployment and configured site differ")
		compose = manage.Compose(self.root, previous["release"], previous["image"], manage.Commands(), config)
		return previous, compose

	def space_required(self):
		# At peak either Docker export + host copy, or local snapshot + remote readback.
		for candidate in sorted((self.root / "backups").glob("*"), reverse=True):
			try:
				manifest = manage.read_json(candidate / "manifest.json")
				size = sum((candidate / name).stat().st_size for name in manifest["checksums"])
				return max(2 * size + 512 * 1024**2, 2 * 1024**3)
			except (OSError, ValueError, KeyError, TypeError):
				continue
		raise RuntimeError("A completed deployment snapshot is required to estimate staging space")

	def reopen(self, previous, compose):
		if manage.read_json(self.root / "shared/current.json") != previous:
			raise RuntimeError("Deployment changed during backup; manual recovery required")
		compose.mode(False)
		compose.web()
		# Do not change a deliberately disabled scheduler's database setting.
		compose.call("up", "-d", *manage.WORKERS)
		manage.smoke_check(compose)
		compose.running()
		self.pending.unlink()

	def capture(self):
		with manage.deployment_lock(self.root):
			if self.pending.exists():
				raise RuntimeError("Unfinished deployment/restore/backup; inspect shared/pending.json first")
			if self.staging.exists():
				raise RuntimeError("An existing snapshot must be published or inspected first")
			previous, compose = self.current()
			compose.running()
			configuration = json.loads(
				compose.call(
					"exec",
					"-T",
					"backend",
					"env/bin/python",
					"-c",
					"import json,os; c={}; "
					+ "[c.update(json.load(open(p))) for p in ['sites/common_site_config.json', "
					+ repr("sites/" + previous["site"] + "/site_config.json")
					+ "] if os.path.isfile(p)]; print(json.dumps({'maintenance': bool(c.get('maintenance_mode'))}))",
				)
			)
			if configuration["maintenance"]:
				raise RuntimeError("Site is already in maintenance; refusing to change its state")
			if shutil.disk_usage(self.root).free < self.space_required():
				raise RuntimeError("Insufficient free space for snapshot and verified remote readback")
			manage.atomic_json(self.pending, {"operation": "offsite-backup", "previous": previous})
			self.status(phase="snapshot", snapshot_started=time.time())
			try:
				compose.mode(True)
				try:
					compose.stop()
				except BaseException as error:
					# Docker may continue handling a stop after its client was killed.
					raise manage.UnsafeContainerError(
						"Cannot confirm application shutdown; explicit backup recovery required"
					) from error
				deployer = manage.Deployer(
					self.root,
					previous["release"],
					previous["image"],
					previous["revision"],
					previous["tag"],
				)
				deployer.backup(previous, destination=self.staging)
			except BaseException as error:
				if not isinstance(error, manage.UnsafeContainerError):
					self.reopen(previous, compose)
				raise
			self.reopen(previous, compose)
			print("ERP resumed; starting encrypted external transfer", flush=True)

	def publish(self):
		# ERP stays online, but multi-GB backup/deploy disk usage must not overlap.
		with manage.deployment_lock(self.root):
			if self.pending.exists():
				raise RuntimeError("Unfinished maintenance; cannot publish during deployment/restore")
			manage.verify_backup(self.staging)
			if self.verification.is_symlink():
				raise ValueError("Verification directory must not be a symlink")
			if self.verification.exists():
				shutil.rmtree(self.verification)
			self.readback_space(sum(p.stat().st_size for p in self.staging.rglob("*") if p.is_file()))
			return self._publish()

	def readback_space(self, size):
		if shutil.disk_usage(self.root).free < size + 512 * 1024**2:
			raise RuntimeError("Insufficient free space for external snapshot readback")

	def _publish(self):
		manifest = manage.verify_backup(self.staging)
		site = manifest["previous"]["site"]
		self.status(phase="upload")
		output = self.restic("backup", "--json", "--host", site, "--tag", "kanzlei-erp", self.staging)
		summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
		ids = [item.get("snapshot_id", "") for item in summaries if item.get("message_type") == "summary"]
		if len(ids) != 1 or not SNAPSHOT_ID.fullmatch(ids[0]):
			raise RuntimeError("Restic did not confirm a complete snapshot")
		snapshot = ids[0]
		self.status(phase="verify", snapshot=snapshot)
		if self.verification.is_symlink():
			raise ValueError("Verification directory must not be a symlink")
		if self.verification.exists():
			shutil.rmtree(self.verification)
		try:
			self.readback_space(sum(p.stat().st_size for p in self.staging.rglob("*") if p.is_file()))
			self.restic("restore", snapshot, "--target", self.verification, "--verify")
			restored = self.verification / self.staging.relative_to("/")
			manage.verify_backup(restored)
			if (restored / "manifest.json").read_bytes() != (self.staging / "manifest.json").read_bytes():
				raise ValueError("Restored snapshot manifest differs from local snapshot")
		finally:
			if self.verification.exists():
				shutil.rmtree(self.verification)
		self.status(phase="retention", last_success=time.time(), snapshot=snapshot)
		self.restic(
			"forget",
			"--host",
			site,
			"--tag",
			"kanzlei-erp",
			"--group-by",
			"host,paths",
			"--keep-daily",
			"7",
			"--keep-weekly",
			"4",
			"--keep-monthly",
			"6",
			"--prune",
		)
		self.restic("check")
		shutil.rmtree(self.staging)
		self.status(phase="complete", last_success=time.time(), snapshot=snapshot)
		print("Verified external snapshot: " + snapshot, flush=True)
		return snapshot

	def run(self):
		with self.lock():
			self.status(last_attempt=time.time(), phase="preflight")
			try:
				self.restic("cat", "config")  # Access/password failures must not interrupt ERP.
				if self.pending.exists():
					raise RuntimeError("Unfinished maintenance; explicit recovery required")
				if self.staging.exists():
					self.publish()  # Retry a failed transfer before creating today's snapshot.
				self.capture()
				return self.publish()
			except BaseException as error:
				self.status(phase="failed", failure_type=type(error).__name__)
				raise

	def recover(self):
		with self.lock(), manage.deployment_lock(self.root):
			pending = manage.read_json(self.pending)
			previous, compose = self.current()
			if pending.get("operation") != "offsite-backup" or pending.get("previous") != previous:
				raise RuntimeError("Recovery is restricted to this backup's unchanged deployment")
			compose.clear_interrupted_tasks()
			compose.stop()
			if compose.call("ps", "--status", "running", "-q", *manage.APP_SERVICES).strip():
				raise RuntimeError("Application services are still stopping; keep maintenance enabled")
			# Recreate app containers so an old Docker stop request cannot stop a reused ID.
			compose.call("rm", "-f", *manage.APP_SERVICES)
			self.reopen(previous, compose)
			self.status(phase="recovered")
			print("Backup maintenance ended; inspect incomplete staging before another run", flush=True)

	def fetch(self, snapshot, destination):
		if not SNAPSHOT_ID.fullmatch(snapshot):
			raise ValueError("Recovery requires the full 64-character snapshot ID")
		destination = Path(destination).resolve()
		if not destination.is_relative_to(self.root / "backups") or destination.exists():
			raise ValueError("Fetch destination must be a new directory under this deployment's backups")
		with self.lock(), manage.deployment_lock(self.root):
			metadata = json.loads(self.restic("snapshots", "--json", snapshot))
			if (
				len(metadata) != 1
				or metadata[0]["id"] != snapshot
				or "kanzlei-erp" not in metadata[0].get("tags", [])
			):
				raise ValueError("Snapshot is not an ERP backup")
			paths = metadata[0]["paths"]
			if len(paths) != 1 or not Path(paths[0]).is_absolute() or ".." in Path(paths[0]).parts:
				raise ValueError("Unexpected snapshot root")
			if self.verification.exists():
				raise RuntimeError("Inspect leftover verification directory before fetching")
			stats = json.loads(self.restic("stats", "--json", snapshot))
			self.readback_space(stats["total_size"])
			self.restic("restore", snapshot, "--target", self.verification, "--verify")
			source = self.verification / Path(paths[0]).relative_to("/")
			manage.verify_backup(source)
			destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
			shutil.move(source, destination)
			shutil.rmtree(self.verification)
			return str(destination)


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", type=Path, default=Path("/opt/kanzlei-erp"))
	parser.add_argument("--env-file", type=Path)
	parser.add_argument("--restic", default="/usr/local/bin/restic")
	parser.add_argument("--allow-local-repository", action="store_true")
	sub = parser.add_subparsers(dest="command", required=True)
	for name in ("run", "init", "list", "status", "recover"):
		sub.add_parser(name)
	fetch = sub.add_parser("fetch")
	fetch.add_argument("--snapshot", required=True)
	fetch.add_argument("--destination", required=True, type=Path)
	args = parser.parse_args()
	os.umask(0o077)

	def interrupted(signum, _frame):
		raise InterruptedError("Backup interrupted by signal " + str(signum))

	for signum in (signal.SIGTERM, signal.SIGHUP):
		signal.signal(signum, interrupted)
	try:
		runner = OffsiteBackup(
			args.root, env_file=args.env_file, restic=args.restic, allow_local=args.allow_local_repository
		)
		if args.command == "run":
			runner.run()
		elif args.command == "init":
			with runner.lock():
				runner.restic("init")
			print("Encrypted backup repository initialized")
		elif args.command == "list":
			print(runner.restic("snapshots", "--json", "--tag", "kanzlei-erp"))
		elif args.command == "status":
			print(json.dumps(runner.health(), sort_keys=True))
		elif args.command == "recover":
			runner.recover()
		else:
			print(runner.fetch(args.snapshot, args.destination))
	except Exception as error:
		parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
	main()
