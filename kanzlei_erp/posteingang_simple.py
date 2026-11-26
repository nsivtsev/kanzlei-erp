"""One-click Posteingang: internal working date and the proposal card projection."""

from datetime import date, timedelta

import frappe

from kanzlei_erp import posteingang_confirmation as review
from kanzlei_erp import posteingang_matching as matching
from kanzlei_erp.posteingang_permissions import staff

WORKING_DAYS_BEFORE_DEADLINE = 5
DAYS_WITHOUT_DEADLINE = 7
IDENTIFIER_LABELS = {"de_steuernummer": "Steuernummer", "de_tax_id": "Steuer-ID", "de_vat_id": "USt-IdNr.", "other": "Referenz"}
NO_TASK_REASON = "Im Schreiben wurde keine Aufgabe gefunden."


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


def _proposed(index, action, today, holidays):
	deadline = action["deadline"]
	proof = deadline["evidence"][0] if deadline["evidence"] else {}
	internal = None
	if deadline["kind"] in ("explicit_date", "not_found"):
		internal = working_date(frappe.utils.getdate(deadline["date"]) if deadline["date"] else None, today, holidays)
	return {"source_index": index, "title": action["title"], "description": action["description"],
		"deadline": {"kind": deadline["kind"], "date": deadline["date"], "wording": deadline["wording"],
			"page": proof.get("page"), "quote": proof.get("quote")},
		"internal_date": str(internal) if internal else None, "assigned_to": None}


def _stored(row, today, holidays):
	external = frappe.utils.getdate(row.external_date) if row.external_date else None
	internal = frappe.utils.getdate(row.internal_date) if row.internal_date else working_date(external, today, holidays)
	return {"source_index": row.source_index, "title": row.title, "description": row.description or "",
		"deadline": {"kind": "explicit_date" if external else "not_found", "date": str(external) if external else None,
			"wording": None, "page": None, "quote": None},
		"internal_date": str(internal), "assigned_to": row.assigned_to}


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
		tasks = [_stored(row, today, holidays) for row in rows]
	elif state == "proposal":
		tasks = [_proposed(index, action, today, holidays) for index, action in enumerate(extraction["actions"])]
	else:
		tasks = []
	default = _assignee(doc, doc.selected_customer or (customer["suggested"] or {}).get("customer")) if tasks else None
	names = {}
	for task in tasks:
		task["assigned_to"] = task["assigned_to"] or default
		if task["assigned_to"] not in names:
			names[task["assigned_to"]] = frappe.db.get_value("User", task["assigned_to"], "full_name") or task["assigned_to"]
		task["assigned_to_name"] = names[task["assigned_to"]]
	no_task_reason = None
	if state == "proposal" and extraction["action_assessment"] == "none_found":
		no_task_reason = extraction["summary"]["value"] or NO_TASK_REASON
	return {"state": state, "run": run, "customer": customer, "tasks": tasks, "no_task_reason": no_task_reason, "undo_until": None}
