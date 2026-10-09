"""Staff-managed Rückfragen on ordinary Tasks; no mail is sent by this module."""

import hashlib
import json

import frappe
from frappe.utils import cint, get_datetime, getdate, now_datetime, nowdate

from kanzlei_erp.fibu_materials import _check_communication, _check_file

STATES = {"Draft": "Open", "Waiting": "Working", "Answer Received": "Pending Review",
	"Resolved": "Completed", "Cancelled": "Cancelled"}
TERMINAL = ("Resolved", "Cancelled")
PREFIX = "kanzlei_question_"
FIELDS = ("state", "responsible", "recipient", "entry_key", "file", "operation", "requested_on",
	"expected_response_on", "answered_on", "remaining", "reminder_on", "reminder_cycle",
	"notified_cycle", "decision", "decided_by", "decided_at", "creation_key")
EDITABLE = ("subject", "description", "responsible", "recipient", "entry_key", "file", "operation",
	"expected_response_on", "reminder_on")
IDENTITY = ("kanzlei_customer", "kanzlei_fibu_package", "kanzlei_fibu_supplement", "kanzlei_work_kind")
EVENT_FIELDS = ("action", "before_json", "after_json", "details_json", "communication", "recorded_by", "recorded_at")


def _error(message):
	frappe.throw(frappe._(message))


def _dict(value):
	value = frappe.parse_json(value) if isinstance(value, str) else value
	if not isinstance(value or {}, dict):
		_error("Question values must be an object")
	return value or {}


def _snapshot(task):
	values = {field: task.get(PREFIX + field) or "" for field in FIELDS}
	values.update(subject=task.subject or "", description=task.description or "")
	return json.loads(frappe.as_json(values))


def _events(task):
	return [json.loads(frappe.as_json({f: row.get(f) or "" for f in (*EVENT_FIELDS, "name", "parent", "parenttype", "parentfield", "idx")}))
		for row in task.get(PREFIX + "events", [])]


def _context(task):
	if task.is_new() and task.get("kanzlei_fibu_supplement") and not task.get("kanzlei_fibu_package"):
		task.kanzlei_fibu_package = frappe.db.get_value("FiBu Supplement", task.kanzlei_fibu_supplement, "package")
	if not task.get("kanzlei_fibu_package"):
		_error("Managed questions require a FiBu period")
	package = frappe.get_doc("FiBu Package", task.kanzlei_fibu_package, for_update=True)
	context = frappe.get_doc("FiBu Supplement", task.kanzlei_fibu_supplement, for_update=True) if task.get("kanzlei_fibu_supplement") else package
	if context.doctype == "FiBu Supplement" and context.package != package.name:
		_error("The supplement belongs to another FiBu period")
	return package, context


def _locked_doc(doctype, name, background=False):
	try:
		return frappe.get_doc(doctype, name, for_update=True)
	except frappe.QueryDeadlockError:
		if background:
			raise
		_error("This record changed. Reload it before applying the workflow action")


def _lock_context(package_name, supplement_name="", check_permission=True):
	package = _locked_doc("FiBu Package", package_name, background=not check_permission)
	context = package
	if supplement_name:
		context = _locked_doc("FiBu Supplement", supplement_name, background=not check_permission)
		if context.package != package.name:
			_error("The supplement belongs to another FiBu period")
	if check_permission:
		package.check_permission("read")
		context.check_permission("write")
	return package, context


def _lock_task(name, expected_modified=None, background=False):
	task = frappe.get_doc("Task", name)
	if not background:
		task.check_permission("write")
	package, context = _lock_context(task.kanzlei_fibu_package, task.get("kanzlei_fibu_supplement"), not background)
	identity = tuple(task.get(f) or "" for f in IDENTITY)
	task = _locked_doc("Task", name, background)
	if identity != tuple(task.get(f) or "" for f in IDENTITY):
		_error("This record changed. Reload it before applying the workflow action")
	if not background:
		task.check_permission("write")
		if not expected_modified or get_datetime(task.modified) != get_datetime(expected_modified):
			_error("This record changed. Reload it before applying the workflow action")
	if context.status != "Open":
		_error("Questions in a closed FiBu context cannot be changed")
	return task, package, context


def _responsible_ok(task, user):
	if not task.get("kanzlei_fibu_package"):
		return False
	return bool(user and frappe.db.get_value("User", user, "enabled")
		and frappe.db.get_value("User", user, "user_type") == "System User"
		and frappe.has_permission("Customer", "read", doc=task.kanzlei_customer, user=user)
		and frappe.has_permission("FiBu Package", "read", doc=task.kanzlei_fibu_package, user=user)
		and (not task.get("kanzlei_fibu_supplement") or frappe.has_permission("FiBu Supplement", "read", doc=task.kanzlei_fibu_supplement, user=user))
		and frappe.has_permission("Task", "read", doc=task, user=user))


def _validate_links(task, package, context):
	if task.kanzlei_customer != package.customer:
		_error("Task Mandant does not match the FiBu package Mandant")
	if not _responsible_ok(task, task.kanzlei_question_responsible):
		_error("The question responsible must be an enabled employee with access to this period")
	if task.kanzlei_question_entry_key and not any(
		row.entry_key == task.kanzlei_question_entry_key for row in context.get("checklist", [])):
		_error("The checklist source belongs to another work context")
	if task.kanzlei_question_recipient:
		contact = frappe.get_doc("Contact", task.kanzlei_question_recipient)
		contact.check_permission("read")
		if not any(row.link_doctype == "Customer" and row.link_name == package.customer for row in contact.links):
			_error("The question recipient must be linked to this Mandant")
	if task.kanzlei_question_file:
		_check_file(task.kanzlei_question_file)


def validate_question_assignment(todo, method=None):
	if todo.reference_type != "Task" or todo.status != "Open" or not todo.reference_name:
		return
	task = frappe.get_doc("Task", todo.reference_name)
	if task.get(PREFIX + "state") and not _responsible_ok(task, todo.allocated_to):
		_error("Question participants must be enabled employees with access to this period")


def validate_question_share(share, method=None):
	if share.share_doctype != "Task" or not share.share_name:
		return
	task = frappe.get_doc("Task", share.share_name)
	if task.get(PREFIX + "state") and (share.everyone or not _responsible_ok(task, share.user)):
		_error("Question participants must be enabled employees with access to this period")


def _validate_legacy_participants(task):
	users = frappe.get_all("ToDo", filters={"reference_type": "Task", "reference_name": task.name,
		"status": "Open"}, pluck="allocated_to")
	for share in frappe.get_all("DocShare", filters={"share_doctype": "Task", "share_name": task.name}, fields=["user", "everyone"]):
		if share.everyone:
			_error("Remove question assignments or shares without period access before initialization")
		users.append(share.user)
	if any(not _responsible_ok(task, user) for user in set(users)):
		_error("Remove question assignments or shares without period access before initialization")


def _set_values(task, values):
	for field in EDITABLE:
		if field in values:
			name = field if field in ("subject", "description") else PREFIX + field
			value = values[field] or ""
			if field.endswith("_on") and value:
				value = str(getdate(value))
			task.set(name, value)
	if not (task.subject or "").strip():
		_error("Question subject is required")


def _event(task, action, before, details=None, communication=""):
	task.append(PREFIX + "events", {"action": action, "before_json": frappe.as_json(before),
		"after_json": frappe.as_json(_snapshot(task)), "details_json": frappe.as_json(details or {}),
		"communication": communication, "recorded_by": frappe.session.user, "recorded_at": now_datetime()})


def _cycle(task, before):
	after = _snapshot(task)
	if any(before.get(field, "") != after[field] for field in ("reminder_on", "responsible")):
		task.kanzlei_question_reminder_cycle = frappe.generate_hash(length=24) if task.kanzlei_question_reminder_on else ""
		task.kanzlei_question_notified_cycle = ""


def _initialize(task, context, values):
	for field in FIELDS:
		task.set(PREFIX + field, "")
	task.set(PREFIX + "events", [])
	task.kanzlei_question_responsible = values.get("responsible") or context.responsible
	task.kanzlei_question_state = "Draft"
	task.status = "Open"
	task.completed_by = ""
	task.completed_on = None
	task.progress = 0
	_set_values(task, values)
	_cycle(task, {})
	_event(task, "Initialized", {})


def prepare_question(task, method=None):
	"""Normalize new copies; legacy untracked questions remain untracked."""
	if not task.is_new() or task.get("kanzlei_work_kind") != "Question":
		return
	package, context = _context(task)
	context.check_permission("write")
	if context.status != "Open":
		_error("Questions in a closed FiBu context cannot be changed")
	_initialize(task, context, task.flags.question_values or {})
	task.kanzlei_customer = package.customer
	task.flags.question_action = True


def finish_question_creation(task, method=None):
	task.flags.question_action = False
	task.flags.question_values = None


def validate_question(task, method=None):
	previous = task.get_doc_before_save() if not task.is_new() else None
	managed = bool(task.get(PREFIX + "state") or (previous and previous.get(PREFIX + "state")))
	if not managed:
		if task.get(PREFIX + "events") or any(task.get(PREFIX + f) for f in FIELDS):
			_error("Use the question actions to change question management")
		return
	if task.kanzlei_work_kind != "Question":
		_error("A managed question cannot be changed to an ordinary Task")
	if task.is_template or task.status == "Template":
		_error("Managed questions cannot be Task templates")
	if previous and any((previous.get(f) or "") != (task.get(f) or "") for f in IDENTITY):
		_error("The context of a managed question cannot be changed")
	if not task.flags.question_action:
		if not previous or _snapshot(task) != _snapshot(previous) or _events(task) != _events(previous):
			_error("Use the question actions to change question management")
		if task.status != previous.status and (task.status in ("Completed", "Cancelled")
			or previous.status in ("Completed", "Cancelled")):
			_error("Use the question actions to change question management")
		if previous and any(task.get(f) != previous.get(f) for f in ("completed_by", "completed_on")):
			_error("Use the question actions to change question management")
		return
	package, context = _context(task)
	if context.status != "Open":
		_error("Questions in a closed FiBu context cannot be changed")
	_validate_links(task, package, context)


def protect_question(task, method=None):
	if task.get(PREFIX + "state"):
		_error("Managed questions cannot be deleted; cancel the question with a reason")


def _save(task):
	task.flags.question_action = True
	try:
		task.save()
	finally:
		task.flags.question_action = False


@frappe.whitelist()
def create_question(context_doctype, context_name, values=None):
	values = _dict(values)
	if context_doctype not in ("FiBu Package", "FiBu Supplement"):
		_error("Choose a FiBu period or supplement")
	context = frappe.get_doc(context_doctype, context_name)
	package_name = context.name if context_doctype == "FiBu Package" else context.package
	package, context = _lock_context(package_name, context.name if context_doctype == "FiBu Supplement" else "")
	if context.status != "Open":
		_error("Questions in a closed FiBu context cannot be changed")
	frappe.has_permission("Task", "create", throw=True)
	key = ""
	if values.get("creation_key"):
		key = hashlib.sha256(f"{frappe.session.user}:{context.doctype}:{context.name}:{values['creation_key']}".encode()).hexdigest()
		# A locking/current read sees the first request's committed Task even when
		# this transaction took its repeatable-read snapshot before waiting.
		try:
			found = frappe.db.sql("SELECT name FROM `tabTask` WHERE kanzlei_question_creation_key=%s FOR UPDATE", key)
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			_error("This record changed. Reload it before applying the workflow action")
		existing = found[0][0] if found else None
		if existing:
			frappe.get_doc("Task", existing).check_permission("read")
			return existing
	task = frappe.get_doc({"doctype": "Task", "subject": values.get("subject"), "kanzlei_work_kind": "Question",
		"kanzlei_customer": package.customer, "kanzlei_fibu_package": package.name,
		"kanzlei_fibu_supplement": context.name if context.doctype == "FiBu Supplement" else ""})
	task.flags.question_create = True
	task.flags.question_values = values
	task.insert()
	if key:
		task.kanzlei_question_creation_key = key
		# The idempotency key is internal bookkeeping, not a second user action.
		task.kanzlei_question_events[0].after_json = frappe.as_json(_snapshot(task))
		_save(task)
	return task.name


@frappe.whitelist()
def update_question(name, action, values=None, expected_modified=""):
	values = _dict(values)
	task, package, context = _lock_task(name, expected_modified)
	if task.kanzlei_work_kind != "Question":
		_error("Choose a Question Task")
	state = task.get(PREFIX + "state")
	if action == "initialize":
		if state or task.status in ("Completed", "Cancelled"):
			_error("Only an open historical question can be initialized")
		_validate_legacy_participants(task)
		_initialize(task, context, values)
		_save(task)
		return task.name
	if not state or action not in ("edit", "request", "response", "resolve", "cancel", "reopen"):
		_error("Initialize the question before using its actions")
	if state in TERMINAL and action != "reopen":
		_error("Reopen the question before changing it")
	before = _snapshot(task)
	communication = values.get("communication") or ""
	if communication:
		_check_communication(communication)
	if action == "edit":
		_set_values(task, values)
	elif action == "request":
		_set_values(task, values)
		if not task.kanzlei_question_recipient or not values.get("requested_on") or not task.kanzlei_question_expected_response_on:
			_error("Recipient, request date and expected response date are required")
		if not communication and not (values.get("note") or "").strip():
			_error("Link a communication or describe the request")
		task.kanzlei_question_requested_on = str(getdate(values["requested_on"]))
		if getdate(task.kanzlei_question_expected_response_on) < getdate(task.kanzlei_question_requested_on):
			_error("Expected response date cannot be before the request date")
		task.kanzlei_question_state = "Waiting"
	elif action == "response":
		if state not in ("Waiting", "Answer Received"):
			_error("Record a request before recording its answer")
		if not values.get("answered_on") or not (values.get("answer") or "").strip():
			_error("Answer date and content are required")
		if cint(values.get("is_partial")) and not (values.get("remaining") or "").strip():
			_error("Describe what remains unanswered")
		task.kanzlei_question_answered_on = str(getdate(values["answered_on"]))
		if getdate(task.kanzlei_question_answered_on) < getdate(task.kanzlei_question_requested_on):
			_error("Answer date cannot be before the request date")
		task.kanzlei_question_remaining = values.get("remaining", "") if cint(values.get("is_partial")) else ""
		task.kanzlei_question_reminder_on = ""
		task.kanzlei_question_state = "Answer Received"
	elif action in ("resolve", "cancel"):
		decision = (values.get("decision" if action == "resolve" else "reason") or "").strip()
		if not decision:
			_error("A decision or reason is required")
		task.kanzlei_question_decision = decision
		task.kanzlei_question_decided_by = frappe.session.user
		task.kanzlei_question_decided_at = now_datetime()
		task.kanzlei_question_reminder_on = ""
		task.kanzlei_question_state = "Resolved" if action == "resolve" else "Cancelled"
		if action == "resolve":
			task.completed_by = frappe.session.user
			task.completed_on = nowdate()
	elif action == "reopen":
		if state not in TERMINAL or not (values.get("reason") or "").strip():
			_error("A closed question and reopening reason are required")
		for field in ("decision", "decided_by", "decided_at", "requested_on", "answered_on", "remaining", "expected_response_on", "reminder_on"):
			task.set(PREFIX + field, "")
		task.completed_by = ""
		task.completed_on = None
		task.progress = 0
		task.kanzlei_question_state = "Draft"
	if action != "edit":
		task.status = STATES[task.kanzlei_question_state]
	_cycle(task, before)
	# Persist only documented payload fields, never arbitrary submitted metadata.
	details = {f: values[f] for f in (*EDITABLE, "requested_on", "answered_on", "answer", "is_partial", "remaining", "decision", "reason", "note") if f in values}
	_event(task, action.title(), before, details, communication)
	_save(task)
	if task.kanzlei_question_state in TERMINAL and context.next_action_task == task.name:
		frappe.msgprint(frappe._("This question was the next action of the period. Update the period work context."), alert=True)
	return task.name


@frappe.whitelist()
def set_project_status(project, status):
	"""ERPNext's bulk Project action bypasses Task.save; protect managed history."""
	if status not in ("Completed", "Cancelled"):
		_error("Status must be Cancelled or Completed")
	doc = frappe.get_doc("Project", project)
	doc.check_permission("write")
	if frappe.db.exists("Task", {"project": project, PREFIX + "state": ["!=", ""]}):
		_error("Use the question actions to change question management")
	for task in frappe.get_all("Task", filters={"project": project}, pluck="name"):
		# Also exclude a question linked concurrently after the initial guard.
		frappe.db.set_value("Task", {"name": task, "project": project, PREFIX + "state": ["is", "not set"]}, "status", status)
	doc.status = status
	doc.save()


@frappe.whitelist()
def get_questions(filters=None, start=0, page_length=20):
	filters = _dict(filters)
	query = {"kanzlei_work_kind": "Question"}
	for field, name in (("customer", "kanzlei_customer"), ("package", "kanzlei_fibu_package"),
		("supplement", "kanzlei_fibu_supplement"), ("responsible", PREFIX + "responsible"), ("state", PREFIX + "state")):
		if filters.get(field):
			query[name] = filters[field]
	active_only = not filters.get("state") and not cint(filters.get("include_closed"))
	if cint(filters.get("overdue")):
		query[PREFIX + "state"] = "Waiting"
		query[PREFIX + "expected_response_on"] = ["<", nowdate()]
	if cint(filters.get("reminder_due")):
		query[PREFIX + "reminder_on"] = ["between", ["1900-01-01", nowdate()]]
	if active_only:
		from frappe.query_builder.functions import Coalesce
		table = frappe.qb.DocType("Task")
		query = [query, table.kanzlei_question_state.isin(["Draft", "Waiting", "Answer Received"])
			| ((Coalesce(table.kanzlei_question_state, "") == "") & table.status.notin(["Completed", "Cancelled"]))]
	fields = ["name", "subject", "status", "modified", "kanzlei_customer", "kanzlei_fibu_package", "kanzlei_fibu_supplement"]
	fields += [PREFIX + f for f in ("state", "responsible", "recipient", "expected_response_on", "reminder_on", "remaining")]
	rows = frappe.qb.get_query("Task", filters=query, fields=fields, order_by="modified desc",
		offset=max(0, cint(start)), limit=min(100, max(1, cint(page_length))), ignore_permissions=False).run(as_dict=True)
	total = frappe.qb.get_query("Task", filters=query, fields=[{"COUNT": "name", "as": "total"}], ignore_permissions=False).run(as_dict=True)[0].total
	counts = frappe.qb.get_query("Task", filters=query, fields=[PREFIX + "state", {"COUNT": "name", "as": "count"}], group_by=PREFIX + "state", ignore_permissions=False).run(as_dict=True)
	for row in rows:
		task = frappe.get_doc("Task", row.name)
		row.context_title = frappe.db.get_value("FiBu Supplement" if task.kanzlei_fibu_supplement else "FiBu Package",
			task.kanzlei_fibu_supplement or task.kanzlei_fibu_package, "display_title")
		row.reminder_issue = not _responsible_ok(task, task.get(PREFIX + "responsible")) if task.get(PREFIX + "state") else False
	return {"rows": rows, "total": total,
		"summary": {row.get(PREFIX + "state") or "Historical": row.count for row in counts}}


def send_question_reminders():
	"""Atomic per-cycle notifications; Alert is an in-app-only Frappe type."""
	names = frappe.get_all("Task", filters={PREFIX + "state": ["in", ["Draft", "Waiting", "Answer Received"]],
		PREFIX + "reminder_on": ["between", ["1900-01-01", nowdate()]]}, pluck="name", order_by="name asc")
	for name in names:
		frappe.db.savepoint("question_reminder")
		try:
			task, _, _ = _lock_task(name, background=True)
			cycle = task.get(PREFIX + "reminder_cycle")
			if (task.kanzlei_question_state in TERMINAL or not task.kanzlei_question_reminder_on
				or getdate(task.kanzlei_question_reminder_on) > getdate(nowdate())
				or not cycle or cycle == task.kanzlei_question_notified_cycle
				or not _responsible_ok(task, task.kanzlei_question_responsible)):
				continue
			user = task.kanzlei_question_responsible
			language = frappe.db.get_value("User", user, "language") or frappe.get_system_settings("language")
			frappe.get_doc({"doctype": "Notification Log", "type": "Alert", "for_user": user,
				"document_type": "Task", "document_name": task.name,
				"subject": frappe._("Question reminder", lang=language),
				"email_content": frappe._("Open the question to review the next action.", lang=language)}).insert(ignore_permissions=True)
			before = _snapshot(task)
			task.kanzlei_question_notified_cycle = cycle
			_event(task, "Reminder Sent", before, {"responsible": user, "reminder_on": str(task.kanzlei_question_reminder_on)})
			_save(task)
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as error:
			# Snapshot conflicts also invalidate savepoints on MariaDB. Ask Frappe's
			# job runner to roll back/retry the entire atomic batch (up to five times).
			raise frappe.RetryBackgroundJobError from error
		except (frappe.DoesNotExistError, frappe.ValidationError, frappe.PermissionError):
			frappe.db.rollback(save_point="question_reminder")
			frappe.log_error(title="Question reminder failed")
