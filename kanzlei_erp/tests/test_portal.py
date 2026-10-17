import importlib
import importlib.util
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestPortal(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("portal_test")
		self.throttle = patch("frappe.core.doctype.user.user.throttle_user_creation")
		self.throttle.start()
		self.addCleanup(self.throttle.stop)
		self.assertIsNotNone(importlib.util.find_spec("kanzlei_erp.portal"), "The approved portal backend is missing")
		self.api = importlib.import_module("kanzlei_erp.portal")
		self.reader = self.user("Reader")
		self.other = self.user("Other")
		self.customer = frappe.get_doc({
			"doctype": "Customer", "customer_name": f"_Portal {frappe.generate_hash(8)}",
			"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories",
		}).insert()
		frappe.share.add("Customer", self.customer.name, self.reader.name, read=1)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="portal_test")
		frappe.clear_cache()
		super().tearDown()

	def user(self, label, **values):
		return frappe.get_doc({
			"doctype": "User", "email": f"portal-{frappe.generate_hash(8)}@example.invalid",
			"first_name": label, "send_welcome_email": 0,
			"roles": [{"role": "Projects User"}], **values,
		}).insert(ignore_permissions=True)

	def enter(self):
		frappe.set_user(self.reader.name)

	def test_staff_workspaces_and_administrative_settings(self):
		self.enter()
		data = self.api.get_portal()
		self.assertEqual({row["key"] for row in data["workspaces"]}, {"erp", "drive"})
		self.assertIsNone(data["settings_url"])
		self.assertEqual(data["user"], self.reader.name)
		frappe.set_user("Administrator")
		self.assertEqual(self.api.get_portal()["settings_url"], "/desk/company")

	def test_suite_absence_hides_drive_and_its_shortcut(self):
		self.enter()
		with patch("frappe.get_installed_apps", return_value=["frappe", "erpnext", "kanzlei_erp"]):
			data = self.api.get_portal()
		self.assertEqual([row["key"] for row in data["workspaces"]], ["erp"])
		self.assertFalse(any(row["url"].startswith("/drive") for row in data["shortcuts"]))

	def test_guest_and_website_users_cannot_read_or_change_personal_state(self):
		portal_user = self.user("Website", roles=[], user_type="Website User")
		for user in ("Guest", portal_user.name):
			frappe.set_user(user)
			for action in (
				lambda: self.api.get_portal(),
				lambda: self.api.record_visit("Customer", self.customer.name),
				lambda: self.api.set_pin("Customer", self.customer.name, 1),
				lambda: self.api.open_record("Customer", self.customer.name),
			):
				with self.assertRaises(frappe.PermissionError):
					action()

	def test_history_is_deduplicated_and_pins_survive_reload(self):
		self.enter()
		for _ in range(2):
			self.api.record_visit("Customer", self.customer.name)
		self.api.set_pin("Customer", self.customer.name, 1)
		frappe.clear_cache(user=self.reader.name)
		data = self.api.get_personal()
		self.assertEqual([row["name"] for row in data["recent"]], [self.customer.name])
		self.assertEqual([row["name"] for row in data["pins"]], [self.customer.name])
		self.assertNotIn("<", data["recent"][0]["url"])
		self.api.set_pin("Customer", self.customer.name, 0)
		self.assertEqual(self.api.get_personal()["pins"], [])

	def test_personal_history_and_pins_are_scoped_to_session_user(self):
		self.enter()
		self.api.record_visit("Customer", self.customer.name)
		self.api.set_pin("Customer", self.customer.name, 1)
		frappe.set_user(self.other.name)
		self.assertEqual(self.api.get_personal(), {"user": self.other.name, "recent": [], "pins": []})

	def test_revoked_records_disappear_and_cannot_be_opened_or_pinned(self):
		self.enter()
		self.api.record_visit("Customer", self.customer.name)
		self.api.set_pin("Customer", self.customer.name, 1)
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		self.enter()
		data = self.api.get_personal()
		self.assertEqual(data["recent"], [])
		self.assertEqual(data["pins"], [])
		for action in (self.api.open_record, lambda dt, name: self.api.set_pin(dt, name, 1)):
			with self.assertRaises(frappe.PermissionError):
				action("Customer", self.customer.name)
		# A user can remove an inaccessible stale pin without needing record access.
		self.api.set_pin("Customer", self.customer.name, 0)

	def test_display_labels_are_resolved_fresh_and_untrusted_types_are_rejected(self):
		self.enter()
		self.api.record_visit("Customer", self.customer.name)
		frappe.set_user("Administrator")
		frappe.db.set_value("Customer", self.customer.name, "customer_name", '<img src=x onerror="bad()">')
		self.enter()
		row = self.api.get_personal()["recent"][0]
		self.assertEqual(row["label"], '<img src=x onerror="bad()">')
		for doctype, name in (("User", "Administrator"), ("Customer", []), ("javascript:alert(1)", "x")):
			with self.assertRaises(frappe.ValidationError):
				self.api.set_pin(doctype, name, 1)

	def test_invalid_pin_state_is_rejected_and_removal_is_idempotent(self):
		self.enter()
		for value in ("false", "true", "0", "1", 2, None, {}):
			with self.assertRaises(frappe.ValidationError):
				self.api.set_pin("Customer", self.customer.name, value)
		self.api.set_pin("Customer", self.customer.name, 0)
		self.api.set_pin("Customer", self.customer.name, 0)
		self.assertEqual(self.api.get_personal()["pins"], [])

	def test_corrupt_preferences_and_deleted_records_do_not_break_workspaces(self):
		frappe.defaults.set_user_default(self.api.HISTORY_KEY, "{broken", user=self.reader.name)
		frappe.defaults.set_user_default(self.api.PINS_KEY, frappe.as_json([{"doctype": "Customer", "name": "missing"}]), user=self.reader.name)
		self.enter()
		self.assertTrue(self.api.get_portal()["workspaces"])
		self.assertEqual(self.api.get_personal()["pins"], [])

	def test_drive_recents_use_current_access_and_do_not_expose_private_file_urls(self):
		from suite.drive.api.files import create_link, track_visit

		from kanzlei_erp.mandant_drive import create_folder

		folder = create_folder(self.customer.name)["folder"]
		file = create_link("Portal reference", "https://example.invalid/portal", folder)
		self.reader.add_roles("Suite User")
		self.enter()
		track_visit(entity_name=file.name)
		self.api.set_pin("File", folder, 1)
		row = next(row for row in self.api.get_personal()["recent"] if row["name"] == file.name)
		self.assertEqual(row["url"], f"/drive/f/{file.name}")
		self.assertNotIn("file_url", row)
		self.assertEqual(self.api.open_record("File", file.name)["url"], row["url"])
		self.assertEqual(self.api.open_record("File", folder)["url"], f"/drive/d/{folder}")
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		self.enter()
		self.assertEqual(self.api.get_personal()["recent"], [])
		self.assertEqual(self.api.get_personal()["pins"], [])

	def test_website_home_and_erp_application_home_are_distinct(self):
		from kanzlei_erp.hooks import app_home, home_page
		self.assertEqual(home_page, "/kanzlei-portal")
		self.assertEqual(app_home, "/desk/kanzlei-overview")

	def test_deleted_pins_do_not_exhaust_the_visible_pin_limit(self):
		frappe.defaults.set_user_default(self.api.PINS_KEY, frappe.as_json([
			{"doctype": "Customer", "name": f"missing-portal-client-{i}"} for i in range(self.api.PIN_LIMIT)
		]), user=self.reader.name)
		self.enter()
		self.api.set_pin("Customer", self.customer.name, 1)
		self.assertEqual([row["name"] for row in self.api.get_personal()["pins"]], [self.customer.name])

	def test_stale_browser_actor_cannot_mutate_the_new_session(self):
		self.enter()
		for action in (
			lambda: self.api.set_pin("Customer", self.customer.name, 1, expected_user=self.other.name),
			lambda: self.api.open_record("Customer", self.customer.name, expected_user=self.other.name),
		):
			with self.assertRaises(frappe.PermissionError):
				action()
		self.assertEqual(self.api.get_personal()["pins"], [])

	def test_personal_labels_follow_the_requested_language(self):
		self.enter()
		self.api.record_visit("Customer", self.customer.name)
		original = frappe.local.lang
		try:
			for lang, title, kind in (("de", "Dokumente", "Mandant"), ("ru", "Документы", "Клиент")):
				frappe.local.lang = lang
				self.assertEqual(self.api.get_personal()["recent"][0]["kind"], kind)
				self.assertEqual(next(row["label"] for row in self.api.get_portal()["workspaces"] if row["key"] == "drive"), title)
		finally:
			frappe.local.lang = original
