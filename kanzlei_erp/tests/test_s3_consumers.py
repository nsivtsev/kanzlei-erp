"""Dependency consumers must use File storage rather than retained disk copies."""

import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe


class TestS3Consumers(TestCase):
	def test_archive_uses_managed_namespace(self):
		from suite.drive.api import files

		manager = SimpleNamespace(
			s3_enabled=True,
			bucket="test",
			settings=frappe._dict(kanzlei_s3_prefix="site/ns"),
			conn=SimpleNamespace(upload_file=lambda path, bucket, key: None),
		)
		with (
			patch.object(files, "FileManager", return_value=manager),
			patch.object(
				files, "_build_zip", side_effect=lambda manager, selection, output: output.write(b"archive")
			),
		):
			key, size = files._write_archive("test-token", [])
		self.assertEqual(key, "site/ns/.drive-downloads/test-token.zip")
		self.assertEqual(size, 7)

	def test_trigger_download_resolves_attachment_binding(self):
		from suite.drive.api import files

		file = frappe._dict(
			name="source",
			file_url="/private/files/source.bin",
			file_name="source.bin",
			file_type="File",
			mime_type="application/octet-stream",
		)
		manager = SimpleNamespace(s3_enabled=True, require_storage_access=lambda file: None)
		with (
			patch.object(files, "FileManager", return_value=manager),
			patch.object(frappe, "get_hooks", return_value=["storage.resolve_key"]),
			patch.object(frappe, "call", return_value="site/attachments/object"),
			patch.object(files, "_serve_resumable", return_value="served") as serve,
		):
			self.assertEqual(files.get_file_internal(file, trigger_download=1), "served")
		self.assertEqual(serve.call_args.args[1], "site/attachments/object")

	def test_outgoing_inline_image_reads_file_bytes(self):
		from frappe.email import email_body

		file = SimpleNamespace(get_content=lambda encodings: b"remote-image")
		with (
			patch.object(frappe.db, "get_value", return_value="image-file"),
			patch.object(frappe, "get_doc", return_value=file),
		):
			self.assertEqual(
				email_body.get_filecontent_from_path("/private/files/new-image.png"), b"remote-image"
			)

	def test_exchange_import_materializes_stored_file(self):
		from suite.mail.doctype.contacts_exchange.contacts_exchange import ContactsExchange
		from suite.mail.doctype.mail_exchange.mail_exchange import MailExchange

		for doctype in (ContactsExchange, MailExchange):
			with self.subTest(doctype=doctype.__name__):
				doc = SimpleNamespace(import_file="/private/files/import.zip")
				file = SimpleNamespace(
					get_full_path=lambda: "/tmp/s3-import.zip", check_permission=lambda ptype: None
				)
				with (
					patch.object(frappe.db, "get_value", return_value="source"),
					patch.object(frappe, "get_doc", return_value=file),
				):
					self.assertEqual(doctype._resolve_import_file(doc), "/tmp/s3-import.zip")

	def test_inline_image_resolves_a_migrated_legacy_alias(self):
		from frappe.email import email_body

		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe, "get_hooks", return_value=["storage.url_content"]),
			patch.object(frappe, "call", return_value=b"migrated-image") as resolve,
		):
			self.assertEqual(
				email_body.get_filecontent_from_path("/private/files/old-inline.png"), b"migrated-image"
			)
			self.assertEqual(resolve.call_args.kwargs["url"], "/private/files/old-inline.png")

	def test_exchange_export_inserts_content_without_overwriting_storage_url(self):
		from suite.mail.doctype.contacts_exchange import contacts_exchange
		from suite.mail.doctype.mail_exchange import mail_exchange

		for module, cls in (
			(contacts_exchange, contacts_exchange.ContactsExchange),
			(mail_exchange, mail_exchange.MailExchange),
		):
			with self.subTest(doctype=cls.__name__):
				doc = SimpleNamespace(name="exchange", doctype=cls.__name__, export_archive_type=".zip")
				file = SimpleNamespace(insert=lambda: None, db_set=lambda field, value: None)
				with (
					patch.object(frappe, "new_doc", return_value=file),
					patch.object(
						module,
						"compress_directory",
						side_effect=lambda source, target: Path(target).write_bytes(b"exported"),
					),
				):
					cls._attach_export(doc, "/tmp/source")
				self.assertEqual(file.content, b"exported")
				self.assertFalse(hasattr(file, "file_url"))
