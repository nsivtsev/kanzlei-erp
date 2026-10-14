from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestSuiteSetup(FrappeTestCase):
	def test_configure_invalidates_cached_suite_profiles(self):
		from kanzlei_erp.suite_setup import configure

		with patch("suite.api.account.forget_logged_in_users") as forget:
			configure()
			forget.assert_called_once()

	def test_configure_removes_pre_upgrade_profile_cache_for_all_users(self):
		from kanzlei_erp.suite_setup import configure

		key = "suite.api.account.get_logged_in_user::pre-upgrade"
		users = ("Administrator", "old-profile@example.invalid")
		try:
			for user in users:
				frappe.cache.set_value(key, {"roles": []}, user=user, expires_in_sec=3600)
			configure()
			for user in users:
				self.assertIsNone(frappe.cache.get_value(key, user=user))
		finally:
			for user in users:
				frappe.cache.delete_value(key, user=user)

	def test_partial_install_rebuilds_only_suite_owned_rate_limits(self):
		from kanzlei_erp import suite_setup

		self.assertTrue(hasattr(suite_setup, "prepare_suite_install"))
		suite_rows = frappe.get_all("Rate Limit", filters={"method_path": ["like", "suite.%"]}, pluck="name")
		self.assertTrue(suite_rows)
		other = frappe.get_doc({"doctype": "Rate Limit", "method_path": "kanzlei_erp.fibu_questions.get_questions", "enabled": 1, "limit": 10, "seconds": 60, "ip_based": 1})
		other.name = frappe.generate_hash(length=10)
		other.db_insert()
		with patch.dict(frappe.conf, {"kanzlei_suite_install_pending": 0}):
			suite_setup.prepare_suite_install("suite")
			self.assertTrue(frappe.db.exists("Rate Limit", suite_rows[0]))
		with patch.dict(frappe.conf, {"kanzlei_suite_install_pending": 1}):
			suite_setup.prepare_suite_install("suite")
		self.assertFalse(frappe.get_all("Rate Limit", filters={"method_path": ["like", "suite.%"]}))
		self.assertTrue(frappe.db.exists("Rate Limit", other.name))

	def test_configure_keeps_kanzlei_hooks_last_and_skips_suite_onboarding(self):
		from kanzlei_erp.suite_setup import configure

		original = frappe.get_installed_apps()
		self.addCleanup(frappe.db.set_global, "installed_apps", frappe.as_json(original))
		frappe.db.set_global("installed_apps", frappe.as_json(["frappe", "erpnext", "kanzlei_erp", "suite"]))
		frappe.clear_cache()
		configure()
		self.assertEqual(frappe.get_installed_apps()[-1], "kanzlei_erp")
		self.assertTrue(frappe.db.get_single_value("Suite Settings", "is_onboarded"))
		self.assertEqual(frappe.get_hooks("home_page")[-1], "/desk/kanzlei-overview")
		first = frappe.get_installed_apps()
		configure()
		self.assertEqual(frappe.get_installed_apps(), first)
