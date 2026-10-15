import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


class MinioSetupTests(unittest.TestCase):
	def load_helper(self):
		path = ROOT / "scripts/minio_setup.py"
		self.assertTrue(path.exists(), "Local MinIO setup helper is missing")
		spec = importlib.util.spec_from_file_location("minio_setup", path)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module

	def test_credentials_are_private_persistent_and_do_not_replace_existing_env(self):
		helper = self.load_helper()
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / ".env"
			path.write_text("LOCAL_DB_ROOT_PASSWORD=keep-me\n# Keep this comment\n")
			first = helper.ensure_credentials(path)
			self.assertEqual(helper.ensure_credentials(path), first)
			self.assertIn("LOCAL_DB_ROOT_PASSWORD=keep-me\n# Keep this comment\n", path.read_text())
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
			self.assertNotEqual(first["LOCAL_MINIO_ROOT_USER"], first["LOCAL_S3_ACCESS_KEY"])
			for value in first.values():
				self.assertGreaterEqual(len(value), 20)

	def test_incomplete_existing_credentials_fail_without_rotating_them(self):
		helper = self.load_helper()
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / ".env"
			path.write_text("LOCAL_MINIO_ROOT_USER=previous-user\n")
			with self.assertRaisesRegex(ValueError, "incomplete"):
				helper.ensure_credentials(path)
			self.assertEqual(path.read_text(), "LOCAL_MINIO_ROOT_USER=previous-user\n")

	def test_existing_app_credentials_gain_separate_test_account_without_rotation(self):
		helper = self.load_helper()
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / ".env"
			base = {key: "existing-" + key for key in (
				"LOCAL_MINIO_ROOT_USER", "LOCAL_MINIO_ROOT_PASSWORD", "LOCAL_S3_ACCESS_KEY", "LOCAL_S3_SECRET_KEY")}
			path.write_text("".join(f"{key}={value}\n" for key, value in base.items()))
			result = helper.ensure_credentials(path)
			self.assertTrue("LOCAL_MINIO_TEST_ACCESS_KEY" in result, "Missing isolated test account")
			self.assertTrue("LOCAL_MINIO_TEST_SECRET_KEY" in result, "Missing isolated test secret")
			self.assertEqual({key: result[key] for key in base}, base)
			self.assertNotEqual(result["LOCAL_MINIO_TEST_ACCESS_KEY"], result["LOCAL_S3_ACCESS_KEY"])

	def test_site_settings_are_local_disabled_and_do_not_include_admin_credentials(self):
		helper = self.load_helper()
		settings = helper.local_settings({"LOCAL_S3_ACCESS_KEY": "app-key", "LOCAL_S3_SECRET_KEY": "app-secret"})
		self.assertEqual(settings["endpoint_url"], "http://minio:9000")
		self.assertEqual(settings["kanzlei_s3_public_endpoint"], "http://localhost:9002")
		self.assertEqual(settings["bucket"], "stb-golke-local")
		self.assertEqual(settings["enabled"], 0)
		self.assertEqual(settings["kanzlei_s3_prefix"], "local/development.localhost")
		self.assertEqual(settings["root_folder"], "local/development.localhost/drive")
		self.assertEqual(settings["kanzlei_s3_addressing_style"], "path")
		self.assertEqual(settings["kanzlei_s3_region"], "us-east-1")
		self.assertEqual(settings["aws_key"], "app-key")

	def test_refuses_to_overwrite_existing_s3_or_nonlocal_credentials(self):
		helper = self.load_helper()
		for current in ({"enabled": 1}, {"endpoint_url": "https://fsn1.your-objectstorage.com"},
			{"bucket": "stb-golke"}, {"aws_key": "production-access-key"}):
			with self.subTest(current=current), self.assertRaisesRegex(ValueError, "existing"):
				helper.validate_current_settings(current, "local-app-key")
		helper.validate_current_settings({}, "local-app-key")
		helper.validate_current_settings({"endpoint_url": "http://minio:9000", "bucket": "stb-golke-local", "aws_key": "local-app-key"}, "local-app-key")

	def test_configure_enters_sites_directory_before_initializing_frappe(self):
		helper = self.load_helper()
		with patch("os.chdir") as chdir:
			def initialize(**kwargs):
				chdir.assert_called_once_with("/workspace/development/frappe-bench/sites")
				self.assertEqual(kwargs["site"], "development.localhost")
			frappe = SimpleNamespace(init=initialize, connect=Mock(), set_user=Mock(),
				get_single=lambda doctype: SimpleNamespace(enabled=1), destroy=Mock())
			with patch.dict(sys.modules, {"frappe": frappe, "minio_setup": helper}), \
				patch("sys.stdin", io.StringIO(json.dumps({}))), patch("sys.stdout", io.StringIO()):
				exec(helper.CONFIGURE, {})
			frappe.connect.assert_called_once()
			frappe.destroy.assert_called_once()

	def test_verification_reads_only_test_credentials_and_does_not_generate_them(self):
		path = ROOT / "scripts/verify-s3-minio.py"
		self.assertTrue(path.exists(), "Reusable MinIO verification CLI is missing")
		spec = importlib.util.spec_from_file_location("verify_s3_minio", path)
		helper = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(helper)
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / ".env"
			original = "LOCAL_MINIO_ROOT_PASSWORD=never-use-root\nLOCAL_MINIO_TEST_ACCESS_KEY=test-key\nLOCAL_MINIO_TEST_SECRET_KEY=test-secret\n"
			path.write_text(original)
			settings = helper.read_test_settings(path)
			self.assertEqual(settings["bucket"], "stb-golke-test")
			self.assertEqual(settings["prefix"], "tests/s3-test.localhost")
			self.assertEqual(settings["public_endpoint"], "http://localhost:9002")
			self.assertNotIn("never-use-root", str(settings))
			self.assertEqual(path.read_text(), original)
			path.write_text("LOCAL_MINIO_TEST_ACCESS_KEY=incomplete\n")
			with self.assertRaisesRegex(ValueError, "test credentials"):
				helper.read_test_settings(path)

	def test_server_build_pins_source_and_all_base_image_digests(self):
		dockerfile = ROOT / ".devcontainer/minio.Dockerfile"
		self.assertTrue(dockerfile.exists(), "Source-built MinIO image is missing")
		text = dockerfile.read_text()
		self.assertIn("9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a", text)
		self.assertIn("7394ce0dd2a80935aded936b09fa12cbb3cb8096", text)
		for line in text.splitlines():
			if line.startswith("FROM "):
				self.assertRegex(line, r"@sha256:[a-f0-9]{64}")
		self.assertNotIn(":latest", text)


if __name__ == "__main__":
	unittest.main()
