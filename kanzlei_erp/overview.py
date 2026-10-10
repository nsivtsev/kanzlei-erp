"""Read-only, permission-aware data for the Kanzlei work overview."""

import re

import frappe
from frappe.core.utils import html_to_plain_text
from frappe.utils import add_days, getdate, nowdate

MAIL_BATCH_SIZE = 50
TASK_FIELDS = ["name", "subject", "status", "exp_end_date", "kanzlei_customer"]


def _require_desk_user():
	user = frappe.session.user
	if user == "Guest" or frappe.get_cached_value("User", user, "user_type") != "System User":
		frappe.throw(frappe._("The work overview is available to Desk users only."), frappe.PermissionError)


def _integer(value, minimum, maximum=None):
	if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
		frappe.throw(frappe._("Invalid overview pagination."), frappe.ValidationError)
	value = int(value)
	if value < minimum or (maximum is not None and value > maximum):
		frappe.throw(frappe._("Invalid overview pagination."), frappe.ValidationError)
	return value


def _dates():
	today = nowdate()
	return {"today": today, "range_start": str(add_days(today, 1)), "range_end": str(add_days(today, 7))}


def _task_filters(bucket, dates):
	filters = [
		["status", "not in", ["Completed", "Cancelled", "Template"]],
		["is_template", "=", 0],
		["is_group", "=", 0],
		["kanzlei_work_kind", "not in", ["Question"]],
		["exp_end_date", "is", "set"],
	]
	# Task.exp_end_date is Datetime in this ERPNext version: compare whole site-calendar days.
	if bucket == "upcoming":
		filters.extend(
			[
				["exp_end_date", ">=", dates["range_start"]],
				["exp_end_date", "<", str(add_days(dates["range_end"], 1))],
			]
		)
	elif bucket == "today":
		filters.extend([["exp_end_date", ">=", dates["today"]], ["exp_end_date", "<", dates["range_start"]]])
	else:
		filters.append(["exp_end_date", "<", dates["range_start"] if bucket == "urgent" else dates["today"]])
	return filters


def _task_count(filters):
	return int(frappe.get_list("Task", filters=filters, fields=[{"COUNT": "name", "as": "total"}])[0].total)


def _task_rows(filters, start, page_length):
	rows = frappe.get_list(
		"Task",
		filters=filters,
		fields=TASK_FIELDS,
		order_by="exp_end_date asc, name asc",
		offset=start,
		limit=page_length,
	)
	if not rows:
		return []
	customer_names = sorted({row.kanzlei_customer for row in rows if row.kanzlei_customer})
	customers = {}
	if customer_names:
		try:
			customers = {
				row.name: frappe.get_cached_value("Customer", row.name, "customer_name")
				for row in frappe.get_list(
					"Customer",
					filters={"name": ["in", customer_names]},
					fields=["name"],
					limit=len(customer_names),
				)
				if frappe.has_permission("Customer", "read", doc=row.name)
			}
		except frappe.PermissionError:
			pass
	assignments = {}
	# The standard Task form exposes open assignments after checking access to the Task.
	for assignment in frappe.get_all(
		"ToDo",
		filters={
			"reference_type": "Task",
			"reference_name": ["in", [row.name for row in rows]],
			"status": ["not in", ["Closed", "Cancelled"]],
			"allocated_to": ["is", "set"],
		},
		fields=["reference_name", "allocated_to"],
		order_by="allocated_to asc",
	):
		assignments.setdefault(assignment.reference_name, set()).add(assignment.allocated_to)
	users = {user for values in assignments.values() for user in values}
	names = {user: frappe.get_cached_value("User", user, "full_name") or user for user in users}
	return [
		{
			"name": row.name,
			"subject": row.subject,
			"status": row.status,
			"exp_end_date": getdate(row.exp_end_date).isoformat(),
			"customer": row.kanzlei_customer if row.kanzlei_customer in customers else None,
			"customer_name": customers.get(row.kanzlei_customer),
			"customer_restricted": bool(row.kanzlei_customer and row.kanzlei_customer not in customers),
			"assignees": [
				{"name": user, "full_name": names[user]} for user in sorted(assignments.get(row.name, set()))
			],
		}
		for row in rows
	]


@frappe.whitelist()
def get_task_overview(bucket="urgent", start=0, page_length=8):
	_require_desk_user()
	if not isinstance(bucket, str) or bucket not in {"urgent", "overdue", "today", "upcoming"}:
		frappe.throw(frappe._("Invalid overview task category."), frappe.ValidationError)
	start, page_length = _integer(start, 0), _integer(page_length, 1, 20)
	dates = _dates()
	if not frappe.has_permission("Task", "read"):
		return {"state": "no_access", **dates}
	summary = {key: _task_count(_task_filters(key, dates)) for key in ("overdue", "today", "upcoming")}
	total = summary["overdue"] + summary["today"] if bucket == "urgent" else summary[bucket]
	return {
		"state": "ready",
		**dates,
		"summary": summary,
		"rows": _task_rows(_task_filters(bucket, dates), start, page_length),
		"total": total,
		"has_more": start + page_length < total,
	}


@frappe.whitelist()
def get_deadline_agenda(start=0, page_length=8):
	_require_desk_user()
	start, page_length = _integer(start, 0), _integer(page_length, 1, 20)
	dates = _dates()
	if not frappe.has_permission("Task", "read"):
		return {"state": "no_access", **dates}
	filters = _task_filters("upcoming", dates)
	total = _task_count(filters)
	return {
		"state": "ready",
		**dates,
		"rows": _task_rows(filters, start, page_length),
		"total": total,
		"has_more": start + page_length < total,
	}


@frappe.whitelist()
def get_incoming_mail(page_length=5):
	_require_desk_user()
	page_length = _integer(page_length, 1, 5)
	if not frappe.has_permission("Communication", "read"):
		return {"state": "no_access", "accounts": [], "rows": []}
	accounts = frappe.get_all(
		"User Email",
		filters={"parent": frappe.session.user, "parenttype": "User"},
		fields=["email_account", "email_id"],
		order_by="idx asc",
	)
	accounts = list(
		{
			row.email_account: {"name": row.email_account, "email_id": row.email_id}
			for row in accounts
			if row.email_account
		}.values()
	)
	if not accounts:
		return {"state": "no_account", "accounts": [], "rows": []}
	filters = {
		"communication_type": "Communication",
		"communication_medium": "Email",
		"sent_or_received": "Received",
		"status": "Open",
		"email_status": ["not in", ["Spam", "Trash"]],
		"email_account": ["in", [account["name"] for account in accounts]],
	}
	rows, offset = [], 0
	while len(rows) < page_length:
		candidates = frappe.get_list(
			"Communication",
			filters=filters,
			fields=["name"],
			order_by="communication_date desc, name desc",
			offset=offset,
			limit=MAIL_BATCH_SIZE,
		)
		for candidate in candidates:
			if not frappe.has_permission("Communication", "read", doc=candidate.name):
				continue
			doc = frappe.get_doc("Communication", candidate.name)
			preview = " ".join(html_to_plain_text(doc.content or "").split())
			rows.append(
				{
					"name": doc.name,
					"sender": doc.sender,
					"sender_full_name": doc.sender_full_name,
					"subject": doc.subject,
					"communication_date": doc.communication_date,
					"email_account": doc.email_account,
					"preview": preview if len(preview) <= 180 else preview[:179] + "…",
					"has_attachment": bool(doc.has_attachment),
				}
			)
			if len(rows) == page_length:
				break
		if len(candidates) < MAIL_BATCH_SIZE:
			break
		offset += len(candidates)
	return {"state": "ready", "accounts": accounts, "rows": rows}
