"""Mandatory provenance/Customer guards for AP-07 Tasks and their projections.

No permission is granted here: True only means that the additional guard allows
Frappe's existing role, FiBu and document checks to continue.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256

import frappe

from kanzlei_erp.posteingang_permissions import letter_access, names_condition, staff

PROVENANCE = ("kanzlei_incoming_letter", "kanzlei_incoming_action_id", "kanzlei_incoming_action_key")
IMMUTABLE = (*PROVENANCE, "kanzlei_customer")
_creation = ContextVar("posteingang_task_creation", default=None)
_checking = ContextVar("posteingang_task_permission_graph", default=None)
REFERENCES = {
	"ToDo": ("reference_type", "reference_name"),
	"Notification Log": ("document_type", "document_name"),
	"Comment": ("reference_doctype", "reference_name"),
	"Communication": ("reference_doctype", "reference_name"),
	"Version": ("ref_doctype", "docname"),
	"File": ("attached_to_doctype", "attached_to_name"),
	"DocShare": ("share_doctype", "share_name"),
}


def deny():
	frappe.throw(frappe._("Posteingang task operation is not permitted"), frappe.PermissionError)


def ready():
	return frappe.db.has_column("Task", "kanzlei_incoming_letter")


def action_key(letter_name, action_id):
	return sha256((letter_name + "\0" + action_id).encode()).hexdigest()


@contextmanager
def creating_task(letter_name, action_id):
	token = _creation.set((letter_name, action_id))
	try:
		yield
	finally:
		_creation.reset(token)


def _row(name):
	if not name or not ready():
		return None
	return frappe.db.get_value(
		"Task", name, ["name", *PROVENANCE, "kanzlei_customer", "parent_task", "exp_end_date"], as_dict=True
	)


def source(task):
	if isinstance(task, str):
		task = _row(task)
	if not task or not any(task.get(k) for k in PROVENANCE):
		return None
	return (
		frappe.db.get_value("Incoming Letter", task.get("kanzlei_incoming_letter"), "*", as_dict=True)
		or False
	)


def _additional(task, user, seen=None):
	if isinstance(task, str):
		task = _row(task)
	if not task:
		return True
	seen = set() if seen is None else seen
	if task.name in seen:
		return True
	seen.add(task.name)
	letter = source(task)
	if letter is not None:
		if not letter or not staff(user) or not all(task.get(k) for k in PROVENANCE):
			return False
		if task.kanzlei_customer != letter.confirmed_customer or not letter_access(letter, user):
			return False
		if task.kanzlei_incoming_action_key != action_key(letter.name, task.kanzlei_incoming_action_id):
			return False
	# A readable parent must not expose cached subjects of a hidden dependency.
	refs = [task.get("parent_task")]
	if task.name and frappe.db.table_exists("Task Depends On"):
		refs += frappe.get_all("Task Depends On", filters={"parent": task.name}, pluck="task")
	return all(
		_additional(name, user, seen) and frappe.has_permission("Task", "read", doc=name, user=user)
		for name in refs
		if name
	)


def task_access(task, user=None):
	seen = _checking.get()
	token = _checking.set(set() if seen is None else seen)
	try:
		return _additional(task, user or frappe.session.user, _checking.get())
	finally:
		_checking.reset(token)


def required_permission(doctype, doc, ptype, user, **kwargs):
	if doctype not in {"Task", "Task Depends On", *REFERENCES} or doc is None or not ready():
		return None
	if isinstance(doc, str):
		doc = frappe.db.get_value(doctype, doc, "*", as_dict=True)
	if not doc:
		return None
	if doctype == "Task":
		return task_access(doc, user)
	if doctype == "Task Depends On":
		return task_access(doc.get("parent"), user) and task_access(doc.get("task"), user)
	if doctype == "File":
		tasks = _file_tasks(doc)
		if tasks:
			return all(frappe.has_permission("Task", "read", doc=name, user=user) for name in tasks)
	if doctype in REFERENCES:
		kind, name = REFERENCES[doctype]
		if doc.get(kind) == "Task" and doc.get(name):
			if source(doc.get(name)) is not None or not task_access(doc.get(name), user):
				return frappe.has_permission("Task", "read", doc=doc.get(name), user=user)
	return None


def _hidden_tasks(user):
	return [
		row.name
		for row in frappe.get_all(
			"Task", fields=["name", *PROVENANCE, "kanzlei_customer", "parent_task"], limit=0
		)
		if not task_access(row, user)
		or (source(row) is not None and not frappe.has_permission("Task", "read", doc=row.name, user=user))
	]


def required_query(doctype, user, query_doctype=None):
	actual = query_doctype or doctype
	if actual not in {"Task", "Task Depends On", *REFERENCES} or not ready():
		return None
	hidden = _hidden_tasks(user)
	if not hidden:
		return None
	if actual == "Task":
		return "NOT (" + names_condition("`tabTask`.name", hidden) + ")"
	if actual == "Task Depends On":
		return (
			"NOT ("
			+ names_condition("`tabTask Depends On`.parent", hidden)
			+ " OR "
			+ names_condition("`tabTask Depends On`.task", hidden)
			+ ")"
		)
	if actual == "File":
		urls = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Task", "attached_to_name": ["in", hidden]},
			pluck="file_url",
		)
		return (
			"NOT ((COALESCE(`tabFile`.attached_to_doctype,'')='Task' AND ("
			+ names_condition("`tabFile`.attached_to_name", hidden)
			+ ")) OR ("
			+ names_condition("`tabFile`.file_url", [url for url in urls if url])
			+ "))"
		)
	if actual in REFERENCES:
		kind, name = REFERENCES[actual]
		return f"NOT (COALESCE(`tab{actual}`.`{kind}`,'')='Task' AND ({names_condition('`tab' + actual + '`.`' + name + '`', hidden)}))"
	return None


def child_export_permission(doctype, parent_doctype, parent, parentfield, user):
	if doctype == "Task Depends On" and parent_doctype == "Task":
		return task_access(parent, user)
	return None


def _lock_sources(names):
	for name in sorted(set(name for name in names if name)):
		try:
			rows = frappe.db.sql(
				"SELECT * FROM `tabIncoming Letter` WHERE name=%s FOR UPDATE", (name,), as_dict=True
			)
		except Exception as error:
			causes = (error, error.__cause__)
			if any(getattr(cause, "args", ()) and cause.args[0] == 1020 for cause in causes):
				# Do not rollback someone else's framework save transaction. The
				# caller receives a safe denial and its request owns the rollback.
				deny()
			raise
		# A normal Task form can have loaded its document before waiting on a
		# concurrent correction. Evaluate quarantine on the current locking read,
		# not on a later repeatable-read snapshot of that same Letter.
		if not rows or not letter_access(rows[0], frappe.session.user):
			deny()


def _check_new(task):
	# Project.copy_from_template builds a new Task without frappe.copy_doc.
	# Its server source remains in template_task even after provenance stripping.
	if source(task.get("template_task")) is not None:
		deny()
	if not any(task.get(k) for k in PROVENANCE):
		if task.get("kanzlei_incoming_needs_planning"):
			deny()
		return
	if frappe.utils.cint(task.get("is_template")):
		deny()
	if _creation.get() != (task.get("kanzlei_incoming_letter"), task.get("kanzlei_incoming_action_id")):
		deny()
	_lock_sources([task.kanzlei_incoming_letter])
	letter = source(task)
	if not letter or letter.status != "Completed" or letter.correction_quarantined or not task_access(task):
		deny()
	if not frappe.db.exists(
		"Incoming Letter Action",
		{"parent": letter.name, "action_id": task.kanzlei_incoming_action_id, "decision": "accept"},
	):
		deny()


def _existing(task, updates=None):
	old = _row(task.name)
	if old and any(old.get(k) for k in PROVENANCE):
		_lock_sources([old.kanzlei_incoming_letter])
		if frappe.utils.cint((updates or {}).get("is_template", task.get("is_template"))):
			deny()
		if not task_access(old):
			deny()
		for field in IMMUTABLE:
			value = updates.get(field, task.get(field)) if updates is not None else task.get(field)
			if (value or None) != (old.get(field) or None):
				deny()
	elif any((updates or {}).get(k) or task.get(k) for k in PROVENANCE):
		deny()
	return old


def _planning(task):
	if not task.get("kanzlei_incoming_letter"):
		return
	assigned = bool(
		task.name
		and frappe.db.exists(
			"ToDo", {"reference_type": "Task", "reference_name": task.name, "status": "Open"}
		)
	)
	task.kanzlei_incoming_needs_planning = int(
		task.status not in ("Completed", "Cancelled") and (not assigned or not task.exp_end_date)
	)


def validate_task(doc, method=None):
	if doc.is_new():
		_check_new(doc)
	else:
		_existing(doc)
	for ref in [doc.get("parent_task"), *(row.task for row in doc.get("depends_on", []))]:
		if ref and not frappe.has_permission("Task", "read", doc=ref):
			deny()
	_planning(doc)


class TaskGuard:
	def insert(self, *args, **kwargs):
		_check_new(self)
		return super().insert(*args, **kwargs)

	def _save(self, *args, **kwargs):
		_existing(self)
		return super()._save(*args, **kwargs)

	def db_set(self, fieldname, value=None, *args, **kwargs):
		updates = fieldname if isinstance(fieldname, dict) else {fieldname: value}
		_existing(self, updates)
		if "kanzlei_incoming_needs_planning" in updates:
			deny()
		return super().db_set(fieldname, value, *args, **kwargs)

	def before_rename(self, *args, **kwargs):
		if source(_row(self.name)) is not None:
			deny()


def protect_task(doc, method=None, **kwargs):
	if source(_row(doc.name)) is not None:
		deny()


def _todo_check(doc):
	previous = (
		frappe.db.get_value("ToDo", doc.name, ["reference_type", "reference_name"], as_dict=True)
		if not doc.is_new()
		else None
	)
	refs = [
		row.reference_name
		for row in (previous, doc)
		if row and row.get("reference_type") == "Task" and source(row.get("reference_name")) is not None
	]
	_lock_sources([_row(name).kanzlei_incoming_letter for name in refs])
	for name in refs:
		if not frappe.has_permission("Task", "read", doc=name):
			deny()
	if (
		previous
		and refs
		and (doc.reference_type, doc.reference_name) != (previous.reference_type, previous.reference_name)
	):
		deny()
	if doc.reference_type == "Task" and source(doc.reference_name) is not None:
		task = _row(doc.reference_name)
		if doc.status == "Open" and (
			not staff(doc.allocated_to)
			or not frappe.has_permission("Customer", "read", doc=task.kanzlei_customer, user=doc.allocated_to)
		):
			deny()
		doc.description = frappe._("Posteingang task assignment")
		if not task.exp_end_date:
			doc.date = None


class ToDoGuard:
	def insert(self, *args, **kwargs):
		_todo_check(self)
		return super().insert(*args, **kwargs)

	def _save(self, *args, **kwargs):
		_todo_check(self)
		return super()._save(*args, **kwargs)

	def db_set(self, fieldname, value=None, *args, **kwargs):
		updates = fieldname if isinstance(fieldname, dict) else {fieldname: value}
		for key, item in updates.items():
			self.set(key, item)
		_todo_check(self)
		return super().db_set(fieldname, value, *args, **kwargs)


def validate_todo(doc, method=None):
	# Document.insert applies defaults after the early persistence guard. Run
	# once more after defaults so ToDo's Today default cannot invent a deadline.
	_todo_check(doc)


def _file_tasks(doc):
	names = set()
	if doc.get("attached_to_doctype") == "Task" and source(doc.get("attached_to_name")) is not None:
		names.add(doc.get("attached_to_name"))
	if doc.get("file_url"):
		for name in frappe.get_all(
			"File",
			filters={"file_url": str(doc.get("file_url")), "attached_to_doctype": "Task"},
			pluck="attached_to_name",
		):
			if source(name) is not None:
				names.add(name)
	return names


def _file_check(doc, updates=None):
	old = frappe.db.get_value("File", doc.name, "*", as_dict=True) if not doc.is_new() else None
	desired = frappe._dict(doc.as_dict())
	desired.update(updates or {})
	names = _file_tasks(desired) | (_file_tasks(old) if old else set())
	if not names:
		return
	_lock_sources([_row(name).kanzlei_incoming_letter for name in names])
	if not all(frappe.has_permission("Task", "read", doc=name) for name in names):
		deny()
	if (
		not desired.is_private
		or desired.attached_to_doctype != "Task"
		or desired.attached_to_name not in names
	):
		deny()
	if old and (old.attached_to_doctype, old.attached_to_name, old.file_url) != (
		desired.attached_to_doctype,
		desired.attached_to_name,
		desired.file_url,
	):
		deny()


class TaskFileGuard:
	def insert(self, *args, **kwargs):
		_file_check(self)
		return super().insert(*args, **kwargs)

	def _save(self, *args, **kwargs):
		_file_check(self)
		return super()._save(*args, **kwargs)

	def db_set(self, fieldname, value=None, *args, **kwargs):
		_file_check(self, fieldname if isinstance(fieldname, dict) else {fieldname: value})
		return super().db_set(fieldname, value, *args, **kwargs)


def file_access_override(doc, ptype, user, **kwargs):
	if any(not frappe.has_permission("Task", "read", doc=name, user=user) for name in _file_tasks(doc)):
		return False
	return None


def update_planning(doc, method=None):
	if doc.reference_type == "Task" and source(doc.reference_name) is not None:
		task = frappe.get_doc("Task", doc.reference_name)
		_planning(task)
		frappe.db.set_value(
			"Task",
			task.name,
			"kanzlei_incoming_needs_planning",
			task.kanzlei_incoming_needs_planning,
			update_modified=False,
		)


def validate_related(doc, method=None):
	if doc.doctype in REFERENCES:
		old = frappe.db.get_value(doc.doctype, doc.name, "*", as_dict=True) if not doc.is_new() else None
		for row in (old, doc):
			if row and required_permission(doc.doctype, row, "write", frappe.session.user) is False:
				deny()
		if old:
			kind, name = REFERENCES[doc.doctype]
			if (
				old.get(kind) == "Task"
				and source(old.get(name)) is not None
				and (old.get(kind), old.get(name)) != (doc.get(kind), doc.get(name))
			):
				deny()


def copy_guard(doc):
	if doc.get("doctype") == "Task" and (
		any(doc.get(k) for k in PROVENANCE) or source(doc.get("name")) is not None
	):
		deny()


def assignment_notification(assigned_by, allocated_to, doc_type, doc_name, action, **kwargs):
	if doc_type != "Task" or source(doc_name) is None:
		return None
	if not frappe.has_permission("Task", "read", doc=doc_name, user=allocated_to):
		return True
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	user_language = frappe.db.get_value("User", allocated_to, "language")
	enqueue_create_notification(
		allocated_to,
		{
			"type": "Assignment",
			"document_type": "Task",
			"document_name": doc_name,
			"from_user": assigned_by,
			"subject": frappe._("Posteingang task assignment updated", lang=user_language),
			"email_content": None,
			"email_header": frappe._("Assignment update", lang=user_language),
		},
	)
	return True


def notification_delivery(doc, user):
	if doc.get("document_type") == "Task" and source(doc.get("document_name")) is not None:
		return bool(
			staff(user) and frappe.has_permission("Task", "read", doc=doc.get("document_name"), user=user)
		)
	return None


def assignment_read(args):
	if args.get("doctype") == "Task" and source(args.get("name")) is not None:
		frappe.get_doc("Task", args.get("name")).check_permission("read")


@frappe.whitelist()
def get_notification_logs(limit=20):
	# The framework endpoint has an HTTP stale-while-revalidate cache. Protected
	# Task metadata must disappear on the next read after Customer revocation.
	try:
		limit = min(100, max(1, int(limit)))
	except (TypeError, ValueError):
		deny()
	rows = frappe.get_list("Notification Log", fields=["*"], limit=limit, order_by="creation desc")
	info = frappe._dict()
	for user in {row.from_user for row in rows}:
		frappe.utils.add_user_info(user, info)
	return {"notification_logs": rows, "user_info": info}


@frappe.whitelist()
def check_if_child_exists(name):
	frappe.get_doc("Task", name).check_permission("read")
	from frappe.utils import get_link_to_form

	return [
		get_link_to_form("Task", row.name) for row in frappe.get_list("Task", filters={"parent_task": name})
	]


@frappe.whitelist()
def make_timesheet(source_name, target_doc=None, ignore_permissions=False):
	frappe.get_doc("Task", source_name).check_permission("read")
	from erpnext.projects.doctype.task.task import make_timesheet as original

	return original(source_name, target_doc=target_doc, ignore_permissions=False)


@frappe.whitelist()
def assignment_get(args=None):
	args = frappe.parse_json(args) if isinstance(args, str) else args or frappe.local.form_dict
	if args.get("doctype") == "Task" and source(args.get("name")) is not None:
		frappe.get_doc("Task", args.get("name")).check_permission("read")
	from frappe.desk.form.assign_to import get

	return get(args)
