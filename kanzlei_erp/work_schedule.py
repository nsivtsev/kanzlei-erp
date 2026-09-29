"""Create ordinary ERPNext Tasks for one-off and recurring Mandant work."""

from datetime import date, timedelta

import frappe
from frappe.desk.form import assign_to
from frappe.utils import getdate, nowdate

from kanzlei_erp.recurrence import occurrence_dates


def set_task_calendar_title(task, method=None):
	"""Keep a readable title for the standard Task calendar."""
	customer = task.get("kanzlei_customer")
	customer_name = frappe.db.get_value("Customer", customer, "customer_name") if customer else None
	title = f"{customer_name} — {task.subject}" if customer_name else task.subject
	task.kanzlei_calendar_title = title[:255]


def backfill_task_calendar_titles():
	"""Make Tasks created before this app readable in the calendar."""
	for name in frappe.get_all(
		"Task", filters={"kanzlei_calendar_title": ["is", "not set"]}, pluck="name"
	):
		task = frappe.get_doc("Task", name)
		set_task_calendar_title(task)
		frappe.db.set_value("Task", name, "kanzlei_calendar_title", task.kanzlei_calendar_title, update_modified=False)


def generate_for_schedule(schedule, window_start: date, window_end: date) -> list[str]:
	"""Create missing occurrences in an inclusive date window."""
	if not schedule.enabled:
		return []

	dates = occurrence_dates(
		getdate(schedule.first_due_date),
		schedule.frequency,
		getdate(window_start),
		getdate(window_end),
		getdate(schedule.last_due_date) if schedule.last_due_date else None,
	)
	created = []
	for due_date in dates:
		key = f"{schedule.name}:{due_date.isoformat()}"
		if frappe.db.exists("Task", {"kanzlei_occurrence_key": key}):
			continue
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"subject": schedule.subject,
				"status": "Open",
				"exp_start_date": due_date,
				"exp_end_date": due_date,
				"kanzlei_customer": schedule.customer,
				"kanzlei_work_schedule": schedule.name,
				"kanzlei_occurrence_key": key,
			}
		).insert(ignore_permissions=True)
		if schedule.assigned_to:
			assign_to.add(
				{
					"assign_to": [schedule.assigned_to],
					"doctype": "Task",
					"name": task.name,
					"description": schedule.subject,
				}
			)
		created.append(task.name)
	return created


def generate_due_tasks(today: date | None = None):
	"""Daily scheduler entry point; keep the coming year visible."""
	current = getdate(today) if today else getdate(nowdate())
	for name in frappe.get_all("Work Schedule", filters={"enabled": 1}, pluck="name"):
		schedule = frappe.get_doc("Work Schedule", name)
		generate_for_schedule(schedule, current - timedelta(days=30), current + timedelta(days=365))
