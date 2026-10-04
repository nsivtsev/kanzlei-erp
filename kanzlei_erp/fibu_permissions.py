"""Apply Mandant access to FiBu records and linked Tasks."""

import frappe


def _allowed_names(doctype: str, user: str) -> list[str]:
	if user == "Administrator":
		return []
	return [row.name for row in frappe.get_list(doctype, fields=["name"], limit=0, user=user)]


def _in_condition(column: str, names: list[str]) -> str:
	if not names:
		return "1=0"
	return f"{column} IN ({', '.join(frappe.db.escape(name) for name in names)})"


def package_has_permission(doc, ptype, user=None, debug=False):
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if doc.customer and not frappe.has_permission("Customer", "read", doc=doc.customer, user=user):
		return False
	if ptype in ("write", "delete") and doc.status == "Closed":
		return False
	return True


def supplement_has_permission(doc, ptype, user=None, debug=False):
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if doc.package and not frappe.has_permission("FiBu Package", "read", doc=doc.package, user=user):
		return False
	if ptype in ("write", "delete") and doc.status == "Closed":
		return False
	return True


def linked_task_has_permission(doc, ptype, user=None, debug=False):
	if doc.get("kanzlei_fibu_package"):
		return frappe.has_permission("FiBu Package", "read", doc=doc.kanzlei_fibu_package, user=user)
	return True


def package_query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	return _in_condition("`tabFiBu Package`.customer", _allowed_names("Customer", user))


def supplement_query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	return _in_condition("`tabFiBu Supplement`.package", _allowed_names("FiBu Package", user))


def task_query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	allowed = _in_condition("`tabTask`.kanzlei_fibu_package", _allowed_names("FiBu Package", user))
	return f"(COALESCE(`tabTask`.kanzlei_fibu_package, '')='' OR {allowed})"
