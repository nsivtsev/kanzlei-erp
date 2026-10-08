"""Mandant source snapshots and staff-confirmed completeness, independent of Freigabe."""

import json
from datetime import timedelta
from urllib.parse import urlsplit

import frappe
from frappe.utils import get_datetime, getdate, now_datetime

from kanzlei_erp.fibu_materials import _check_communication, _check_file
from kanzlei_erp.fibu_workflow import _lock_workflow_document

SOURCE_CATEGORIES = (
	"Incoming Invoices", "Outgoing Invoices", "Bank Account", "Card", "Cash",
	"Payment Service", "Marketplace", "Contracts", "Assets", "Other",
)
CHECKLIST_STATUSES = ("Expected", "Partially Received", "Received", "Checked", "Not Applicable")
ITEM_FIELDS = ("entry_key", "source_key", "category", "source_title", "expected_from", "expected_to", "status", "reason", "note", "checked_by", "checked_at")
EVIDENCE_FIELDS = ("entry_key", "file", "communication", "external_url", "covered_from", "covered_to", "note")
EVENT_FIELDS = ("entry_key", "action", "before_json", "after_json", "recorded_by", "recorded_at")


def _error(message):
	frappe.throw(frappe._(message))


def _range(start, end):
	try:
		if not start or not end:
			raise ValueError
		start, end = getdate(start), getdate(end)
		if start > end:
			raise ValueError
		return start, end
	except (ValueError, TypeError):
		_error("Start date must not be after end date")


def missing_ranges(start, end, intervals):
	"""Return inclusive gaps; overlapping and adjacent evidence counts only once."""
	start, end = _range(start, end)
	cursor, gaps = start, []
	for first, last in sorted((_range(a, b) for a, b in intervals)):
		first, last = max(first, start), min(last, end)
		if first > last or last < cursor:
			continue
		if first > cursor:
			gaps.append((cursor.isoformat(), (first - timedelta(days=1)).isoformat()))
		if last >= end:
			return gaps
		cursor = max(cursor, last + timedelta(days=1))
	if cursor <= end:
		gaps.append((cursor.isoformat(), end.isoformat()))
	return gaps


def validate_customer_sources(doc, method=None):
	previous = doc.get_doc_before_save()
	old = {row.name: row.source_key for row in previous.get("kanzlei_data_sources", [])} if previous else {}
	seen = set()
	for row in doc.get("kanzlei_data_sources", []):
		if row.name in old and old[row.name] and row.source_key != old[row.name]:
			_error("Source identity cannot be changed")
		if row.name not in old:
			# A submitted identifier is never trusted for a new source.
			row.source_key = frappe.generate_hash(length=20)
		if not row.source_key:
			row.source_key = frappe.generate_hash(length=20)
		if row.source_key in seen:
			_error("Source IDs must be unique")
		seen.add(row.source_key)
		row.source_title = (row.source_title or "").strip()
		if not row.source_title or row.category not in SOURCE_CATEGORIES:
			_error("Choose a source category and enter a source name")
		if row.valid_from and row.valid_to:
			_range(row.valid_from, row.valid_to)


def _values(row, fields):
	values = {}
	for field in fields:
		value = row.get(field)
		if value and field in ("expected_from", "expected_to", "covered_from", "covered_to"):
			value = str(getdate(value))
		elif value and field in ("checked_at", "recorded_at"):
			value = str(get_datetime(value))
		values[field] = value or ""
	return values


def _snapshot(doc):
	return {
		"initialized": int(doc.get("checklist_initialized") or 0),
		"items": [_values(row, ITEM_FIELDS) for row in doc.get("checklist", [])],
		"evidence": [_values(row, EVIDENCE_FIELDS) for row in doc.get("checklist_evidence", [])],
		"events": [_values(row, EVENT_FIELDS) for row in doc.get("checklist_events", [])],
	}


def _period(doc):
	if doc.doctype == "FiBu Package":
		return doc
	parent = frappe.get_doc("FiBu Package", doc.package)
	parent.check_permission("read")
	return parent


def _append_item(doc, category, source_title, first, last, source_key=""):
	if category not in SOURCE_CATEGORIES or not (source_title or "").strip():
		_error("Choose a source category and enter a source name")
	first, last = _range(first, last)
	parent = _period(doc)
	if first < getdate(parent.period_start) or last > getdate(parent.period_end):
		_error("Expected dates must be within the accounting period")
	if source_key and any(row.source_key == source_key for row in doc.get("checklist", [])):
		_error("This source is already in the checklist")
	return doc.append("checklist", {
		"entry_key": frappe.generate_hash(length=20), "source_key": source_key,
		"category": category, "source_title": source_title.strip(), "expected_from": first,
		"expected_to": last, "status": "Expected",
	})


def _copy_profile(doc):
	parent = _period(doc)
	customer = frappe.get_doc("Customer", parent.customer)
	customer.check_permission("read")
	for source in customer.get("kanzlei_data_sources", []):
		first = max(getdate(parent.period_start), getdate(source.valid_from) if source.valid_from else getdate(parent.period_start))
		last = min(getdate(parent.period_end), getdate(source.valid_to) if source.valid_to else getdate(parent.period_end))
		if first <= last:
			_append_item(doc, source.category, source.source_title, first, last, source.source_key)


def validate_checklist(doc):
	if doc.is_new():
		if not doc.flags.get("fibu_initial_checklist"):
			# copy_doc may deliberately retain no_copy fields; rebuild all server-owned state.
			for field in ("checklist", "checklist_evidence", "checklist_events"):
				doc.set(field, [])
			doc.flags.fibu_initial_checklist = True
			doc.checklist_initialized = 1
			if doc.doctype == "FiBu Package":
				_copy_profile(doc)
		return
	previous = doc.get_doc_before_save()
	if _snapshot(doc) != _snapshot(previous) and not doc.flags.get("fibu_checklist_action"):
		_error("Use checklist actions to change completeness")
	if doc.flags.get("fibu_checklist_action"):
		_validate_entries(doc)


def _validate_entries(doc):
	items = {row.entry_key: row for row in doc.get("checklist", [])}
	if len(items) != len(doc.get("checklist", [])) or "" in items:
		_error("Checklist entry IDs must be unique")
	parent = _period(doc)
	for item in items.values():
		first, last = _range(item.expected_from, item.expected_to)
		if first < getdate(parent.period_start) or last > getdate(parent.period_end):
			_error("Expected dates must be within the accounting period")
		if item.status not in CHECKLIST_STATUSES:
			_error("Unknown completeness status")
		evidence = [row for row in doc.get("checklist_evidence", []) if row.entry_key == item.entry_key]
		if item.status == "Not Applicable" and not (item.reason or "").strip():
			_error("A reason is required for a source that is not applicable")
		if item.status in ("Partially Received", "Received", "Checked") and not evidence:
			_error("Received sources require evidence")
		if item.status in ("Received", "Checked") and missing_ranges(first, last, [(row.covered_from, row.covered_to) for row in evidence]):
			_error("Evidence does not cover the full expected interval")
	for row in doc.get("checklist_evidence", []):
		item = items.get(row.entry_key)
		if not item:
			_error("Evidence must belong to a checklist entry")
		first, last = _range(row.covered_from, row.covered_to)
		if first < getdate(item.expected_from) or last > getdate(item.expected_to):
			_error("Evidence dates must be within the expected interval")
		if sum(bool(row.get(key)) for key in ("file", "communication", "external_url")) != 1:
			_error("Choose exactly one evidence file, communication, or external URL")
		if row.file:
			_check_file(row.file)
		if row.communication:
			_check_communication(row.communication)
		if row.external_url:
			try:
				parsed = urlsplit(row.external_url)
				valid = parsed.scheme.lower() in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
			except ValueError:
				valid = False
			if not valid:
				_error("External evidence must use an HTTP or HTTPS URL")


def _lock(doc, expected_modified):
	_lock_workflow_document(doc, expected_modified)
	if doc.status != "Open":
		_error("Closed work cannot change completeness")


def _save(doc, action, before, entry_key=""):
	doc.append("checklist_events", {"entry_key": entry_key, "action": action,
		"before_json": json.dumps({key: value for key, value in before.items() if key != "events"}, ensure_ascii=False),
		"after_json": json.dumps({key: value for key, value in _snapshot(doc).items() if key != "events"}, ensure_ascii=False),
		"recorded_by": frappe.session.user, "recorded_at": now_datetime()})
	doc.flags.fibu_checklist_action = True
	try:
		doc.save()
	finally:
		doc.flags.fibu_checklist_action = False
	return doc.name


def checklist_summary(doc):
	items = doc.get("checklist", [])
	counts = {state: sum(row.status == state for row in items) for state in CHECKLIST_STATUSES}
	pending = []
	for row in items:
		if row.status in ("Checked", "Not Applicable"):
			continue
		evidence = [item for item in doc.get("checklist_evidence", []) if item.entry_key == row.entry_key]
		pending.append({"entry_key": row.entry_key, "source_title": row.source_title, "status": row.status,
			"missing": missing_ranges(row.expected_from, row.expected_to, [(item.covered_from, item.covered_to) for item in evidence])})
	return {"counts": counts, "pending": pending, "complete": bool(items) and not pending,
		"initialized": bool(doc.get("checklist_initialized"))}


def warn_incomplete(doc):
	if not checklist_summary(doc)["complete"]:
		frappe.msgprint(frappe._("The source checklist is incomplete. Missing or unchecked sources remain visible."), indicator="orange")


@frappe.whitelist()
def get_checklist_summary(doctype: str, name: str):
	if doctype not in ("FiBu Package", "FiBu Supplement"):
		_error("Choose a FiBu package or supplement")
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	return checklist_summary(doc)


class FiBuChecklistMixin:
	@frappe.whitelist()
	def get_checklist_summary(self):
		self.reload()
		self.check_permission("read")
		return checklist_summary(self)

	@frappe.whitelist()
	def initialize_checklist(self, expected_modified: str):
		_lock(self, expected_modified)
		if self.checklist_initialized:
			return self.name
		before = _snapshot(self)
		self.checklist_initialized = 1
		if self.doctype == "FiBu Package":
			_copy_profile(self)
		return _save(self, "Checklist Initialized", before)

	@frappe.whitelist()
	def add_checklist_source(self, expected_modified: str, source_key: str = "", parent_entry_key: str = "",
		category: str = "", source_title: str = "", expected_from: str = "", expected_to: str = ""):
		_lock(self, expected_modified)
		if not self.checklist_initialized:
			_error("Initialize the checklist before adding sources")
		before = _snapshot(self)
		parent = _period(self)
		if source_key and parent_entry_key:
			_error("Choose one source origin")
		if parent_entry_key:
			if self.doctype != "FiBu Supplement":
				_error("Only supplements can select sources from the original package")
			row = next((row for row in parent.checklist if row.entry_key == parent_entry_key), None)
			if not row:
				_error("Source not found in the original package")
			category, source_title, source_key = row.category, row.source_title, row.source_key or row.entry_key
			expected_from, expected_to = expected_from or row.expected_from, expected_to or row.expected_to
		elif source_key:
			customer = frappe.get_doc("Customer", parent.customer)
			customer.check_permission("read")
			row = next((row for row in customer.get("kanzlei_data_sources", []) if row.source_key == source_key), None)
			if not row:
				_error("Source not found for this client")
			category, source_title = row.category, row.source_title
			expected_from = max(getdate(parent.period_start), getdate(row.valid_from) if row.valid_from else getdate(parent.period_start))
			expected_to = min(getdate(parent.period_end), getdate(row.valid_to) if row.valid_to else getdate(parent.period_end))
		item = _append_item(self, category, source_title, expected_from, expected_to, source_key)
		return _save(self, "Source Added", before, item.entry_key)

	@frappe.whitelist()
	def update_checklist_entry(self, entry_key: str, status: str, expected_modified: str, evidence=None,
		reason: str = "", note: str = "", expected_from: str = "", expected_to: str = ""):
		_lock(self, expected_modified)
		item = next((row for row in self.get("checklist", []) if row.entry_key == entry_key), None)
		if not item:
			_error("Checklist entry not found")
		if status not in CHECKLIST_STATUSES:
			_error("Unknown completeness status")
		before = _snapshot(self)
		old_evidence = [_values(row, EVIDENCE_FIELDS) for row in self.get("checklist_evidence", []) if row.entry_key == entry_key]
		if evidence is not None:
			if isinstance(evidence, str):
				evidence = frappe.parse_json(evidence)
			if not isinstance(evidence, list) or len(evidence) > 100:
				_error("Evidence must be a list of at most 100 entries")
			allowed = set(EVIDENCE_FIELDS) - {"entry_key"}
			for row in evidence:
				if not isinstance(row, dict) or set(row) - allowed:
					_error("Unknown evidence fields")
			self.set("checklist_evidence", [row for row in self.get("checklist_evidence", []) if row.entry_key != entry_key])
			for row in evidence:
				self.append("checklist_evidence", {**row, "entry_key": entry_key})
		new_evidence = [_values(row, EVIDENCE_FIELDS) for row in self.get("checklist_evidence", []) if row.entry_key == entry_key]
		old_dates = (str(getdate(item.expected_from)), str(getdate(item.expected_to)))
		if expected_from:
			item.expected_from = expected_from
		if expected_to:
			item.expected_to = expected_to
		changed = old_evidence != new_evidence or old_dates != (str(getdate(item.expected_from)), str(getdate(item.expected_to)))
		if item.status == "Checked" and changed and status == "Checked":
			status = "Received" if not missing_ranges(item.expected_from, item.expected_to, [(row["covered_from"], row["covered_to"]) for row in new_evidence]) else ("Partially Received" if new_evidence else "Expected")
		item.status, item.reason, item.note = status, (reason or "").strip(), (note or "").strip()
		if status == "Checked":
			if before["items"][next(i for i, row in enumerate(self.checklist) if row.entry_key == entry_key)]["status"] != "Checked":
				item.checked_by, item.checked_at = frappe.session.user, now_datetime()
		else:
			item.checked_by, item.checked_at = None, None
		_validate_entries(self)
		if before == _snapshot(self):
			return self.name
		return _save(self, "Entry Updated", before, entry_key)
