"""Download decisions must follow stored content, not visual File types."""

from io import BytesIO
from pathlib import Path
import shutil
from unittest.mock import patch
from zipfile import ZipFile

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.tests import test_mandant_drive as fixtures
from kanzlei_erp.tests.drive_samples import MIMES, samples


class TestDriveDownloads(FrappeTestCase):
	setUp = fixtures.TestMandantDrive.setUp
	make_user = fixtures.TestMandantDrive.make_user
	root = fixtures.TestMandantDrive.root

	def tearDown(self):
		frappe.set_user("Administrator")
		for path in self.paths:
			if path.is_dir():
				shutil.rmtree(path)
			elif path.exists():
				path.unlink()
		for key in self.cache_keys:
			frappe.cache().delete_value(key)
		FrappeTestCase.tearDown(self)

	def upload(self, root, name, content):
		from suite.drive.utils import create_drive_file, get_file_type
		from suite.drive.utils.files import FileManager
		manager = FileManager()
		mime = MIMES[name.rsplit(".", 1)[-1]]
		file = create_drive_file(name, root.name, get_file_type(mime),
			lambda doc: "/" + str(manager.get_disk_path(doc)), mime, len(content))
		manager.get_local_path(file.file_url).write_bytes(content)
		return file

	def archive(self, entities):
		from suite.drive.api.files import _download_cache_key, build_download_archive
		token = "download-test-" + frappe.generate_hash(12)
		key = _download_cache_key(token)
		self.cache_keys.append(key)
		build_download_archive(token, entities, "test.zip", frappe.session.user)
		entry = frappe.cache().get_value(key)
		if entry.get("key"):
			self.paths.append(Path(frappe.get_site_path(entry["key"])))
		return token, entry

	def test_direct_office_download_preserves_real_document_bytes(self):
		from suite.drive.api.files import get_file_content
		root = self.root()
		for extension, content in samples().items():
			file = self.upload(root, "invoice." + extension, content)
			frappe.set_user(self.reader.name)
			with self.subTest(extension=extension), patch.object(frappe.local, "request",
				Request(EnvironBuilder().get_environ()), create=True):
				response = get_file_content(file.name)
				response.direct_passthrough = False
				self.assertEqual(response.get_data(), content)
			frappe.set_user("Administrator")

	def test_zip_contains_all_office_and_other_uploaded_files(self):
		root = self.root()
		contents = samples()
		for extension, content in contents.items():
			self.upload(root, "invoice." + extension, content)
		frappe.set_user(self.reader.name)
		_, entry = self.archive([root.name])
		self.assertEqual(entry["status"], "ready")
		with ZipFile(frappe.get_site_path(entry["key"])) as archive:
			self.assertEqual(len(archive.namelist()), len(contents))
			for extension, content in contents.items():
				self.assertEqual(archive.read(root.file_name + "/invoice." + extension), content)

	def test_card_and_list_expose_download_capability_without_storage_key(self):
		from suite.drive.api.permissions import get_entity_with_permissions
		from suite.drive.api.list import files
		root = self.root()
		file = self.upload(root, "slides.pptx", samples()["pptx"])
		frappe.set_user(self.reader.name)
		card = get_entity_with_permissions(file.name)
		self.assertTrue(card.get("can_download"))
		self.assertEqual(card.get("mime_type"), MIMES["pptx"])
		self.assertIsNone(card.get("file_url"))
		row = next(row for row in files(root.name) if row["name"] == file.name)
		self.assertTrue(row.get("can_download"))
		self.assertIsNone(row.get("file_url"))

	def test_overlapping_selection_and_name_collisions_keep_every_blob_once(self):
		from suite.drive.api.files import create_folder
		root = self.root()
		frappe.set_user(self.writer.name)
		left = create_folder("left", root.name)
		right = create_folder("right", root.name)
		one = self.upload(left, "same.xml", b"<one />")
		two = self.upload(right, "same.xml", b"<two />")
		_, entry = self.archive([one.name, root.name, two.name, root.name])
		self.assertEqual(entry["status"], "ready")
		with ZipFile(frappe.get_site_path(entry["key"])) as archive:
			self.assertEqual(len(archive.namelist()), 2)
			self.assertEqual(len(set(archive.namelist())), 2)
			self.assertEqual({archive.read(name) for name in archive.namelist()}, {b"<one />", b"<two />"})

	def test_standalone_files_with_same_name_have_distinct_zip_members(self):
		from suite.drive.api.files import create_folder
		root = self.root()
		frappe.set_user(self.writer.name)
		one = self.upload(create_folder("left", root.name), "same.xml", b"<one />")
		two = self.upload(create_folder("right", root.name), "same.xml", b"<two />")
		_, entry = self.archive([one.name, two.name])
		with ZipFile(frappe.get_site_path(entry["key"])) as archive:
			self.assertEqual(len(set(archive.namelist())), 2)
			self.assertEqual({archive.read(name) for name in archive.namelist()}, {b"<one />", b"<two />"})

	def test_old_ready_manifest_requires_rebuilding(self):
		from suite.drive.api.files import _download_cache_key, download_status
		root = self.root()
		file = self.upload(root, "invoice.xml", b"<one />")
		frappe.set_user(self.reader.name)
		key = _download_cache_key("legacy-download")
		self.cache_keys.append(key)
		frappe.cache().set_value(key, {"status": "ready", "owner": self.reader.name,
			"entities": [root.name], "files": [file.name], "key": "private/files/old.zip"})
		with self.assertRaises(frappe.DoesNotExistError):
			download_status("legacy-download")

	def ordinary_folder(self):
		from suite.drive.api.files import create_folder
		from suite.drive.utils import get_user_folder
		from suite.drive.utils.files import FileManager
		frappe.set_user(self.writer.name)
		folder = create_folder("downloads-" + frappe.generate_hash(8), get_user_folder().name)
		self.paths.append(FileManager().get_local_path(folder.file_url))
		return folder

	def test_hosted_writer_and_links_are_counted_without_reading_their_storage(self):
		from suite.writer.api.docs import create_document
		from suite.drive.api.files import create_link, download_status, get_file_content
		folder = self.ordinary_folder()
		file = self.upload(folder, "invoice.xml", b"<ordinary />")
		hosted = create_document(title="Hosted document", parent=folder.name)
		link = create_link("Source", "https://example.invalid/source", folder.name)
		token, entry = self.archive([folder.name])
		self.assertEqual(entry["status"], "ready")
		self.assertEqual(entry["files"], [file.name])
		self.assertEqual(download_status(token)["skipped_counts"], {"hosted_content": 1, "links": 1})
		for doc in (hosted, link):
			with self.assertRaises(frappe.DoesNotExistError):
				get_file_content(doc.name)

	def test_skipped_warning_rechecks_access_and_hides_revoked_content(self):
		from suite.writer.api.docs import create_document
		from suite.drive.api.files import download_status
		folder = self.ordinary_folder()
		self.upload(folder, "invoice.xml", b"<ordinary />")
		hosted = create_document(title="Hosted document", parent=folder.name)
		token, _ = self.archive([folder.name])
		self.assertEqual(download_status(token)["skipped_counts"]["hosted_content"], 1)
		hosted.db_set("owner", self.stranger.name)
		frappe.get_doc({"doctype": "Drive Permission", "entity": hosted.name,
			"user": self.writer.name, "read": 1, "deny": 1}).insert(ignore_permissions=True)
		frappe.clear_document_cache("File", hosted.name)
		self.assertEqual(download_status(token)["skipped_counts"], {"hosted_content": 0, "links": 0})

	def test_missing_uploaded_bytes_fail_and_remove_partial_zip(self):
		from suite.drive.utils.files import FileManager
		root = self.root()
		file = self.upload(root, "missing.xml", b"<gone />")
		FileManager().get_local_path(file.file_url).unlink()
		frappe.set_user(self.reader.name)
		token, entry = self.archive([root.name])
		self.assertEqual(entry["status"], "failed")
		self.assertNotIn("key", entry)
		self.assertFalse(Path(frappe.get_site_path("private/files/.drive-downloads", token + ".zip")).exists())

	def test_access_revocation_blocks_finished_zip_and_direct_office(self):
		from suite.drive.api.files import download_status, download_archive, get_file_content
		root = self.root()
		file = self.upload(root, "invoice.docx", samples()["docx"])
		frappe.set_user(self.reader.name)
		token, entry = self.archive([root.name])
		self.assertEqual(entry["status"], "ready")
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user(self.reader.name)
		for operation in (lambda: download_status(token), lambda: download_archive(token),
			lambda: get_file_content(file.name)):
			with self.assertRaises(frappe.PermissionError):
				operation()

	def test_revocation_before_build_produces_no_downloadable_archive(self):
		root = self.root()
		self.upload(root, "invoice.pptx", samples()["pptx"])
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user(self.reader.name)
		_, entry = self.archive([root.name])
		self.assertEqual(entry["status"], "failed")
		self.assertNotIn("key", entry)

	def test_erp_office_attachment_remains_downloadable(self):
		from frappe.utils.file_manager import save_file
		from suite.drive.api.files import get_file_content
		from suite.drive.utils.files import FileManager
		data = samples()["docx"]
		file = save_file("erp-invoice.docx", data, "Customer", self.customer.name, is_private=1)
		self.paths.append(FileManager().get_local_path(file.file_url))
		frappe.set_user(self.reader.name)
		with patch.object(frappe.local, "request", Request(EnvironBuilder().get_environ()), create=True):
			response = get_file_content(file.name)
			response.direct_passthrough = False
			self.assertEqual(response.get_data(), data)

	def test_s3_zip_preserves_uploaded_bytes_and_key(self):
		from unittest.mock import MagicMock
		from suite.drive.api.files import _write_archive
		from suite.drive.utils.files import FileManager, get_s3_url
		root = self.root()
		data = samples()["pptx"]
		file = self.upload(root, "invoice.pptx", data)
		file.db_set("file_url", get_s3_url("/protected/presentation object.pptx"))
		manager = FileManager()
		manager.s3_enabled = True
		manager.bucket = "downloads-test"
		manager.conn = MagicMock()
		manager.conn.get_object.return_value = {"Body": BytesIO(data)}
		captured = []
		def upload(path, bucket, key):
			with ZipFile(path) as archive:
				captured.append((bucket, key, archive.read("invoice.pptx")))
		manager.conn.upload_file.side_effect = upload
		frappe.set_user(self.reader.name)
		with patch("suite.drive.api.files.FileManager", return_value=manager):
			key, size = _write_archive("s3-acceptance", [("invoice.pptx", file)])
		self.assertGreater(size, len(data))
		self.assertEqual(key, ".drive-downloads/s3-acceptance.zip")
		self.assertEqual(captured, [("downloads-test", key, data)])
		manager.conn.get_object.assert_called_once_with(Bucket="downloads-test", Key="/protected/presentation object.pptx")

	def test_expired_cached_selection_gets_a_new_archive_token(self):
		from suite.drive.api.files import _download_cache_key, download_folder
		root = self.root()
		self.upload(root, "invoice.xml", b"<one />")
		frappe.set_user(self.reader.name)
		with patch("frappe.enqueue"):
			first = download_folder([root.name])
			frappe.cache().delete_value(_download_cache_key(first["token"]))
			second = download_folder([root.name])
		self.cache_keys.extend(_download_cache_key(result["token"]) for result in (first, second))
		self.assertNotEqual(first["token"], second["token"])

	def test_changed_folder_does_not_reuse_incomplete_cached_archive(self):
		from suite.drive.api.files import _download_cache_key, download_folder
		root = self.root()
		self.upload(root, "invoice.xml", b"<one />")
		frappe.set_user(self.reader.name)
		with patch("frappe.enqueue"):
			first = download_folder([root.name])
			frappe.set_user("Administrator")
			self.upload(root, "new.xml", b"<two />")
			frappe.set_user(self.reader.name)
			second = download_folder([root.name])
		self.cache_keys.extend(_download_cache_key(result["token"]) for result in (first, second))
		self.assertNotEqual(first["token"], second["token"])

	def test_archive_paths_cannot_escape_and_alias_cannot_join_zip(self):
		from suite.drive.api.files import _collect_download_files
		root = self.root()
		file = self.upload(root, "invoice.xml", b"<one />")
		file.db_set("file_name", "../../invoice.xml")  # A legacy malformed label.
		frappe.set_user(self.reader.name)
		_, entry = self.archive([file.name])
		with ZipFile(frappe.get_site_path(entry["key"])) as archive:
			self.assertEqual(len(archive.namelist()), 1)
			self.assertNotIn("/", archive.namelist()[0])
		# Simulate a legacy alias through a fixture-only database write.
		frappe.set_user(self.stranger.name)
		folder = self.ordinary_folder()
		frappe.set_user(self.stranger.name)
		alias = self.upload(folder, "alias.xml", b"alias")
		alias.db_set("file_url", file.file_url)
		with self.assertRaises(frappe.PermissionError):
			list(_collect_download_files([alias.name]))

	def test_explicitly_readable_child_survives_denied_intermediate_folder(self):
		from suite.drive.api.files import create_folder
		folder = self.ordinary_folder()
		hidden = create_folder("Hidden-folder", folder.name)
		self.upload(folder, "ordinary.xml", b"<ordinary />")
		child = self.upload(hidden, "explicit.xml", b"<explicit />")
		for name, deny in ((folder.name, 0), (hidden.name, 1), (child.name, 0)):
			frappe.get_doc({"doctype": "Drive Permission", "entity": name,
				"user": self.reader.name, "read": 1, "deny": deny}).insert(ignore_permissions=True)
		frappe.set_user(self.reader.name)
		_, entry = self.archive([folder.name, child.name])
		self.assertEqual(entry["status"], "ready")
		with ZipFile(frappe.get_site_path(entry["key"])) as archive:
			self.assertEqual(len(archive.namelist()), 2)
			self.assertEqual({archive.read(name) for name in archive.namelist()}, {b"<ordinary />", b"<explicit />"})
			self.assertFalse(any("Hidden-folder" in name for name in archive.namelist()))

	def test_file_folder_name_collision_produces_extractable_archive(self):
		from tempfile import TemporaryDirectory
		from suite.drive.api.files import create_folder
		root = self.root()
		frappe.set_user(self.writer.name)
		folder = create_folder("reports", root.name)
		self.upload(folder, "report.xml", b"<nested />")
		file = self.upload(root, "reports.xml", b"<standalone />")
		file.db_set("file_name", "reports")
		_, entry = self.archive([folder.name, file.name])
		with TemporaryDirectory() as target, ZipFile(frappe.get_site_path(entry["key"])) as archive:
			archive.extractall(target)
			self.assertEqual({path.read_bytes() for path in Path(target).rglob("*") if path.is_file()},
				{b"<nested />", b"<standalone />"})
