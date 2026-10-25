"""Human review and atomic confirmation. No AI, enqueue or generic CRUD bypass."""

import copy
from collections import deque
from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

import frappe
from frappe.utils import now_datetime

from kanzlei_erp import posteingang_jobs as workflow
from kanzlei_erp import posteingang_matching as matching
from kanzlei_erp.posteingang import canonical
from kanzlei_erp.posteingang_ai_validation import InvalidResult, parse_json, validate_pages
from kanzlei_erp.posteingang_permissions import letter_access, staff
from kanzlei_erp.posteingang_review_validation import validate_review

DRAFT_STATES = {"Needs Review", "Needs Clarification"}
DOCUMENT_FIELDS = ("sender", "document_date", "letter_type", "summary")
ACTION_FIELDS = ("action_id", "source_index", "title", "description", "decision", "assigned_to",
	"assignment_decision", "assignment_reason", "external_date", "external_date_basis", "internal_date",
	"scheduling_decision", "scheduling_reason", "rejection_reason", "needs_planning")
WRITABLE = set(DOCUMENT_FIELDS) | {"applied_run", "fact_evidence_json", "no_actions_reason", "clarification_reason",
	"status", "confirmed_customer", "confirmed_by", "confirmed_at", "review_outcome", "confirmation_receipt_json",
	"replacement_letter", "replaces_letter", "correction_reason", "correction_quarantined", "cancellation_reason"}


def _boundary(operation, extra):
	workflow._http_boundary()
	cmd = extra.pop("cmd", None)
	if extra or cmd not in (None, "kanzlei_erp.posteingang_confirmation." + operation):
		workflow.fail("INVALID_REQUEST")


def _object(value):
	try:
		return parse_json(value) if isinstance(value, str) else parse_json(canonical(value))
	except InvalidResult:
		workflow.fail("INVALID_RESULT")


def _array(value):
	if not value:
		return []
	return _object('{"items":' + value + '}')["items"] if isinstance(value, str) else copy.deepcopy(value)


def _revision(value):
	if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 10:
		value = int(value)
	if type(value) is not int or not 1 <= value <= 2147483647:
		workflow.fail("INVALID_REQUEST")
	return value


def _read_letter(name):
	if not workflow._text(name, 140):
		workflow.fail("INVALID_REQUEST")
	doc = frappe.db.get_value("Incoming Letter", name, "*", as_dict=True)
	if not doc or not letter_access(doc) or not frappe.has_permission("Incoming Letter", "read", doc=name):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	return doc


def _authorized(doc, *, administrative=False):
	actor = frappe.session.user
	allowed = workflow._operator(actor) and letter_access(doc)
	customer = doc.confirmed_customer if administrative else doc.selected_customer
	if customer:
		allowed = allowed and all(frappe.has_permission("Customer", ptype, doc=customer) for ptype in ("read", "write"))
	if administrative:
		allowed = allowed and "System Manager" in frappe.get_roles(actor)
	else:
		allowed = allowed and not doc.correction_quarantined
	if not allowed:
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)


def _lock_related(name):
	if not workflow._text(name, 140):
		workflow.fail("INVALID_REQUEST")
	links = frappe.db.get_value("Incoming Letter", name, ["replaces_letter", "replacement_letter"], as_dict=True)
	names = {name} | {v for v in (links or {}).values() if v}
	# Discovery is read-only. Drop its repeatable-read snapshot before waiting
	# for the first lock; otherwise MariaDB raises 1020 after a competing write.
	if getattr(frappe.local, "request", None) is not None:
		workflow._fresh()
	rows = {key: workflow._locked_letter(key) for key in sorted(names)}
	if any(rows[name].get(field) and rows[name][field] not in rows for field in ("replaces_letter", "replacement_letter")):
		workflow.fail("REVISION_CONFLICT")
	workflow._clear()
	return rows[name]


def _semantic(value):
	value = copy.deepcopy(value)
	if isinstance(value.get("review"), dict):
		value["review"].pop("expected_revision", None)
		actions = value["review"].get("actions")
		if not isinstance(actions, list) or any(not isinstance(row, dict) or not isinstance(row.get("action_id"), str) for row in actions):
			workflow.fail("INVALID_RESULT")
		value["review"]["actions"] = sorted(actions, key=lambda row: row["action_id"])
	return value


def _begin(operation, letter, revision, request_key, payload, *, administrative=False):
	if not workflow._text(request_key, 80):
		workflow.fail("INVALID_REQUEST")
	revision = _revision(revision)
	doc = _lock_related(letter)
	_authorized(doc, administrative=administrative)
	key = sha256(canonical([frappe.session.user, letter, operation, request_key]).encode()).hexdigest()
	fingerprint = sha256(canonical({"operation": operation, "letter": letter, **_semantic(payload)}).encode()).hexdigest()
	old = frappe.db.get_value("Incoming Letter Event", {"operation_key": key}, ["payload_fingerprint", "result_json"], as_dict=True, for_update=True)
	if old:
		if old.payload_fingerprint != fingerprint:
			workflow.fail("IDEMPOTENCY_CONFLICT")
		return doc, (key, fingerprint), {**_object(old.result_json), "replayed": True}
	return doc, (key, fingerprint), None


def _check_revision(doc, revision):
	if doc.revision != _revision(revision):
		workflow.fail("REVISION_CONFLICT")


def _editable(doc):
	if doc.status not in DRAFT_STATES or not frappe.db.get_single_value("Posteingang Settings", "enabled"):
		workflow.fail("INVALID_TRANSITION")


@contextmanager
def _atomic():
	point = "ap07_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(point)
	callbacks = {key: deque(getattr(frappe.db, key)._functions) for key in ("before_commit", "after_commit", "before_rollback", "after_rollback")}
	try:
		yield
	except Exception:
		frappe.db.rollback(save_point=point)
		for key, functions in callbacks.items():
			getattr(frappe.db, key)._functions = functions
		workflow._clear()
		raise


def _actions(letter):
	return frappe.get_all("Incoming Letter Action", filters={"parent": letter, "parenttype": "Incoming Letter"}, fields=["*"], order_by="idx asc")


def _draft(doc):
	actions = []
	for row in _actions(doc.name):
		item = {key: row.get(key) for key in ACTION_FIELDS}
		item["needs_planning"] = bool(item["needs_planning"])
		for key in ("internal_date", "external_date"):
			item[key] = str(item[key]) if item[key] else None
		item["evidence"] = _array(row.evidence_json)
		actions.append(item)
	return {"schema_version": "1.0", "mode": "draft", "expected_revision": doc.revision,
		"selected_customer": doc.selected_customer, "customer_reason": doc.customer_reason,
		"no_actions_reason": doc.no_actions_reason, "clarification_reason": doc.clarification_reason,
		"human_verified": False, "actions": actions}


def _fields(doc):
	return {key: str(doc.get(key)) if key == "document_date" and doc.get(key) else doc.get(key) for key in DOCUMENT_FIELDS}


def _source(doc, run_name=None):
	if not run_name or run_name == doc.current_run:
		value = matching._source(doc)
	else:
		previous = copy.copy(doc)
		previous.current_run = run_name
		run = frappe.db.get_value("Incoming Letter Run", run_name, ["letter", "generation"], as_dict=True)
		if not run or run.letter != doc.name:
			workflow.fail("INVALID_RESULT")
		previous.analysis_generation = run.generation
		value = matching._source(previous)
	return value


def _latest_successful(doc):
	rows = frappe.get_all("Incoming Letter Run", filters={"letter": doc.name, "status": "Succeeded"}, fields=["name"], order_by="generation desc", limit=1)
	return (rows[0].name, _source(doc, rows[0].name)) if rows else (None, None)


def _pages(doc):
	if not doc.applied_run:
		return None
	if _source(doc, doc.applied_run) is None:
		workflow.fail("INVALID_RESULT")
	try:
		return validate_pages(_object(frappe.db.get_value("Incoming Letter Run", doc.applied_run, "ocr_pages_json"))["pages"], doc.page_count)
	except (InvalidResult, KeyError, TypeError):
		workflow.fail("INVALID_RESULT")


def _validated(doc, review, fields, *, confirm=False):
	if review.get("selected_customer") != doc.selected_customer or review.get("customer_reason") != doc.customer_reason:
		workflow.fail("CUSTOMER_CHANGED")
	try:
		return validate_review(review, fields, stored_actions=_actions(doc.name), pages=_pages(doc), page_count=doc.page_count, confirm=confirm)
	except InvalidResult as error:
		workflow.fail(error.code)


def _write_actions(doc, actions, *, replace=False, run=None, proposals=None):
	stored = {row.action_id: row for row in _actions(doc.name)}
	if replace:
		frappe.db.delete("Incoming Letter Action", {"parent": doc.name, "parenttype": "Incoming Letter"})
		stored = {}
	for index, item in enumerate(actions, 1):
		values = {key: item[key] for key in ACTION_FIELDS}
		values.update(idx=index, evidence_json=canonical(item["evidence"]))
		if item["action_id"] in stored:
			frappe.db.set_value("Incoming Letter Action", stored[item["action_id"]].name, values)
		else:
			values.update(parent=doc.name, parenttype="Incoming Letter", parentfield="actions")
			if run:
				values.update(source_run=run, original_proposal_json=canonical(proposals[item["source_index"]]))
			workflow._insert("Incoming Letter Action", values)


def _finish(doc, operation, changes, payload, receipt=None, result=None):
	if set(changes) - WRITABLE:
		raise ValueError("INVALID_SERVER_OPERATION")
	before = doc.revision
	frappe.db.set_value("Incoming Letter", doc.name, {**changes, "revision": before + 1})
	doc.update({**changes, "revision": before + 1})
	event_name = frappe.generate_hash(length=10)
	result = {"schema_version": "1.0", "letter": doc.name, "revision": doc.revision, "status": doc.status,
		"run": doc.current_run, "generation": doc.analysis_generation, **(result or {})}
	if operation == "confirm":
		result["event"] = event_name
		frappe.db.set_value("Incoming Letter", doc.name, "confirmation_receipt_json", canonical(result))
	workflow._insert("Incoming Letter Event", {"name": event_name, "letter": doc.name, "event_type": operation,
		"actor": frappe.session.user, "occurred_at": now_datetime(), "revision_before": before, "revision_after": doc.revision,
		"generation": doc.analysis_generation, "payload_json": canonical(payload),
		**({"operation_key": receipt[0], "payload_fingerprint": receipt[1], "result_json": canonical(result)} if receipt else {})})
	frappe.clear_document_cache("Incoming Letter", doc.name)
	return {**result, "replayed": False}


def _pending(action_id, title, description, *, source_index=None, evidence=None):
	return {"action_id": action_id, "source_index": source_index, "title": title, "description": description,
		"decision": "pending", "assigned_to": None, "assignment_decision": "pending", "assignment_reason": None,
		"external_date": None, "external_date_basis": None, "internal_date": None, "scheduling_decision": "pending",
		"scheduling_reason": None, "rejection_reason": None, "needs_planning": False, "evidence": evidence or []}


@frappe.whitelist(methods=["POST"])
def apply_result(letter=None, expected_run=None, expected_revision=None, request_key=None, acknowledge_replace=False, **extra):
	_boundary("apply_result", extra)
	if acknowledge_replace not in (True, False, "true", "false", 0, 1, "0", "1"):
		workflow.fail("INVALID_REQUEST")
	ack = acknowledge_replace in (True, "true", "1")
	doc, receipt, replay = _begin("apply_result", letter, expected_revision, request_key, {"run": expected_run, "acknowledge_replace": ack})
	if replay:
		return replay
	_check_revision(doc, expected_revision)
	_editable(doc)
	if not expected_run or expected_run != doc.current_run or (source := _source(doc)) is None:
		workflow.fail("STALE_RUN")
	if len(source["actions"]) > frappe.db.get_single_value("Posteingang Settings", "max_actions"):
		workflow.fail("LIMIT_EXCEEDED")
	before = {"review": _draft(doc), "document_fields": _fields(doc), "applied_run": doc.applied_run}
	if (doc.applied_run or before["review"]["actions"] or any(before["document_fields"].values())) and not ack:
		workflow.fail("REPLACE_ACKNOWLEDGEMENT_REQUIRED")
	actions = [_pending(str(uuid4()), action["title"], action["description"], source_index=index, evidence=action["evidence"]) for index, action in enumerate(source["actions"])]
	with _atomic():
		_write_actions(doc, actions, replace=True, run=expected_run, proposals=source["actions"])
		changes = {key: source[key]["value"] for key in DOCUMENT_FIELDS}
		changes.update(applied_run=expected_run, fact_evidence_json=canonical({key: source[key]["evidence"] for key in DOCUMENT_FIELDS}), no_actions_reason=None)
		return _finish(doc, "apply_result", changes, {"before": before, "run": expected_run}, receipt)


@frappe.whitelist(methods=["POST"])
def add_manual_action(letter=None, title=None, description=None, expected_revision=None, request_key=None, **extra):
	_boundary("add_manual_action", extra)
	if not workflow._text(title, 140) or not workflow._text(description, 4000):
		workflow.fail("INVALID_REQUEST")
	doc, receipt, replay = _begin("add_manual_action", letter, expected_revision, request_key, {"title": title, "description": description})
	if replay:
		return replay
	_check_revision(doc, expected_revision)
	_editable(doc)
	rows = _draft(doc)["actions"]
	if len(rows) >= min(30, frappe.db.get_single_value("Posteingang Settings", "max_actions")):
		workflow.fail("LIMIT_EXCEEDED")
	item = _pending(str(uuid4()), title, description)
	with _atomic():
		_write_actions(doc, [*rows, item])
		return _finish(doc, "add_manual_action", {}, {"action": item}, receipt, {"action_id": item["action_id"]})


@frappe.whitelist(methods=["POST"])
def save_review(letter=None, review=None, document_fields=None, request_key=None, **extra):
	_boundary("save_review", extra)
	review, fields = _object(review), _object(document_fields)
	doc, receipt, replay = _begin("save_review", letter, review.get("expected_revision"), request_key, {"review": review, "document_fields": fields})
	if replay:
		return replay
	_check_revision(doc, review["expected_revision"])
	_editable(doc)
	review, fields = _validated(doc, review, fields)
	before = {"review": _draft(doc), "document_fields": _fields(doc)}
	with _atomic():
		_write_actions(doc, review["actions"])
		changes = {**fields, "no_actions_reason": review["no_actions_reason"], "clarification_reason": review["clarification_reason"]}
		if review["mode"] == "clarification":
			changes["status"] = "Needs Clarification"
		return _finish(doc, "save_review", changes, {"before": before, "review": review, "document_fields": fields, "origin": "human"}, receipt)


def _receipt_projection(result):
	result = copy.deepcopy(result)
	if "actions" in result:
		result["actions"] = [{**row, "task": row["task"] if frappe.has_permission("Task", "read", doc=row["task"]) else None} for row in result["actions"]]
	return result


@frappe.whitelist(methods=["POST"])
def confirm(letter=None, review=None, document_fields=None, request_key=None, retained_result=None, **extra):
	_boundary("confirm", extra)
	review, fields = _object(review), _object(document_fields)
	retained = _object(retained_result) if retained_result is not None else None
	payload = {"review": review, "document_fields": fields, "retained_result": retained}
	doc, receipt, replay = _begin("confirm", letter, review.get("expected_revision"), request_key, payload)
	if replay:
		return _receipt_projection(replay)
	if doc.status == "Completed":
		previous = frappe.db.get_value("Incoming Letter Event", {"letter": letter, "event_type": "confirm"}, "payload_fingerprint")
		if previous != receipt[1]:
			workflow.fail("ALREADY_COMPLETED")
		return _receipt_projection({**_object(doc.confirmation_receipt_json), "replayed": True})
	_check_revision(doc, review["expected_revision"])
	_editable(doc)
	if doc.status != "Needs Review":
		workflow.fail("INVALID_TRANSITION")
	review, fields = _validated(doc, review, fields, confirm=True)
	source_run, source = _latest_successful(doc)
	if source is not None:
		applied = _source(doc, doc.applied_run) if doc.applied_run else None
		if any(len(matching._recipients(value)) > 1 for value in (source, applied) if value is not None):
			workflow.fail("MULTIPLE_ADDRESSEES")
		if source_run != doc.applied_run and (not retained or set(retained) != {"run", "reason"} or retained["run"] != source_run or not workflow._text(retained["reason"], 1000)):
			workflow.fail("RESULT_NOT_APPLIED")
	elif retained is not None:
		workflow.fail("INVALID_REQUEST")
	accepted = [item for item in review["actions"] if item["decision"] == "accept"]
	if accepted and not frappe.has_permission("Task", "create"):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	for item in accepted:
		user = item["assigned_to"]
		if user and (not staff(user) or not frappe.has_permission("Customer", "read", doc=doc.selected_customer, user=user)):
			workflow.fail("ASSIGNEE_NOT_ALLOWED")
	if doc.replaces_letter and not _correction_state(doc, lock=True)["ready"]:
		workflow.fail("CORRECTION_REVIEW_REQUIRED")
	before = {"review": _draft(doc), "document_fields": _fields(doc)}
	with _atomic():
		_write_actions(doc, review["actions"])
		timestamp = now_datetime()
		changes = {**fields, "no_actions_reason": review["no_actions_reason"], "clarification_reason": review["clarification_reason"],
			"status": "Completed", "confirmed_customer": doc.selected_customer, "confirmed_by": frappe.session.user,
			"confirmed_at": timestamp, "review_outcome": "actions" if accepted else "no_actions"}
		# Frappe appends modified/modified_by to a supplied mapping in place.
		frappe.db.set_value("Incoming Letter", doc.name, dict(changes))
		frappe.clear_document_cache("Incoming Letter", doc.name)
		created = []
		try:
			from frappe.desk.form import assign_to

			from kanzlei_erp.posteingang_task_access import action_key, creating_task

			for item in accepted:
				with creating_task(doc.name, item["action_id"]):
					task = frappe.get_doc({"doctype": "Task", "subject": item["title"], "description": frappe.utils.escape_html(item["description"]),
						"kanzlei_customer": doc.selected_customer, "kanzlei_work_kind": "Task", "exp_start_date": item["internal_date"], "exp_end_date": item["internal_date"],
						"kanzlei_incoming_letter": doc.name, "kanzlei_incoming_action_id": item["action_id"],
						"kanzlei_incoming_action_key": action_key(doc.name, item["action_id"]), "kanzlei_incoming_needs_planning": item["needs_planning"]}).insert()
				row = next(row for row in _actions(doc.name) if row.action_id == item["action_id"])
				frappe.db.set_value("Incoming Letter Action", row.name, "created_task", task.name)
				if item["assigned_to"]:
					assign_to.add({"doctype": "Task", "name": task.name, "assign_to": [item["assigned_to"]], "description": frappe._("Posteingang task assignment"), "date": item["internal_date"]})
				created.append({"action_id": item["action_id"], "task": task.name})
		except Exception:
			workflow.fail("TASK_CREATION_FAILED")
		_authorized(doc)
		return _finish(doc, "confirm", changes, {"before": before, **payload}, receipt,
			{"confirmed_customer": doc.selected_customer, "confirmed_by": frappe.session.user, "confirmed_at": str(timestamp), "actions": created})


def _correction_state(doc, *, lock=False):
	if not doc.replaces_letter:
		return None
	old = frappe.db.get_value("Incoming Letter", doc.replaces_letter, "*", as_dict=True)
	if not old or not old.correction_quarantined or old.replacement_letter != doc.name:
		workflow.fail("INVALID_RESULT")
	rows, ready = [], True
	for row in sorted(_actions(old.name), key=lambda a: a.created_task or ""):
		if not row.created_task:
			continue
		status = frappe.db.get_value("Task", row.created_task, "status", for_update=lock)
		events = frappe.get_all("Incoming Letter Event", filters={"letter": doc.name, "event_type": "correction_disposition"}, fields=["payload_json"], order_by="revision_after desc")
		decision = next((_object(event.payload_json) for event in events if _object(event.payload_json).get("original_action_id") == row.action_id), None)
		valid = bool(decision and status and decision.get("task_status") == status and (decision["disposition"] == "historical" or status == "Cancelled"))
		ready &= valid
		rows.append({"action_id": row.action_id, "task": row.created_task, "status": status,
			"disposition": decision.get("disposition") if decision else None, "reason": decision.get("reason") if decision else None})
	result = {"ready": bool(ready)}
	if letter_access(old) and "System Manager" in frappe.get_roles() and workflow._operator(frappe.session.user):
		result.update(original_letter=old.name, tasks=[row for row in rows if frappe.has_permission("Task", "read", doc=row["task"])])
	return result


@frappe.whitelist(methods=["GET", "POST"])
def get_review_state(letter=None, **extra):
	_boundary("get_review_state", extra)
	doc = _read_letter(letter)
	return review_projection(doc)


def review_projection(doc):
	"""Read the review within the caller's snapshot; public guards remain outside."""
	selected = matching._customer(doc.selected_customer)
	review = _draft(doc)
	if not selected:
		review.update(selected_customer=None, customer_reason=None)
	source_run, source = _latest_successful(doc)
	allowed = []
	try:
		_authorized(doc)
		if doc.status in DRAFT_STATES and frappe.db.get_single_value("Posteingang Settings", "enabled"):
			allowed += ["add_manual_action", "save_review"]
			if source is not None and source_run == doc.current_run:
				allowed.append("apply_result")
			if doc.status == "Needs Review":
				allowed.append("confirm")
	except frappe.PermissionError:
		pass
	if doc.status == "Completed":
		try:
			_authorized(doc, administrative=True)
			allowed.append("create_correction")
		except frappe.PermissionError:
			pass
	correction = _correction_state(doc)
	if correction and "tasks" in correction and doc.status in {"Received", "Needs Review", "Needs Clarification", "Error"}:
		allowed.append("correction_disposition")
	file = frappe.db.get_value("File", doc.original_file, ["name", "file_url", "file_name"], as_dict=True)
	return {"schema_version": "1.0", "letter": doc.name, "revision": doc.revision, "status": doc.status,
		"document_fields": _fields(doc), "review": review, "proposal": {"run": source_run, "extraction": source} if source is not None else None,
		"applied_run": doc.applied_run, "selected_customer": selected, "allowed_operations": allowed,
		"receipt": _receipt_projection(_object(doc.confirmation_receipt_json)) if doc.confirmation_receipt_json else None,
		"correction": correction, "original_file": file,
		"action_proposals": {row.action_id: _object(row.original_proposal_json) for row in _actions(doc.name) if row.original_proposal_json}}


@frappe.whitelist(methods=["GET", "POST"])
def search_assignees(letter=None, text="", **extra):
	_boundary("search_assignees", extra)
	doc = _read_letter(letter)
	_authorized(doc)
	if not isinstance(text, str) or len(text) > 140:
		workflow.fail("INVALID_REQUEST")
	if not doc.selected_customer:
		return {"rows": []}
	rows = []
	for user in frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, fields=["name", "full_name"], order_by="name asc"):
		if text.casefold() not in (user.name + " " + (user.full_name or "")).casefold():
			continue
		if staff(user.name) and frappe.has_permission("Customer", "read", doc=doc.selected_customer, user=user.name):
			rows.append({"user": user.name, "full_name": user.full_name})
	return {"rows": rows[:20]}


def _boolean(value):
	if value is True or value == "true":
		return True
	if value is False or value == "false":
		return False
	workflow.fail("INVALID_REQUEST")


@frappe.whitelist(methods=["POST"])
def create_correction(old_letter=None, expected_revision=None, request_key=None, reason=None,
	reuse_original=True, received_date_known=None, received_date=None, **extra):
	"""Own the HTTP commit: core clears rollback callbacks before SQL COMMIT."""
	from kanzlei_erp.object_storage import isolated_writes
	from kanzlei_erp.posteingang_model import _cleanup_correction_file

	kwargs = dict(old_letter=old_letter, expected_revision=expected_revision, request_key=request_key,
		reason=reason, reuse_original=reuse_original, received_date_known=received_date_known,
		received_date=received_date, **extra)
	if getattr(frappe.local, "request", None) is None:
		return _create_correction(**kwargs)
	frappe.local.ap07_correction_file = None
	result = None
	with isolated_writes(cleanup_on_error=False) as cleanups:
		try:
			result = _create_correction(**kwargs)
			frappe.db.commit()
			return result
		except Exception as error:
			# A lost connection after COMMIT is not proof of rollback. Establish
			# the durable receipt before removing bytes. An unavailable DB leaves
			# bytes intact, matching the intake's unknown-outcome policy.
			try:
				frappe.db.rollback()
				key = sha256(canonical([frappe.session.user, old_letter, "create_correction", request_key]).encode()).hexdigest()
				durable = frappe.db.get_value("Incoming Letter Event", {"operation_key": key}, "result_json")
			except Exception:
				workflow.fail("CORRECTION_FAILED")
			if result and durable:
				return _correction_receipt({**_object(durable), "replayed": bool(result.get("replayed"))})
			if created := frappe.local.ap07_correction_file:
				_cleanup_correction_file(created)
			for cleanup in cleanups:
				cleanup()
			if isinstance(error, (workflow.WorkflowError, frappe.PermissionError)):
				raise
			workflow.fail("CORRECTION_FAILED")


def _correction_receipt(result):
	result = copy.deepcopy(result)
	name = result.get("replacement")
	if name and not frappe.has_permission("Incoming Letter", "read", doc=name):
		result["replacement"] = None
	return result


def _create_correction(old_letter=None, expected_revision=None, request_key=None, reason=None,
	reuse_original=True, received_date_known=None, received_date=None, **extra):
	_boundary("create_correction", extra)
	from kanzlei_erp.object_storage import isolated_writes, open_file
	from kanzlei_erp.posteingang import _filename, _receipt_date
	from kanzlei_erp.posteingang_model import _cleanup_correction_file, create_correction_letter
	from kanzlei_erp.posteingang_pdf import validate_pdf

	if not workflow._text(reason, 1000):
		workflow.fail("INVALID_REQUEST")
	reuse = _boolean(reuse_original)
	date = _receipt_date(received_date_known, received_date)
	preliminary = _read_letter(old_letter)
	_authorized(preliminary, administrative=True)
	settings = frappe.get_single("Posteingang Settings")
	max_bytes, max_pages = min(settings.max_file_bytes, 20971520), min(settings.max_pages, 50)
	request = getattr(frappe.local, "request", None)
	files = list(request.files.items(multi=True)) if request else []
	if reuse:
		if files:
			workflow.fail("INVALID_REQUEST")
		file = frappe.get_doc("File", preliminary.original_file)
		with open_file(file) as stream:
			content = stream.read(max_bytes + 1)
		pages, filename = preliminary.page_count, file.file_name
		if sha256(content).hexdigest() != preliminary.content_sha256:
			workflow.fail("INVALID_RESULT")
	else:
		if len(files) != 1 or files[0][0] != "file":
			workflow.fail("INVALID_REQUEST")
		content = files[0][1].stream.read(max_bytes + 1)
		filename = _filename(files[0][1].filename)
		pages = None
	if not content or len(content) > max_bytes:
		workflow.fail("FILE_TOO_LARGE" if content else "INVALID_REQUEST")
	if not reuse:
		try:
			pages = validate_pdf(content, max_pages)
		except ValueError as error:
			workflow.fail(str(error))
	if not 1 <= pages <= max_pages:
		workflow.fail("PAGE_LIMIT_EXCEEDED")
	digest = sha256(content).hexdigest()
	payload = {"reason": reason, "reuse_original": reuse, "content_sha256": digest, **date}
	doc, receipt, replay = _begin("create_correction", old_letter, expected_revision, request_key, payload, administrative=True)
	if replay:
		return _correction_receipt(replay)
	_check_revision(doc, expected_revision)
	if doc.replacement_letter:
		workflow.fail("CORRECTION_EXISTS")
	if doc.status != "Completed" or doc.correction_quarantined:
		workflow.fail("INVALID_TRANSITION")
	created = None
	try:
		with isolated_writes(), _atomic():
			new = create_correction_letter(old_letter=doc.name, actor=frappe.session.user,
				original={"content": content, "file_name": filename, "page_count": pages}, receipt=date,
				operation={"key": receipt[0], "fingerprint": receipt[1]})
			created = new._intake_file
			frappe.local.ap07_correction_file = created
			newrow = frappe.db.get_value("Incoming Letter", new.name, "*", as_dict=True)
			_finish(newrow, "correction_created", {"replaces_letter": doc.name, "correction_reason": reason}, {"original": doc.name, "reason": reason})
			return _correction_receipt(_finish(doc, "create_correction", {"status": "Cancelled", "correction_quarantined": 1,
				"replacement_letter": new.name, "correction_reason": reason, "cancellation_reason": reason}, payload,
				receipt, {"replacement": new.name}))
	except Exception:
		if created:
			_cleanup_correction_file(created)
		raise


@frappe.whitelist(methods=["POST"])
def correction_disposition(replacement=None, expected_revision=None, original_action_id=None,
	disposition=None, reason=None, request_key=None, **extra):
	_boundary("correction_disposition", extra)
	if disposition not in ("cancelled", "historical") or not workflow._text(reason, 1000) or not workflow._text(original_action_id, 36):
		workflow.fail("INVALID_REQUEST")
	payload = {"original_action_id": original_action_id, "disposition": disposition, "reason": reason}
	doc, receipt, replay = _begin("correction_disposition", replacement, expected_revision, request_key, payload)
	if not doc.replaces_letter:
		workflow.fail("INVALID_TRANSITION")
	old = workflow._locked_letter(doc.replaces_letter)
	_authorized(old, administrative=True)
	if replay:
		return replay
	if doc.status not in {"Received", "Needs Review", "Needs Clarification", "Error"}:
		workflow.fail("INVALID_TRANSITION")
	_check_revision(doc, expected_revision)
	if not old.correction_quarantined or old.replacement_letter != doc.name:
		workflow.fail("INVALID_RESULT")
	row = next((row for row in _actions(old.name) if row.action_id == original_action_id and row.created_task), None)
	if not row or not frappe.has_permission("Task", "read", doc=row.created_task):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	status = frappe.db.get_value("Task", row.created_task, "status", for_update=True)
	if disposition == "cancelled" and status != "Cancelled":
		workflow.fail("CORRECTION_REVIEW_REQUIRED")
	payload.update(task=row.created_task, task_status=status, replacement=doc.name)
	with _atomic():
		_finish(old, "correction_disposition", {}, payload)
		return _finish(doc, "correction_disposition", {}, payload, receipt)
