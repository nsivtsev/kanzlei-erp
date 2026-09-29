from datetime import date, timedelta
import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import getdate, nowdate

from kanzlei_erp.work_schedule import backfill_task_calendar_titles, generate_for_schedule


class TestWorkSchedule(FrappeTestCase):
	def setUp(self):
		self.customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Kanzlei Test {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()

	def make_schedule(self, **overrides):
		values = {
			"doctype": "Work Schedule",
			"customer": self.customer.name,
			"subject": "Prepare bookkeeping",
			"frequency": "Monthly",
			"first_due_date": date(2026, 9, 10),
			"enabled": 1,
		}
		values.update(overrides)
		return frappe.get_doc(values).insert()

	def test_generates_customer_tasks_once_per_due_date(self):
		schedule = self.make_schedule()
		generate_for_schedule(schedule, date(2026, 9, 1), date(2026, 11, 30))
		generate_for_schedule(schedule, date(2026, 9, 1), date(2026, 11, 30))
		tasks = frappe.get_all(
			"Task",
			filters={
				"kanzlei_work_schedule": schedule.name,
				"exp_end_date": ["between", ["2026-09-01", "2026-11-30"]],
			},
			fields=["name", "kanzlei_customer", "exp_end_date", "kanzlei_calendar_title"],
			order_by="exp_end_date asc",
		)
		self.assertEqual(len(tasks), 3)
		self.assertEqual(
			[getdate(task.exp_end_date) for task in tasks],
			[date(2026, 9, 10), date(2026, 10, 10), date(2026, 11, 10)],
		)
		self.assertTrue(all(task.kanzlei_customer == self.customer.name for task in tasks))
		self.assertTrue(all(self.customer.customer_name in task.kanzlei_calendar_title for task in tasks))

	def test_completed_occurrence_does_not_change_the_next_one(self):
		schedule = self.make_schedule()
		generate_for_schedule(schedule, date(2026, 9, 1), date(2026, 10, 31))
		tasks = frappe.get_all(
			"Task", filters={"kanzlei_work_schedule": schedule.name}, fields=["name", "status"], order_by="exp_end_date asc"
		)
		first = frappe.get_doc("Task", tasks[0].name)
		first.status = "Completed"
		first.save()
		self.assertEqual(frappe.db.get_value("Task", tasks[1].name, "status"), "Open")

	def test_disabled_schedule_creates_nothing(self):
		schedule = self.make_schedule(enabled=0)
		generate_for_schedule(schedule, date(2026, 9, 1), date(2026, 11, 30))
		self.assertFalse(frappe.db.exists("Task", {"kanzlei_work_schedule": schedule.name}))

	def test_new_schedule_populates_calendar_immediately(self):
		first_due_date = getdate(nowdate()) + timedelta(days=7)
		schedule = self.make_schedule(first_due_date=first_due_date)
		self.assertTrue(
			frappe.db.exists(
				"Task", {"kanzlei_work_schedule": schedule.name, "exp_end_date": first_due_date}
			)
		)

	def test_extending_schedule_generates_new_tasks_immediately(self):
		first_due_date = getdate(nowdate()) + timedelta(days=7)
		schedule = self.make_schedule(
			first_due_date=first_due_date, last_due_date=first_due_date + timedelta(days=35)
		)
		before = frappe.db.count("Task", {"kanzlei_work_schedule": schedule.name})
		schedule.last_due_date = first_due_date + timedelta(days=95)
		schedule.save()
		after = frappe.db.count("Task", {"kanzlei_work_schedule": schedule.name})
		self.assertGreater(after, before)

	def test_generated_task_is_assigned_to_selected_user(self):
		first_due_date = getdate(nowdate()) + timedelta(days=7)
		schedule = self.make_schedule(first_due_date=first_due_date, assigned_to="Administrator")
		task_name = frappe.db.get_value(
			"Task", {"kanzlei_work_schedule": schedule.name, "exp_end_date": first_due_date}, "name"
		)
		self.assertTrue(
			frappe.db.exists(
				"ToDo",
				{"reference_type": "Task", "reference_name": task_name, "allocated_to": "Administrator"},
			)
		)

	def test_one_off_task_gets_customer_in_calendar_title(self):
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"subject": "Answer client request",
				"kanzlei_customer": self.customer.name,
				"exp_start_date": date(2026, 9, 14),
				"exp_end_date": date(2026, 9, 14),
			}
		).insert()
		self.assertEqual(task.kanzlei_calendar_title, f"{self.customer.customer_name} — Answer client request")

	def test_task_calendar_has_customer_filter_and_title(self):
		from frappe.desk.form.meta import FormMeta

		calendar_js = FormMeta("Task").get("__calendar_js")
		self.assertIn("kanzlei_calendar_title", calendar_js)
		self.assertIn("kanzleiTaskFilterBar", calendar_js)
		self.assertTrue(frappe.get_meta("Task").get_field("kanzlei_customer").in_standard_filter)

	def test_calendar_api_returns_mandant_title(self):
		from frappe.desk.calendar import get_events

		task = frappe.get_doc(
			{
				"doctype": "Task",
				"subject": "Review documents",
				"kanzlei_customer": self.customer.name,
				"exp_start_date": date(2026, 9, 14),
				"exp_end_date": date(2026, 9, 14),
			}
		).insert()
		events = get_events(
			"Task",
			"2026-09-01",
			"2026-09-30",
			json.dumps(
				{"start": "exp_start_date", "end": "exp_end_date", "title": "kanzlei_calendar_title"}
			),
			filters=json.dumps([["Task", "kanzlei_customer", "=", self.customer.name]]),
		)
		self.assertEqual([event.name for event in events if event.name == task.name], [task.name])
		self.assertEqual(
			next(event.kanzlei_calendar_title for event in events if event.name == task.name),
			f"{self.customer.customer_name} — Review documents",
		)

	def test_existing_task_title_can_be_backfilled(self):
		task = frappe.get_doc({"doctype": "Task", "subject": "Existing task"}).insert()
		frappe.db.set_value("Task", task.name, "kanzlei_calendar_title", None, update_modified=False)
		backfill_task_calendar_titles()
		self.assertEqual(frappe.db.get_value("Task", task.name, "kanzlei_calendar_title"), "Existing task")
