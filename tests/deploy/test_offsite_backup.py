import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
sys.path.insert(0, str(DEPLOY))
SOURCE = DEPLOY / "offsite_backup.py"
offsite = None
if SOURCE.exists():
	spec = importlib.util.spec_from_file_location("offsite_backup", SOURCE)
	offsite = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(offsite)


class OffsiteTests(unittest.TestCase):
	def setUp(self):
		self.assertIsNotNone(offsite, "Offsite backup runner has not been implemented")
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		(self.root / "shared").mkdir()
		self.env = self.root / "shared/offsite-backup.env"
		self.password = self.root / "shared/offsite-backup.password"
		self.password.write_text("independent-recovery-password-long-enough\n")
		self.password.chmod(0o600)
		self.env.write_text(
			"AWS_ACCESS_KEY_ID=backup-access\nAWS_SECRET_ACCESS_KEY=backup-secret\n"
			"AWS_DEFAULT_REGION=nbg1\n"
			"RESTIC_REPOSITORY=s3:https://nbg1.your-objectstorage.com/stb-golke-backups/erp\n"
			"RESTIC_PASSWORD_FILE=" + str(self.password) + "\n"
		)
		self.env.chmod(0o600)
		self.events = []

	def runner(self):
		return offsite.OffsiteBackup(self.root, restic="/test/restic")

	def snapshot(self, destination):
		destination.mkdir(parents=True)
		for name in ("database.sql.gz", "public.tgz", "private.tgz", "site_config.json", "production.env"):
			(destination / name).write_bytes(b"complete snapshot fixture")
		offsite.manage.atomic_json(
			destination / "manifest.json",
			{
				"previous": {"site": "erp.example.com"},
				"s3": False,
				"checksums": {p.name: offsite.manage.checksum(p) for p in destination.iterdir()},
			},
		)
		return destination

	def restic(self, *args):
		self.events.append(args)
		if args[0] == "backup":
			return json.dumps({"message_type": "summary", "snapshot_id": "a" * 64})
		if args[0] == "restore":
			target = Path(args[args.index("--target") + 1])
			import shutil

			shutil.copytree(self.runner().staging, target / self.runner().staging.relative_to("/"))
		return "{}"

	def test_private_config_never_uses_inherited_restic_password_or_other_credentials(self):
		with patch.dict("os.environ", {"RESTIC_PASSWORD": "wrong", "AWS_SESSION_TOKEN": "wrong"}):
			env = self.runner().environment
		self.assertNotIn("RESTIC_PASSWORD", env)
		self.assertNotIn("AWS_SESSION_TOKEN", env)
		self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], "backup-secret")

	def test_unsafe_config_permissions_rejected(self):
		self.env.chmod(0o644)
		with self.assertRaisesRegex(ValueError, "600"):
			self.runner()

	def test_password_file_must_be_private_and_separate_from_staging(self):
		self.password.chmod(0o644)
		with self.assertRaisesRegex(ValueError, "600"):
			self.runner()

	def test_publish_restores_and_verifies_before_removing_local_or_pruning_remote(self):
		runner = self.runner()
		self.snapshot(runner.staging)
		with patch.object(runner, "restic", side_effect=self.restic):
			runner.publish()
		self.assertFalse(runner.staging.exists())
		commands = [args[0] for args in self.events]
		self.assertLess(commands.index("restore"), commands.index("forget"))
		forget = next(args for args in self.events if args[0] == "forget")
		self.assertIn("--keep-daily", forget)
		self.assertIn("--tag", forget)
		self.assertIn("--host", forget)
		self.assertIn("--prune", forget)
		self.assertIn("--verify", next(args for args in self.events if args[0] == "restore"))

	def test_corrupt_remote_restore_preserves_staging_and_never_prunes(self):
		runner = self.runner()
		self.snapshot(runner.staging)

		def corrupt(*args):
			result = self.restic(*args)
			if args[0] == "restore":
				target = Path(args[args.index("--target") + 1]) / runner.staging.relative_to("/")
				(target / "database.sql.gz").write_bytes(b"corrupt")
			return result

		with patch.object(runner, "restic", side_effect=corrupt):
			with self.assertRaisesRegex(ValueError, "checksum"):
				runner.publish()
		self.assertTrue(runner.staging.exists())
		self.assertNotIn("forget", [args[0] for args in self.events])

	def test_partial_backup_exit_preserves_staging_and_never_prunes(self):
		runner = self.runner()
		self.snapshot(runner.staging)
		with patch.object(runner, "restic", side_effect=RuntimeError("partial backup")):
			with self.assertRaisesRegex(RuntimeError, "partial"):
				runner.publish()
		self.assertTrue(runner.staging.exists())

	def test_restic_command_error_does_not_expose_credentials(self):
		runner = self.runner()
		with patch("subprocess.run") as run:
			run.return_value.returncode = 1
			run.return_value.stderr = "backup-access backup-secret password"
			run.return_value.stdout = "backup-secret"
			with self.assertRaises(RuntimeError) as caught:
				runner.restic("snapshots")
		self.assertNotIn("backup-secret", str(caught.exception))
		self.assertNotIn("backup-access", str(caught.exception))

	def test_stale_health_fails_even_after_a_recent_failed_attempt(self):
		runner = self.runner()
		offsite.manage.atomic_json(runner.status_path, {"last_success": 1, "last_attempt": 99999999999})
		with self.assertRaisesRegex(RuntimeError, "successful"):
			runner.health()

	def test_fetch_rejects_latest_and_nonexplicit_snapshot_before_remote_access(self):
		runner = self.runner()
		with patch.object(runner, "restic") as remote:
			with self.assertRaisesRegex(ValueError, "full"):
				runner.fetch("latest", self.root / "backups/recovery")
		remote.assert_not_called()

	def prepare_capture(self):
		runner = self.runner()
		previous = {
			"image": "fixture",
			"site": "erp.example.com",
			"release": "fixture",
			"revision": "fixture",
			"tag": "fixture",
		}
		offsite.manage.atomic_json(self.root / "shared/current.json", previous)
		from unittest.mock import Mock

		compose = Mock()
		compose.call.return_value = '{"maintenance": false}'
		compose.mode.side_effect = lambda enabled: self.events.append(("maintenance", enabled))
		compose.stop.side_effect = lambda: self.events.append(("stop",))
		compose.web.side_effect = lambda: self.events.append(("web",))
		self.addCleanup(patch.stopall)
		patch.object(runner, "current", return_value=(previous, compose)).start()
		patch.object(runner, "space_required", return_value=0).start()
		patch.object(offsite.manage, "smoke_check").start()
		deployer = patch.object(offsite.manage, "Deployer").start().return_value
		deployer.backup.side_effect = lambda *a, **kw: self.snapshot(kw["destination"])
		return runner, compose, deployer

	def test_capture_resumes_erp_and_releases_deployment_lock_before_upload(self):
		runner, compose, _ = self.prepare_capture()
		runner.capture()
		self.assertEqual(self.events, [("maintenance", True), ("stop",), ("maintenance", False), ("web",)])
		self.assertFalse(runner.pending.exists())
		compose.running.assert_called()
		with offsite.manage.deployment_lock(self.root):
			self.assertTrue(runner.staging.exists())

	def test_already_maintained_site_is_never_reopened(self):
		runner, compose, deployer = self.prepare_capture()
		compose.call.return_value = '{"maintenance": true}'
		with self.assertRaisesRegex(RuntimeError, "already in maintenance"):
			runner.capture()
		compose.mode.assert_not_called()
		deployer.backup.assert_not_called()

	def test_backup_failure_reopens_unchanged_site_without_advancing_deployment(self):
		runner, compose, deployer = self.prepare_capture()
		deployer.backup.side_effect = RuntimeError("snapshot failed")
		with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
			runner.capture()
		self.assertFalse(runner.pending.exists())
		self.assertEqual(compose.mode.call_args.args, (False,))

	def test_uncertain_container_shutdown_keeps_maintenance_marker(self):
		runner, compose, deployer = self.prepare_capture()
		deployer.backup.side_effect = offsite.manage.UnsafeContainerError("cannot stop writer")
		with self.assertRaisesRegex(offsite.manage.UnsafeContainerError, "cannot stop"):
			runner.capture()
		self.assertTrue(runner.pending.exists())
		self.assertEqual(compose.mode.call_args.args, (True,))
		compose.web.assert_not_called()

	def test_interrupted_application_stop_cannot_reopen_site_automatically(self):
		runner, compose, _ = self.prepare_capture()
		compose.stop.side_effect = InterruptedError("stop client interrupted")
		with self.assertRaisesRegex(offsite.manage.UnsafeContainerError, "application"):
			runner.capture()
		self.assertTrue(runner.pending.exists())
		compose.web.assert_not_called()

	def test_remote_authentication_failure_does_not_interrupt_erp(self):
		runner = self.runner()
		with (
			patch.object(runner, "restic", side_effect=RuntimeError("authentication failed")),
			patch.object(runner, "capture") as capture,
		):
			with self.assertRaisesRegex(RuntimeError, "authentication"):
				runner.run()
		capture.assert_not_called()

	def test_retry_publishes_existing_snapshot_before_creating_fresh_snapshot(self):
		runner = self.runner()
		self.snapshot(runner.staging)
		with (
			patch.object(runner, "restic", return_value="{}"),
			patch.object(runner, "publish", side_effect=lambda: self.events.append("publish")),
			patch.object(runner, "capture", side_effect=lambda: self.events.append("capture")),
		):
			runner.run()
		self.assertEqual(self.events, ["publish", "capture", "publish"])

	def test_recovery_cannot_reopen_a_deployment_or_restore_operation(self):
		runner, compose, _ = self.prepare_capture()
		offsite.manage.atomic_json(runner.pending, {"operation": "restore"})
		with self.assertRaisesRegex(RuntimeError, "restricted"):
			runner.recover()
		compose.mode.assert_not_called()

	def test_full_disk_preflight_does_not_interrupt_erp(self):
		runner, compose, _ = self.prepare_capture()
		with patch.object(runner, "space_required", return_value=2**63):
			with self.assertRaisesRegex(RuntimeError, "Insufficient"):
				runner.capture()
		compose.mode.assert_not_called()

	def test_global_maintenance_is_respected_before_any_state_change(self):
		import subprocess

		runner, compose, _ = self.prepare_capture()
		(self.root / "sites/erp.example.com").mkdir(parents=True)
		(self.root / "sites/erp.example.com/site_config.json").write_text("{}")
		(self.root / "sites/common_site_config.json").write_text('{"maintenance_mode": 1}')
		# Execute the actual narrow preflight script against real JSON files.
		compose.call.side_effect = lambda *args: subprocess.check_output(
			[sys.executable, "-c", args[-1]], cwd=self.root, text=True
		)
		with self.assertRaisesRegex(RuntimeError, "already in maintenance"):
			runner.capture()
		compose.mode.assert_not_called()

	def test_publish_refuses_low_space_before_upload_or_deleting_staging(self):
		runner = self.runner()
		self.snapshot(runner.staging)
		with patch("shutil.disk_usage") as space, patch.object(runner, "restic") as remote:
			space.return_value.free = 0
			with self.assertRaisesRegex(RuntimeError, "space"):
				runner.publish()
		remote.assert_not_called()
		self.assertTrue(runner.staging.exists())

	def test_publish_cannot_overlap_deployment_disk_usage(self):
		runner = self.runner()
		self.snapshot(runner.staging)
		with offsite.manage.deployment_lock(self.root), patch.object(runner, "restic") as remote:
			with self.assertRaisesRegex(RuntimeError, "Another deployment"):
				runner.publish()
		remote.assert_not_called()

	def test_complete_run_captures_then_publishes_without_nested_deployment_lock(self):
		runner, _, _ = self.prepare_capture()
		with patch.object(runner, "restic", side_effect=self.restic):
			self.assertEqual(runner.run(), "a" * 64)
		self.assertFalse(runner.staging.exists())

	def test_interrupted_readback_is_cleaned_before_retry_space_check(self):
		from types import SimpleNamespace

		runner = self.runner()
		self.snapshot(runner.staging)
		runner.verification.mkdir()
		(runner.verification / "partial-copy").write_bytes(b"partial readback")
		with (
			patch(
				"shutil.disk_usage",
				side_effect=lambda _: SimpleNamespace(free=0 if runner.verification.exists() else 1024**3),
			),
			patch.object(runner, "restic", side_effect=self.restic),
		):
			self.assertEqual(runner.publish(), "a" * 64)


if __name__ == "__main__":
	unittest.main()
