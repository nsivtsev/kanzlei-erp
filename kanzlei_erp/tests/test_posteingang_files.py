"""Original identity, aliases and private byte responses."""

from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangFiles(PosteingangTestCase):
	def test_original_cannot_be_detached_or_made_public(self):
		doc = self.letter()
		for values in ({"is_private": 0}, {"attached_to_name": None}, {"file_name": "renamed.pdf"}):
			with self.subTest(values=values):
				file = frappe.get_doc("File", doc.original_file)
				file.update(values)
				with self.assertRaises(frappe.PermissionError):
					file.save(ignore_permissions=True)

	def test_reader_cannot_create_alias(self):
		doc = self.letter()
		file = frappe.get_doc("File", doc.original_file)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{"doctype": "File", "file_url": file.file_url, "file_name": "alias.pdf", "is_private": 1}
			).insert(ignore_permissions=True)

	def test_byte_response_has_no_redirect_and_checks_each_request(self):
		from kanzlei_erp.posteingang_files import response

		doc = self.confirm_fixture(self.letter())
		file = frappe.get_doc("File", doc.original_file)
		frappe.set_user(self.reader.name)
		for method, headers, status in (
			("GET", {}, 200),
			("HEAD", {}, 200),
			("GET", {"Range": "bytes=0-4"}, 206),
			("GET", {"Range": "bytes=99999999-"}, 416),
		):
			env = EnvironBuilder(path=file.file_url, method=method, headers=headers).get_environ()
			with patch.object(frappe.local, "request", Request(env), create=True):
				res = response(file)
				self.assertEqual(res.status_code, status)
				self.assertNotIn("Location", res.headers)
				self.assertIn("no-store", res.headers["Cache-Control"])
				res.close()
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user(self.reader.name)
		with self.assertRaises(frappe.PermissionError):
			response(file)

	def test_archive_rejected_even_for_operator(self):
		from kanzlei_erp.posteingang_files import reject_archive

		doc = self.letter()
		with self.assertRaises(frappe.PermissionError):
			reject_archive([doc.original_file])

	def test_content_overwrite_is_rejected_before_disk_write(self):
		doc = self.letter()
		file = frappe.get_doc("File", doc.original_file)
		with self.assertRaises(frappe.PermissionError):
			file.save_file(content=b"replacement", overwrite=True)
		self.assertNotEqual(file.get_content(encodings=[]), b"replacement")

	def test_same_bytes_new_original_gets_independent_file_url(self):
		from kanzlei_erp.posteingang_model import create_received_letter

		doc = self.letter()
		first = frappe.get_doc("File", doc.original_file)
		second = create_received_letter(
			actor=self.operator.name,
			original={"content": first.get_content(encodings=[]), "file_name": "same.pdf", "page_count": 1},
			receipt={"received_date_known": False, "received_date": None},
			operation={
				"key": frappe.generate_hash(length=64),
				"fingerprint": frappe.generate_hash(length=64),
			},
		)
		file = frappe.get_doc("File", second.original_file)
		self.paths.append(__import__("pathlib").Path(file.get_full_path()))
		self.assertNotEqual(first.name, file.name)
		self.assertNotEqual(first.file_url, file.file_url)
		self.assertEqual(doc.content_sha256, second.content_sha256)

	def test_suite_entry_points_cannot_mint_token_or_archive(self):
		if "suite" not in frappe.get_installed_apps():
			self.skipTest("Suite integration")
		from suite.drive.api.files import create_auth_token, get_file_content
		from suite.drive.utils.downloads import collect_downloads

		doc = self.letter()
		with self.assertRaises(frappe.PermissionError):
			create_auth_token(doc.original_file)
		with self.assertRaises(frappe.PermissionError):
			collect_downloads([doc.original_file])
		env = EnvironBuilder(path="/api/v2/method/suite.drive.api.files.get_file_content").get_environ()
		with patch.object(frappe.local, "request", Request(env), create=True):
			res = get_file_content(doc.original_file, trigger_download=True)
			self.assertIn("no-store", res.headers["Cache-Control"])
			res.close()

	def test_parent_folder_cannot_mutate_protected_descendant(self):
		from kanzlei_erp.posteingang_files import path_guard, require_access
		doc = self.letter()
		file = frappe.get_doc("File", doc.original_file)
		with self.assertRaises(frappe.PermissionError):
			require_access(frappe.get_doc("File", file.folder), "write")
		with self.assertRaises(frappe.PermissionError):
			path_guard(file.get_full_path().rsplit("/", 1)[0], "write")

	def test_filesystem_alias_forms_cannot_claim_original(self):
		from pathlib import Path
		doc = self.letter()
		file = frappe.get_doc("File", doc.original_file)
		original = Path(file.get_full_path())
		link = original.parent / ("alias-" + frappe.generate_hash(length=10) + ".pdf")
		link.symlink_to(original.resolve())
		self.paths.append(link)
		for url in (str(original), str(original.resolve()), str(link.resolve()), str(link)):
			with self.subTest(url=url):
				with self.assertRaises(frappe.PermissionError):
					frappe.get_doc({"doctype": "File", "file_name": "alias.pdf", "file_url": url,
						"is_private": 1}).insert(ignore_permissions=True)


class TestPosteingangS3(PosteingangTestCase):
	def setUp(self):
		if "suite" not in frappe.get_installed_apps():
			self.skipTest("Suite S3 integration")
		from kanzlei_erp.tests.test_object_storage import TestObjectStorage

		TestObjectStorage.setUp(self)
		super().setUp()

	def test_s3_legacy_alias_is_rejected(self):
		from suite.drive.utils.files import get_s3_url

		doc = self.letter()
		file = frappe.get_doc("File", doc.original_file)
		key = self.storage.get_binding(file.file_url).object_key
		for url in (get_s3_url(key), key):
			with self.subTest(url=url):
				with self.assertRaises(frappe.PermissionError):
					frappe.get_doc(
						{"doctype": "File", "file_name": "alias.pdf", "file_url": url, "is_private": 1}
					).insert(ignore_permissions=True)

	def test_presigned_url_never_issued_for_original_key(self):
		from suite.drive.utils.files import FileManager

		doc = self.letter()
		file = frappe.get_doc("File", doc.original_file)
		key = self.storage.get_binding(file.file_url).object_key
		with self.assertRaises(frappe.PermissionError):
			FileManager().presigned_url(key, file.file_name)

	def test_failed_constructor_removes_s3_bytes_before_caller_commit(self):
		from kanzlei_erp.posteingang_model import ProtectedRecord

		validate = ProtectedRecord.validate
		before = set(self.s3.objects)

		def fail(doc):
			if doc.doctype == "Incoming Letter Event":
				raise RuntimeError("Synthetic failure")
			return validate(doc)

		with patch.object(ProtectedRecord, "validate", fail):
			with self.assertRaises(RuntimeError):
				self.letter()
		self.assertEqual(set(self.s3.objects), before)

	def test_derivative_keeps_source_permissions_and_its_own_content_type(self):
		from kanzlei_erp.posteingang_files import response

		doc = self.letter()
		file = frappe.get_doc("File", doc.original_file)
		url, key = "/private/files/synthetic-preview.webp", "unit/derived/synthetic-preview"
		self.s3.objects[key] = b"synthetic preview"
		self.storage.bind(url, key, 17, "a" * 64, "image/webp", source_file=file.name, keep_with_source=True)
		file.file_url = url
		env = EnvironBuilder(path=url).get_environ()
		with patch.object(frappe.local, "request", Request(env), create=True):
			res = response(file)
			self.assertEqual(res.mimetype, "image/webp")
			res.close()
		self.confirm_fixture(doc, quarantine=True)
		frappe.set_user(self.operator.name)
		with self.assertRaises(frappe.PermissionError):
			response(file)

	def test_readable_preview_cannot_be_claimed_as_ordinary_thumbnail(self):
		doc = self.letter()
		url, key = "/private/files/no-alias-preview.webp", "unit/derived/no-alias-preview"
		self.s3.objects[key] = b"synthetic preview"
		self.storage.bind(
			url, key, 17, "b" * 64, "image/webp", source_file=doc.original_file, keep_with_source=True
		)
		ordinary = frappe.get_doc(
			{"doctype": "File", "file_name": "ordinary.txt", "content": b"ordinary", "is_private": 1}
		).insert(ignore_permissions=True)
		ordinary.thumbnail_url = url
		with self.assertRaises(frappe.PermissionError):
			ordinary.save(ignore_permissions=True)

	def test_old_thumbnail_alias_cannot_read_derivative_after_revocation(self):
		doc = self.letter()
		url, key = "/private/files/old-preview.webp", "unit/derived/old-preview"
		self.s3.objects[key] = b"synthetic preview"
		self.storage.bind(
			url, key, 17, "c" * 64, "image/webp", source_file=doc.original_file, keep_with_source=True
		)
		ordinary = frappe.get_doc(
			{"doctype": "File", "file_name": "old-alias.txt", "content": b"ordinary old", "is_private": 1}
		).insert(ignore_permissions=True)
		frappe.db.set_value("File", ordinary.name, "thumbnail_url", url)
		frappe.set_user("Administrator")
		frappe.share.add("File", ordinary.name, self.reader.name, read=1)
		self.confirm_fixture(doc)
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user(self.reader.name)
		from werkzeug.exceptions import Forbidden

		with self.assertRaises((frappe.PermissionError, Forbidden)):
			self.storage.url_content(url)
