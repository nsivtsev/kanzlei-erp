"""Validate linked sources and preserve manually recorded transfer events."""

import frappe
from frappe.utils import get_datetime, getdate, now_datetime


def _transfer_values(row):
	"""Compare persisted values despite Frappe form date and empty-link serialization."""
	return (
		str(getdate(row.event_date)) if row.event_date else None,
		row.note,
		row.evidence_file or None,
		row.evidence_communication or None,
		row.recorded_by,
		get_datetime(row.recorded_at).replace(microsecond=0) if row.recorded_at else None,
	)


def validate_fibu_attachment(file, method=None):
	if file.attached_to_doctype not in ("FiBu Package", "FiBu Supplement") or not file.attached_to_name:
		return
	parent = frappe.get_doc(file.attached_to_doctype, file.attached_to_name)
	parent.check_permission("write")
	if parent.status != "Open":
		frappe.throw(frappe._("Files in a closed FiBu context cannot be changed"))
	if not file.is_private:
		frappe.throw(frappe._("FiBu documents must be private files"))


def _check_file(name):
	file = frappe.get_doc("File", name)
	file.check_permission("read")
	if not file.is_private:
		frappe.throw(frappe._("FiBu documents and evidence must be private files"))


def _check_communication(name):
	frappe.get_doc("Communication", name).check_permission("read")


def validate_materials(doc):
	previous = doc.get_doc_before_save() if not doc.is_new() else None
	old_transfers = {row.name: row for row in previous.get("transfers", [])} if previous else {}
	new_transfers = {row.name: row for row in doc.get("transfers", [])}
	if not set(old_transfers).issubset(new_transfers):
		frappe.throw(frappe._("Transfer history cannot be removed"))
	for name, old in old_transfers.items():
		if _transfer_values(old) != _transfer_values(new_transfers[name]):
			frappe.throw(frappe._("Transfer history cannot be changed"))
	for row in doc.get("files", []):
		_check_file(row.file)
		if row.source_communication:
			_check_communication(row.source_communication)
	for row in doc.get("messages", []):
		_check_communication(row.communication)
	for row in doc.get("transfers", []):
		if not (row.note or "").strip():
			frappe.throw(frappe._("Transfer note is required"))
		if row.evidence_file:
			_check_file(row.evidence_file)
		if row.evidence_communication:
			_check_communication(row.evidence_communication)
		if row.name not in old_transfers:
			row.recorded_by = frappe.session.user
			row.recorded_at = now_datetime()
