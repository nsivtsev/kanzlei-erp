"""Keep Tasks linked to exactly one editable FiBu work context."""

import frappe


def validate_task_package_link(task, method=None):
	previous = task.get_doc_before_save() if not task.is_new() else None
	if previous and previous.get("kanzlei_fibu_package"):
		old_package_name = previous.kanzlei_fibu_package
		frappe.db.sql("SELECT name FROM `tabFiBu Package` WHERE name=%s FOR UPDATE", old_package_name)
		old_package = frappe.db.get_value("FiBu Package", old_package_name, "status")
		old_supplement = previous.get("kanzlei_fibu_supplement")
		old_supplement_status = (
			frappe.db.get_value("FiBu Supplement", old_supplement, "status") if old_supplement else None
		)
		if (old_supplement and old_supplement_status == "Closed") or (
			old_package == "Closed" and not old_supplement
		):
			frappe.throw(frappe._("Tasks in a closed FiBu context cannot be changed"))

	package_name = task.get("kanzlei_fibu_package")
	supplement_name = task.get("kanzlei_fibu_supplement")
	if not package_name and not supplement_name:
		return
	if supplement_name:
		supplement = frappe.get_doc("FiBu Supplement", supplement_name)
		package_name = package_name or supplement.package
		task.kanzlei_fibu_package = package_name
		if supplement.package != package_name:
			frappe.throw(frappe._("The supplement belongs to another FiBu package"))

	frappe.db.sql("SELECT name FROM `tabFiBu Package` WHERE name=%s FOR UPDATE", package_name)
	package = frappe.get_doc("FiBu Package", package_name)
	if supplement_name:
		package.check_permission("read")
		supplement.check_permission("write")
	else:
		package.check_permission("write")
	if not task.get("kanzlei_customer"):
		task.kanzlei_customer = package.customer
	elif task.kanzlei_customer != package.customer:
		frappe.throw(frappe._("Task Mandant does not match the FiBu package Mandant"))

	if supplement_name:
		frappe.db.sql("SELECT name FROM `tabFiBu Supplement` WHERE name=%s FOR UPDATE", supplement_name)
		if frappe.db.get_value("FiBu Supplement", supplement_name, "status") != "Open":
			frappe.throw(frappe._("The FiBu supplement is closed"))
	elif package.status != "Open":
		frappe.throw(frappe._("The FiBu package is closed; create a supplement"))


def protect_closed_task(task, method=None):
	package_name = task.get("kanzlei_fibu_package")
	if not package_name:
		return
	frappe.db.sql("SELECT name FROM `tabFiBu Package` WHERE name=%s FOR UPDATE", package_name)
	supplement_name = task.get("kanzlei_fibu_supplement")
	if supplement_name:
		frappe.db.sql("SELECT name FROM `tabFiBu Supplement` WHERE name=%s FOR UPDATE", supplement_name)
		status = frappe.db.get_value("FiBu Supplement", supplement_name, "status")
	else:
		status = frappe.db.get_value("FiBu Package", package_name, "status")
	if status == "Closed":
		frappe.throw(frappe._("Tasks in a closed FiBu context cannot be deleted"))
