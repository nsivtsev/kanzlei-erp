"""ERPNext's standard fixtures, exclusively for the disposable CI site."""

import os

import frappe

os.chdir("/home/frappe/frappe-bench/sites")
frappe.init(site="ci.localhost", sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
try:
	assert frappe.conf.allow_tests, "Test data requires an explicitly test-enabled site"
	frappe.in_test = True
	frappe.flags.in_test = True
	# Upstream fixtures contain test users with intentionally simple passwords.
	frappe.db.set_single_value("System Settings", "enable_password_policy", 0)
	frappe.clear_cache()
	# Importing this upstream test module bootstraps its standard fixtures.
	import erpnext.tests.utils

	frappe.defaults.set_global_default("company", "_Test Company")
	frappe.defaults.set_user_default("company", "_Test Company", user="Administrator")
	frappe.db.commit()
finally:
	frappe.destroy()
