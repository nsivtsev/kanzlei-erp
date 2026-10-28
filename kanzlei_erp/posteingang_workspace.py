"""AP-09 read projections. All writes remain in the AP-03…07 services."""

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from hashlib import sha256
from urllib.parse import urlencode

import frappe

from kanzlei_erp import posteingang_confirmation as review
from kanzlei_erp import posteingang_jobs as jobs
from kanzlei_erp import posteingang_matching as matching
from kanzlei_erp.mandant_identifiers import readable_customers
from kanzlei_erp.posteingang_ai_validation import InvalidResult, parse_json
from kanzlei_erp.posteingang_documents import document_endpoint, original, pdf_response, unavailable
from kanzlei_erp.posteingang_permissions import letter_access

OPEN = {"Received", "Queued", "Processing", "Needs Review", "Needs Clarification", "Error"}
DEFAULTS = {"state": "open", "uploaded_by": "all", "query": "", "sort": "oldest"}


def _boundary(operation, extra):
	# Guest dispatch reaches our safe HTTP error envelope; authorization still
	# requires an active, explicitly assigned Operator for every operation.
	cmd = extra.pop("cmd", None)
	if extra or cmd not in (None, "kanzlei_erp.posteingang_workspace." + operation):
		jobs.fail("INVALID_REQUEST")
	jobs._http_boundary()
	if not jobs._operator(frappe.session.user):
		unavailable()


def _letter(name):
	return review._read_letter(name)


def _base():
	return {"schema_version": "1.0", "user": frappe.session.user}


def _capabilities(doc):
	settings = frappe.get_single("Posteingang Settings")
	return {"enabled": bool(settings.enabled), "external_analysis_enabled": bool(settings.external_analysis_enabled),
		"customer_read": not doc.selected_customer or bool(matching._customer(doc.selected_customer)),
		"customer_write": not doc.selected_customer or bool((matching._customer(doc.selected_customer) or {}).get("can_select"))}


def _status(doc):
	original(doc)
	latest = frappe.db.get_value("Incoming Letter Run", {"letter": doc.name, "status": "Succeeded"}, "name", order_by="generation desc")
	analysis, capabilities = jobs.analysis_projection(doc), _capabilities(doc)
	# No OCR here. Reference access can change without a Letter revision: a
	# candidate Customer, confirmed Task, or administrative capability may vanish.
	references = [(name, str(frappe.db.get_value("Customer", name, "modified"))) for name in sorted(readable_customers())]
	receipt = review._receipt_projection(review._object(doc.confirmation_receipt_json)) if doc.confirmation_receipt_json else None
	roles = frappe.get_all("Has Role", filters={"parent": frappe.session.user, "parenttype": "User"}, pluck="role", order_by="role")
	visibility = sha256(json.dumps([references, receipt, roles, review._correction_state(doc), capabilities, analysis["allowed_operations"]], sort_keys=True, default=str).encode()).hexdigest()
	return {**_base(), "letter": doc.name, "revision": doc.revision, "status": doc.status,
		"analysis": analysis, "capabilities": capabilities, "visibility_key": visibility,
		"current_run": doc.current_run, "applied_run": doc.applied_run, "latest_successful_run": latest}


def _original(doc):
	file = original(doc)
	base = "/api/method/kanzlei_erp.posteingang_workspace.download_original?"
	return {"file_name": file.file_name, "page_count": doc.page_count,
		"preview_url": base + urlencode({"letter": doc.name, "disposition": "inline"}),
		"download_url": base + urlencode({"letter": doc.name, "disposition": "attachment"})}


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@document_endpoint
def get_letter_workspace(letter=None, **extra):
	_boundary("get_letter_workspace", extra)
	doc = _letter(letter)
	state = _status(doc)
	draft = review.review_projection(doc)
	draft.pop("original_file", None)
	return {**state, "matching": matching.matching_projection(doc), "review": draft, "original": _original(doc)}


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@document_endpoint
def get_workspace_status(letter=None, **extra):
	_boundary("get_workspace_status", extra)
	return _status(_letter(letter))


@frappe.whitelist(allow_guest=True, methods=["GET", "HEAD"])
@document_endpoint
def download_original(letter=None, disposition="inline", **extra):
	_boundary("download_original", extra)
	if disposition not in {"inline", "attachment"}:
		jobs.fail("INVALID_REQUEST")
	file = original(_letter(letter))
	return pdf_response(frappe.get_doc("File", file.name), disposition)


def _filters(value):
	try:
		value = parse_json(value) if isinstance(value, str) else {} if value is None else value
	except InvalidResult:
		jobs.fail("INVALID_REQUEST")
	if not isinstance(value, dict) or set(value) - set(DEFAULTS):
		jobs.fail("INVALID_REQUEST")
	value = {**DEFAULTS, **value}
	if any(not isinstance(item, str) for item in value.values()):
		jobs.fail("INVALID_REQUEST")
	if value["state"] not in {"open", "history", "all", *jobs.TRANSITIONS} or value["uploaded_by"] not in {"all", "me"} or value["sort"] not in {"oldest", "newest"}:
		jobs.fail("INVALID_REQUEST")
	if not isinstance(value["query"], str) or len(value["query"]) > 140:
		jobs.fail("INVALID_REQUEST")
	return value


def _fingerprint(filters):
	return sha256(json.dumps(["ap09-v1", frappe.local.site, frappe.session.user, filters], sort_keys=True).encode()).hexdigest()


def _cursor(value, filters):
	if value is None:
		return None
	try:
		if not isinstance(value, str) or len(value) > 2048:
			raise ValueError
		result = parse_json(urlsafe_b64decode(value.encode() + b"=" * (-len(value) % 4)))
		if set(result) != {"fingerprint", "snapshot", "date", "name"} or result["fingerprint"] != _fingerprint(filters):
			raise ValueError
		for key in ("snapshot", "date"):
			if not isinstance(result[key], str):
				raise ValueError
			frappe.utils.get_datetime(result[key])
		if not jobs._text(result["name"], 140):
			raise ValueError
		return result
	except (ValueError, TypeError, KeyError, Base64Error, InvalidResult):
		jobs.fail("INVALID_REQUEST")


def _row(doc, file):
	state = {"analysis": jobs.analysis_projection(doc)}
	selected = matching._customer(doc.confirmed_customer or doc.selected_customer)
	row = {"letter": doc.name, "revision": doc.revision, "status": doc.status, "file_name": file.file_name,
		"received_at": str(doc.received_at), "received_by": doc.received_by,
		"received_date_known": bool(doc.received_date_known), "received_date": str(doc.received_date) if doc.received_date else None,
		"sender": doc.sender, "letter_type": doc.letter_type, "customer": selected,
		"analysis": state["analysis"], "matching": None, "deadline": None, "result_error": None}
	try:
		row["matching"] = matching.matching_projection(doc)["matching"]
		if doc.status in OPEN:
			_, proposal = review._latest_successful(doc)
			if proposal is not None:
				deadlines = [a["deadline"] for a in proposal["actions"]]
				dates = sorted(d["date"] for d in deadlines if d["kind"] == "explicit_date")
				row["deadline"] = {"proposed_date": dates[0] if dates else None,
					"explicit_count": len(dates), "relative_count": sum(d["kind"] == "relative" for d in deadlines),
					"uncertain_count": sum(d["kind"] == "uncertain" for d in deadlines)}
	except jobs.WorkflowError as error:
		if str(error) not in {"INVALID_RESULT", "LIMIT_EXCEEDED"}:
			raise
		row.update(matching=None, deadline=None, result_error="INVALID_RESULT")
	return row


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@document_endpoint
def get_queue(filters=None, cursor=None, page_length=20, **extra):
	_boundary("get_queue", extra)
	filters = _filters(filters)
	if isinstance(page_length, str) and page_length.isascii() and page_length.isdecimal() and len(page_length) <= 2:
		page_length = int(page_length)
	if type(page_length) is not int or not 1 <= page_length <= 50:
		jobs.fail("INVALID_REQUEST")
	position = _cursor(cursor, filters)
	snapshot = frappe.utils.get_datetime(position["snapshot"]) if position else frappe.utils.now_datetime()
	states = OPEN if filters["state"] == "open" else {"Completed", "Cancelled"} if filters["state"] == "history" else set(jobs.TRANSITIONS) if filters["state"] == "all" else {filters["state"]}
	query = filters["query"].casefold()
	rows = []
	for doc in frappe.get_all("Incoming Letter", filters={"status": ["in", sorted(states)], "received_at": ["<=", snapshot]}, fields=["*"]):
		if not letter_access(doc) or not frappe.has_permission("Incoming Letter", "read", doc=doc.name):
			continue
		if filters["uploaded_by"] == "me" and doc.received_by != frappe.session.user:
			continue
		key = (str(doc.received_at), doc.name)
		if position and not (key > (position["date"], position["name"]) if filters["sort"] == "oldest" else key < (position["date"], position["name"])):
			continue
		try:
			file = original(doc)
		except frappe.PermissionError:
			continue
		if query and not any(query in (value or "").casefold() for value in (doc.name, file.file_name, doc.sender, doc.letter_type)):
			continue
		rows.append((key, doc, file))
	rows.sort(key=lambda row: row[0], reverse=filters["sort"] == "newest")
	page = rows[:page_length]
	next_cursor = None
	if len(rows) > page_length:
		key = page[-1][0]
		next_cursor = urlsafe_b64encode(json.dumps({"fingerprint": _fingerprint(filters), "snapshot": str(snapshot), "date": key[0], "name": key[1]}).encode()).decode().rstrip("=")
	return {**_base(), "server_time": str(frappe.utils.now_datetime()), "items": [_row(doc, file) for _, doc, file in page], "next_cursor": next_cursor}
