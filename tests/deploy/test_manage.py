import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "deploy" / "manage.py"
manager = None
if SOURCE.exists():
	spec = importlib.util.spec_from_file_location("deploy_manage", SOURCE)
	manager = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(manager)


class FakeCommands:
	"""Replace the Docker process boundary; state and files use the real filesystem."""

	def __init__(self, fail_on=None):
		self.calls = []
		self.fail_on = fail_on

	def run(self, args, *, env=None, timeout=None):
		self.calls.append(list(args))
		if self.fail_on and self.fail_on in args:
			raise RuntimeError("Injected failure: " + self.fail_on)
		if args[:2] == ["docker", "cp"]:
			destination = Path(args[-1])
			destination.mkdir(parents=True, exist_ok=True)
			for name in ("database.sql.gz", "public.tgz", "private.tgz"):
				(destination / name).write_bytes(b"backup fixture")
			(destination / "site_config.json").write_text('{"encryption_key":"preserved-key"}')
		if "ps" in args:
			return "container-id\n"
		if "inspect" in args:
			if "org.opencontainers.image.revision" in " ".join(args):
				return "2" * 40
			return "true 0\n"
		return ""


class DeploymentTests(unittest.TestCase):
	def setUp(self):
		self.assertIsNotNone(manager, "Deployment manager has not been implemented")
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name).resolve()
		(self.root / "shared").mkdir()
		(self.root / "backups").mkdir()
		self.release = self.root / "releases" / "new"
		(self.release / "deploy").mkdir(parents=True)
		(self.release / "deploy" / "compose.yml").write_text("services: {}\n")
		(self.release / "deploy" / "Caddyfile").write_text("test fixture\n")
		self.config = self.root / "shared" / "production.env"
		self.config.write_text(
			"DOMAIN=erp.example.com\nSITE_NAME=erp.example.com\nACME_EMAIL=ops@example.com\n"
			"DB_PASSWORD=database-secret\nADMIN_PASSWORD=admin-secret\n"
		)
		self.config.chmod(0o600)
		self.old_image = "ghcr.io/nsivtsev/kanzlei-erp@sha256:" + "a" * 64
		self.new_image = "ghcr.io/nsivtsev/kanzlei-erp@sha256:" + "b" * 64
		self.previous = self.root / "releases" / "old"
		(self.previous / "deploy").mkdir(parents=True)
		(self.previous / "deploy" / "compose.yml").write_text("services: {}\n")
		self.state = {
			"image": self.old_image,
			"revision": "1" * 40,
			"tag": "v1.0.0",
			"release": str(self.previous),
			"site": "erp.example.com",
		}
		manager.atomic_json(self.root / "shared" / "current.json", self.state)
		(self.root / "current").symlink_to(self.previous)

	def deployer(self, commands=None, smoke=None, **kwargs):
		return manager.Deployer(
			self.root,
			self.release,
			self.new_image,
			"2" * 40,
			"v1.1.0",
			commands=commands or FakeCommands(),
			smoke=smoke or (lambda _compose: None),
			**kwargs,
		)

	def test_backup_failure_never_runs_migration_or_advances_current(self):
		commands = FakeCommands("backup")
		with self.assertRaisesRegex(RuntimeError, "backup"):
			self.deployer(commands).deploy()
		self.assertFalse(any("migrate" in call for call in commands.calls))
		self.assertEqual(json.loads((self.root / "shared/current.json").read_text()), self.state)
		self.assertFalse((self.root / "shared/pending.json").exists())
		self.assertTrue(any("up" in call and "scheduler" in call for call in commands.calls))

	def test_migration_failure_keeps_recovery_marker_and_previous_release(self):
		commands = FakeCommands("migrate")
		with self.assertRaisesRegex(RuntimeError, "migrate"):
			self.deployer(commands).deploy()
		pending = json.loads((self.root / "shared/pending.json").read_text())
		self.assertEqual(pending["image"], self.new_image)
		self.assertTrue((Path(pending["backup"]) / "manifest.json").exists())
		self.assertEqual((self.root / "current").resolve(), self.previous)
		self.assertFalse(any("restore" in call for call in commands.calls))

	def test_success_preserves_backup_key_and_commits_new_digest(self):
		commands = FakeCommands()
		self.deployer(commands).deploy()
		current = json.loads((self.root / "shared/current.json").read_text())
		self.assertEqual(current["image"], self.new_image)
		self.assertEqual((self.root / "current").resolve(), self.release)
		backup = next((self.root / "backups").iterdir())
		self.assertEqual(
			json.loads((backup / "site_config.json").read_text())["encryption_key"], "preserved-key"
		)
		self.assertEqual((backup / "production.env").read_bytes(), self.config.read_bytes())
		self.assertEqual(os.stat(backup).st_mode & 0o777, 0o700)
		self.assertFalse((self.root / "shared/pending.json").exists())
		pull = next(i for i, call in enumerate(commands.calls) if "pull" in call)
		backup_call = next(i for i, call in enumerate(commands.calls) if "backup" in call)
		for flag in (
			"--backup-path-db",
			"--backup-path-files",
			"--backup-path-private-files",
			"--backup-path-conf",
		):
			path = commands.calls[backup_call][commands.calls[backup_call].index(flag) + 1]
			self.assertTrue(
				path.startswith("/home/frappe/frappe-bench/sites/"),
				"Bench changes cwd to sites; backup paths must be absolute",
			)
		migrate = next(i for i, call in enumerate(commands.calls) if "migrate" in call)
		self.assertLess(pull, backup_call)
		self.assertLess(backup_call, migrate)

	def test_unfinished_deployment_blocks_new_release_without_docker_changes(self):
		manager.atomic_json(self.root / "shared/pending.json", {"image": self.new_image})
		commands = FakeCommands()
		with self.assertRaisesRegex(RuntimeError, "unfinished"):
			self.deployer(commands).deploy()
		self.assertEqual(commands.calls, [])

	def test_failed_smoke_never_commits_release_or_restores_database(self):
		def smoke(_compose):
			raise RuntimeError("HTTP check failed")

		commands = FakeCommands()
		with self.assertRaisesRegex(RuntimeError, "HTTP"):
			self.deployer(commands, smoke).deploy()
		self.assertTrue((self.root / "shared/pending.json").exists())
		self.assertEqual(json.loads((self.root / "shared/current.json").read_text()), self.state)
		self.assertFalse(any("restore" in call for call in commands.calls))

	def test_first_install_has_no_previous_backup_and_is_not_destructive(self):
		(self.root / "shared/current.json").unlink()
		(self.root / "current").unlink()
		commands = FakeCommands()
		self.deployer(commands).deploy()
		self.assertFalse(any("backup" in call or "restore" in call for call in commands.calls))
		self.assertFalse(any("down" in call for call in commands.calls))
		self.assertEqual(json.loads((self.root / "shared/current.json").read_text())["image"], self.new_image)

	def test_normal_deploy_rejects_downgrade(self):
		deployer = manager.Deployer(
			self.root, self.release, self.new_image, "2" * 40, "v0.9.0", commands=FakeCommands()
		)
		with self.assertRaisesRegex(ValueError, "older"):
			deployer.deploy()

	def test_restore_rejects_corrupted_backup_before_docker_changes(self):
		self.deployer().deploy()
		backup = next((self.root / "backups").iterdir())
		(backup / "database.sql.gz").write_bytes(b"corrupted")
		commands = FakeCommands()
		with self.assertRaisesRegex(ValueError, "checksum"):
			manager.restore(self.root, backup, "erp.example.com", commands=commands, smoke=lambda _: None)
		self.assertEqual(commands.calls, [])

	def test_restore_requires_matching_site_and_restores_previous_digest(self):
		self.deployer().deploy()
		backup = next((self.root / "backups").iterdir())
		with self.assertRaisesRegex(ValueError, "site"):
			manager.restore(self.root, backup, "another.example.com", commands=FakeCommands())
		commands = FakeCommands()
		manager.restore(self.root, backup, "erp.example.com", commands=commands, smoke=lambda _: None)
		self.assertTrue(any("restore" in call[-1] for call in commands.calls))
		self.assertEqual(json.loads((self.root / "shared/current.json").read_text())["image"], self.old_image)

	def test_rejects_mutable_image_and_unsafe_tag(self):
		for image, tag in (
			("ghcr.io/nsivtsev/kanzlei-erp:latest", "v1.1.0"),
			(self.new_image, "v1;touch /tmp/pwned"),
		):
			with self.assertRaises(ValueError):
				manager.Deployer(self.root, self.release, image, "2" * 40, tag)

	def test_timed_out_oneoff_is_removed_before_recovery(self):
		class TimeoutCommands(FakeCommands):
			def run(self, args, **kwargs):
				if "migrate" in args:
					self.calls.append(list(args))
					raise subprocess.TimeoutExpired(args, 1)
				return super().run(args, **kwargs)

		commands = TimeoutCommands()
		with self.assertRaises(subprocess.TimeoutExpired):
			self.deployer(commands).deploy()
		migration = next(i for i, call in enumerate(commands.calls) if "migrate" in call)
		self.assertEqual(commands.calls[migration + 1][:3], ["docker", "rm", "-f"])
		self.assertIn("--name", commands.calls[migration])
		self.assertTrue((self.root / "shared/pending.json").exists())

	def test_resume_reuses_original_backup(self):
		with self.assertRaisesRegex(RuntimeError, "migrate"):
			self.deployer(FakeCommands("migrate")).deploy()
		original = json.loads((self.root / "shared/pending.json").read_text())["backup"]
		commands = FakeCommands()
		self.deployer(commands, resume=True).deploy()
		self.assertFalse(any("backup" in call for call in commands.calls))
		self.assertEqual([str(path) for path in (self.root / "backups").iterdir()], [original])

	def test_retention_keeps_seven_verified_snapshots_and_ignores_incomplete(self):
		self.deployer().deploy()
		original = next((self.root / "backups").iterdir())
		for index in range(9):
			shutil.copytree(original, self.root / "backups" / f"snapshot-{index}")
		incomplete = self.root / "backups" / "zz-incomplete"
		incomplete.mkdir()
		(incomplete / "manifest.json").write_text("{}")
		self.deployer().prune_backups()
		complete = []
		for path in (self.root / "backups").iterdir():
			if path != incomplete:
				manager.verify_backup(path)
				complete.append(path)
		self.assertEqual(len(complete), 7)
		self.assertTrue(incomplete.exists())

	def test_changed_site_is_rejected_before_docker_changes(self):
		self.config.write_text(self.config.read_text().replace("erp.example.com", "another.example.com"))
		commands = FakeCommands()
		with self.assertRaisesRegex(ValueError, "site"):
			self.deployer(commands).deploy()
		self.assertEqual(commands.calls, [])

	def test_restore_rejects_changed_compose_project_before_docker_changes(self):
		self.deployer().deploy()
		backup = next((self.root / "backups").iterdir())
		with self.config.open("a") as handle:
			handle.write("PROJECT_NAME=kanzlei-other\n")
		commands = FakeCommands()
		with self.assertRaisesRegex(ValueError, "PROJECT_NAME"):
			manager.restore(self.root, backup, "erp.example.com", commands=commands, smoke=lambda _: None)
		self.assertEqual(commands.calls, [])


if __name__ == "__main__":
	unittest.main()
