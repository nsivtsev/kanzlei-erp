"""Regression tests use real storage and ordinary users, including old bad rows."""

from pathlib import Path
import shutil
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp.tests import test_mandant_drive as fixtures


class TestDriveStorageSecurity(FrappeTestCase):
	setUp = fixtures.TestMandantDrive.setUp
	make_user = fixtures.TestMandantDrive.make_user
	root = fixtures.TestMandantDrive.root
	child = fixtures.TestMandantDrive.child

	def tearDown(self):
		frappe.set_user("Administrator")
		for path in self.paths:
			if path.is_symlink():
				path.unlink()
			elif path.is_dir():
				shutil.rmtree(path)
			elif path.is_file():
				path.unlink()
		FrappeTestCase.tearDown(self)

	def ordinary(self):
		from suite.drive.utils import get_user_folder
		folder = get_user_folder()
		file = self.child(folder, f"ordinary-{frappe.generate_hash(8)}.xml")
		from suite.drive.utils.files import FileManager
		self.paths.append(FileManager().get_local_path(file.file_url))
		return file

	def old_alias(self, target):
		frappe.set_user(self.stranger.name)
		alias = self.ordinary()
		# Simulate a row saved before the security fix, bypassing validation only here.
		alias.db_set("file_url", target.file_url)
		return alias

	def test_update_cannot_alias_protected_bytes(self):
		file = self.child(self.root())
		frappe.set_user(self.stranger.name)
		alias = self.ordinary()
		alias.file_url = file.file_url
		with self.assertRaises(frappe.PermissionError):
			alias.save()

	def test_insert_checks_before_framework_reads_bytes(self):
		file = self.child(self.root())
		frappe.set_user(self.stranger.name)
		with patch("frappe.core.doctype.file.file.File.get_content") as read:
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc({"doctype": "File", "file_name": "alias.xml", "file_url": file.file_url,
					"is_private": 1, "flags": {"file_created": True}}).insert(ignore_permissions=True)
			read.assert_not_called()

	def test_current_reader_also_cannot_create_independent_alias(self):
		file = self.child(self.root())
		frappe.set_user(self.reader.name)
		alias = self.ordinary()
		alias.file_url = file.file_url
		with self.assertRaises(frappe.PermissionError):
			alias.save()

	def test_revoked_uploader_cannot_alias_own_original(self):
		root = self.root()
		frappe.set_user(self.writer.name)
		file = self.child(root)
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.writer.name)
		frappe.set_user(self.writer.name)
		alias = self.ordinary()
		alias.file_url = file.file_url
		with self.assertRaises(frappe.PermissionError):
			alias.save()

	def test_old_alias_has_no_suite_or_framework_permissions(self):
		from suite.drive.api.permissions import user_has_permission
		alias = self.old_alias(self.child(self.root()))
		for permission in ("read", "write", "upload"):
			self.assertFalse(user_has_permission(alias, permission))
		self.assertFalse(frappe.has_permission("File", "read", alias))

	def test_old_alias_cannot_read_through_file_manager(self):
		from suite.drive.utils.files import FileManager
		alias = self.old_alias(self.child(self.root()))
		with self.assertRaises(frappe.PermissionError):
			FileManager().get_file(alias)

	def test_old_alias_cannot_delete_and_original_bytes_survive(self):
		from suite.drive.utils.files import FileManager
		file = self.child(self.root())
		path = FileManager().get_local_path(file.file_url)
		alias = self.old_alias(file)
		with self.assertRaises(frappe.PermissionError):
			FileManager().delete_file(alias)
		self.assertEqual(path.read_bytes(), b"<invoice />")

	def test_alias_normalization_and_symlink(self):
		from suite.drive.utils.files import FileManager
		file = self.child(self.root())
		frappe.set_user(self.stranger.name)
		alias = self.ordinary()
		original = FileManager().get_local_path(file.file_url)
		link = original.parent.parent / f"alias-{frappe.generate_hash(8)}.xml"
		link.symlink_to(original)
		self.paths.append(link)
		variants = [file.file_url.lstrip("/"), file.file_url.replace("/invoice.xml", "/./invoice.xml"),
			str(link.relative_to(Path(frappe.get_site_path()).resolve()))]
		for url in variants:
			with self.subTest(url=url):
				alias.reload()
				alias.file_url = url
				with self.assertRaises(frappe.PermissionError):
					alias.save()

	def test_ordinary_erp_attachment_copy_keeps_shared_url(self):
		attachment = frappe.get_doc({"doctype": "File", "file_name": "original.xml", "is_private": 1,
			"attached_to_doctype": "Customer", "attached_to_name": self.customer.name,
			"content": b"<ordinary />"}).insert()
		self.paths.append(Path(attachment.get_full_path()))
		frappe.set_user(self.writer.name)
		copy = attachment.create_attachment_copy("Customer", self.customer.name, ignore_permissions=True)
		self.assertEqual(copy.file_url, attachment.file_url)
		self.assertEqual(copy.get_content(), "<ordinary />")

	def test_protected_copy_is_rejected_even_for_reader(self):
		file = self.child(self.root())
		frappe.set_user(self.writer.name)
		with self.assertRaises(frappe.PermissionError):
			file.create_attachment_copy("Customer", self.customer.name, ignore_permissions=True)

	def test_framework_content_dedup_cannot_alias_managed_original(self):
		from hashlib import md5
		file = self.child(self.root())
		file.db_set("content_hash", md5(b"<invoice />").hexdigest())
		frappe.set_user(self.writer.name)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": "File", "file_name": "duplicate.xml", "is_private": 1,
				"content": b"<invoice />"}).insert(ignore_permissions=True)

	def test_managed_rename_and_trash_restore_preserve_access(self):
		from suite.drive.api.files import remove_or_restore
		from suite.drive.utils.files import FileManager
		root = self.root()
		frappe.set_user(self.writer.name)
		file = self.child(root)
		file.rename("renamed.xml")
		remove_or_restore([file.name])
		remove_or_restore([file.name])
		file.reload()
		self.assertEqual(FileManager().get_file(file).read(), b"<invoice />")

	def test_thumbnail_cannot_be_aliased(self):
		from suite.drive.utils.files import FileManager
		file = self.child(self.root())
		url = str(FileManager().get_thumbnail_path(file.name))
		frappe.set_user(self.stranger.name)
		alias = self.ordinary()
		alias.file_url = url
		with self.assertRaises(frappe.PermissionError):
			alias.save()

	def test_old_alias_move_checks_source_before_destination(self):
		from suite.drive.utils.files import FileManager
		file = self.child(self.root())
		manager = FileManager()
		path = manager.get_local_path(file.file_url)
		alias = self.old_alias(file)
		target = str(manager.get_disk_path(alias))
		with self.assertRaises(frappe.PermissionError):
			manager.move(alias, target)
		self.assertEqual(path.read_bytes(), b"<invoice />")

	def test_cookieless_preview_token_uses_issuer_for_only_bound_file(self):
		from suite.drive.api.files import create_auth_token, get_file_content
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request
		file = self.child(self.root())
		frappe.set_user(self.reader.name)
		token = create_auth_token(file.name)
		frappe.set_user("Guest")
		with patch.object(frappe.local, "request", Request(EnvironBuilder().get_environ()), create=True):
			response = get_file_content(file.name, token=token)
			response.direct_passthrough = False
			self.assertEqual(response.get_data(), b"<invoice />")

	def test_insert_with_spoofed_original_id_cannot_read_bytes(self):
		file = self.child(self.root())
		frappe.set_user(self.reader.name)
		with patch("frappe.core.doctype.file.file.File.get_content") as read:
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc({"doctype": "File", "name": file.name, "file_name": "alias.xml",
					"file_url": file.file_url, "is_private": 1}).insert(ignore_permissions=True)
			read.assert_not_called()

	def test_batch_permissions_and_orm_hide_old_alias(self):
		from suite.drive.webdav.perms import resolve_children_access
		alias = self.old_alias(self.child(self.root()))
		self.assertFalse(resolve_children_access([], [alias], self.stranger.name)[alias.name]["read"])
		self.assertNotIn(alias.name, frappe.get_list("File", pluck="name", limit=0))

	def test_restore_with_occupied_name_retains_both_blobs(self):
		from suite.drive.api.files import remove_or_restore
		from suite.drive.utils.files import FileManager
		root = self.root()
		frappe.set_user(self.writer.name)
		file = self.child(root)
		remove_or_restore([file.name])
		other = self.child(root)
		remove_or_restore([file.name])
		file.reload()
		self.assertNotEqual(file.file_url, other.file_url)
		for doc in (file, other):
			self.assertEqual(FileManager().get_file(doc).read(), b"<invoice />")

	def test_canonical_alias_of_foreign_ordinary_attachment_is_rejected(self):
		file = frappe.get_doc({"doctype": "File", "file_name": "ordinary-private.xml", "is_private": 1,
			"attached_to_doctype": "Customer", "attached_to_name": self.customer.name,
			"content": b"<private />"}).insert()
		self.paths.append(Path(file.get_full_path()))
		frappe.set_user(self.stranger.name)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": "File", "file_name": "alias.xml", "is_private": 1,
				"file_url": file.file_url.replace("/files/", "/files/./"),
				"flags": {"file_created": True}}).insert(ignore_permissions=True)

	def test_ignore_validate_cannot_bypass_url_guard(self):
		file = self.child(self.root())
		frappe.set_user(self.stranger.name)
		alias = self.ordinary()
		alias.file_url = file.file_url
		alias.flags.ignore_validate = True
		with self.assertRaises(frappe.PermissionError):
			alias.save()

	def test_s3_wrapper_preserves_leading_and_trailing_slashes(self):
		from kanzlei_erp.drive_storage import identity
		from suite.drive.utils.files import get_s3_url
		self.assertEqual(identity(get_s3_url("/bucket/file")), ("s3", "/bucket/file"))
		self.assertNotEqual(identity(get_s3_url("/bucket/file/")), identity(get_s3_url("/bucket/file")))

	def test_copied_subfolder_inherits_mandant_and_has_distinct_storage(self):
		from suite.drive.api.files import create_folder
		from suite.drive.webdav.copy import _Copier
		from suite.drive.utils.files import FileManager
		root = self.root()
		frappe.set_user(self.writer.name)
		source = create_folder("Source", root.name)
		_Copier(FileManager(), self.writer.name, recurse=False).copy(source, root, "Copy")
		target = frappe.get_doc("File", {"folder": root.name, "file_name": "Copy"})
		self.assertEqual(target.kanzlei_drive_customer, self.customer.name)
		self.assertNotEqual(target.file_url, source.file_url)

	def test_storage_guard_never_bootstraps_drive_root(self):
		# CI starts without the lazy shared Drive root. Permission checks must be reads.
		with patch("suite.drive.utils.files.get_root_folder", side_effect=AssertionError("Root initialization during guard")):
			self.root()

	def test_direct_path_guard_is_read_only_with_managed_files(self):
		from kanzlei_erp.drive_storage import require_path_access
		file = self.child(self.root())
		with patch("suite.drive.utils.files.get_root_folder", side_effect=AssertionError("Root initialization during guard")):
			require_path_access(file.file_url)

	def test_s3_alias_is_denied_before_any_backend_io(self):
		from unittest.mock import MagicMock
		from suite.drive.utils.files import FileManager, get_s3_url
		from kanzlei_erp.drive_storage import invalidate
		file = self.child(self.root())
		alias = self.old_alias(file)
		file.db_set("file_url", get_s3_url("/protected/invoice.xml"))
		alias.db_set("file_url", file.file_url)
		manager = FileManager()
		manager.s3_enabled = True
		manager.conn = MagicMock()
		invalidate()
		for operation in (lambda: manager.get_file(alias), lambda: manager.upload_file("/unused", alias, False),
			lambda: manager.delete_file(alias), lambda: manager.move(alias, "unrelated/target.xml")):
			with self.assertRaises(frappe.PermissionError):
				operation()
		self.assertEqual(manager.conn.mock_calls, [])

	def test_s3_upload_uses_decoded_object_key(self):
		from unittest.mock import MagicMock
		from suite.drive.utils.files import FileManager, get_s3_url
		file = self.child(self.root())
		file.db_set("file_url", get_s3_url("/protected/object key.xml"))
		manager = FileManager()
		manager.s3_enabled = True
		manager.bucket = "test-bucket"
		manager.conn = MagicMock()
		frappe.set_user(self.writer.name)
		with patch("suite.drive.utils.files.os.remove"):
			manager.upload_file("/staged.xml", file, False)
		manager.conn.upload_file.assert_called_once_with("/staged.xml", "test-bucket", "/protected/object key.xml")
