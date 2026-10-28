"""Current-user policy shared by document, query and byte access."""

import frappe

READ_TYPES = {"read", "select"}


def staff(user):
	row = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
	return bool(user != "Guest" and row and row.enabled and row.user_type == "System User")


def letter_access(doc, user=None, ptype="read"):
	user = user or frappe.session.user
	if not staff(user):
		return False
	if user == "Administrator":
		return ptype in READ_TYPES
	if ptype not in READ_TYPES:
		return False
	roles = frappe.get_roles(user)
	customer = doc.get("confirmed_customer")
	if doc.get("correction_quarantined"):
		return bool(
			customer
			and "System Manager" in roles
			and "Posteingang Operator" in roles
			and frappe.has_permission("Customer", "read", doc=customer, user=user)
		)
	if customer:
		return bool(frappe.has_permission("Customer", "read", doc=customer, user=user))
	return "Posteingang Operator" in roles


def has_permission(doc, ptype, user=None, debug=False):
	if doc.doctype != "Incoming Letter":
		name = doc.get("letter") or (doc.parent if doc.get("parenttype") == "Incoming Letter" else None)
		if not name or not frappe.db.exists("Incoming Letter", name):
			return False
		doc = frappe.get_doc("Incoming Letter", name)
	return letter_access(doc, user, ptype)


def allowed_letters(user=None):
	user = user or frappe.session.user
	if not frappe.db.table_exists("Incoming Letter") or not staff(user):
		return []
	return [
		d.name
		for d in frappe.get_all(
			"Incoming Letter", fields=["name", "confirmed_customer", "correction_quarantined"], limit=0
		)
		if letter_access(d, user)
	]


def names_condition(column, names):
	return column + " IN (" + ",".join(frappe.db.escape(n) for n in names) + ")" if names else "1=0"


def letter_query(user=None, doctype=None):
	return names_condition("`tabIncoming Letter`.name", allowed_letters(user))


def run_query(user=None, doctype=None):
	return names_condition("`tabIncoming Letter Run`.letter", allowed_letters(user))


def event_query(user=None, doctype=None):
	return names_condition("`tabIncoming Letter Event`.letter", allowed_letters(user))


def file_access_override(doc, ptype, user, debug=False):
	from kanzlei_erp.posteingang_files import protected_letters

	letters = protected_letters(doc)
	if letters is None:
		return None
	if not letters or doc.get("kanzlei_drive_customer"):
		return False
	return ptype in READ_TYPES and all(letter_access(d, user) for d in letters)


def file_query_condition_override(user, condition):
	from kanzlei_erp.posteingang_files import protected_letters

	if not frappe.db.table_exists("Incoming Letter"):
		return condition
	protected, allowed = [], []
	for row in frappe.get_all(
		"File", fields=["name", "file_url", "attached_to_doctype", "attached_to_name", "is_folder"], limit=0
	):
		if protected_letters(row) is not None:
			protected.append(row.name)
			if file_access_override(row, "read", user):
				allowed.append(row.name)
	if not protected:
		return condition
	return f"((({condition or '1=1'}) AND NOT ({names_condition('`tabFile`.name', protected)})) OR ({names_condition('`tabFile`.name', allowed)}))"


def drive_access(entity, user):
	doc = frappe.get_doc("File", entity) if isinstance(entity, str) else entity
	access = file_access_override(doc, "read", user)
	if access is None:
		return None
	return {"read": int(access), "write": 0, "upload": 0, "comment": 0, "share": 0, "type": "guest"}


def drive_access_batch(entities, user):
	return {entity.name: access for entity in entities if (access := drive_access(entity, user)) is not None}


def drive_criterion(user, table, criterion):
	from kanzlei_erp.posteingang_files import protected_letters

	file = table or frappe.qb.DocType("File")
	protected, allowed = [], []
	for row in frappe.get_all(
		"File", fields=["name", "file_url", "attached_to_doctype", "attached_to_name"], limit=0
	):
		if protected_letters(row) is not None:
			protected.append(row.name)
			if file_access_override(row, "read", user):
				allowed.append(row.name)
	if not protected:
		return criterion
	base = file.name.notin(protected)
	if criterion is not None:
		base &= criterion
	return base | file.name.isin(allowed) if allowed else base


def required_permission(doctype, doc, ptype, user):
	if doctype == "Incoming Letter Intake Receipt":
		return False
	if doctype == "Posteingang Settings":
		return settings_permission(doc, ptype, user)
	if doc is None:
		return None
	if doctype in (
		"Incoming Letter",
		"Incoming Letter Run",
		"Incoming Letter Event",
		"Incoming Letter Action",
	):
		if isinstance(doc, str):
			doc = frappe.get_doc(doctype, doc)
		return has_permission(doc, ptype, user)
	if doctype == "File":
		if isinstance(doc, str):
			doc = frappe.get_doc(doctype, doc)
		return file_access_override(doc, ptype, user)
	return None


def required_query(doctype, user):
	if doctype == "Incoming Letter Intake Receipt":
		return "1=0"
	queries = {
		"Incoming Letter": letter_query,
		"Incoming Letter Run": run_query,
		"Incoming Letter Event": event_query,
	}
	if doctype in queries:
		return queries[doctype](user)
	if doctype == "File":
		return file_query_condition_override(user, "1=1")
	return None


def settings_permission(doc, ptype, user=None, debug=False):
	user = user or frappe.session.user
	return staff(user) and "System Manager" in frappe.get_roles(user) and ptype in ("read", "write")


def receipt_permission(doc=None, ptype=None, user=None, debug=False):
	return False


def receipt_query(user=None, doctype=None):
	return "1=0"


def receipt_administrator_permission(doctype, doc=None, ptype=None, user=None):
	return False if doctype == "Incoming Letter Intake Receipt" else None
