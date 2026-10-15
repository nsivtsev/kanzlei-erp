"""Storage behavior on a disposable Frappe site; no external credentials."""

from io import BytesIO
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class MemoryS3:
	def __init__(self):
		self.objects = {}

	def put_object(self, Bucket, Key, Body, **kwargs):
		self.objects[Key] = Body.read() if hasattr(Body, "read") else Body

	def get_object(self, Bucket, Key, **kwargs):
		data = self.objects[Key]
		return {"Body": BytesIO(data), "ContentLength": len(data)}

	def head_object(self, Bucket, Key):
		return {"ContentLength": len(self.objects[Key])}

	def delete_object(self, Bucket, Key):
		self.objects.pop(Key, None)


class TestObjectStorage(FrappeTestCase):
	def test_framework_thumbnail_is_stored_with_source_permissions(self):
		from io import BytesIO

		from PIL import Image

		image = BytesIO()
		Image.new("RGB", (32, 32), "red").save(image, format="PNG")
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "framework-image.png",
				"content": image.getvalue(),
				"is_private": 1,
			}
		).insert()
		url = file.make_thumbnail()
		self.assertTrue(url)
		binding = self.storage.get_binding(url)
		self.assertEqual(binding.source_file, file.name)
		self.assertIn("/derived/", binding.object_key)
		self.assertTrue(self.s3.objects[binding.object_key])
		from werkzeug.exceptions import Forbidden

		from kanzlei_erp.s3_http import authorize

		self.assertEqual(authorize(url, binding.object_key).name, file.name)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(Forbidden):
				authorize(url, binding.object_key)
		finally:
			frappe.set_user("Administrator")
		unassigned = file.make_thumbnail(set_as_thumbnail=False)
		variant = self.storage.get_binding(unassigned)
		self.assertEqual(authorize(unassigned, variant.object_key).name, file.name)
		self.assertTrue(self.storage.has_references(variant.object_key, current=True))
		frappe.delete_doc("File", file.name)
		self.storage.retry_deletions()
		self.assertNotIn(binding.object_key, self.s3.objects)
		self.assertNotIn(variant.object_key, self.s3.objects)

	def test_removed_native_is_not_required_for_backups(self):
		from suite.drive.utils.files import get_s3_url

		row = frappe._dict(
			file_url=get_s3_url("unit/drive/removed"),
			file_type="Text",
			content_doctype=None,
			status="Removed",
		)
		with patch.object(frappe, "get_all", return_value=[row]):
			self.assertEqual(list(self.storage.required_keys()), [])

	def test_failed_materialization_never_publishes_partial_file(self):
		from pathlib import Path

		file = frappe.get_doc(
			{"doctype": "File", "file_name": "retry.txt", "content": b"complete", "is_private": 1}
		).insert()
		self.storage.cleanup_temporary_files()
		with patch.object(self.s3, "get_object", side_effect=RuntimeError("interrupted")):
			with self.assertRaises(RuntimeError):
				file.get_full_path()
		self.assertEqual(Path(file.get_full_path()).read_bytes(), b"complete")

	def test_preview_cannot_be_claimed_without_source_permission(self):
		from PIL import Image

		image = BytesIO()
		Image.new("RGB", (32, 32), "blue").save(image, format="PNG")
		private = frappe.get_doc(
			{"doctype": "File", "file_name": "secret.png", "content": image.getvalue(), "is_private": 1}
		).insert()
		url = private.make_thumbnail()
		other = frappe.get_doc(
			{"doctype": "File", "file_name": "other.txt", "content": b"other", "is_private": 1}
		).insert()
		previous = frappe.get_doc("File", other.name)
		other.thumbnail_url = url
		frappe.set_user("Guest")
		try:
			from kanzlei_erp.drive_storage import validate_file_binding

			with self.assertRaises(frappe.PermissionError):
				validate_file_binding(other, previous=previous)
		finally:
			frappe.set_user("Administrator")
		from kanzlei_erp.drive_storage import invalidate

		managed = frappe._dict(private.as_dict())
		managed.kanzlei_drive_customer = "protected-customer"
		with patch("kanzlei_erp.drive_storage._managed", return_value=[managed]):
			for candidate in (url, private.file_url):
				invalidate()
				other.thumbnail_url = candidate
				with self.assertRaises(frappe.PermissionError):
					validate_file_binding(other, previous=previous)
		invalidate()

	def test_legacy_local_path_tracks_a_replaced_object(self):
		import hashlib

		file = frappe.get_doc(
			{"doctype": "File", "file_name": "cache.txt", "content": b"before", "is_private": 1}
		).insert()
		before = file.get_full_path()
		key = "unit/attachments/replacement"
		self.s3.objects[key] = b"after"
		self.storage.replace_content(file, key, 5, hashlib.sha256(b"after").hexdigest())
		after = file.get_full_path()
		self.assertNotEqual(before, after)
		from pathlib import Path

		self.assertEqual(Path(after).read_bytes(), b"after")

	def setUp(self):
		from kanzlei_erp import object_storage as storage

		storage.configure()
		self.storage = storage
		self.real_client = storage.client
		self.s3 = MemoryS3()
		self.client_patch = patch.object(storage, "client", return_value=self.s3)
		self.client_patch.start()
		self.addCleanup(self.client_patch.stop)
		with storage.configuration_change():
			settings = storage.settings()
			settings.update(
				{
					"enabled": 1,
					"flat": 1,
					"bucket": "unit-test",
					"root_folder": "unit/drive",
					"kanzlei_s3_prefix": "unit",
					"endpoint_url": "http://minio:9000",
				}
			)
			settings.save()

	def test_explicit_restore_target_uses_detached_secret(self):
		target = frappe._dict(
			bucket="target",
			aws_key="target-key",
			aws_secret="target-secret",
			endpoint_url="http://minio:9000",
			kanzlei_s3_region="us-east-1",
			kanzlei_s3_addressing_style="path",
		)
		with patch("boto3.client", return_value="configured") as create:
			self.assertEqual(self.real_client(settings=target), "configured")
		self.assertEqual(create.call_args.kwargs["aws_secret_access_key"], "target-secret")

	def test_framework_upload_is_readable_without_local_original(self):
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "object-test.txt",
				"is_private": 1,
				"content": b"remote original",
			}
		).insert()
		binding = self.storage.get_binding(file.file_url)
		self.assertTrue(binding)
		self.assertEqual(self.s3.objects[binding.object_key], b"remote original")
		file = frappe.get_doc("File", file.name)
		self.assertEqual(file.get_content(), "remote original")
		self.assertTrue(file.exists_on_disk())

	def test_raw_upload_hook_supports_legacy_file_manager(self):
		from frappe.utils.file_manager import save_file

		file = save_file("legacy-s3.xml", b"<s3 />", None, None, is_private=1)
		self.assertTrue(self.storage.get_binding(file.file_url))
		self.assertEqual(file.get_content(), "<s3 />")

	def test_bucket_failure_does_not_fall_back_to_disk(self):
		with patch.object(self.s3, "put_object", side_effect=RuntimeError("offline")):
			with self.assertRaises(RuntimeError):
				frappe.get_doc(
					{"doctype": "File", "file_name": "offline.txt", "content": b"no", "is_private": 1}
				).insert()
		self.assertFalse(frappe.db.exists("File", {"file_name": "offline.txt"}))

	def test_mapping_cannot_escape_managed_prefix(self):
		with self.assertRaises(frappe.ValidationError):
			self.storage.bind("/private/files/x", "other-site/secret", 1, "0" * 64)

	def test_mapping_cannot_be_written_through_document_api(self):
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": "S3 File Object",
					"file_url": "/private/files/forged",
					"object_key": "unit/a",
					"sha256": "0" * 64,
					"file_size": 1,
				}
			).insert(ignore_permissions=True)

	def test_live_backend_cannot_be_disabled_from_settings(self):
		file = frappe.get_doc(
			{"doctype": "File", "file_name": "settings.txt", "content": b"settings", "is_private": 1}
		).insert()
		settings = self.storage.settings()
		settings.enabled = 0
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		self.assertTrue(self.storage.get_binding(file.file_url))

	def test_local_path_context_cleans_up(self):
		file = frappe.get_doc(
			{"doctype": "File", "file_name": "temporary.txt", "content": b"temporary", "is_private": 1}
		).insert()
		with self.storage.local_path(file) as path:
			self.assertEqual(path.read_bytes(), b"temporary")
		self.assertFalse(path.exists())

	def test_shared_attachment_survives_deleting_one_reference(self):
		file = frappe.get_doc(
			{"doctype": "File", "file_name": "shared.txt", "content": b"shared", "is_private": 1}
		).insert()
		duplicate = frappe.get_doc(
			{"doctype": "File", "file_name": "shared-copy.txt", "file_url": file.file_url, "is_private": 1}
		).insert()
		frappe.delete_doc("File", file.name)
		self.storage.retry_deletions()
		self.assertEqual(duplicate.get_content(), "shared")

	def test_private_legacy_url_is_denied_to_guest(self):
		from werkzeug.exceptions import Forbidden

		from kanzlei_erp.s3_http import authorize

		file = frappe.get_doc(
			{"doctype": "File", "file_name": "private.txt", "content": b"private", "is_private": 1}
		).insert()
		key = self.storage.get_binding(file.file_url).object_key
		frappe.set_user("Guest")
		try:
			with self.assertRaises(Forbidden):
				authorize(file.file_url, key)
		finally:
			frappe.set_user("Administrator")

	def test_head_returns_metadata_without_a_get_signed_redirect(self):
		from werkzeug.exceptions import HTTPException
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		from kanzlei_erp.s3_http import before_request

		file = frappe.get_doc(
			{"doctype": "File", "file_name": "head.txt", "content": b"head-body", "is_private": 1}
		).insert()
		previous_request = getattr(frappe.local, "request", None)
		frappe.local.request = Request(EnvironBuilder(path=file.file_url, method="HEAD").get_environ())
		try:
			with self.assertRaises(HTTPException) as response:
				before_request()
			self.assertEqual(response.exception.response.status_code, 200)
			self.assertEqual(response.exception.response.headers["Content-Length"], "9")
			self.assertNotIn("Location", response.exception.response.headers)
		finally:
			frappe.local.request = previous_request

	def test_native_s3_file_supports_framework_byte_and_path_apis(self):
		from suite.drive.utils.files import get_s3_url

		key = "unit/drive/native-test"
		self.s3.objects[key] = b"native bytes"
		# A persisted server-created native File, without a legacy URL binding.
		file = frappe.get_doc(
			{
				"doctype": "File",
				"name": "native-test",
				"file_name": "native.txt",
				"file_url": get_s3_url(key),
				"is_private": 1,
				"is_folder": 0,
			}
		)
		file.db_insert()
		self.assertEqual(file.get_content(encodings=[]), b"native bytes")
		self.assertTrue(file.exists_on_disk())
		with self.storage.local_path(file) as path:
			self.assertEqual(path.read_bytes(), b"native bytes")

	def test_rolled_back_upload_does_not_leave_unreferenced_bytes(self):
		frappe.db.savepoint("s3_upload")
		file = frappe.get_doc(
			{"doctype": "File", "file_name": "rollback.txt", "content": b"rollback", "is_private": 1}
		).insert()
		key = self.storage.get_binding(file.file_url).object_key
		self.assertIn(key, self.s3.objects)
		# A full transaction rollback runs the armed remote cleanup callback.
		frappe.db.rollback()
		self.assertNotIn(key, self.s3.objects)
