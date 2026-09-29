"""Create clearly labelled, fictitious records on the local development site."""

from datetime import timedelta
import os
from pathlib import Path

import frappe
from frappe.utils import getdate, nowdate


SITE = "development.localhost"


def customer(name: str) -> str:
	existing = frappe.db.get_value("Customer", {"customer_name": name}, "name")
	if existing:
		return existing
	return frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": "Company",
			"customer_group": "Commercial",
			"territory": "All Territories",
		}
	).insert().name


def seed() -> None:
	if frappe.local.site != SITE:
		raise RuntimeError("Demo records may only be created on development.localhost")

	today = getdate(nowdate())
	mueller = customer("DEMO Müller GmbH")
	schneider = customer("DEMO Schneider e.K.")

	one_off_subject = "DEMO: Unterlagen anfordern"
	if not frappe.db.exists("Task", {"kanzlei_customer": mueller, "subject": one_off_subject}):
		frappe.get_doc(
			{
				"doctype": "Task",
				"subject": one_off_subject,
				"kanzlei_customer": mueller,
				"status": "Open",
				"exp_start_date": today + timedelta(days=3),
				"exp_end_date": today + timedelta(days=3),
			}
		).insert()

	if not frappe.db.exists(
		"Work Schedule", {"customer": schneider, "subject": "DEMO: Buchhaltung abschließen"}
	):
		first_due_date = today + timedelta(days=7)
		frappe.get_doc(
			{
				"doctype": "Work Schedule",
				"customer": schneider,
				"subject": "DEMO: Buchhaltung abschließen",
				"frequency": "Monthly",
				"first_due_date": first_due_date,
				"last_due_date": first_due_date + timedelta(days=95),
				"enabled": 1,
			}
		).insert()

	frappe.db.commit()
	print("Created or verified two DEMO Customers, one Task, and one Work Schedule.")


if __name__ == "__main__":
	os.chdir(Path(__file__).resolve().parents[1] / "development/frappe-bench/sites")
	frappe.init(site=SITE, sites_path=".")
	frappe.connect()
	try:
		seed()
	finally:
		frappe.destroy()
