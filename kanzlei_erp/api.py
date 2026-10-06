"""Whitelisted actions shared by standard ERPNext DocTypes."""

import frappe

from kanzlei_erp.fibu_checklist import confirm_customer_sources as _confirm_customer_sources


@frappe.whitelist()
def confirm_customer_sources(customer_name: str, expected_modified: str):
	return _confirm_customer_sources(customer_name, expected_modified)
