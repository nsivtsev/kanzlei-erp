"""One-click Posteingang: internal working date and the proposal card projection."""

from datetime import date, timedelta
from uuid import uuid4

import frappe

from kanzlei_erp import posteingang_confirmation as review
from kanzlei_erp import posteingang_jobs as workflow
from kanzlei_erp import posteingang_matching as matching
from kanzlei_erp.posteingang import canonical
from kanzlei_erp.posteingang_ai_validation import InvalidResult, parse_json
from kanzlei_erp.posteingang_permissions import staff

WORKING_DAYS_BEFORE_DEADLINE = 5
DAYS_WITHOUT_DEADLINE = 7
IDENTIFIER_LABELS = {"de_steuernummer": "Steuernummer", "de_tax_id": "Steuer-ID", "de_vat_id": "USt-IdNr.", "other": "Referenz"}
NO_TASK_REASON = "Im Schreiben wurde keine Aufgabe gefunden."
DEFAULT_ASSIGNEE = object()
# Audit texts stored on confirm_simple; each must stay literally true.
CUSTOMER_SUGGESTED = "Vorschlag übernommen"
CUSTOMER_MANUAL = "Manuell gewählt"
CUSTOMER_MULTIPLE = "Mehrere Empfänger, Mandant manuell gewählt"
DATE_PROPOSED = "Vorschlag übernommen"
DATE_MANUAL = "Manuell eingetragen"
NOT_SELECTED = "Nicht ausgewählt"
NO_TASKS_PROPOSED = "Vom Assistenten: keine Handlung erforderlich, übernommen"
NO_TASKS = "Ohne Aufgabe abgelegt"
TASK_KEYS = {"source_index", "title", "description", "external_date", "internal_date", "assigned_to"}


def working_date(deadline: date | None, today: date, holidays: frozenset[date]) -> date:
	"""Deadline minus 5 working days (Mon-Fri, not a holiday), never before today.

	Without a deadline the date is today plus 7 days.
	"""
	if deadline is None:
		return today + timedelta(days=DAYS_WITHOUT_DEADLINE)
	result = deadline
	remaining = WORKING_DAYS_BEFORE_DEADLINE
	while remaining:
		result -= timedelta(days=1)
		if result.weekday() < 5 and result not in holidays:
			remaining -= 1
	return max(result, today)


def company_holidays() -> frozenset[date]:
	"""Holiday dates of the default company's holiday list; empty if any link is missing."""
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	holiday_list = company and frappe.db.get_value("Company", company, "default_holiday_list")
	if not holiday_list:
		return frozenset()
	dates = frappe.get_all("Holiday", filters={"parent": holiday_list}, pluck="holiday_date")
	return frozenset(frappe.utils.getdate(value) for value in dates)


def _why(candidate):
	reason = candidate["reasons"][0]
	proof = reason["evidence"][0]
	if reason["kind"] == "exact_identifier":
		return f"{IDENTIFIER_LABELS[reason['identifier_type']]} „{proof['quote']}“ im Schreiben", proof["page"]
	return "Name und Adresse im Schreiben", proof["page"]


def _customer_block(extraction):
	if extraction is None:
		return {"suggested": None, "candidates": [], "ambiguity": None}
	result = matching._match(extraction)
	candidates, pages, suggested = [], [], None
	for item in result["candidates"]:
		why, page = _why(item)
		candidates.append({"customer": item["customer"], "customer_name": item["customer_name"], "why": why, "can_select": item["can_select"]})
		pages.append(page)
	if result["ambiguity"] == "none" and len(candidates) == 1 and candidates[0]["can_select"]:
		suggested = {**{k: candidates[0][k] for k in ("customer", "customer_name", "why")}, "page": pages[0]}
	return {"suggested": suggested, "candidates": candidates, "ambiguity": result["ambiguity"]}


def _assignee(doc, customer):
	"""The uploader if active staff who can read the Mandant; otherwise the current user."""
	user = doc.received_by
	if staff(user) and (not customer or frappe.has_permission("Customer", "read", doc=customer, user=user)):
		return user
	return frappe.session.user


def _deadline(deadline):
	proof = deadline["evidence"][0] if deadline["evidence"] else {}
	return {"kind": deadline["kind"], "date": deadline["date"], "wording": deadline["wording"],
		"page": proof.get("page"), "quote": proof.get("quote")}


def _internal(deadline, today, holidays):
	"""Relative and uncertain deadlines are never turned into a date by code."""
	if deadline["kind"] not in ("explicit_date", "not_found"):
		return None
	return str(working_date(frappe.utils.getdate(deadline["date"]) if deadline["date"] else None, today, holidays))


def _proposed(index, action, today, holidays):
	deadline = _deadline(action["deadline"])
	return {"source_index": index, "title": action["title"], "description": action["description"],
		"deadline": deadline, "internal_date": _internal(deadline, today, holidays), "assigned_to": DEFAULT_ASSIGNEE}


def _stored(row, today, holidays):
	proposal = review._object(row.original_proposal_json) if row.original_proposal_json else None
	deadline = _deadline(proposal["deadline"]) if proposal else {"kind": "not_found", "date": None, "wording": None, "page": None, "quote": None}
	if row.external_date and str(row.external_date) != deadline["date"]:
		# A date a human entered from the letter wins over the proposal.
		deadline = {"kind": "explicit_date", "date": str(row.external_date), "wording": None, "page": None, "quote": None}
	if row.internal_date:
		internal = str(row.internal_date)
	elif row.scheduling_decision == "unscheduled":
		internal = None
	else:
		internal = _internal(deadline, today, holidays)
	if row.assignment_decision == "unassigned":
		assigned = None
	else:
		assigned = row.assigned_to or DEFAULT_ASSIGNEE
	return {"source_index": row.source_index, "title": row.title, "description": row.description or "",
		"deadline": deadline, "internal_date": internal, "assigned_to": assigned}


def _state(doc, rows, extraction):
	if doc.status in ("Queued", "Processing"):
		return "analyzing"
	if doc.status in ("Completed", "Cancelled"):
		return "done"
	if doc.status == "Error":
		return "failed"
	if doc.status not in review.DRAFT_STATES:
		return "not_started"
	if rows:
		return "draft"
	if extraction is None:
		return "not_started"
	return "failed" if extraction["readability"] == "unreadable" else "proposal"


def simple_card(doc):
	"""One proposal card for the one-click Posteingang, read in the caller's snapshot."""
	run, extraction = review._latest_successful(doc)
	rows = review._actions(doc.name) if doc.status in review.DRAFT_STATES else []
	state = _state(doc, rows, extraction)
	customer = _customer_block(extraction)
	today, holidays = frappe.utils.getdate(), company_holidays()
	if state == "draft":
		tasks = [_stored(row, today, holidays) for row in rows if row.decision != "reject"]
	elif state == "proposal":
		tasks = [_proposed(index, action, today, holidays) for index, action in enumerate(extraction["actions"])]
	else:
		tasks = []
	default = _assignee(doc, doc.selected_customer or (customer["suggested"] or {}).get("customer")) if tasks else None
	names = {None: None}
	for task in tasks:
		if task["assigned_to"] is DEFAULT_ASSIGNEE:
			task["assigned_to"] = default
		if task["assigned_to"] not in names:
			names[task["assigned_to"]] = frappe.db.get_value("User", task["assigned_to"], "full_name") or task["assigned_to"]
		task["assigned_to_name"] = names[task["assigned_to"]]
	no_task_reason = None
	if state == "proposal" and extraction["action_assessment"] == "none_found":
		no_task_reason = extraction["summary"]["value"] or NO_TASK_REASON
	return {"state": state, "run": run, "customer": customer, "tasks": tasks, "no_task_reason": no_task_reason, "undo_until": None}


def _iso(value):
	if value is None:
		return None
	try:
		if isinstance(value, str) and len(value) == 10 and date.fromisoformat(value):
			return value
	except ValueError:
		pass
	workflow.fail("INVALID_REQUEST")


def simple_tasks(tasks, limit):
	"""Shape-check the confirm_simple task list before any lock or write."""
	try:
		if isinstance(tasks, str):
			tasks = parse_json('{"items":' + tasks + "}")["items"]
		elif isinstance(tasks, list):
			tasks = parse_json(canonical({"items": tasks}))["items"]
		else:
			tasks = None
	except InvalidResult:
		tasks = None
	if not isinstance(tasks, list) or len(tasks) > limit:
		workflow.fail("INVALID_REQUEST")
	result = []
	for task in tasks:
		if not isinstance(task, dict) or not {"title", "assigned_to"} <= set(task) <= TASK_KEYS:
			workflow.fail("INVALID_REQUEST")
		index, description = task.get("source_index"), task.get("description") or ""
		if (index is not None and type(index) is not int) or not isinstance(description, str) or len(description) > 4000:
			workflow.fail("INVALID_REQUEST")
		if not workflow._text(task["title"], 140) or not workflow._text(task["assigned_to"], 140):
			workflow.fail("INVALID_REQUEST")
		result.append({"source_index": index, "title": task["title"], "description": description,
			"external_date": _iso(task.get("external_date")), "internal_date": _iso(task.get("internal_date")),
			"assigned_to": task["assigned_to"]})
	return result


def _basis(deadline, external):
	"""Where the external date came from; relative wording is never turned into a date by code."""
	if deadline is None:
		return DATE_MANUAL if external else None
	if deadline["kind"] in ("relative", "uncertain"):
		if external is None:
			workflow.fail("DEADLINE_REQUIRED")
		text = f"Vom Benutzer eingetragen: „{deadline['wording']}“"
		return text if len(text) <= 1000 else text[:998] + "…“"
	if external is None:
		return None
	if deadline["kind"] == "explicit_date" and external == deadline["date"]:
		page = deadline["evidence"][0]["page"] if deadline["evidence"] else None
		return f"{DATE_PROPOSED} (Fundstelle S. {page})" if page else DATE_PROPOSED
	return DATE_MANUAL


def _accepted(task, basis, today, holidays):
	external = task["external_date"]
	internal = task["internal_date"] or str(working_date(frappe.utils.getdate(external) if external else None, today, holidays))
	return {"title": task["title"], "description": task["description"] if task["description"].strip() else task["title"],
		"decision": "accept", "assigned_to": task["assigned_to"], "assignment_decision": "assigned",
		"external_date": external, "external_date_basis": basis, "internal_date": internal,
		"scheduling_decision": "scheduled", "needs_planning": False}


def simple_review(doc, origin, customer, tasks):
	"""The full confirm review for confirm_simple, built from the card choice; writes nothing."""
	run, extraction = review._latest_successful(doc)
	today, holidays = frappe.utils.getdate(), company_holidays()
	actions = []
	if origin == "proposal":
		if extraction is None or extraction["readability"] == "unreadable" or review._actions(doc.name):
			workflow.fail("INVALID_REQUEST")
		proposals = extraction["actions"]
		indices = [task["source_index"] for task in tasks]
		if any(index is None or not 0 <= index < len(proposals) for index in indices) or len(set(indices)) != len(indices):
			workflow.fail("INVALID_REQUEST")
		chosen = {task["source_index"]: task for task in tasks}
		for index, proposal in enumerate(proposals):
			item = review._pending(str(uuid4()), proposal["title"], proposal["description"], source_index=index, evidence=proposal["evidence"])
			if index in chosen:
				item.update(_accepted(chosen[index], _basis(proposal["deadline"], chosen[index]["external_date"]), today, holidays))
			else:
				item.update(decision="reject", rejection_reason=NOT_SELECTED)
			actions.append(item)
		fields = {key: extraction[key]["value"] for key in review.DOCUMENT_FIELDS}
		evidence = canonical({key: extraction[key]["evidence"] for key in review.DOCUMENT_FIELDS})
	else:
		if any(task["source_index"] is not None for task in tasks):
			workflow.fail("INVALID_REQUEST")
		for task in tasks:
			item = review._pending(str(uuid4()), task["title"], task["title"])
			item.update(_accepted(task, _basis(None, task["external_date"]), today, holidays))
			actions.append(item)
		run, proposals, fields, evidence = None, None, review._fields(doc), None
	applied = review._source(doc, doc.applied_run) if doc.applied_run else None
	if any(len(matching._recipients(value)) > 1 for value in (extraction, applied) if value is not None):
		reason = CUSTOMER_MULTIPLE
	elif origin == "proposal" and (_customer_block(extraction)["suggested"] or {}).get("customer") == customer:
		reason = CUSTOMER_SUGGESTED
	else:
		reason = CUSTOMER_MANUAL
	if any(item["decision"] == "accept" for item in actions):
		no_actions = None
	elif origin == "proposal" and extraction["action_assessment"] == "none_found":
		no_actions = NO_TASKS_PROPOSED
	else:
		no_actions = NO_TASKS
	clarification = doc.clarification_reason if (doc.clarification_reason or "").strip() else None
	return {"run": run, "proposals": proposals, "document_fields": fields, "fact_evidence_json": evidence,
		"review": {"schema_version": "1.0", "mode": "confirm", "expected_revision": doc.revision, "selected_customer": customer,
			"customer_reason": reason, "no_actions_reason": no_actions, "clarification_reason": clarification,
			"human_verified": True, "actions": actions}}
