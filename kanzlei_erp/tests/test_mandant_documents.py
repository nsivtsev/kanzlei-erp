from io import BytesIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

import frappe
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp.mandant_documents import (
	_file_group,
	_external_url,
	_normalize_filters,
	_safe_archive_names,
	download_document,
	download_documents_zip,
	get_documents,
)


class TestMandantDocumentHelpers(TestCase):
	def test_file_group_uses_known_extensions_without_case_sensitivity(self):
		self.assertEqual(_file_group("report.PDF"), "pdf")
		self.assertEqual(_file_group("bank.CSV"), "spreadsheet")
		self.assertEqual(_file_group("original.XML"), "xml")
		self.assertEqual(_file_group("signature.svg"), "image")
		self.assertEqual(_file_group("readme"), "other")

	def test_filters_reject_unknown_values_and_inconsistent_mail_direction(self):
		defaults = _normalize_filters(None)
		self.assertEqual(defaults["source"], "all")
		self.assertEqual(defaults["direction"], "all")
		self.assertEqual(defaults["sort"], "newest")

		with self.assertRaises(frappe.ValidationError):
			_normalize_filters({"doctype": "File"})
		with self.assertRaises(frappe.ValidationError):
			_normalize_filters({"source": "unknown"})
		with self.assertRaises(frappe.ValidationError):
			_normalize_filters({"source": "task", "direction": "Received"})

	def test_archive_names_cannot_escape_and_collisions_get_stable_suffixes(self):
		self.assertEqual(
			_safe_archive_names(["../Rechnung.pdf", "Rechnung.pdf", "rechnUNG.PDF"]),
			["Rechnung.pdf", "Rechnung (2).pdf", "rechnUNG (3).PDF"],
		)

	def test_bound_s3_http_url_is_stored_content(self):
		file = frappe._dict(file_url="https://erp.example.invalid/private/files/report.pdf")
		with patch("kanzlei_erp.object_storage.get_binding", return_value=object()):
			self.assertIsNone(_external_url(file))
		with patch("kanzlei_erp.object_storage.get_binding", return_value=None):
			self.assertEqual(_external_url(file), file.file_url)


class TestMandantDocuments(FrappeTestCase):
	def setUp(self):
		self.customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Documents {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()
		self.other_customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Other Documents {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()
		self.service = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"_Documents Service {frappe.generate_hash(length=8)}",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		self.file_names = set()
		self.communication_names = set()

	def tearDown(self):
		frappe.set_user("Administrator")
		for file_name in self.file_names:
			file = frappe.get_doc("File", file_name) if frappe.db.exists("File", file_name) else None
			if file and not file.is_remote_file:
				Path(file.get_full_path()).unlink(missing_ok=True)
		super().tearDown()

	def make_drive_root(self, customer=None):
		customer = customer or self.customer
		root = frappe.get_doc({"doctype": "File", "name": frappe.generate_hash(length=10),
			"file_name": "Mandant documents", "file_type": "Folder", "is_folder": 1,
			"is_private": 1, "status": "Active", "kanzlei_drive_customer": customer.name})
		root.db_insert()
		customer.db_set("kanzlei_drive_folder", root.name)
		return root

	def make_drive_child(self, parent, name, content=b"drive document"):
		if content is None:
			folder = frappe.get_doc({"doctype": "File", "name": frappe.generate_hash(length=10),
				"file_name": name, "file_type": "Folder", "is_folder": 1, "is_private": 1,
				"status": "Active", "folder": parent.name,
				"kanzlei_drive_customer": parent.kanzlei_drive_customer})
			folder.db_insert()
			return folder
		file = self.make_file(name, None, None, None if content == b"drive document" else content)
		frappe.db.set_value("File", file.name, {"folder": parent.name, "status": "Active",
			"kanzlei_drive_customer": parent.kanzlei_drive_customer})
		return file

	def make_file(self, file_name, doctype, docname, content=None):
		from frappe.utils.file_manager import save_file
		fixture_suffix = frappe.generate_hash(length=8)
		if content is None and file_name.casefold().endswith(".pdf"):
			from pypdf import PdfWriter

			pdf = BytesIO()
			writer = PdfWriter()
			writer.add_metadata({"/Title": fixture_suffix})
			writer.write(pdf)
			# Keep the fixture ASCII so Frappe's text decoding preserves PDF byte offsets.
			content = pdf.getvalue().replace(b"%\xe2\xe3\xcf\xd3", b"%test", 1)
		elif content is None:
			content = f"test document {fixture_suffix}".encode()

		file = save_file(file_name, content, doctype, docname, is_private=1)
		self.file_names.add(file.name)
		return file

	def make_communication(self, subject, direction, customer, link_method="timeline", reference_doctype="Customer"):
		values = {
			"doctype": "Communication",
			"communication_type": "Communication",
			"communication_medium": "Email",
			"subject": subject,
			"content": "test body",
			"sender": f"{frappe.generate_hash(length=8)}@example.invalid",
			"recipients": "team@example.invalid",
			"sent_or_received": direction,
			"communication_date": "2026-09-15 09:30:00",
			"email_status": "Open",
		}
		if reference_doctype:
			values.update(reference_doctype=reference_doctype, reference_name=customer)
		if link_method == "timeline":
			values["timeline_links"] = [{"link_doctype": reference_doctype or "Customer", "link_name": customer}]
		communication = frappe.get_doc(values).insert(ignore_permissions=True)
		self.communication_names.add(communication.name)
		return communication

	def make_package(self):
		# Direct fixture insertion avoids running customer-installed package workflows.
		name = f"_documents-{frappe.generate_hash(length=8)}"
		package = frappe.get_doc(
			{
				"doctype": "FiBu Package",
				"name": name,
				"customer": self.customer.name,
				"service": self.service.name,
				"period_type": "Monthly",
				"period_year": 2026,
				"period_number": 9,
				"period_start": "2026-09-01",
				"period_end": "2026-09-30",
				"display_title": f"{self.customer.name} — September 2026",
				"status": "Open",
				"responsible": "Administrator",
			}
		)
		package.db_insert()
		return package

	def add_child(self, parent, parentfield, values):
		child = frappe.get_doc(
			{
				"doctype": parent.meta.get_field(parentfield).options,
				"parent": parent.name,
				"parenttype": parent.doctype,
				"parentfield": parentfield,
				"idx": frappe.db.count(parent.meta.get_field(parentfield).options, {"parent": parent.name}) + 1,
				**values,
			}
		)
		child.db_insert()
		return child

	def test_list_combines_customer_mail_fibu_and_task_files_without_duplicates(self):
		package = self.make_package()
		mandant_file = self.make_file("mandant.pdf", "Customer", self.customer.name)
		profile_image = self.make_file("profile.png", "Customer", self.customer.name)
		frappe.db.set_value("File", profile_image.name, "attached_to_field", "image")
		received = self.make_communication("Received invoice", "Received", self.customer.name)
		frappe.db.set_value("Communication", received.name, "bcc", "hidden@example.invalid")
		received_file = self.make_file("received.xml", "Communication", received.name, b"<invoice />")
		reference_only = self.make_communication(
			"Reference only", "Received", self.customer.name, link_method="none"
		)
		reference_only_file = self.make_file("reference-only.txt", "Communication", reference_only.name)
		timeline_only = self.make_communication(
			"Timeline only", "Received", self.customer.name, reference_doctype=None
		)
		timeline_only_file = self.make_file("timeline-only.txt", "Communication", timeline_only.name)
		frappe.db.set_value("Communication", timeline_only.name, "email_status", "Linked")
		sent = self.make_communication("Sent answer", "Sent", self.customer.name, reference_doctype=None)
		sent_file = self.make_file("sent.pdf", "Communication", sent.name)
		task = frappe.get_doc({"doctype": "Task", "subject": "Review evidence", "kanzlei_customer": self.customer.name}).insert()
		task_file = self.make_file("task.txt", "Task", task.name)
		fibu_task = frappe.get_doc(
			{
				"doctype": "Task",
				"subject": "Review period evidence",
				"kanzlei_customer": self.customer.name,
				"kanzlei_fibu_package": package.name,
			}
		).insert()
		fibu_task_file = self.make_file("period-task.txt", "Task", fibu_task.name)
		other_task = frappe.get_doc(
			{"doctype": "Task", "subject": "Other Mandant task", "kanzlei_customer": self.other_customer.name}
		).insert()
		other_file = self.make_file("other.pdf", "Task", other_task.name)
		package_file = self.make_file("package.pdf", "FiBu Package", package.name)
		source_only_mail = self.make_communication(
			"Transfer source only", "Received", "", link_method="none", reference_doctype=None
		)
		explicitly_linked_file = self.make_file("curated-evidence.txt", "FiBu Package", package.name)
		unlinked_mail_attachment = self.make_file("must-not-expand.txt", "Communication", source_only_mail.name)
		unlinked_evidence = self.make_file("evidence.txt", None, None)
		evidence_mail = self.make_communication(
			"Transfer receipt", "Received", "", link_method="none", reference_doctype=None
		)
		evidence_mail_file = self.make_file("transfer-mail.txt", "Communication", evidence_mail.name)
		self.add_child(package, "files", {"file": received_file.name, "source_communication": received.name})
		self.add_child(package, "files", {"file": package_file.name})
		self.add_child(
			package,
			"files",
			{"file": explicitly_linked_file.name, "source_communication": source_only_mail.name},
		)
		self.add_child(package, "messages", {"communication": sent.name})
		self.add_child(
			package,
			"transfers",
			{
				"event_date": "2026-09-30",
				"note": "Transfer evidence",
				"evidence_file": unlinked_evidence.name,
				"evidence_communication": evidence_mail.name,
			},
		)
		supplement = frappe.get_doc(
			{
				"doctype": "FiBu Supplement",
				"name": f"_supplement-{frappe.generate_hash(length=8)}",
				"package": package.name,
				"reason": "Late evidence",
				"display_title": "Late evidence",
				"responsible": "Administrator",
				"status": "Open",
			}
		)
		supplement.db_insert()
		supplement_file = self.make_file("supplement.pdf", "FiBu Supplement", supplement.name)
		self.add_child(supplement, "files", {"file": supplement_file.name})
		task_email = self.make_communication("Task background", "Received", task.name, reference_doctype="Task")
		task_email_file = self.make_file("task-email.pdf", "Communication", task_email.name)

		result = get_documents(self.customer.name, page_length=100)
		rows = {item["file_id"]: item for item in result["items"]}
		self.assertEqual(
			set(rows),
			{
				mandant_file.name,
				received_file.name,
				reference_only_file.name,
				timeline_only_file.name,
				sent_file.name,
				task_file.name,
				fibu_task_file.name,
				package_file.name,
				explicitly_linked_file.name,
				supplement_file.name,
				task_email_file.name,
				unlinked_evidence.name,
				evidence_mail_file.name,
			},
		)
		self.assertEqual(rows[received_file.name]["contexts"][0]["source"], "email")
		self.assertEqual(rows[received_file.name]["contexts"][0]["direction"], "received")
		self.assertEqual(rows[received_file.name]["document_date"], "2026-09-15 09:30:00")
		self.assertEqual(len(rows[received_file.name]["contexts"]), 2)
		self.assertNotIn(profile_image.name, rows)
		self.assertNotIn("hidden@example.invalid", str(result))
		self.assertEqual(get_documents(self.customer.name, filters={"query": "hidden@example.invalid"})["items"], [])
		self.assertEqual({context["source"] for context in rows[evidence_mail_file.name]["contexts"]}, {"email", "fibu"})
		self.assertEqual({context["source"] for context in rows[explicitly_linked_file.name]["contexts"]}, {"email", "fibu"})
		self.assertTrue(any(context["source"] == "fibu" for context in rows[fibu_task_file.name]["contexts"]))
		self.assertNotIn(unlinked_mail_attachment.name, rows)
		self.assertNotIn(other_file.name, rows)
		self.assertNotIn("body", str(result))
		self.assertNotIn("bcc", result["items"][0])

	def test_download_preserves_bytes_and_zip_has_unique_flat_names(self):
		original = b"\x00<xml encoding='legacy' />\xff" + frappe.generate_hash(length=8).encode()
		first = self.make_file("statement.xml", "Customer", self.customer.name, original)
		second = self.make_file("STATEMENT.XML", "Customer", self.customer.name, b"second")
		frappe.db.set_value("File", first.name, "file_name", "statement.xml")
		frappe.db.set_value("File", second.name, "file_name", "STATEMENT.XML")
		from kanzlei_erp.object_storage import client, get_binding, settings

		if binding := get_binding(first.file_url):
			client().put_object(Bucket=settings().bucket, Key=binding.object_key, Body=original)
		else:
			Path(first.get_full_path()).write_bytes(original)
		download_document(self.customer.name, first.name, "inline")
		self.assertEqual(frappe.local.response.filecontent, original)
		self.assertEqual(frappe.local.response.content_type, "application/xml")
		self.assertEqual(frappe.local.response.display_content_as, "attachment")

		download_documents_zip(self.customer.name, [first.name, first.name, second.name])
		with ZipFile(BytesIO(frappe.local.response.filecontent)) as archive:
			self.assertEqual(archive.namelist(), ["statement.xml", "STATEMENT (2).XML"])
			self.assertEqual(archive.read(archive.namelist()[0]), original)
			self.assertEqual(archive.read(archive.namelist()[1]), b"second")

	def test_file_membership_is_rechecked_before_download_and_zip(self):
		file = self.make_file("current.txt", "Customer", self.customer.name)
		self.assertEqual(
			[row["file_id"] for row in get_documents(self.customer.name)["items"]],
			[file.name],
		)
		frappe.db.set_value("File", file.name, "attached_to_name", self.other_customer.name)
		with self.assertRaises(frappe.PermissionError):
			download_document(self.customer.name, file.name)
		with self.assertRaises(frappe.PermissionError):
			download_documents_zip(self.customer.name, [file.name])

	def test_zip_rejects_file_count_and_actual_size_limits(self):
		first = self.make_file("first.txt", "Customer", self.customer.name, f"first {frappe.generate_hash()}".encode())
		second = self.make_file("second.txt", "Customer", self.customer.name, f"second {frappe.generate_hash()}".encode())
		with patch("kanzlei_erp.mandant_documents.DOCUMENT_MAX_ZIP_FILES", 1):
			with self.assertRaises(frappe.ValidationError):
				download_documents_zip(self.customer.name, [first.name, second.name])
		with patch("kanzlei_erp.mandant_documents.DOCUMENT_MAX_ZIP_BYTES", 5):
			with self.assertRaises(frappe.ValidationError):
				download_documents_zip(self.customer.name, [first.name, second.name])

	def test_email_attachment_visibility_uses_full_communication_permissions(self):
		direct_file = self.make_file("client-note.txt", "Customer", self.customer.name)
		communication = self.make_communication("Private source", "Received", self.customer.name)
		mail_file = self.make_file("mail-attachment.txt", "Communication", communication.name)
		users = []
		for roles in (["Projects User"], ["Projects User", "Inbox User"]):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": f"document-reader-{frappe.generate_hash(length=8)}@example.invalid",
					"first_name": "Document Reader",
					"send_welcome_email": 0,
					"roles": [{"role": role} for role in roles],
				}
			).insert(ignore_permissions=True)
			users.append(user)
			frappe.share.add("Customer", self.customer.name, user.name, read=1)
		try:
			frappe.set_user(users[0].name)
			without_mail_access = get_documents(self.customer.name)
			self.assertEqual({row["file_id"] for row in without_mail_access["items"]}, {direct_file.name})
			frappe.set_user(users[1].name)
			with_mail_access = get_documents(self.customer.name)
			self.assertEqual({row["file_id"] for row in with_mail_access["items"]}, {direct_file.name, mail_file.name})
		finally:
			frappe.set_user("Administrator")

	def test_search_direction_source_date_and_package_filters(self):
		received = self.make_communication("Bank statement for September", "Received", self.customer.name)
		received_file = self.make_file("bank.csv", "Communication", received.name)
		sent = self.make_communication("Sent reminder", "Sent", self.customer.name)
		sent_file = self.make_file("reminder.pdf", "Communication", sent.name)
		package = self.make_package()
		package_file = self.make_file("period.pdf", "FiBu Package", package.name)
		self.add_child(package, "files", {"file": package_file.name})

		incoming = get_documents(
			self.customer.name,
			filters={"source": "email", "direction": "received", "query": "BANK STATEMENT"},
		)
		self.assertEqual([row["file_id"] for row in incoming["items"]], [received_file.name])
		period = get_documents(self.customer.name, filters={"source": "fibu", "package": package.name})
		self.assertEqual([row["file_id"] for row in period["items"]], [package_file.name])
		self.assertNotIn(sent_file.name, {row["file_id"] for row in incoming["items"]})

	def test_listing_uses_a_stable_keyset_and_keeps_distinct_same_named_files(self):
		first = self.make_file("same.txt", "Customer", self.customer.name, f"first {frappe.generate_hash()}".encode())
		second = self.make_file("same.txt", "Customer", self.customer.name, f"second {frappe.generate_hash()}".encode())
		first_page = get_documents(self.customer.name, page_length=1)
		second_page = get_documents(self.customer.name, cursor=first_page["next_cursor"], page_length=1)
		self.assertEqual(len(first_page["items"]), 1)
		self.assertEqual(len(second_page["items"]), 1)
		self.assertNotEqual(first_page["items"][0]["file_id"], second_page["items"][0]["file_id"])
		self.assertEqual({first.name, second.name}, {first_page["items"][0]["file_id"], second_page["items"][0]["file_id"]})
		self.assertFalse(second_page["has_more"])

	def test_unreadable_mandant_is_rejected_before_any_document_metadata_is_returned(self):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"document-reader-{frappe.generate_hash(length=8)}@example.invalid",
				"first_name": "Document Reader",
				"send_welcome_email": 0,
				"roles": [{"role": "Projects User"}],
			}
		).insert(ignore_permissions=True)
		try:
			frappe.set_user(user.name)
			with self.assertRaises(frappe.PermissionError):
				get_documents(self.customer.name)
		finally:
			frappe.set_user("Administrator")

	def test_drive_documents_join_erp_once_with_folder_context_and_filters(self):
		root = self.make_drive_root()
		folder = self.make_drive_child(root, "Belege", None)
		drive = self.make_drive_child(folder, "statement.pdf")
		other_root = self.make_drive_root(self.other_customer)
		other = self.make_drive_child(other_root, "other.pdf")
		erp = self.make_file("erp.txt", "Customer", self.customer.name)
		frappe.db.set_value("File", drive.name, {"attached_to_doctype": "Customer", "attached_to_name": self.customer.name})

		first = get_documents(self.customer.name, page_length=1)
		second = get_documents(self.customer.name, cursor=first["next_cursor"], page_length=1)
		rows = {row["file_id"]: row for row in first["items"] + second["items"]}
		self.assertEqual(set(rows), {drive.name, erp.name})
		self.assertNotIn(other.name, rows)
		self.assertEqual({ctx["source"] for ctx in rows[drive.name]["contexts"]}, {"drive", "mandant"})
		self.assertEqual(next(ctx["url"] for ctx in rows[drive.name]["contexts"] if ctx["source"] == "drive"),
			f"/drive/d/{folder.name}")
		self.assertEqual([row["file_id"] for row in get_documents(self.customer.name,
			filters={"source": "drive", "file_group": "pdf", "query": "statement"})["items"]], [drive.name])
		package = self.make_package()
		self.assertEqual(get_documents(self.customer.name, filters={"package": package.name})["items"], [])

	def test_drive_trash_and_corrupt_root_never_expose_documents(self):
		root = self.make_drive_root()
		folder = self.make_drive_child(root, "Belege", None)
		file = self.make_drive_child(folder, "invoice.txt")
		erp = self.make_file("erp.txt", "Customer", self.customer.name)
		self.assertIn(file.name, {row["file_id"] for row in get_documents(self.customer.name)["items"]})
		frappe.db.set_value("File", folder.name, "status", "Trashed")
		self.assertEqual([row["file_id"] for row in get_documents(self.customer.name)["items"]], [erp.name])
		with self.assertRaises(frappe.PermissionError):
			download_document(self.customer.name, file.name)
		with self.assertRaises(frappe.PermissionError):
			download_documents_zip(self.customer.name, [file.name])
		frappe.db.set_value("File", folder.name, "status", "Active")
		self.assertIn(file.name, {row["file_id"] for row in get_documents(self.customer.name)["items"]})
		frappe.db.set_value("File", file.name, "status", "Trashed")
		self.assertEqual([row["file_id"] for row in get_documents(self.customer.name)["items"]], [erp.name])
		with self.assertRaises(frappe.PermissionError):
			download_document(self.customer.name, file.name)
		frappe.db.set_value("File", file.name, "status", "Active")
		frappe.db.set_value("Customer", self.customer.name, "kanzlei_drive_folder", "missing-root")
		self.assertEqual([row["file_id"] for row in get_documents(self.customer.name)["items"]], [erp.name])
		with self.assertRaises(frappe.PermissionError):
			download_document(self.customer.name, file.name)

	def test_drive_file_and_erp_file_download_in_same_zip(self):
		root = self.make_drive_root()
		drive = self.make_drive_child(root, "invoice.txt", b"drive bytes")
		erp = self.make_file("invoice.txt", "Customer", self.customer.name, b"erp bytes")
		link = frappe.get_doc({"doctype": "File", "name": frappe.generate_hash(length=10),
			"file_name": "Reference", "file_type": "Link", "file_url": "https://example.invalid/document",
			"status": "Active", "is_private": 1, "folder": root.name,
			"kanzlei_drive_customer": self.customer.name})
		link.db_insert()
		rows = {row["file_id"]: row for row in get_documents(self.customer.name)["items"]}
		self.assertEqual(set(rows), {drive.name, erp.name, link.name})
		self.assertEqual(rows[link.name]["external_url"], "https://example.invalid/document")
		self.assertFalse(rows[link.name]["can_select_for_zip"])
		download_document(self.customer.name, drive.name)
		self.assertEqual(frappe.local.response.filecontent, b"drive bytes")
		download_documents_zip(self.customer.name, [drive.name, erp.name])
		with ZipFile(BytesIO(frappe.local.response.filecontent)) as archive:
			self.assertEqual(sorted(archive.read(name) for name in archive.namelist()), [b"drive bytes", b"erp bytes"])
		with self.assertRaises(frappe.PermissionError):
			download_document(self.customer.name, link.name)

	def test_drive_reader_revocation_and_nonstaff_access(self):
		root = self.make_drive_root()
		drive = self.make_drive_child(root, "private.txt", b"private drive bytes")
		users = []
		for label in ("reader", "stranger", "portal"):
			user = frappe.get_doc({"doctype": "User",
				"email": f"drive-doc-{label}-{frappe.generate_hash(length=8)}@example.invalid",
				"first_name": label, "send_welcome_email": 0,
				"roles": [{"role": "Projects User"}]}).insert(ignore_permissions=True)
			users.append(user)
		frappe.share.add("Customer", self.customer.name, users[0].name, read=1)
		frappe.share.add("Customer", self.customer.name, users[2].name, read=1)
		frappe.db.set_value("User", users[2].name, "user_type", "Website User")
		try:
			frappe.set_user(users[0].name)
			self.assertEqual([row["file_id"] for row in get_documents(self.customer.name)["items"]], [drive.name])
			download_document(self.customer.name, drive.name)
			self.assertEqual(frappe.local.response.filecontent, b"private drive bytes")
			download_documents_zip(self.customer.name, [drive.name])
			frappe.set_user("Administrator")
			frappe.share.remove("Customer", self.customer.name, users[0].name)
			for user in (users[0].name, users[1].name, users[2].name, "Guest"):
				frappe.set_user(user)
				with self.assertRaises(frappe.PermissionError):
					get_documents(self.customer.name)
				with self.assertRaises(frappe.PermissionError):
					download_document(self.customer.name, drive.name)
				with self.assertRaises(frappe.PermissionError):
					download_documents_zip(self.customer.name, [drive.name])
		finally:
			frappe.set_user("Administrator")

	def test_drive_s3_url_is_downloadable_content_not_external_link(self):
		from suite.drive.utils.files import get_s3_url

		root = self.make_drive_root()
		drive = self.make_drive_child(root, "s3-report.txt", b"S3 report")
		frappe.db.set_value("File", drive.name, "file_url", get_s3_url("local/test/s3-report"))
		row = get_documents(self.customer.name)["items"][0]
		self.assertEqual(row["file_id"], drive.name)
		self.assertEqual(row["storage_kind"], "local")
		self.assertTrue(row["can_select_for_zip"])
		with patch("suite.drive.utils.files.FileManager.get_file", return_value=BytesIO(b"S3 report")):
			download_document(self.customer.name, drive.name)
		self.assertEqual(frappe.local.response.filecontent, b"S3 report")
