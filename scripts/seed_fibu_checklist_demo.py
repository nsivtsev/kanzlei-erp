"""Seed fictitious BL-003/004 browser acceptance records on the local site only.

Read {"password": "temporary test password"} from stdin; never print the password.
"""

import json
import os
from pathlib import Path

import frappe
from frappe.utils.file_manager import save_file
from frappe.utils.password import update_password

SITE = "development.localhost"
CUSTOMER = "DEMO BL003-004 Quellen GmbH"


def seed(password):
	if frappe.local.site != SITE:
		raise RuntimeError("Checklist demo is restricted to development.localhost")
	frappe.set_user("Administrator")
	name = frappe.db.get_value("Customer", {"customer_name": CUSTOMER}, "name")
	customer = frappe.get_doc("Customer", name) if name else frappe.get_doc({
		"doctype": "Customer", "customer_name": CUSTOMER, "customer_type": "Company",
		"customer_group": "Commercial", "territory": "All Territories"}).insert()
	if not customer.get("kanzlei_data_sources"):
		customer.append("kanzlei_data_sources", {"category": "Bank Account", "source_title": "DEMO Основной счет"})
		customer.append("kanzlei_data_sources", {"category": "Bank Account", "source_title": "DEMO Второй счет", "valid_from": "2026-09-10"})
		customer.save()
	staff = []
	for language in ("ru", "de"):
		email = f"bl003-demo-{language}@example.invalid"
		if not frappe.db.exists("User", email):
			frappe.get_doc({"doctype": "User", "email": email, "first_name": "DEMO Checklist " + language,
				"language": language, "send_welcome_email": 0,
				"roles": [{"role": "Projects User"}, {"role": "Sales User"}, {"role": "Inbox User"}]}).insert()
		if not frappe.db.exists("User Permission", {"user": email, "allow": "Customer", "for_value": customer.name}):
			frappe.get_doc({"doctype": "User Permission", "user": email, "allow": "Customer", "for_value": customer.name}).insert(ignore_permissions=True)
		update_password(email, password, logout_all_sessions=False)
		staff.append(email)
	service_name = "DEMO BL003-004 FiBu"
	if not frappe.db.exists("Item", service_name):
		frappe.get_doc({"doctype": "Item", "item_code": service_name, "item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"), "stock_uom": "Nos", "is_stock_item": 0}).insert()
	name = frappe.db.get_value("FiBu Package", {"customer": customer.name, "service": service_name, "period_start": "2026-09-01"}, "name")
	if name:
		package = frappe.get_doc("FiBu Package", name)
	else:
		package = frappe.get_doc({"doctype": "FiBu Package", "customer": customer.name, "service": service_name,
			"period_type": "Monthly", "period_year": 2026, "period_number": 9, "responsible": staff[0], "deputy": staff[1]}).insert()
		for item, filename, start, end, status in (
			(package.checklist[0], "DEMO-full-statement.txt", "2026-09-01", "2026-09-30", "Received"),
			(package.checklist[1], "DEMO-partial-statement.txt", "2026-09-10", "2026-09-15", "Partially Received"),
		):
			file = save_file(filename, ("FICTITIOUS DEMO ONLY: " + filename).encode(), "Customer", customer.name, is_private=1)
			package.update_checklist_entry(entry_key=item.entry_key, status=status,
				evidence=[{"file": file.name, "covered_from": start, "covered_to": end}], expected_modified=str(package.modified))
	frappe.db.commit()
	print(json.dumps({"customer": customer.name, "package": package.name, "staff": staff}, ensure_ascii=False))


if __name__ == "__main__":
	credentials = json.load(__import__("sys").stdin)
	os.chdir(Path(__file__).resolve().parents[1] / "development/frappe-bench/sites")
	frappe.init(site=SITE, sites_path=".")
	frappe.connect()
	try:
		seed(credentials["password"])
	finally:
		frappe.destroy()
