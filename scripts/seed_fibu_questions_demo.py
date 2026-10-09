"""Create fictitious local BL-007 acceptance data; password is read from stdin."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import frappe
from frappe.utils import nowdate
from frappe.utils.password import update_password

SITE = "development.localhost"


def seed(password):
	if frappe.local.site != SITE:
		raise RuntimeError("Question demo is restricted to development.localhost")
	frappe.set_user("Administrator")
	name = frappe.db.get_value("Customer", {"customer_name": "DEMO BL007 Rückfragen GmbH"}, "name")
	customer = frappe.get_doc("Customer", name) if name else frappe.get_doc({"doctype": "Customer",
		"customer_name": "DEMO BL007 Rückfragen GmbH", "customer_type": "Company",
		"customer_group": "Commercial", "territory": "All Territories",
		"kanzlei_data_sources": [{"category": "Bank Account", "source_title": "DEMO Account 1"},
			{"category": "Bank Account", "source_title": "DEMO Account 2", "valid_from": "2026-09-10"}]}).insert()
	staff = []
	for language in ("ru", "de"):
		user = f"bl007-demo-{language}@example.invalid"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "DEMO Questions " + language,
				"language": language, "send_welcome_email": 0,
				"roles": [{"role": "Projects User"}, {"role": "Sales User"}, {"role": "Inbox User"}]}).insert()
		if not frappe.db.exists("User Permission", {"user": user, "allow": "Customer", "for_value": customer.name}):
			frappe.get_doc({"doctype": "User Permission", "user": user, "allow": "Customer", "for_value": customer.name}).insert()
		update_password(user, password, logout_all_sessions=False)
		staff.append(user)
	service = "DEMO BL007 FiBu"
	if not frappe.db.exists("Item", service):
		frappe.get_doc({"doctype": "Item", "item_code": service,
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"), "stock_uom": "Nos", "is_stock_item": 0}).insert()
	name = frappe.db.get_value("FiBu Package", {"customer": customer.name, "service": service, "period_start": "2026-09-01"}, "name")
	package = frappe.get_doc("FiBu Package", name) if name else frappe.get_doc({"doctype": "FiBu Package",
		"customer": customer.name, "service": service, "period_type": "Monthly", "period_year": 2026,
		"period_number": 9, "responsible": staff[0], "deputy": staff[1]}).insert()
	if package.checklist[1].status == "Expected":
		package.update_checklist_entry(entry_key=package.checklist[1].entry_key, status="Partially Received",
			evidence=[{"external_url": "https://example.invalid/fictitious-statement", "covered_from": "2026-09-10", "covered_to": "2026-09-15"}],
			expected_modified=str(package.modified))
	contact_name = frappe.db.get_value("Contact", {"first_name": "DEMO BL007 Belege"}, "name")
	contact = frappe.get_doc("Contact", contact_name) if contact_name else frappe.get_doc({"doctype": "Contact",
		"first_name": "DEMO BL007 Belege", "links": [{"link_doctype": "Customer", "link_name": customer.name}]}).insert()
	frappe.db.commit()
	return {"customer": customer.name, "package": package.name, "contact": contact.name, "staff": staff}


def parallel_acceptance(package_name):
	"""Exercise two independent transactions for creation, edits and reminders."""
	from kanzlei_erp.fibu_questions import create_question, update_question
	context = frappe.get_doc("FiBu Package", package_name)
	if context.customer != "DEMO BL007 Rückfragen GmbH":
		raise RuntimeError("Concurrent acceptance requires the fictitious BL007 Mandant")
	for old_name in frappe.get_all("Task", filters={"kanzlei_fibu_package": package_name,
		"subject": ["in", ["DEMO Concurrent Reminder", "DEMO Concurrent Click"]],
		"kanzlei_question_state": ["in", ["Draft", "Waiting", "Answer Received"]]}, pluck="name"):
		old = frappe.get_doc("Task", old_name)
		update_question(old_name, "cancel", {"reason": "Previous concurrent acceptance run finished"}, str(old.modified))
	frappe.db.commit()
	run_key = frappe.generate_hash(length=24)
	name = create_question("FiBu Package", package_name, {"subject": "DEMO Concurrent Reminder",
		"responsible": context.responsible, "reminder_on": nowdate(), "creation_key": run_key + "-reminder"})
	frappe.db.commit()
	def worker(kind, args):
		frappe.init(site=SITE, sites_path=".")
		frappe.connect()
		frappe.set_user("Administrator")
		try:
			from kanzlei_erp.fibu_questions import (
				create_question,
				send_question_reminders,
				update_question,
			)
			if kind == "create":
				for attempt in range(3):
					try:
						result = create_question("FiBu Package", package_name, args)
						break
					except frappe.ValidationError:
						frappe.db.rollback()
						if attempt == 2:
							raise
			elif kind == "edit":
				try:
					update_question(name, "edit", {"operation": "Concurrent edit"}, args)
					result = "saved"
				except frappe.ValidationError:
					frappe.db.rollback()
					result = "stale"
			else:
				for attempt in range(3):
					try:
						send_question_reminders()
						break
					except frappe.RetryBackgroundJobError:
						frappe.db.rollback()
						if attempt == 2:
							raise
				result = "processed"
			frappe.db.commit()
			return result
		finally:
			frappe.destroy()
	with ThreadPoolExecutor(max_workers=2) as pool:
		created = list(pool.map(lambda _: worker("create", {"subject": "DEMO Concurrent Click", "creation_key": run_key + "-click"}), range(2)))
		modified = str(frappe.get_doc("Task", name).modified)
		edited = list(pool.map(lambda _: worker("edit", modified), range(2)))
		list(pool.map(lambda _: worker("remind", None), range(2)))
	assert created[0] == created[1], created
	assert sorted(edited) == ["saved", "stale"], edited
	frappe.db.rollback()
	task = frappe.get_doc("Task", name)
	assert frappe.db.count("Notification Log", {"document_type": "Task", "document_name": name}) == 1
	assert sum(e.action == "Reminder Sent" for e in task.kanzlei_question_events) == 1
	for task_name in (name, created[0]):
		task = frappe.get_doc("Task", task_name)
		if task.kanzlei_question_state not in ("Resolved", "Cancelled"):
			update_question(task_name, "cancel", {"reason": "Concurrent acceptance complete"}, str(task.modified))
	frappe.db.commit()
	return {"same_created_task": created[0], "edits": edited, "notification_count": 1, "reminder_events": 1}


if __name__ == "__main__":
	os.chdir(Path(__file__).resolve().parents[1] / "development/frappe-bench/sites")
	frappe.init(site=SITE, sites_path=".")
	frappe.connect()
	try:
		values = json.load(sys.stdin)
		result = seed(values["password"]) if "password" in values else parallel_acceptance(values["package"])
		print(json.dumps(result, ensure_ascii=False))
	finally:
		frappe.destroy()
