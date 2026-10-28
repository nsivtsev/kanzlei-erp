"""Installation remains additive and does not distribute the Operator role."""

import frappe
from frappe.tests.utils import FrappeTestCase


class TestPosteingangSetup(FrappeTestCase):
	def test_repeated_setup_preserves_settings_and_assignments(self):
		from kanzlei_erp.posteingang_setup import configure

		frappe.db.set_single_value("Posteingang Settings", "max_pages", 7)
		before = frappe.get_all("Has Role", filters={"role": "Posteingang Operator"}, pluck="name")
		configure()
		configure()
		self.assertEqual(frappe.db.get_single_value("Posteingang Settings", "max_pages"), 7)
		self.assertEqual(
			before, frappe.get_all("Has Role", filters={"role": "Posteingang Operator"}, pluck="name")
		)

	def test_settings_limits(self):
		settings = frappe.get_single("Posteingang Settings")
		settings.max_pages = 51
		with self.assertRaises(frappe.ValidationError):
			settings.save()

	def test_storage_indexes_and_nullable_unique_keys(self):
		rows = frappe.db.sql("SHOW INDEX FROM `tabIncoming Letter`", as_dict=True)
		hash_indexes = [r for r in rows if r.Column_name == "content_sha256"]
		self.assertTrue(hash_indexes)
		self.assertTrue(all(r.Non_unique for r in hash_indexes))
		self.assertIn("ap02_queue", {r.Key_name for r in rows})
		for doctype, index in (("Incoming Letter Run", "ap02_letter_generation"),
			("Incoming Letter Action", "ap02_parent_action")):
			rows = frappe.db.sql(f"SHOW INDEX FROM `tab{doctype}`", as_dict=True)
			self.assertTrue(any(r.Key_name == index and not r.Non_unique for r in rows))

	def test_password_serialization_does_not_return_plaintext(self):
		settings = frappe.get_single("Posteingang Settings")
		settings.api_key = "synthetic-not-a-real-provider-key"
		settings.save()
		settings.reload()
		self.assertNotIn("synthetic-not-a-real-provider-key", frappe.as_json(settings.as_dict()))
