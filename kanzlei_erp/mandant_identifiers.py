"""Human-verified Customer identifiers. Normalization is not legal validation."""

import re
import unicodedata
from contextvars import ContextVar

import frappe
from frappe.utils import get_datetime, now_datetime

from kanzlei_erp.posteingang_jobs import _http_boundary, _text, fail
from kanzlei_erp.posteingang_permissions import names_condition, staff

TYPES = ("de_steuernummer", "de_tax_id", "de_vat_id", "other")
IDENTITY = ("identifier_type", "raw_value", "jurisdiction", "authority", "other_type")
VERIFICATION = ("verification_source", "verified_by", "verified_at")
_verification = ContextVar("mandant_identifier_verification", default=None)


def normalized_context(value):
	if value is None or value == "":
		return None
	if not _text(value, 140) or "\x00" in value:
		fail("INVALID_REQUEST")
	return unicodedata.normalize("NFKC", value).strip()


def normalize_value(kind, raw):
	if kind not in TYPES or not _text(raw, 140) or "\x00" in raw:
		fail("INVALID_REQUEST")
	value = unicodedata.normalize("NFKC", raw).strip()
	if kind != "other":
		value = "".join(value.split())
	if kind == "de_steuernummer":
		value = value.replace("/", "")
	if kind == "de_vat_id":
		value = value.upper()
	pattern = r"[A-Z0-9]+" if kind == "de_vat_id" else r"[0-9]+"
	if not value or len(value) > 140 or (kind != "other" and not re.fullmatch(pattern, value)):
		fail("INVALID_REQUEST")
	return value


def identifier_key(kind, raw, jurisdiction=None, authority=None, other_type=None):
	value = normalize_value(kind, raw)
	context = tuple(normalized_context(v) for v in (jurisdiction, authority, other_type))
	if kind == "de_steuernummer":
		return (kind, value, *context[:2], None) if all(context[:2]) else None
	if kind == "other":
		return (kind, value, *context) if all(context[1:]) else None
	return (kind, value, None, None, None)


def validate_customer(doc, method=None):
	if not doc.meta.has_field("kanzlei_identifiers"):
		return
	previous = doc.get_doc_before_save()
	old = {r.name: r for r in previous.get("kanzlei_identifiers", [])} if previous else {}
	seen = set()
	for row in doc.get("kanzlei_identifiers", []):
		if row.name in seen:
			fail("INVALID_REQUEST")
		seen.add(row.name)
		if row.name not in old and row.name and frappe.db.exists("Mandant Identifier", row.name):
			frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
		row.normalized_value = normalize_value(row.identifier_type, row.raw_value)
		for field in ("jurisdiction", "authority", "other_type"):
			row.set(field, normalized_context(row.get(field)))
		before = old.get(row.name)
		unchanged = before and all(row.get(k) == before.get(k) for k in IDENTITY)
		for field in VERIFICATION:
			row.set(field, before.get(field) if unchanged else None)
		verification = _verification.get()
		if verification and verification[:2] == (doc.name, row.name):
			row.verification_source, row.verified_by, row.verified_at = verification[2:]


def readable_customers(user=None):
	"""No cross-request permission cache and no disclosure of denied identifiers."""
	user = user or frappe.session.user
	offset = 0
	while True:
		try:
			rows = frappe.get_list("Customer", fields=["name"], user=user, order_by="name asc", start=offset, limit_page_length=100)
		except frappe.PermissionError:
			return
		for row in rows:
			if frappe.has_permission("Customer", "read", doc=row.name, user=user):
				yield row.name
		if len(rows) < 100:
			return
		offset += len(rows)


def required_query(doctype, user, query_doctype=None):
	if (query_doctype or doctype) != "Mandant Identifier":
		return None
	# Frappe's child detail permission follows Customer. Child lists also need
	# row-level parent filtering, including the legacy DatabaseQuery engine.
	column = "`tabCustomer`.name" if doctype == "Customer" else "`tabMandant Identifier`.parent"
	return names_condition(column, list(readable_customers(user)))


def child_export_permission(doctype, parent_doctype, parent, parentfield, user):
	if doctype != "Mandant Identifier":
		return None
	return bool(parent_doctype == "Customer" and parentfield == "kanzlei_identifiers" and frappe.has_permission("Customer", "read", doc=parent, user=user))


@frappe.whitelist(methods=["POST"])
def verify_identifier(customer=None, identifier=None, expected_modified=None, verification_source=None, **extra):
	_http_boundary()
	cmd = extra.pop("cmd", None)
	if extra or cmd not in (None, "kanzlei_erp.mandant_identifiers.verify_identifier") or not all((_text(customer, 140), _text(identifier, 140), _text(expected_modified, 40), _text(verification_source, 1000))):
		fail("INVALID_REQUEST")
	# Acquire the current parent row before permission reads establish an RR
	# snapshot. A revocation/edit committed while waiting must be visible.
	if not frappe.db.sql("SELECT name FROM `tabCustomer` WHERE name=%s FOR UPDATE", (customer,)):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	doc = frappe.get_doc("Customer", customer, for_update=True)
	if not staff(frappe.session.user) or not frappe.has_permission("Customer", "read", doc=doc) or not frappe.has_permission("Customer", "write", doc=doc):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	try:
		current = get_datetime(expected_modified) == get_datetime(doc.modified)
	except (ValueError, TypeError, OverflowError):
		fail("INVALID_REQUEST")
	if not current:
		fail("CUSTOMER_CHANGED")
	row = next((r for r in doc.get("kanzlei_identifiers", []) if r.name == identifier), None)
	if not row or not identifier_key(row.identifier_type, row.raw_value, row.jurisdiction, row.authority, row.other_type):
		fail("INVALID_REQUEST")
	token = _verification.set((doc.name, row.name, verification_source.strip(), frappe.session.user, now_datetime()))
	try:
		doc.save()
	finally:
		_verification.reset(token)
	return {"customer": doc.name, "identifier": row.name, "modified": str(doc.modified), "verified_by": row.verified_by, "verified_at": row.verified_at}
