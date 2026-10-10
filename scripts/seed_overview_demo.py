"""Temporary fictitious fixtures for overview acceptance; development site only.

From frappe-bench: env/bin/python /workspace/scripts/seed_overview_demo.py [cleanup]
Never use on production.
"""

import json
import os
import sys
from pathlib import Path

import frappe
from frappe.desk.form.assign_to import add
from frappe.utils import add_days, nowdate


def state_path():
	return Path(frappe.get_site_path("private", "overview-acceptance.json"))


def save_state(registry):
	path = state_path()
	temporary = path.with_suffix(".tmp")
	temporary.write_text(json.dumps(registry), encoding="utf-8")
	temporary.chmod(0o600)
	temporary.replace(path)


def seed():
	if frappe.local.site != "development.localhost":
		raise RuntimeError("Development only")
	frappe.set_user("Administrator")
	if state_path().exists():
		raise RuntimeError("Restore earlier overview fixtures first")
	admin = frappe.get_doc("User", "Administrator")
	registry = {
		"emails": [{"email_account": row.email_account} for row in admin.user_emails],
		"language": admin.language,
		"desk_theme": admin.desk_theme,
		"created": [],
	}
	save_state(registry)

	def create(values):
		doc = frappe.get_doc(values).insert(ignore_permissions=True)
		registry["created"].append([doc.doctype, doc.name])
		save_state(registry)
		return doc

	customer = create(
		{
			"doctype": "Customer",
			"customer_name": "DEMO Übersicht GmbH",
			"customer_type": "Company",
			"customer_group": "Commercial",
			"territory": "All Territories",
		}
	)
	people = [
		create(
			{
				"doctype": "User",
				"email": f"overview-qa-{first.lower()}@example.invalid",
				"first_name": first,
				"last_name": "Demo",
				"send_welcome_email": 0,
				"roles": [{"role": "Projects User"}],
			}
		)
		for first in ["Elena", "Maria"]
	]
	today = nowdate()
	for i, offset in enumerate([-2, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 2, 3, 4, 5, 6, 7, 7, 7, 7]):
		subjects = [
			"Unterlagen für den Monatsabschluss prüfen",
			"Belege mit dem Mandanten abstimmen",
			"Rückmeldung zur Lohnabrechnung vorbereiten",
			"Jahresabschluss: fehlende Dokumente und offene Fragen gemeinsam mit dem Team besprechen",
		]
		task = create(
			{
				"doctype": "Task",
				"subject": f"DEMO {i + 1:02}: {subjects[i % 4]}",
				"kanzlei_customer": customer.name,
				"exp_end_date": add_days(today, offset),
				"status": "Open",
			}
		)
		add({"doctype": "Task", "name": task.name, "assign_to": [person.name for person in people]})
	account = create(
		{
			"doctype": "Email Account",
			"email_id": "overview-qa@example.invalid",
			"email_account_name": "DEMO Übersicht",
			"enable_outgoing": 1,
			"awaiting_password": 1,
			"always_use_account_email_id_as_sender": 1,
		}
	)
	for i, subject in enumerate(
		[
			"Unterlagen für Oktober",
			"Termin zur Abstimmung",
			"Belege vollständig eingereicht",
			"Frage zur nächsten Abrechnung",
			"Bestätigung Ihrer Anfrage",
		]
	):
		create(
			{
				"doctype": "Communication",
				"communication_type": "Communication",
				"communication_medium": "Email",
				"sent_or_received": "Received",
				"status": "Open",
				"email_status": "Open",
				"email_account": account.name,
				"sender": "mandant@example.invalid",
				"sender_full_name": "Anna Beispiel",
				"subject": f"DEMO: {subject}",
				"communication_date": f"{today} 10:{i:02}:00",
				"content": "<p>Guten Tag, die angeforderten Unterlagen liegen vor. Bitte geben Sie uns kurz Bescheid, falls noch etwas fehlt.</p>",
			}
		)
	admin.set("user_emails", [{"email_account": account.name}])
	admin.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache(user="Administrator")
	print(
		"Fictitious overview fixtures ready; original user preferences saved in a private file for cleanup."
	)


def cleanup():
	if frappe.local.site != "development.localhost":
		raise RuntimeError("Development only")
	frappe.set_user("Administrator")
	if not state_path().exists():
		print("No tracked overview fixtures found.")
		return
	registry = json.loads(state_path().read_text(encoding="utf-8"))
	admin = frappe.get_doc("User", "Administrator")
	admin.set("user_emails", registry["emails"])
	admin.language = registry["language"]
	admin.desk_theme = registry["desk_theme"]
	admin.save(ignore_permissions=True)
	for doctype, name in reversed(registry["created"]):
		if doctype == "Task":
			for todo in frappe.get_all(
				"ToDo", filters={"reference_type": "Task", "reference_name": name}, pluck="name"
			):
				frappe.delete_doc("ToDo", todo, ignore_permissions=True)
		if frappe.db.exists(doctype, name):
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
	frappe.db.commit()
	state_path().unlink()
	frappe.clear_cache(user="Administrator")
	print("Overview fixtures removed; original user preferences restored.")


if __name__ == "__main__":
	os.chdir("/workspace/development/frappe-bench/sites")
	frappe.init(site="development.localhost", sites_path=".")
	frappe.connect()
	try:
		cleanup() if "cleanup" in sys.argv else seed()
	finally:
		frappe.destroy()
