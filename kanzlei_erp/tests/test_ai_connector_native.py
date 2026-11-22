"""Native Suite uploads on real ERP rows with a synthetic S3 transport."""

import hashlib
from contextlib import contextmanager, nullcontext
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import frappe
from botocore.exceptions import ClientError
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.ai_connector import inventory, policy
from kanzlei_erp.ai_connector.errors import ConnectorError
from kanzlei_erp.tests import test_ai_connector as connector_fixtures
from kanzlei_erp.tests import test_ai_connector_email as mail_fixtures


class SyntheticS3:
	def __init__(self):
		self.objects = {}
		self.heads = []
		self.gets = []
		self.generation = "one"
		self.reject_get = False
		self.override = {}
		self.after_get = None

	def upload_file(self, path, bucket, key):
		self.objects[bucket, key] = Path(path).read_bytes()

	def delete_object(self, Bucket, Key):
		self.objects.pop((Bucket, Key), None)

	def metadata(self, bucket, key):
		body = self.objects.get((bucket, key))
		if body is None:
			raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 404}}, "Object")
		return {
			"ETag": '"' + hashlib.sha256(body).hexdigest() + '"',
			"ContentLength": len(body),
			"VersionId": self.generation,
			**self.override,
		}

	def head_object(self, Bucket, Key):
		self.heads.append((Bucket, Key))
		return self.metadata(Bucket, Key)

	def get_object(self, Bucket, Key, IfMatch):
		self.gets.append((Bucket, Key))
		metadata = self.metadata(Bucket, Key)
		if self.reject_get or IfMatch != metadata["ETag"]:
			raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 412}}, "GetObject")
		data = self.objects[Bucket, Key]
		if self.after_get:
			self.after_get()
		return {**metadata, "Body": BytesIO(data)}


class TestAIConnectorNative(FrappeTestCase):
	api = connector_fixtures.TestAIConnector.api
	call = connector_fixtures.TestAIConnector.call
	customer_fixture = connector_fixtures.TestAIConnector.customer_fixture
	make_drive_root = connector_fixtures.TestAIConnector.make_drive_root
	make_drive_child = connector_fixtures.TestAIConnector.make_drive_child
	make_file = connector_fixtures.TestAIConnector.make_file
	grant = connector_fixtures.TestAIConnector.grant
	scope = connector_fixtures.TestAIConnector.scope
	documents = connector_fixtures.TestAIConnector.documents

	def setUp(self):
		from suite.drive.utils import ROOT_FOLDER
		from suite.drive.utils.files import get_s3_url

		from kanzlei_erp import drive_storage

		frappe.db.rollback()
		frappe.db.set_single_value("Drive Disk Settings", "enabled", 0)
		drive_storage.invalidate()
		connector_fixtures.TestAIConnector.setUp(self)
		self.local_file = self.file
		self.s3 = SyntheticS3()
		# Every File, share, root, upload and event is real. Only network I/O is replaced.
		for target in ("kanzlei_erp.object_storage.client", "boto3.client"):
			with_client = patch(target, return_value=self.s3)
			with_client.start()
			self.addCleanup(with_client.stop)
		from frappe.utils.password import set_encrypted_password

		frappe.set_user("Administrator")
		frappe.db.set_single_value(
			"Drive Disk Settings",
			{
				"enabled": 1,
				"flat": 1,
				"bucket": "synthetic",
				"root_folder": "fixture/drive",
				"kanzlei_s3_prefix": "fixture",
				"endpoint_url": "http://minio:9000",
				"aws_key": "fixture",
			},
		)
		# Suite derives flat upload keys from its persisted Drive root on existing sites.
		if frappe.db.exists("File", ROOT_FOLDER):
			frappe.db.set_value("File", ROOT_FOLDER, "file_url", get_s3_url("fixture/drive"))
		set_encrypted_password(
			"Drive Disk Settings", "Drive Disk Settings", "synthetic-fixture", "aws_secret"
		)

	def tearDown(self):
		from kanzlei_erp import drive_storage

		frappe.set_user("Administrator")
		frappe.db.rollback()  # Run Suite write-intent cleanup while the transport patch is active.
		drive_storage.invalidate()
		super().tearDown()

	def upload_native(self, filename="native.pdf"):
		from suite.drive.api.files import upload_file

		from kanzlei_erp.object_storage import get_binding, settings

		request = Request(
			EnvironBuilder(
				method="POST",
				data={
					"file": (BytesIO(self.original), filename),
				},
			).get_environ()
		)
		with (
			patch.object(frappe.local, "request", request, create=True),
			patch.object(frappe.local, "form_dict", frappe._dict()),
			patch("suite.drive.utils.files.FileManager.can_create_thumbnail", return_value=False),
		):
			file = upload_file(total_file_size=len(self.original), parent=self.root.name)
		self.key_tuple = (settings().bucket, "fixture/drive/" + file.name)
		self.assertTrue(file.file_url.endswith("fixture/drive/" + file.name))
		self.assertIsNone(get_binding(file.file_url))
		self.assertTrue(self.s3.objects.get(self.key_tuple) == self.original, "Upload bytes differ")
		self.assertFalse(self.s3.heads, "Upload transaction performed an indexing HEAD")
		return file

	def as_reader(self):
		frappe.set_user(self.reader.name)
		frappe.session.sid = self.sid

	def assert_error(self, status, function, *args, **kwargs):
		with self.assertRaises(ConnectorError) as result:
			function(*args, **kwargs)
		self.assertEqual(result.exception.status, status)

	def test_native_upload_projects_and_reads_with_the_same_revision(self):
		file = self.upload_native()
		self.as_reader()
		manifest = inventory.project(file.name, self.customer.name, {self.customer.name})
		self.assertTrue(isinstance(manifest, dict), "Native upload missing from inventory")
		self.assertEqual(manifest["byte_size"], len(self.original))
		token = self.grant()
		page = self.documents(token)
		delegated = next(item for item in page["items"] if item["source_id"] == "file:" + file.name)
		self.assertTrue(delegated["revision"] == manifest["revision"], "Manifest revisions differ")
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		self.assertTrue(policy.authorize_sources(self.scope(), [ref])["decisions"][0]["allowed"])
		content = inventory.read_current(ref, {self.customer.name})
		self.assertTrue(content == self.original, "Background bytes differ")
		with patch.object(inventory, "current_permission_context", nullcontext):
			response = self.call("read_source", access_token=token, scope=self.scope(), source=ref)
		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.get_data() == self.original, "Delegated bytes differ")

	def test_native_drive_docx_keeps_file_identity_and_attachment_locator(self):
		self.original = mail_fixtures.synthetic_docx()
		file = self.upload_native("native.DOCX")
		frappe.db.set_value("File", file.name, "mime_type", "application/octet-stream")
		self.as_reader()
		manifest = inventory.project(file.name, self.customer.name, {self.customer.name})
		self.assertEqual(manifest["source_id"], "file:" + file.name)
		self.assertEqual(manifest["mime_type"], mail_fixtures.DOCX_MIME)
		self.assertNotIn("parent_message_id", manifest)
		locator = urlsplit(manifest["locator"])
		self.assertEqual(locator.path, "/api/method/kanzlei_erp.ai_connector.api.download_drive_docx")
		self.assertEqual(
			parse_qs(locator.query),
			{"customer": [self.customer.name], "file_id": [file.name], "revision": [manifest["revision"]]},
		)
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		self.assertEqual(policy.resolve_source_locator(self.scope(), ref)["locator"], manifest["locator"])
		self.assertTrue(policy.authorize_sources(self.scope(), [ref])["decisions"][0]["allowed"])
		self.assertEqual(inventory.read_current(ref, {self.customer.name}), self.original)

	def test_delegated_native_read_denies_if_final_permission_check_denies(self):
		file = self.upload_native()
		self.as_reader()
		manifest = inventory.project(file.name, self.customer.name, {self.customer.name})
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		token = self.grant()

		@contextmanager
		def denied_final_check():
			self.assertEqual(len(self.s3.gets), 1)
			with patch.object(policy, "_file", side_effect=frappe.PermissionError):
				yield

		with patch.object(inventory, "current_permission_context", denied_final_check):
			response = self.call("read_source", access_token=token, scope=self.scope(), source=ref)
		self.assertEqual(response.status_code, 403)
		self.assertNotEqual(response.get_data(), self.original)

	def test_delegated_native_documents_continue_across_bounded_pages(self):
		expected = {"file:" + self.upload_native().name for _index in range(6)}
		self.as_reader()
		cursor, seen = None, set()
		for _page in range(4):
			page = policy.documents(self.customer.name, page_limit=25, cursor=cursor)
			self.assertLessEqual(len(page["items"]), 4)
			ids = {item["source_id"] for item in page["items"]}
			self.assertFalse(seen & ids)
			seen.update(ids)
			cursor = page["next_cursor"]
			if not cursor:
				break
		self.assertIsNone(cursor)
		self.assertTrue(expected <= seen)

	def test_inventory_and_change_cursors_keep_native_uploads_across_yields(self):
		from kanzlei_erp.ai_connector import native_drive

		frappe.db.set_value("File", self.local_file.name, "kanzlei_drive_customer", None)
		ingestion_key = "fixture-ingestion-" + frappe.generate_hash(length=32)
		self.config["ingestion"] = {
			"enabled": True,
			"user": self.reader.name,
			"client_ids": [self.customer.name],
			"generation": "native-fixture",
			"key_sha256": hashlib.sha256(ingestion_key.encode()).hexdigest(),
		}
		with patch.object(self, "key", ingestion_key):
			watermark = self.call("inventory", limit=10)["changes_cursor"]
		expected = {"file:" + self.upload_native().name, "file:" + self.upload_native().name}
		# Make a page yield after its first HEAD. Every candidate and every
		# event must still appear across the resulting short pages.
		with patch.object(self, "key", ingestion_key), patch.object(native_drive, "READ_TIMEOUT", 4):
			cursor, seen = None, set()
			for _page in range(8):
				page = self.call("inventory", cursor=cursor, limit=10)
				seen.update(item["source_id"] for item in page["items"])
				cursor = page["next_cursor"]
				if not cursor:
					break
			self.assertIsNone(cursor)
			self.assertTrue(seen == expected, "Inventory skipped a native source")
			cursor, seen = watermark, set()
			for _page in range(64):
				page = self.call("changes", cursor=cursor, limit=10)
				if not page["items"]:
					break
				for item in page["items"]:
					if item["source_id"] in expected:
						self.assertEqual(item["operation"], "upsert")
						seen.add(item["source_id"])
				self.assertTrue(page["next_cursor"] != cursor, "Change cursor did not advance")
				cursor = page["next_cursor"]
			self.assertTrue(not page["items"], "Change feed did not reach its end")
			self.assertTrue(seen == expected, "Change feed skipped a native source")

	def test_cross_scope_and_wrong_native_keys_do_not_query_s3(self):
		from suite.drive.utils.files import get_s3_url

		from kanzlei_erp.drive_storage import invalidate

		file = self.upload_native()
		self.as_reader()
		self.assertIsNone(inventory.project(file.name, self.other.name, {self.other.name}))
		ref = {"source_id": "file:" + file.name, "revision": "0" * 64, "client_id": self.other.name}
		self.assertFalse(policy.authorize_sources(self.scope(), [ref])["decisions"][0]["allowed"])
		for url in (
			get_s3_url("elsewhere/drive/" + file.name),
			get_s3_url("fixture/drive/other-file"),
			get_s3_url("fixture/drive/../" + file.name),
			get_s3_url("fixture/drive/" + file.name) + "&other=value",
		):
			frappe.db.set_value("File", file.name, "file_url", url)
			invalidate()
			self.assertIsNone(inventory.project(file.name, self.customer.name, {self.customer.name}))
			ref["client_id"] = self.customer.name
			self.assertFalse(policy.authorize_sources(self.scope(), [ref])["decisions"][0]["allowed"])
		self.assertFalse(self.s3.heads)
		self.assertFalse(self.s3.gets)

	def test_native_storage_access_is_required_before_object_metadata(self):
		file = self.upload_native()
		self.as_reader()
		with patch("kanzlei_erp.drive_storage.require_storage_access", side_effect=frappe.PermissionError):
			self.assertIsNone(inventory.project(file.name, self.customer.name, {self.customer.name}))
		self.assertFalse(self.s3.heads)
		self.assertFalse(self.s3.gets)

	def test_current_fingerprint_and_conditional_get_withhold_bytes(self):
		file = self.upload_native()
		self.as_reader()
		manifest = inventory.project(file.name, self.customer.name, {self.customer.name})
		self.assertTrue(isinstance(manifest, dict), "Native upload missing from inventory")
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		self.s3.generation = "two"  # Same size and ETag with a new VersionId.
		self.assertFalse(policy.authorize_sources(self.scope(), [ref])["decisions"][0]["allowed"])
		self.assert_error(409, policy.read_source, self.scope(), ref)
		self.assertFalse(self.s3.gets)
		self.s3.generation = "one"
		self.s3.reject_get = True
		self.assert_error(409, policy.read_source, self.scope(), ref)
		self.assertEqual(len(self.s3.gets), 1)
		self.s3.reject_get = False
		self.s3.after_get = lambda: setattr(self.s3, "generation", "after-read")
		self.assert_error(409, policy.read_source, self.scope(), ref)

	def test_invalid_or_missing_native_fingerprint_fails_closed(self):
		file = self.upload_native()
		self.as_reader()
		clients = {self.customer.name}
		self.s3.override = {"ETag": None}
		self.assert_error(503, inventory.project, file.name, self.customer.name, clients)
		self.s3.override = {"ContentLength": policy.MAX_BYTES + 1}
		self.assertIsNone(inventory.project(file.name, self.customer.name, clients))
		self.s3.override.clear()
		self.s3.objects.pop(self.key_tuple)
		self.assertIsNone(inventory.project(file.name, self.customer.name, clients))
		self.assertFalse(self.s3.gets)
