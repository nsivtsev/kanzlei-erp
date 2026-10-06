"""Mandant source profiles and immutable accounting-period checklist snapshots."""

import json
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

SOURCE_CATEGORIES = (
	"Incoming Invoices",
	"Outgoing Invoices",
	"Bank Account",
	"Card",
	"Cash Register",
	"Payment Service",
	"Marketplace",
	"Contract",
	"Assets",
)

CHECKLIST_STATUSES = ("Expected", "Partially Received", "Received in Full", "Reviewed", "Not Applicable")
_STATUS_ORDER = {"Expected": 0, "Partially Received": 1, "Received in Full": 2, "Reviewed": 3}
_ENTRY_FIELDS = (
	"source_id",
	"category",
	"source_name",
	"service",
	"source_note",
	"expected_from",
	"expected_through",
	"status",
	"inapplicable_reason",
)
_SOURCE_FIELDS = ("source_id", "category", "source_name", "service", "valid_from", "valid_until", "note")
_DATE_FIELDS = frozenset(("valid_from", "valid_until", "expected_from", "expected_through", "covered_from", "covered_through"))
_EVIDENCE_FIELDS = (
	"source_id",
	"evidence_type",
	"file",
	"communication",
	"external_url",
	"covered_from",
	"covered_through",
	"note",
)


def _as_date(value):
	if not value:
		return None
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def _as_datetime(value):
	if not value or isinstance(value, datetime):
		return value
	return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def build_checklist_snapshot(sources, service, period_start, period_end):
	"""Copy applicable source settings and intersect each source's validity with a period."""
	period_start = _as_date(period_start)
	period_end = _as_date(period_end)
	if not period_start or not period_end or period_start > period_end:
		raise ValueError("Invalid accounting period")
	rows = []
	for source in sources:
		source_service = source.get("service") or ""
		if source_service and source_service != service:
			continue
		valid_from = _as_date(source.get("valid_from")) or period_start
		valid_until = _as_date(source.get("valid_until")) or period_end
		expected_from = max(valid_from, period_start)
		expected_through = min(valid_until, period_end)
		if expected_from > expected_through:
			continue
		rows.append(
			{
				"source_id": source.get("source_id") or source.get("name") or "",
				"category": source.get("category") or "",
				"source_name": source.get("source_name") or "",
				"service": source_service,
				"source_note": source.get("note") or "",
				"expected_from": expected_from.isoformat(),
				"expected_through": expected_through.isoformat(),
				"status": "Expected",
				"inapplicable_reason": "",
			}
		)
	return rows


def evidence_covers_period(evidence, expected_from, expected_through):
	"""Return whether inclusive evidence date ranges cover every expected day."""
	start = _as_date(expected_from)
	end = _as_date(expected_through)
	if not start or not end or start > end:
		return False
	spans = sorted((_as_date(row.get("covered_from")), _as_date(row.get("covered_through"))) for row in evidence)
	cursor = start
	for span_start, span_end in spans:
		if not span_start or not span_end or span_start > span_end or span_end < cursor:
			continue
		if span_start > cursor:
			return False
		cursor = max(cursor, span_end + timedelta(days=1))
		if cursor > end:
			return True
	return cursor > end


def validate_entry(entry, evidence, previous_status=None, reason="", check_evidence_within_period=True):
	"""Validate checklist status semantics and evidence date coverage."""
	status = entry.get("status") or ""
	if status not in CHECKLIST_STATUSES:
		raise ValueError("Unknown checklist status")
	start = _as_date(entry.get("expected_from"))
	end = _as_date(entry.get("expected_through"))
	if not start or not end or start > end:
		raise ValueError("Expected coverage dates are invalid")
	for proof in evidence:
		covered_from = _as_date(proof.get("covered_from"))
		covered_through = _as_date(proof.get("covered_through"))
		if not covered_from or not covered_through or covered_from > covered_through:
			raise ValueError("Evidence coverage dates are invalid")
		if check_evidence_within_period and (covered_from < start or covered_through > end):
			raise ValueError("Evidence dates must be within the expected period")
	if status in ("Received in Full", "Reviewed"):
		if not evidence:
			raise ValueError("Evidence is required for received or reviewed sources")
		if not evidence_covers_period(evidence, start, end):
			raise ValueError("Evidence must cover the expected period")
	if status == "Not Applicable" and not (entry.get("inapplicable_reason") or "").strip():
		raise ValueError("A reason is required for a source that is not applicable")
	if previous_status and previous_status != status:
		is_rollback = status == "Not Applicable" or previous_status == "Not Applicable"
		if previous_status in _STATUS_ORDER and status in _STATUS_ORDER:
			is_rollback = is_rollback or _STATUS_ORDER[status] < _STATUS_ORDER[previous_status]
		if is_rollback and not (reason or "").strip():
			raise ValueError("A reason is required when changing a checklist status backward")
	return True


def _json(value):
	return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _plain_rows(rows, fields):
	return [
		{
			field: _as_date(row.get(field)).isoformat()
			if field in _DATE_FIELDS and row.get(field)
			else _as_datetime(row.get(field))
			if field == "recorded_at" and row.get(field)
			else row.get(field)
			for field in fields
		}
		for row in rows or []
	]


def _throw_value_error(function, *args, **kwargs):
	import frappe

	try:
		return function(*args, **kwargs)
	except ValueError as error:
		frappe.throw(frappe._(str(error)))


def validate_customer_sources(doc, method=None):
	"""Normalize Mandant source IDs and reset explicit review after profile edits."""
	import frappe

	previous = doc.get_doc_before_save() if not doc.is_new() else None
	old_rows = {row.name: row for row in (previous.get("kanzlei_data_sources") or [])} if previous else {}
	changed = previous is None or _plain_rows(doc.get("kanzlei_data_sources"), _SOURCE_FIELDS) != _plain_rows(
		previous.get("kanzlei_data_sources"), _SOURCE_FIELDS
	)
	for row in doc.get("kanzlei_data_sources") or []:
		if row.name not in old_rows:
			row.source_id = frappe.generate_hash(length=16)
		if row.name in old_rows and row.source_id != old_rows[row.name].source_id:
			frappe.throw(frappe._("A Mandant source identifier cannot be changed"))
		if row.category not in SOURCE_CATEGORIES:
			frappe.throw(frappe._("Choose a valid source category"))
		if not (row.source_name or "").strip():
			frappe.throw(frappe._("A source name is required"))
		if row.valid_from and row.valid_until and _as_date(row.valid_from) > _as_date(row.valid_until):
			frappe.throw(frappe._("Source start date cannot be after its end date"))
		if row.service:
			service = frappe.db.get_value("Item", row.service, ["disabled", "is_stock_item"], as_dict=True)
			if not service:
				frappe.throw(frappe._("The selected service does not exist"))
			if service.disabled or service.is_stock_item:
				frappe.throw(frappe._("A source can only be linked to an active non-stock service"))
	if changed:
		doc.kanzlei_sources_reviewed = 0
	elif previous and (
		doc.kanzlei_sources_reviewed != previous.kanzlei_sources_reviewed
		and not doc.flags.get("fibu_sources_confirmation")
	):
		frappe.throw(frappe._("Use the source list confirmation action to confirm this list"))


def _check_doc_version(doc, expected_modified):
	import frappe

	doc.check_permission("write")
	if not expected_modified:
		frappe.throw(frappe._("Reload the record before changing the checklist"))
	row = frappe.db.sql(f"SELECT modified FROM `tab{doc.doctype}` WHERE name=%s FOR UPDATE", doc.name)
	if not row or str(row[0][0]) != str(expected_modified) or str(doc.modified) != str(expected_modified):
		frappe.throw(frappe._("This record changed. Reload it before updating the checklist"))
	if doc.status != "Open":
		frappe.throw(frappe._("A closed accounting period checklist cannot be changed"))


def _append_event(doc, event_type, source_id, before, after, reason):
	import frappe

	if not (reason or "").strip():
		frappe.throw(frappe._("A reason is required for checklist changes"))
	doc.append(
		"checklist_events",
		{
			"event_type": event_type,
			"source_id": source_id or "",
			"previous_values": _json(before),
			"new_values": _json(after),
			"reason": reason.strip(),
			"recorded_by": frappe.session.user,
			"recorded_at": frappe.utils.now_datetime(),
		},
	)


def _initialize_entries(doc, entries, reason):
	for values in entries:
		doc.append("checklist_entries", values)
	_append_event(doc, "Initialized", "", {}, _plain_rows(doc.get("checklist_entries"), _ENTRY_FIELDS), reason)
	doc.checklist_initialized = 1


def initialize_new_package(doc):
	"""Capture a reviewed Mandant source profile while creating a new period."""
	import frappe

	if not doc.is_new():
		return
	if doc.get("checklist_entries") or doc.get("checklist_evidence") or doc.get("checklist_events"):
		frappe.throw(frappe._("The period checklist is initialized by the system"))
	if not doc.get("customer"):
		return
	customer = frappe.get_doc("Customer", doc.customer)
	customer.check_permission("read")
	sources = customer.get("kanzlei_data_sources") or []
	if not customer.get("kanzlei_sources_reviewed") and not sources:
		doc.checklist_initialized = 0
		return
	from kanzlei_erp.fibu_period import period_bounds

	start, end = _throw_value_error(period_bounds, doc.period_type, doc.period_year, doc.period_number)
	entries = _throw_value_error(
		build_checklist_snapshot,
		sources,
		doc.service,
		start,
		end,
	)
	_initialize_entries(doc, entries, frappe._("Initial source snapshot at package creation"))


def _save_checklist_action(doc):
	doc.flags.fibu_checklist_action = True
	try:
		doc.save()
	finally:
		doc.flags.fibu_checklist_action = False


def initialize_existing_package(doc, reason, expected_modified):
	"""Initialize an open legacy period from the Mandant's currently reviewed profile."""
	import frappe

	_check_doc_version(doc, expected_modified)
	if not (reason or "").strip():
		frappe.throw(frappe._("A reason is required for checklist changes"))
	if doc.get("checklist_initialized"):
		frappe.throw(frappe._("The period checklist is already initialized"))
	customer = frappe.get_doc("Customer", doc.customer)
	customer.check_permission("read")
	if not customer.get("kanzlei_sources_reviewed"):
		frappe.throw(frappe._("Confirm the Mandant source list before initializing the period checklist"))
	entries = _throw_value_error(
		build_checklist_snapshot,
		customer.get("kanzlei_data_sources") or [],
		doc.service,
		_as_date(doc.period_start),
		_as_date(doc.period_end),
	)
	_initialize_entries(doc, entries, reason)
	_save_checklist_action(doc)
	return doc.name


def initialize_supplement(doc, source_ids, reason, expected_modified):
	"""Copy only selected source snapshots from the immutable closed parent package."""
	import frappe

	_check_doc_version(doc, expected_modified)
	if not (reason or "").strip():
		frappe.throw(frappe._("A reason is required for checklist changes"))
	if doc.get("checklist_initialized"):
		frappe.throw(frappe._("The supplement checklist is already initialized"))
	parent = frappe.get_doc("FiBu Package", doc.package)
	parent.check_permission("read")
	available = {row.source_id: row for row in parent.get("checklist_entries") or []}
	selected_values = frappe.parse_json(source_ids) if isinstance(source_ids, str) else source_ids
	selected = set(selected_values or [])
	if not selected.issubset(available):
		frappe.throw(frappe._("Choose sources from the original period checklist"))
	entries = [
		{
			**{field: available[source_id].get(field) for field in _ENTRY_FIELDS},
			"status": "Expected",
			"inapplicable_reason": "",
		}
		for source_id in (row.source_id for row in parent.get("checklist_entries") or [])
		if source_id in selected
	]
	_initialize_entries(doc, entries, reason)
	_save_checklist_action(doc)
	return doc.name


def _validate_evidence_reference(doc, source_id, evidence):
	import frappe

	kind = evidence.get("evidence_type")
	values = [evidence.get("file"), evidence.get("communication"), evidence.get("external_url")]
	if kind not in ("File", "Communication", "External URL"):
		frappe.throw(frappe._("Choose a valid evidence type"))
	if sum(bool(value) for value in values) != 1:
		frappe.throw(frappe._("Provide exactly one file, communication, or external URL"))
	if kind == "External URL":
		parsed = urlparse(evidence.get("external_url") or "")
		if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
			frappe.throw(frappe._("Evidence links must be valid HTTP or HTTPS URLs"))
	elif kind == "File":
		if not evidence.get("file") or evidence.get("communication") or evidence.get("external_url"):
			frappe.throw(frappe._("Choose exactly one private evidence file"))
		file = frappe.get_doc("File", evidence.get("file"))
		file.check_permission("read")
		if not file.is_private:
			frappe.throw(frappe._("Checklist evidence files must be private"))
		customer = doc.customer if doc.doctype == "FiBu Package" else frappe.db.get_value(
			"FiBu Package", doc.package, "customer"
		)
		if file.attached_to_doctype == "Communication":
			_validate_communication_reference(file.attached_to_name, customer, doc)
		elif file.attached_to_doctype == "Customer":
			if file.attached_to_name != customer:
				frappe.throw(frappe._("Evidence must belong to the same Mandant"))
		elif file.attached_to_doctype == "FiBu Package":
			if file.attached_to_name != (doc.name if doc.doctype == "FiBu Package" else doc.package):
				frappe.throw(frappe._("Evidence must belong to the same accounting period"))
		elif file.attached_to_doctype == "FiBu Supplement":
			if file.attached_to_name != (doc.name if doc.doctype == "FiBu Supplement" else ""):
				frappe.throw(frappe._("Evidence must belong to this supplement"))
		elif file.attached_to_doctype == "Task":
			task = frappe.get_doc("Task", file.attached_to_name)
			task.check_permission("read")
			package_name = doc.name if doc.doctype == "FiBu Package" else doc.package
			if task.kanzlei_customer != customer or (
				task.kanzlei_fibu_package
				and task.kanzlei_fibu_package != package_name
				and task.kanzlei_fibu_supplement != (doc.name if doc.doctype == "FiBu Supplement" else "")
			):
				frappe.throw(frappe._("Evidence must belong to the same Mandant and period"))
		else:
			frappe.throw(frappe._("Link the evidence file to this Mandant, period, supplement, or Communication"))
	elif kind == "Communication":
		if evidence.get("file") or evidence.get("external_url") or not evidence.get("communication"):
			frappe.throw(frappe._("Choose exactly one Communication as evidence"))
		_validate_communication_reference(evidence.get("communication"), customer, doc)
	if not evidence.get("covered_from") or not evidence.get("covered_through"):
		frappe.throw(frappe._("Evidence coverage dates are required"))


def _validate_communication_reference(name, customer, context_doc):
	import frappe

	communication = frappe.get_doc("Communication", name)
	communication.check_permission("read")
	if communication.reference_doctype == "Customer" and communication.reference_name == customer:
		return
	if communication.reference_doctype == "FiBu Package":
		package = frappe.get_doc("FiBu Package", communication.reference_name)
		package.check_permission("read")
		if package.customer == customer:
			return
	if communication.reference_doctype == "FiBu Supplement":
		supplement = frappe.get_doc("FiBu Supplement", communication.reference_name)
		supplement.check_permission("read")
		parent_customer = frappe.db.get_value("FiBu Package", supplement.package, "customer")
		if parent_customer == customer:
			return
	if communication.reference_doctype == "Task":
		task = frappe.get_doc("Task", communication.reference_name)
		task.check_permission("read")
		if task.kanzlei_customer == customer:
			return
	if frappe.db.exists(
		"Communication Link",
		{
			"parent": name,
			"parenttype": "Communication",
			"parentfield": "timeline_links",
			"link_doctype": "Customer",
			"link_name": customer,
		},
	):
		return
	frappe.throw(frappe._("Checklist evidence must belong to the same Mandant"))


def add_entry(doc, source_json, reason, expected_modified):
	import frappe
	from kanzlei_erp.fibu_period import period_bounds

	_check_doc_version(doc, expected_modified)
	if not (reason or "").strip():
		frappe.throw(frappe._("A reason is required for checklist changes"))
	if not doc.get("checklist_initialized"):
		frappe.throw(frappe._("Initialize the checklist before adding a source"))
	values = frappe.parse_json(source_json) if isinstance(source_json, str) else source_json
	if not isinstance(values, dict):
		frappe.throw(frappe._("Source details are invalid"))
	values = {field: values.get(field) for field in _ENTRY_FIELDS}
	values["source_id"] = frappe.generate_hash(length=16)
	values["status"] = "Expected"
	values["inapplicable_reason"] = ""
	if values.get("category") not in SOURCE_CATEGORIES or not (values.get("source_name") or "").strip():
		frappe.throw(frappe._("Choose a source category and enter its name"))
	if values.get("service"):
		service = frappe.db.get_value("Item", values["service"], ["disabled", "is_stock_item"], as_dict=True)
		if not service:
			frappe.throw(frappe._("The selected service does not exist"))
		if service.disabled or service.is_stock_item:
			frappe.throw(frappe._("A source can only be linked to an active non-stock service"))
	if any(row.source_id == values["source_id"] for row in doc.get("checklist_entries")):
		frappe.throw(frappe._("This source is already in the checklist"))
	if not values.get("expected_from") or not values.get("expected_through"):
		frappe.throw(frappe._("Expected coverage dates are required"))
	period_start, period_end = period_bounds(doc.period_type, doc.period_year, doc.period_number) if doc.doctype == "FiBu Package" else (
		_as_date(frappe.db.get_value("FiBu Package", doc.package, "period_start")),
		_as_date(frappe.db.get_value("FiBu Package", doc.package, "period_end")),
	)
	_throw_value_error(validate_entry, values, [], None, reason)
	if _as_date(values["expected_from"]) < period_start or _as_date(values["expected_through"]) > period_end:
		frappe.throw(frappe._("Expected dates must be within the accounting period"))
	before = {}
	doc.append("checklist_entries", values)
	entry = doc.checklist_entries[-1]
	_append_event(doc, "Source Added", entry.source_id, before, _plain_rows([entry], _ENTRY_FIELDS), reason)
	_save_checklist_action(doc)
	return doc.name


def update_entry(doc, source_id, values_json, evidence_json, reason, expected_modified):
	import frappe

	_check_doc_version(doc, expected_modified)
	if not (reason or "").strip():
		frappe.throw(frappe._("A reason is required for checklist changes"))
	values = frappe.parse_json(values_json) if isinstance(values_json, str) else values_json
	evidence_value = frappe.parse_json(evidence_json) if isinstance(evidence_json, str) else (evidence_json or {})
	if not isinstance(values, dict) or not isinstance(evidence_value, dict):
		frappe.throw(frappe._("Checklist update is invalid"))
	entry = next((row for row in doc.get("checklist_entries") or [] if row.source_id == source_id), None)
	if not entry:
		frappe.throw(frappe._("Checklist source was not found"))
	before = _plain_rows([entry], _ENTRY_FIELDS)[0]
	new_values = {field: values.get(field, entry.get(field)) for field in _ENTRY_FIELDS}
	if new_values["source_id"] != source_id:
		frappe.throw(frappe._("A checklist source identifier cannot be changed"))
	if (
		new_values.get("category") != entry.category
		or (new_values.get("service") or "") != (entry.service or "")
		or (new_values.get("source_note") or "") != (entry.source_note or "")
	):
		frappe.throw(frappe._("Category, service, and source note cannot be changed in a checklist"))
	if new_values.get("category") not in SOURCE_CATEGORIES or not (new_values.get("source_name") or "").strip():
		frappe.throw(frappe._("Choose a source category and enter its name"))
	try:
		expected_from = _as_date(new_values.get("expected_from"))
		expected_through = _as_date(new_values.get("expected_through"))
	except (TypeError, ValueError):
		expected_from = expected_through = None
	if not expected_from or not expected_through or expected_from > expected_through:
		frappe.throw(frappe._("Expected coverage dates are invalid"))
	if doc.doctype == "FiBu Package":
		period_start, period_end = _as_date(doc.period_start), _as_date(doc.period_end)
	else:
		period_start, period_end = (
			_as_date(frappe.db.get_value("FiBu Package", doc.package, "period_start")),
			_as_date(frappe.db.get_value("FiBu Package", doc.package, "period_end")),
		)
	if (
		expected_from < period_start
		or expected_through > period_end
	):
		frappe.throw(frappe._("Expected dates must be within the accounting period"))
	old_evidence = [
		row for row in (doc.get("checklist_evidence") or []) if row.source_id == source_id
	]
	if evidence_value:
		_validate_evidence_reference(doc, source_id, evidence_value)
		evidence_value["source_id"] = source_id
		_throw_value_error(
			_validate_entry_fn,
			{"status": "Expected", "expected_from": expected_from, "expected_through": expected_through},
			[evidence_value],
			None,
			"",
			True,
		)
		doc.append("checklist_evidence", evidence_value)
	new_evidence = [
		_plain_rows([row], _EVIDENCE_FIELDS)[0]
		for row in (doc.get("checklist_evidence") or [])
		if row.source_id == source_id
	]
	new_evidence = [{field: row.get(field) for field in _EVIDENCE_FIELDS} for row in new_evidence]
	_validate_entry = {**new_values, "inapplicable_reason": values.get("inapplicable_reason", entry.inapplicable_reason)}
	_throw_value_error(_validate_entry_fn, _validate_entry, new_evidence, entry.status, reason, False)
	for field in _ENTRY_FIELDS:
		entry.set(field, _validate_entry.get(field))
	after = {
		**_plain_rows([entry], _ENTRY_FIELDS)[0],
		"evidence": new_evidence,
	}
	before["evidence"] = _plain_rows(old_evidence, _EVIDENCE_FIELDS)
	_append_event(doc, "Entry Updated", source_id, before, after, reason)
	_save_checklist_action(doc)
	return doc.name


def _validate_entry_fn(entry, evidence, previous_status, reason, check_evidence_within_period=True):
	return validate_entry(entry, evidence, previous_status, reason, check_evidence_within_period)


def confirm_customer_sources(customer_name, expected_modified):
	"""Explicitly confirm the source list, including an intentionally empty list."""
	import frappe

	customer = frappe.get_doc("Customer", customer_name)
	customer.check_permission("write")
	if not expected_modified:
		frappe.throw(frappe._("Reload the Mandant before confirming its source list"))
	row = frappe.db.sql("SELECT modified FROM `tabCustomer` WHERE name=%s FOR UPDATE", customer.name)
	if not row or str(row[0][0]) != str(expected_modified) or str(customer.modified) != str(expected_modified):
		frappe.throw(frappe._("This Mandant changed. Reload it before confirming its source list"))
	for source in customer.get("kanzlei_data_sources") or []:
		if source.category not in SOURCE_CATEGORIES or not (source.source_name or "").strip():
			frappe.throw(frappe._("Complete every source before confirming the source list"))
	customer.kanzlei_sources_reviewed = 1
	customer.flags.fibu_sources_confirmation = True
	customer.save()
	return customer.name


def protect_checklist_changes(doc, method=None):
	"""Reject direct checklist table writes and keep the event log append-only."""
	import frappe

	previous = doc.get_doc_before_save() if not doc.is_new() else None
	if doc.is_new():
		if doc.doctype == "FiBu Supplement" and (
			doc.get("checklist_initialized")
			or doc.get("checklist_entries")
			or doc.get("checklist_evidence")
			or doc.get("checklist_events")
		):
			frappe.throw(frappe._("The supplement checklist is initialized by the system"))
		return
	old_entries = _plain_rows(previous.get("checklist_entries"), _ENTRY_FIELDS)
	new_entries = _plain_rows(doc.get("checklist_entries"), _ENTRY_FIELDS)
	old_evidence = _plain_rows(previous.get("checklist_evidence"), _EVIDENCE_FIELDS)
	new_evidence = _plain_rows(doc.get("checklist_evidence"), _EVIDENCE_FIELDS)
	old_events = _plain_rows(
		previous.get("checklist_events"),
		("event_type", "source_id", "previous_values", "new_values", "reason", "recorded_by", "recorded_at"),
	)
	new_events = _plain_rows(
		doc.get("checklist_events"),
		("event_type", "source_id", "previous_values", "new_values", "reason", "recorded_by", "recorded_at"),
	)
	changed = any(
		[
			old_entries != new_entries,
			old_evidence != new_evidence,
			new_events[: len(old_events)] != old_events,
			len(new_events) < len(old_events),
			doc.get("checklist_initialized") != previous.get("checklist_initialized"),
		]
	)
	if changed and not doc.flags.get("fibu_checklist_action"):
		frappe.throw(frappe._("Use checklist actions to change the period checklist"))
	for entry in doc.get("checklist_entries") or []:
		_throw_value_error(_validate_entry_fn, entry.as_dict(), [
			row.as_dict()
			for row in (doc.get("checklist_evidence") or [])
			if row.source_id == entry.source_id
		], None, "system validation", False)
