"""Keep Kanzlei's Desk and ERPNext onboarding when Suite is installed."""

import frappe


def prepare_suite_install(app_name):
	"""Replay a registered, interrupted install without duplicate bootstrap limits."""
	if app_name != "suite" or not frappe.conf.get("kanzlei_suite_install_pending"):
		return
	if "suite" not in frappe.get_installed_apps() or not frappe.db.table_exists("Rate Limit"):
		return
	# Suite's after_install inserts its defaults rather than upserting them. Only
	# a flagged incomplete installation is replayed; ordinary updates keep settings.
	filters = {"method_path": ["like", "suite.%"]}
	for method in frappe.get_all("Rate Limit", filters=filters, pluck="method_path"):
		frappe.cache.hdel("rate_limits", method)
	frappe.db.delete("Rate Limit", filters)


def configure():
	apps = frappe.get_installed_apps()
	if "suite" not in apps:
		return
	# Adding Suite to an existing site would otherwise give its home/setup hooks priority.
	if "kanzlei_erp" in apps and apps[-1] != "kanzlei_erp":
		frappe.db.set_global("installed_apps", frappe.as_json([app for app in apps if app != "kanzlei_erp"] + ["kanzlei_erp"]))
		frappe.clear_cache()
	# ERPNext owns the site setup; Drive should open without Suite's unrelated onboarding.
	frappe.db.set_single_value("Suite Settings", "is_onboarded", 1)
	# Profile responses cached before an upgrade may lack new navigation fields.
	from suite.api.account import forget_logged_in_users

	forget_logged_in_users()


@frappe.whitelist()
def trash_or_restore(entity_names):
	from suite.drive.api.files import remove_or_restore

	from kanzlei_erp.suite_file import require_drive_material

	if isinstance(entity_names, str):
		entity_names = frappe.parse_json(entity_names)
	if not isinstance(entity_names, list):
		frappe.throw("Expected file names", frappe.ValidationError)
	for name in entity_names:
		doc = frappe.get_doc("File", name)
		doc.check_permission("write")
		require_drive_material(doc)
	return remove_or_restore(entity_names)
