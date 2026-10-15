import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "kanzlei_erp" / "s3_backup.py"


class Settings(dict):
	def __getattr__(self, name):
		return self.get(name)

	def get_password(self, field):
		return self[field]

	def __setattr__(self, field, value):
		self[field] = value

	def save(self, **kwargs):
		self["saved"] = True


class DetachedSettings(dict):
	# Frappe's _dict returns None for unknown attributes, including get_password.
	__getattr__ = dict.get


class Store:
	def __init__(self):
		self.objects = {"site/attachments/a": b"alpha", "site/drive/empty": b"", "other/keep": b"safe"}
		self.writes = []

	def get_paginator(self, operation):
		return self

	def paginate(self, **kwargs):
		for key in sorted(self.objects):
			if key.startswith(kwargs["Prefix"]):
				yield {"Contents": [{"Key": key, "Size": len(self.objects[key])}]}

	def get_object(self, **kwargs):
		return {"Body": io.BytesIO(self.objects[kwargs["Key"]]), "ContentType": "application/octet-stream"}

	def upload_fileobj(self, file, bucket, key, ExtraArgs=None):
		self.writes.append((bucket, key))
		self.objects[key] = file.read()

	def delete_object(self, **kwargs):
		del self.objects[kwargs["Key"]]


class SnapshotTests(unittest.TestCase):
	def setUp(self):
		self.assertTrue(SOURCE.exists(), "S3 snapshot implementation missing")
		spec = importlib.util.spec_from_file_location("test_s3_snapshot_module", SOURCE)
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.path = Path(self.temp.name) / "s3"
		self.store = Store()
		self.settings = Settings(
			enabled=1,
			bucket="source",
			aws_key="source-key",
			aws_secret="source-secret",
			endpoint_url="https://production",
			kanzlei_s3_prefix="site/",
		)
		self.storage = types.SimpleNamespace(
			settings=lambda: self.settings,
			client=lambda **kwargs: self.store,
			prefix=lambda **kwargs: kwargs.get("settings", self.settings).get("kanzlei_s3_prefix", "site/"),
			configuration_change=contextlib.nullcontext,
			required_keys=lambda: ["site/attachments/a", "site/drive/empty"],
		)
		self.frappe = types.SimpleNamespace(
			db=types.SimpleNamespace(commit=lambda: None),
			flags=types.SimpleNamespace(),
			clear_cache=lambda: None,
			_dict=DetachedSettings,
		)
		self.patches = patch.dict(
			sys.modules, {"kanzlei_erp.object_storage": self.storage, "frappe": self.frappe}
		)
		self.patches.start()
		self.addCleanup(self.patches.stop)

	def export(self):
		self.module.export_snapshot(str(self.path))
		return json.loads((self.path / "manifest.json").read_text())

	def test_roundtrip_restores_entire_prefix_including_empty_objects(self):
		manifest = self.export()
		self.assertEqual(len(manifest["objects"]), 2)
		self.assertNotIn("source-secret", (self.path / "manifest.json").read_text())
		self.store.objects["site/attachments/a"] = b"changed"
		self.store.objects["site/new"] = b"new"
		self.module.restore_snapshot(str(self.path))
		self.assertEqual(
			self.store.objects,
			{
				"site/attachments/a": b"alpha",
				"site/drive/empty": b"",
				"site/new": b"new",
				"other/keep": b"safe",
			},
		)

	def test_corrupt_payload_fails_before_any_remote_mutation(self):
		manifest = self.export()
		(self.path / manifest["objects"][0]["payload"]).write_bytes(b"broken")
		with self.assertRaisesRegex(ValueError, "checksum"):
			self.module.restore_snapshot(str(self.path))
		self.assertEqual(self.store.writes, [])

	def test_path_traversal_payload_is_rejected_before_reading(self):
		manifest = self.export()
		manifest["objects"][0]["payload"] = "../escape"
		(self.path / "manifest.json").write_text(json.dumps(manifest))
		with self.assertRaisesRegex(ValueError, "payload"):
			self.module.restore_snapshot(str(self.path))
		self.assertEqual(self.store.writes, [])

	def test_export_missing_referenced_object_cannot_create_complete_snapshot(self):
		del self.store.objects["site/attachments/a"]
		with self.assertRaisesRegex(ValueError, "missing"):
			self.export()
		self.assertFalse((self.path / "manifest.json").exists())

	def test_explicit_target_requires_all_connection_fields(self):
		self.export()
		with self.assertRaisesRegex(ValueError, "target"):
			self.module.restore_snapshot(str(self.path), target={"endpoint_url": "http://minio:9000"})
		self.assertEqual(self.store.writes, [])

	def test_explicit_target_uses_only_target_credentials_and_saves_after_verification(self):
		self.export()
		target = {
			"bucket": "local",
			"aws_key": "local-key",
			"aws_secret": "local-secret",
			"endpoint_url": "http://minio:9000",
			"signature_version": "s3v4",
			"kanzlei_s3_region": "us-east-1",
			"kanzlei_s3_addressing_style": "path",
			"kanzlei_s3_public_endpoint": "http://localhost:9000",
			"kanzlei_s3_prefix": "site/",
		}
		connections = []

		def connect(**kwargs):
			connections.append(dict(kwargs["settings"]))
			self.assertFalse(self.settings.get("saved"))
			return self.store

		self.storage.client = connect
		self.module.restore_snapshot(str(self.path), target=target)
		self.assertEqual(connections, [{**target, "enabled": 1}])
		self.assertEqual(self.settings["aws_secret"], "local-secret")
		self.assertTrue(self.settings["saved"])
		self.assertTrue(all(bucket == "local" for bucket, key in self.store.writes))

	def test_failed_remote_readback_does_not_save_target_settings(self):
		self.export()
		get = self.store.get_object

		def broken(**kwargs):
			return {"Body": io.BytesIO(b"corrupt")} if self.store.writes else get(**kwargs)

		self.store.get_object = broken
		with self.assertRaisesRegex(ValueError, "checksum"):
			self.module.restore_snapshot(str(self.path))
		self.assertFalse(self.settings.get("saved"))

	def test_payload_symlink_is_rejected_before_any_remote_mutation(self):
		manifest = self.export()
		payload = self.path / manifest["objects"][0]["payload"]
		outside = self.path.parent / "outside"
		outside.write_bytes(payload.read_bytes())
		payload.unlink()
		payload.symlink_to(outside)
		with self.assertRaisesRegex(ValueError, "payload"):
			self.module.restore_snapshot(str(self.path))
		self.assertEqual(self.store.writes, [])

	def test_disabled_site_does_not_contact_s3(self):
		self.settings["enabled"] = 0
		self.storage.client = lambda **kwargs: self.fail("disabled snapshot contacted S3")
		manifest = self.export()
		self.assertFalse(manifest["enabled"])
		self.assertEqual(manifest["objects"], [])


if __name__ == "__main__":
	unittest.main()
