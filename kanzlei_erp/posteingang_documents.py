"""Read-only projections of confirmed Posteingang sources; never copy documents."""

import json
from functools import wraps
from urllib.parse import urlencode

import frappe

from kanzlei_erp.posteingang_ai_validation import InvalidResult, parse_json
from kanzlei_erp.posteingang_jobs import WorkflowError, fail
from kanzlei_erp.posteingang_permissions import letter_access, staff
from kanzlei_erp.posteingang_review_validation import validate_confirmed_action
from kanzlei_erp.posteingang_task_access import action_key


def unavailable():
	frappe.throw(frappe._("Document is no longer available"), frappe.PermissionError)


def document_endpoint(function):
	"""Return expected HTTP errors without core's unconditional dev-server traceback.

	All endpoints using this boundary are read-only, including POST ZIP generation.
	Internal calls retain typed exceptions for transaction composition and tests.
	"""
	@wraps(function)
	def guarded(*args, **kwargs):
		try:
			return function(*args, **kwargs)
		except Exception as error:
			if getattr(frappe.local, "request", None) is None:
				raise
			from werkzeug.wrappers import Response

			status, code, kind = 500, "DOCUMENT_UNAVAILABLE", "DocumentError"
			if isinstance(error, frappe.PermissionError):
				status, kind = 403, "PermissionError"
			elif isinstance(error, frappe.ValidationError):
				status, kind, code = 417, "ValidationError", "INVALID_REQUEST"
				if isinstance(error, WorkflowError) and str(error) in {"INVALID_REQUEST", "INVALID_RESULT", "LIMIT_EXCEEDED"}:
					code = str(error)
			message = frappe._("Document is no longer available")
			return Response(json.dumps({"code": code, "exc_type": kind,
				"_server_messages": json.dumps([json.dumps({"message": message})])}), status=status,
				mimetype="application/json", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
	return guarded


def original(letter):
	"""Resolve only the immutable private binding, under current Letter/File policy."""
	if not letter_access(letter) or not frappe.has_permission("Incoming Letter", "read", doc=letter.name):
		unavailable()
	file = frappe.db.get_value("File", letter.original_file, "*", as_dict=True)
	if not file or not file.is_private or file.is_folder or file.attached_to_doctype != "Incoming Letter" or file.attached_to_name != letter.name:
		unavailable()
	if not frappe.has_permission("File", "read", doc=file.name):
		unavailable()
	return file


def linked_action(task, letter, action):
	return bool(
		action and action.parent == letter.name and action.parenttype == "Incoming Letter"
		and action.parentfield == "actions" and action.decision == "accept" and action.created_task == task.name
		and task.kanzlei_incoming_letter == letter.name and task.kanzlei_customer == letter.confirmed_customer
		and task.kanzlei_incoming_action_id == action.action_id
		and task.kanzlei_incoming_action_key == action_key(letter.name, action.action_id)
		and ((action.source_run and action.source_run == letter.applied_run and action.source_index is not None)
			or (not action.source_run and action.source_index is None))
	)


def current_originals(customer, tasks, snapshot=None):
	"""File-ID keyed current Letters; task contexts contain only verified readable links."""
	if not frappe.db.table_exists("Incoming Letter"):
		return {}
	result = {}
	letters = frappe.get_all("Incoming Letter", filters={"confirmed_customer": customer, "status": "Completed", "correction_quarantined": 0}, fields=["*"])
	for letter in letters:
		if not letter.confirmed_at or (snapshot is not None and frappe.utils.get_datetime(letter.confirmed_at) > snapshot):
			continue
		try:
			file = original(letter)
		except frappe.PermissionError:
			continue
		date = str(letter.received_date) + " 00:00:00" if letter.received_date_known and letter.received_date else str(letter.received_at)
		info = {"letter": letter, "tasks": [], "document_date": date[:19].replace("T", " "), "date_kind": "received" if letter.received_date_known else "uploaded"}
		for action in frappe.get_all("Incoming Letter Action", filters={"parent": letter.name, "parenttype": "Incoming Letter", "parentfield": "actions", "decision": "accept"}, fields=["*"]):
			task = tasks.get(action.created_task)
			if task and linked_action(task, letter, action) and frappe.has_permission("Task", "read", doc=task.name):
				info["tasks"].append(task.name)
		result[file.name] = info
	return result


def _boundary(operation, extra):
	cmd = extra.pop("cmd", None)
	if extra or cmd not in (None, "kanzlei_erp.posteingang_documents." + operation):
		fail("INVALID_REQUEST")


def _task_source(task):
	if not staff(frappe.session.user) or not isinstance(task, str) or not task or len(task) > 140:
		unavailable()
	row = frappe.db.get_value("Task", task, "*", as_dict=True)
	if not row or not frappe.has_permission("Task", "read", doc=task):
		unavailable()
	fields = (row.kanzlei_incoming_letter, row.kanzlei_incoming_action_id, row.kanzlei_incoming_action_key)
	if not any(fields):
		return None
	letter = frappe.db.get_value("Incoming Letter", row.kanzlei_incoming_letter, "*", as_dict=True) if all(fields) else None
	if not letter or not letter.confirmed_customer or letter.status not in {"Completed", "Cancelled"}:
		fail("INVALID_RESULT")
	file = original(letter)
	action = frappe.db.get_value("Incoming Letter Action", {"parent": letter.name, "parenttype": "Incoming Letter", "parentfield": "actions", "action_id": row.kanzlei_incoming_action_id}, "*", as_dict=True)
	if not linked_action(row, letter, action):
		fail("INVALID_RESULT")
	return row, letter, action, file


@frappe.whitelist(methods=["GET", "POST"])
@document_endpoint
def get_task_source(task=None, **extra):
	_boundary("get_task_source", extra)
	resolved = _task_source(task)
	if resolved is None:
		return {"schema_version": "1.0", "available": False}
	row, letter, action, file = resolved
	from kanzlei_erp.posteingang_confirmation import ACTION_FIELDS, _pages

	try:
		value = {key: action.get(key) for key in ACTION_FIELDS}
		for key in ("internal_date", "external_date"):
			value[key] = str(value[key]) if value[key] else None
		value["needs_planning"] = bool(value["needs_planning"])
		value["evidence"] = parse_json('{"items":' + (action.evidence_json or "[]") + '}')["items"]
		value = validate_confirmed_action(value, pages=_pages(letter), page_count=letter.page_count)
	except (InvalidResult, TypeError, KeyError, ValueError):
		fail("INVALID_RESULT")
	base = "/api/method/kanzlei_erp.posteingang_documents.download_task_source?"
	return {"schema_version": "1.0", "available": True,
		"letter": {"name": letter.name, "status": letter.status, "quarantined": bool(letter.correction_quarantined),
			"document_date": str(letter.document_date) if letter.document_date else None,
			"received_date_known": bool(letter.received_date_known), "received_date": str(letter.received_date) if letter.received_date else None, "received_at": str(letter.received_at)},
		"action": {key: value[key] for key in ("action_id", "external_date", "external_date_basis", "evidence")},
		"original": {"file_id": file.name, "file_name": file.file_name, "page_count": letter.page_count},
		"preview_url": base + urlencode({"task": row.name, "disposition": "inline"}),
		"download_url": base + urlencode({"task": row.name, "disposition": "attachment"})}


@frappe.whitelist(methods=["GET", "HEAD"])
@document_endpoint
def download_task_source(task=None, disposition="inline", **extra):
	_boundary("download_task_source", extra)
	if disposition not in {"inline", "attachment"}:
		fail("INVALID_REQUEST")
	resolved = _task_source(task)
	if resolved is None:
		unavailable()
	return pdf_response(frappe.get_doc("File", resolved[3].name), disposition)


def pdf_response(file, disposition):
	from kanzlei_erp.posteingang_files import response

	try:
		return response(file, disposition=disposition)
	except frappe.SecurityException:
		raise
	except Exception:
		unavailable()
