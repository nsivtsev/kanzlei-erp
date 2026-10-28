"""Independent correction bindings and rollback on the real storage adapter."""

from unittest.mock import patch

import frappe

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangCorrectionS3(PosteingangTestCase):
	def setUp(self):
		if "suite" not in frappe.get_installed_apps():
			self.skipTest("Suite storage integration")
		from kanzlei_erp.tests.test_object_storage import TestObjectStorage

		TestObjectStorage.setUp(self)
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		from kanzlei_erp import posteingang_confirmation, posteingang_jobs, posteingang_matching

		self.service = posteingang_confirmation
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		frappe.share.add("Customer", self.customer.name, self.manager.name, read=1, write=1)
		self.doc = self.letter()
		posteingang_matching.select_customer(self.doc.name, self.customer.name, "Synthetic source reviewed", 1, "customer")
		posteingang_jobs.manual_review(self.doc.name, 2, "manual")
		state = self.service.get_review_state(self.doc.name)
		state["review"].update(mode="confirm", human_verified=True, no_actions_reason="Synthetic informational original")
		self.confirmed = self.service.confirm(self.doc.name, state["review"], state["document_fields"], "confirm")
		self.original = frappe.get_doc("File", self.doc.original_file)
		self.original_key = self.storage.get_binding(self.original.file_url).object_key
		self.original_bytes = self.s3.objects[self.original_key]
		frappe.set_user(self.manager.name)

	def correct(self):
		return self.service.create_correction(self.doc.name, self.confirmed["revision"], "correct", "Wrong synthetic original", True, False, None)

	def test_copy_has_own_binding_and_new_operator_can_read_it(self):
		result = self.correct()
		new = frappe.get_doc("Incoming Letter", result["replacement"])
		file = frappe.get_doc("File", new.original_file)
		key = self.storage.get_binding(file.file_url).object_key
		self.assertNotEqual(file.file_url, self.original.file_url)
		self.assertNotEqual(key, self.original_key)
		self.assertEqual(self.s3.objects[key], self.original_bytes)
		frappe.set_user(self.operator.name)
		with self.storage.open_file(file) as stream:
			self.assertEqual(stream.read(), self.original_bytes)
		with self.assertRaises(frappe.PermissionError):
			with self.storage.open_file(self.original):
				pass

	def test_receipt_failure_cleans_new_objects_and_retry_succeeds(self):
		before = set(self.s3.objects)
		finish = self.service._finish

		def fail_receipt(doc, operation, *args, **kwargs):
			if operation == "create_correction":
				raise RuntimeError("Synthetic receipt failure")
			return finish(doc, operation, *args, **kwargs)

		with patch.object(self.service, "_finish", side_effect=fail_receipt):
			with self.assertRaisesRegex(RuntimeError, "Synthetic receipt failure"):
				self.correct()
		self.assertEqual(set(self.s3.objects), before)
		self.assertEqual(self.s3.objects[self.original_key], self.original_bytes)
		self.assertEqual(frappe.db.get_value("Incoming Letter", self.doc.name, "status"), "Completed")
		self.assertTrue(self.correct()["replacement"])
