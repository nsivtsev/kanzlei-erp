"""Storage invariants, exercised on a disposable Frappe site."""

import frappe
from frappe.tests.utils import FrappeTestCase


class TestPosteingangModel(FrappeTestCase):
	def test_model_is_installed(self):
		self.assertTrue(frappe.db.exists("DocType", "Incoming Letter"))
		self.assertTrue(frappe.get_meta("Incoming Letter Action").istable)
		self.assertTrue(frappe.get_meta("Posteingang Settings").issingle)

	def test_plain_crud_cannot_create_completed_letter(self):
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": "Incoming Letter", "status": "Completed"}).insert(
				ignore_permissions=True
			)

	def test_manual_action_retains_null_source_index(self):
		action = frappe.get_doc(
			{"doctype": "Incoming Letter Action", "title": "Manual", "description": "Synthetic"}
		)
		self.assertIsNone(action.get_valid_dict()["source_index"])


from unittest.mock import patch

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangStorage(PosteingangTestCase):
	def test_manual_letter_has_no_run_or_tasks(self):
		doc = self.letter()
		self.assertEqual(doc.status, "Received")
		self.assertFalse(doc.current_run)
		self.assertEqual(doc.actions, [])
		self.assertFalse(doc.received_date)
		self.assertEqual(frappe.db.count("Incoming Letter Event", {"letter": doc.name}), 1)
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": doc.name}), 0)

	def test_crud_cannot_change_accepted_original_or_confirmation(self):
		doc = self.letter()
		for field, value in (
			("content_sha256", "f" * 64),
			("confirmed_customer", self.customer.name),
			("status", "Completed"),
			("summary", "Untracked edit"),
			("revision", 2),
		):
			with self.subTest(field=field):
				doc.reload()
				doc.set(field, value)
				with self.assertRaises(frappe.PermissionError):
					doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Incoming Letter", doc.name, "revision"), 1)

	def test_event_cannot_be_updated_or_deleted(self):
		doc = self.letter()
		event = frappe.get_doc("Incoming Letter Event", {"letter": doc.name})
		event.payload_json = '{"changed":true}'
		with self.assertRaises(frappe.PermissionError):
			event.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Incoming Letter Event", event.name, ignore_permissions=True)

	def test_event_failure_rolls_back_letter_and_file(self):
		from kanzlei_erp.posteingang_model import ProtectedRecord

		original_validate = ProtectedRecord.validate
		before = {d: frappe.db.count(d) for d in ("Incoming Letter", "File", "Incoming Letter Event")}

		def fail_event(doc):
			if doc.doctype == "Incoming Letter Event":
				raise RuntimeError("Synthetic event failure")
			return original_validate(doc)

		with patch.object(ProtectedRecord, "validate", fail_event):
			with self.assertRaisesRegex(RuntimeError, "Synthetic event failure"):
				self.letter()
		for doctype, count in before.items():
			self.assertEqual(frappe.db.count(doctype), count)

	def test_private_snapshots_are_not_generic_rest_fields(self):
		doc = self.letter()
		frappe.db.set_value(
			"Incoming Letter", doc.name, "matching_result_json", '{"secret":"unreadable-customer"}'
		)
		doc.reload()
		self.assertIsNone(doc.as_dict()["matching_result_json"])
		frappe.set_user(self.operator.name)
		rows = frappe.get_list("Incoming Letter", fields=["name", "matching_result_json"])
		self.assertNotIn("unreadable-customer", frappe.as_json(rows))

	def test_lowered_settings_limits_are_enforced(self):
		from kanzlei_erp.posteingang_model import create_received_letter

		frappe.set_user(self.operator.name)
		for settings, original in (
			({"max_pages": 7}, {"page_count": 8, "content": b"synthetic"}),
			({"max_file_bytes": 100}, {"page_count": 1, "content": b"x" * 101}),
		):
			with self.subTest(settings=settings):
				frappe.db.set_single_value("Posteingang Settings", settings)
				with self.assertRaises(frappe.ValidationError):
					create_received_letter(
						actor=self.operator.name,
						original={"file_name": "limit.pdf", **original},
						receipt={"received_date_known": False, "received_date": None},
						operation={
							"key": frappe.generate_hash(length=64),
							"fingerprint": frappe.generate_hash(length=64),
						},
					)

	def test_failed_event_removes_disk_bytes_immediately(self):
		from pathlib import Path

		from kanzlei_erp.posteingang_model import ProtectedRecord

		root = Path(frappe.get_site_path("private", "files"))
		before = set(root.rglob("*.pdf"))
		validate = ProtectedRecord.validate

		def fail(doc):
			if doc.doctype == "Incoming Letter Event":
				raise RuntimeError("Synthetic failure")
			return validate(doc)

		with patch.object(ProtectedRecord, "validate", fail):
			with self.assertRaises(RuntimeError):
				self.letter()
		self.assertEqual(set(root.rglob("*.pdf")), before)

	def test_failed_same_bytes_creation_cleans_only_new_physical_file(self):
		from pathlib import Path

		from kanzlei_erp.posteingang_model import ProtectedRecord, create_received_letter
		first = self.letter()
		file = frappe.get_doc("File", first.original_file)
		content = file.get_content(encodings=[])
		root = Path(frappe.get_site_path("private", "files"))
		before = set(root.rglob("*.pdf"))
		validate = ProtectedRecord.validate
		def fail(doc):
			if doc.doctype == "Incoming Letter Event":
				raise RuntimeError("Synthetic failure")
			return validate(doc)
		with patch.object(ProtectedRecord, "validate", fail):
			with self.assertRaises(RuntimeError):
				create_received_letter(actor=self.operator.name,
					original={"content": content, "file_name": "same-rollback.pdf", "page_count": 1},
					receipt={"received_date_known": False, "received_date": None},
					operation={"key": frappe.generate_hash(length=64), "fingerprint": frappe.generate_hash(length=64)})
		self.assertEqual(set(root.rglob("*.pdf")), before)
		self.assertEqual(frappe.get_doc("File", first.original_file).get_content(encodings=[]), content)
