"""Shared preparation stages for FiBu packages and late supplements."""

from datetime import datetime

PREPARATION_STAGES = (
	"Collection",
	"Review",
	"Clarification",
	"Ready",
	"Transfer",
	"Receipt Confirmed",
)

_FORWARD_TRANSITIONS = {
	"Collection": {"Review"},
	"Review": {"Clarification", "Ready"},
	"Clarification": set(),
	"Ready": {"Transfer"},
	"Transfer": {"Receipt Confirmed"},
	"Receipt Confirmed": set(),
}
_CONTEXT_FIELDS = (
	"preparation_stage",
	"waiting_for_mandant",
	"waiting_reason",
	"blocking_reason",
	"next_action",
	"next_action_assignee",
	"next_action_task",
)
_EVENT_FIELDS = (
	"event_type",
	"from_stage",
	"to_stage",
	"from_waiting_for_mandant",
	"to_waiting_for_mandant",
	"previous_waiting_reason",
	"waiting_reason",
	"previous_blocking_reason",
	"blocking_reason",
	"previous_action",
	"next_action",
	"previous_assignee",
	"next_action_assignee",
	"previous_task",
	"next_action_task",
	"note",
	"recorded_by",
	"recorded_at",
)


def _workflow_event_rows(rows):
	return [
		(
			row.name,
			row.idx,
			*(
				datetime.fromisoformat(str(row.get(field)).replace("Z", "+00:00"))
				if field == "recorded_at" and row.get(field) and not isinstance(row.get(field), datetime)
				else row.get(field)
				for field in _EVENT_FIELDS
			),
		)
		for row in rows or []
	]


def validate_stage_transition(current: str, target: str, note: str = "") -> None:
	"""Reject skipped forward stages and unexplained returns to earlier work."""
	if current not in PREPARATION_STAGES or target not in PREPARATION_STAGES:
		raise ValueError("Unknown preparation stage")
	if current == target:
		raise ValueError("The preparation stage transition must change the stage")
	if current == "Receipt Confirmed":
		raise ValueError("Receipt confirmation is a terminal preparation stage")
	if target in _FORWARD_TRANSITIONS[current]:
		return
	if PREPARATION_STAGES.index(target) < PREPARATION_STAGES.index(current):
		if not (note or "").strip():
			raise ValueError("A reason is required when returning to an earlier stage")
		return
	raise ValueError("The preparation stage transition is not allowed")


def validate_work_context(
	status: str,
	stage: str,
	waiting_for_mandant: bool,
	waiting_reason: str,
	next_action: str,
	next_action_assignee: str,
) -> dict:
	"""Normalize the independent waiting marker and validate the next action."""
	if stage not in PREPARATION_STAGES:
		raise ValueError("Unknown preparation stage")
	if waiting_for_mandant not in (True, False, 0, 1):
		raise ValueError("Waiting for Mandant must be enabled or disabled")
	waiting_for_mandant = bool(waiting_for_mandant)
	waiting_reason = (waiting_reason or "").strip()
	if waiting_for_mandant and not waiting_reason:
		raise ValueError("A waiting reason is required while waiting for Mandant")
	if not waiting_for_mandant:
		waiting_reason = ""
	next_action = (next_action or "").strip()
	next_action_assignee = (next_action_assignee or "").strip()
	if status == "Open" and stage != "Receipt Confirmed" and (not next_action or not next_action_assignee):
		raise ValueError("An open period needs a next action and an assignee")
	return {
		"waiting_for_mandant": waiting_for_mandant,
		"waiting_reason": waiting_reason,
		"next_action": next_action,
		"next_action_assignee": next_action_assignee,
	}


def initialize_work_context(doc) -> None:
	"""Give new packages and supplements a useful initial action."""
	if not doc.is_new():
		return
	if not doc.preparation_stage:
		doc.preparation_stage = "Collection"
	if not doc.get("next_action"):
		doc.next_action = "Review the current period status"
	if not doc.get("next_action_assignee"):
		doc.next_action_assignee = doc.get("responsible")


def validate_workflow_document(doc) -> None:
	"""Protect workflow fields and append-only history from ordinary saves."""
	import frappe

	if doc.is_new():
		if doc.preparation_stage != "Collection" or doc.waiting_for_mandant or doc.get("waiting_reason") or doc.get("blocking_reason"):
			frappe.throw(frappe._("Use the workflow actions to change preparation state"))
		if doc.get("workflow_events"):
			frappe.throw(frappe._("Workflow history is maintained by the system"))
	else:
		previous = doc.get_doc_before_save()
		context_changed = any(doc.get(field) != previous.get(field) for field in _CONTEXT_FIELDS)
		old_events = _workflow_event_rows(previous.get("workflow_events"))
		new_events = _workflow_event_rows(doc.get("workflow_events"))
		history_is_append_only = len(new_events) >= len(old_events) and new_events[: len(old_events)] == old_events
		history_changed = old_events != new_events
		workflow_action = doc.flags.get("fibu_workflow_action")
		if not history_is_append_only or ((context_changed or history_changed) and not workflow_action):
			frappe.throw(frappe._("Use the workflow actions to change preparation state"))

	try:
		validate_work_context(
			doc.status,
			doc.preparation_stage,
			doc.waiting_for_mandant,
			doc.get("waiting_reason"),
			doc.get("next_action"),
			doc.get("next_action_assignee"),
		)
	except ValueError as error:
		frappe.throw(frappe._(str(error)))
	validate_next_action_task(doc)


def change_preparation_stage(doc, target_stage: str, note: str, expected_modified: str) -> str:
	"""Apply one allowed stage transition and record its server-authored event."""
	import frappe
	from frappe.utils import now_datetime

	_lock_workflow_document(doc, expected_modified)
	if doc.status != "Open":
		frappe.throw(frappe._("Closed work cannot change preparation stage"))
	note = (note or "").strip()
	try:
		validate_stage_transition(doc.preparation_stage, target_stage, note)
	except ValueError as error:
		frappe.throw(frappe._(str(error)))
	if target_stage == "Receipt Confirmed" and not note:
		frappe.throw(frappe._("A note is required to confirm receipt"))

	before = _context_snapshot(doc)
	doc.preparation_stage = target_stage
	if target_stage == "Receipt Confirmed":
		doc.next_action = ""
		doc.next_action_assignee = ""
		doc.next_action_task = ""
	_append_event(doc, "Stage Changed", before, note, now_datetime())
	_save_workflow_action(doc)
	return doc.name


def update_work_context(
	doc,
	waiting_for_mandant,
	waiting_reason: str,
	blocking_reason: str,
	next_action: str,
	next_action_assignee: str,
	next_action_task: str,
	expected_modified: str,
	note: str = "",
) -> str:
	"""Update the independent Mandant wait marker and the next assigned action."""
	import frappe
	from frappe.utils import now_datetime

	_lock_workflow_document(doc, expected_modified)
	if doc.status != "Open":
		frappe.throw(frappe._("Closed work cannot change its preparation context"))
	try:
		values = validate_work_context(
			doc.status,
			doc.preparation_stage,
			waiting_for_mandant,
			waiting_reason,
			next_action,
			next_action_assignee,
		)
	except ValueError as error:
		frappe.throw(frappe._(str(error)))

	before = _context_snapshot(doc)
	doc.waiting_for_mandant = values["waiting_for_mandant"]
	doc.waiting_reason = values["waiting_reason"]
	doc.blocking_reason = (blocking_reason or "").strip()
	doc.next_action = values["next_action"]
	doc.next_action_assignee = values["next_action_assignee"]
	doc.next_action_task = (next_action_task or "").strip()
	validate_next_action_task(doc)
	after = _context_snapshot(doc)
	if before != after:
		event_type = "Waiting Changed" if (
			before["waiting_for_mandant"] != after["waiting_for_mandant"]
			or before["waiting_reason"] != after["waiting_reason"]
		) else "Work Context Changed"
		_append_event(doc, event_type, before, (note or "").strip(), now_datetime())
		_save_workflow_action(doc)
	return doc.name


def validate_next_action_task(doc) -> None:
	"""Ensure the optional linked Task belongs to this exact open work context."""
	if not doc.get("next_action_task"):
		return
	import frappe

	task = frappe.get_doc("Task", doc.next_action_task)
	task.check_permission("read")
	package_name = doc.name if doc.doctype == "FiBu Package" else doc.package
	supplement_name = doc.name if doc.doctype == "FiBu Supplement" else ""
	if task.get("kanzlei_fibu_package") != package_name or (task.get("kanzlei_fibu_supplement") or "") != supplement_name:
		frappe.throw(frappe._("Next action Task must belong to this FiBu work context"))
	if task.status in ("Completed", "Cancelled"):
		frappe.throw(frappe._("Next action Task must still be open"))


def _lock_workflow_document(doc, expected_modified: str) -> None:
	import frappe

	doc.check_permission("write")
	if not expected_modified:
		frappe.throw(frappe._("Reload the record before changing workflow state"))
	row = frappe.db.sql(
		f"SELECT modified FROM `tab{doc.doctype}` WHERE name=%s FOR UPDATE",
		doc.name,
	)
	if not row or str(row[0][0]) != str(expected_modified) or str(doc.modified) != str(expected_modified):
		frappe.throw(frappe._("This record changed. Reload it before applying the workflow action"))


def _context_snapshot(doc) -> dict:
	return {field: doc.get(field) for field in _CONTEXT_FIELDS}


def _append_event(doc, event_type: str, before: dict, note: str, recorded_at) -> None:
	import frappe

	after = _context_snapshot(doc)
	doc.append(
		"workflow_events",
		{
			"event_type": event_type,
			"from_stage": before["preparation_stage"],
			"to_stage": after["preparation_stage"],
			"from_waiting_for_mandant": before["waiting_for_mandant"],
			"to_waiting_for_mandant": after["waiting_for_mandant"],
			"previous_waiting_reason": before["waiting_reason"],
			"waiting_reason": after["waiting_reason"],
			"previous_blocking_reason": before["blocking_reason"],
			"blocking_reason": after["blocking_reason"],
			"previous_action": before["next_action"],
			"next_action": after["next_action"],
			"previous_assignee": before["next_action_assignee"],
			"next_action_assignee": after["next_action_assignee"],
			"previous_task": before["next_action_task"],
			"next_action_task": after["next_action_task"],
			"note": note,
			"recorded_by": frappe.session.user,
			"recorded_at": recorded_at,
		},
	)


def _save_workflow_action(doc) -> None:
	doc.flags.fibu_workflow_action = True
	try:
		doc.save()
	finally:
		doc.flags.fibu_workflow_action = False
