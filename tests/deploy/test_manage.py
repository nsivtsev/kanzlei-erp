import importlib.util
import json
import os
import shutil
import subprocess
import sys
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

	def test_offsite_snapshot_destination_is_outside_deployment_retention(self):
		destination = self.root / "offsite" / "snapshot"
		result = self.deployer().backup(self.state, destination=destination)
		self.assertEqual(Path(result), destination)
		manager.verify_backup(destination)
		self.assertEqual(list((self.root / "backups").iterdir()), [])

	def test_snapshot_staging_does_not_break_frappe_backup_file_cleanup(self):
		commands = FakeCommands()
		self.deployer(commands).backup(self.state)
		backup_call = next(call for call in commands.calls if "backup" in call)
		paths = [value for value in backup_call if "/private/" in value]
		self.assertTrue(paths)
		self.assertFalse(any("/private/backups/" in value for value in paths))

	def test_duplicate_successful_deploy_checks_health_without_another_backup_or_migration(self):
		self.deployer().deploy()
		commands = FakeCommands()
		observed = []
		self.deployer(commands, smoke=lambda compose: observed.append(compose.image)).deploy()
		self.assertEqual(observed, [self.new_image])
		self.assertFalse(any("backup" in call or "migrate" in call or "pull" in call or "up" in call
			for call in commands.calls))
		self.assertEqual(len(list((self.root / "backups").iterdir())), 1)

	def test_duplicate_deploy_with_unhealthy_services_fails_without_changing_database(self):
		self.deployer().deploy()
		commands = FakeCommands("ps")
		with self.assertRaisesRegex(RuntimeError, "ps"):
			self.deployer(commands).deploy()
		self.assertFalse(any("backup" in call or "migrate" in call for call in commands.calls))

	def test_existing_site_installs_missing_apps_after_backup_before_migration(self):
		commands = FakeCommands()
		self.deployer(commands).deploy()
		installation = next(i for i, call in enumerate(commands.calls) if "install-app suite" in call[-1])
		backup = next(i for i, call in enumerate(commands.calls) if "backup" in call)
		migration = next(i for i, call in enumerate(commands.calls) if "migrate" in call)
		self.assertLess(backup, installation)
		self.assertLess(installation, migration)
		self.assertNotIn("new-site", commands.calls[installation][-1])

	def test_suite_install_failure_preserves_recovery_marker(self):
		class FailedInstall(FakeCommands):
			def run(self, args, **kwargs):
				if "install-app suite" in args[-1]:
					self.calls.append(list(args))
					raise RuntimeError("Suite installation failed")
				return super().run(args, **kwargs)

		commands = FailedInstall()
		with self.assertRaisesRegex(RuntimeError, "Suite installation"):
			self.deployer(commands).deploy()
		self.assertTrue((self.root / "shared/pending.json").exists())
		self.assertFalse(any("migrate" in call for call in commands.calls))
		self.assertEqual(json.loads((self.root / "shared/current.json").read_text()), self.state)

	def test_resume_completes_committed_partial_suite_install_only_once(self):
		commands = FakeCommands()
		self.deployer(commands).ensure_apps()
		script = commands.calls[-1][-1]
		bin_path = self.root / "bin"
		bin_path.mkdir()
		(self.root / "env/bin").mkdir(parents=True)
		(self.root / "env/bin/python").symlink_to(sys.executable)
		site_path = self.root / "sites/erp.example.com"
		site_path.mkdir(parents=True)
		(site_path / "site_config.json").write_text('{"kanzlei_suite_install_pending":1}')
		bench = bin_path / "bench"
		bench.write_text(
			"#!" + sys.executable + "\n"
			"import json, os, sys\nfrom pathlib import Path\n"
			"args=sys.argv[1:]\n"
			"with open('bench-calls.jsonl','a') as f: f.write(json.dumps(args)+'\\n')\n"
			"if args[2]=='list-apps': print('frappe 16\\nerpnext 16\\nsuite 0\\nkanzlei_erp 0')\n"
			"if args[2]=='set-config':\n"
			" p=Path('sites')/os.environ['SITE_NAME']/'site_config.json'\n"
			" data=json.loads(p.read_text()); data[args[3]]=json.loads(args[4]); p.write_text(json.dumps(data))\n"
		)
		bench.chmod(0o700)
		env = {**os.environ, "PATH": str(bin_path) + ":" + os.environ["PATH"], "SITE_NAME": "erp.example.com"}
		for _ in range(2):
			subprocess.run(["bash", "-euc", script], cwd=self.root, env=env, check=True)
		calls = [json.loads(line) for line in (self.root / "bench-calls.jsonl").read_text().splitlines()]
		installs = [call for call in calls if call[2] == "install-app"]
		self.assertEqual(installs, [["--site", "erp.example.com", "install-app", "suite", "--force"]])
		cleanup = ["--site", "erp.example.com", "execute", '__import__("kanzlei_erp.suite_setup", fromlist=["prepare_suite_install"]).prepare_suite_install("suite")']
		self.assertEqual(calls.count(cleanup), 1)
		self.assertLess(calls.index(cleanup), calls.index(installs[0]))
		self.assertEqual(json.loads((site_path / "site_config.json").read_text())["kanzlei_suite_install_pending"], 0)

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


	def test_s3_export_runs_before_snapshot_copy_and_failure_blocks_migration(self):
		commands = FakeCommands()
		self.deployer(commands).deploy()
		export = next(i for i, call in enumerate(commands.calls) if "kanzlei_erp.s3_backup.export_snapshot" in " ".join(call))
		copy = next(i for i, call in enumerate(commands.calls) if call[:2] == ["docker", "cp"])
		self.assertLess(export, copy)
		class BrokenS3(FakeCommands):
			def run(self, args, **kwargs):
				if "kanzlei_erp.s3_backup.export_snapshot" in " ".join(args):
					raise RuntimeError("S3 export failed")
				return super().run(args, **kwargs)
		# A fresh candidate release ensures this goes through the backup path again.
		commands = BrokenS3()
		with self.assertRaisesRegex(RuntimeError, "S3 export"):
			self.deployer(commands).backup(self.state)
		self.assertFalse(any("migrate" in call for call in commands.calls))

	def test_restore_script_restores_s3_before_returning_to_smoke(self):
		commands = FakeCommands()
		compose = manager.Compose(self.root, self.release, self.old_image, commands)
		manager.restore_snapshot(compose, self.root / "backups" / "example")
		script = commands.calls[-1][-1]
		self.assertIn("kanzlei_erp.s3_backup.restore_snapshot", script)
		self.assertLess(script.index("bench --site \"$SITE_NAME\" restore"), script.index("kanzlei_erp.s3_backup.restore_snapshot"))

	def test_backup_checks_allow_verified_empty_s3_objects(self):
		self.deployer().deploy()
		backup = next((self.root / "backups").iterdir())
		(backup / "s3/objects").mkdir(parents=True)
		payload = backup / "s3/objects/000000000000"
		payload.write_bytes(b"")
		s3 = {"version": 1, "enabled": True, "bucket": "bucket", "prefix": "site/", "objects": [{"key": "empty", "payload": "objects/000000000000", "size": 0, "sha256": manager.checksum(payload)}]}
		manager.atomic_json(backup / "s3/manifest.json", s3)
		manifest = manager.read_json(backup / "manifest.json")
		manifest["s3"] = True
		for path in (payload, backup / "s3/manifest.json"):
			manifest["checksums"][str(path.relative_to(backup))] = manager.checksum(path)
		manager.atomic_json(backup / "manifest.json", manifest)
		manager.verify_backup(backup)

	def test_local_s3_restore_requires_explicit_target_before_docker_changes(self):
		self.deployer().deploy()
		backup = next((self.root / "backups").iterdir())
		manifest = manager.read_json(backup / "manifest.json")
		manifest["s3"] = True
		(backup / "s3").mkdir()
		manager.atomic_json(backup / "s3/manifest.json", {"version": 1, "enabled": True, "bucket": "bucket", "prefix": "site/", "objects": []})
		manifest["checksums"]["s3/manifest.json"] = manager.checksum(backup / "s3/manifest.json")
		manager.atomic_json(backup / "manifest.json", manifest)
		# Mark this as a local restore without altering the backup's production credentials.
		self.config.write_text(self.config.read_text().replace("erp.example.com", "erp.localhost") + "HTTPS_ENABLED=0\n")
		manifest["previous"]["site"] = "erp.localhost"
		manager.atomic_json(backup / "manifest.json", manifest)
		commands = FakeCommands()
		with self.assertRaisesRegex(ValueError, "S3.*target"):
			manager.restore(self.root, backup, "erp.localhost", commands=commands)
		self.assertEqual(commands.calls, [])

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
