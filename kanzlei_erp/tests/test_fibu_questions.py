"""Acceptance tests for staff-managed questions, independent of mail delivery."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import nowdate

from kanzlei_erp.tests import test_fibu_package


class TestFiBuQuestions(FrappeTestCase):
	def setUp(self):
		test_fibu_package.TestFiBuPackage.setUp(self)
		self.package = test_fibu_package.TestFiBuPackage.make_package(self)
		self.contact = frappe.get_doc({"doctype": "Contact", "first_name": "Question contact",
			"links": [{"link_doctype": "Customer", "link_name": self.customer.name}]}).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def api(self):
		from kanzlei_erp import fibu_questions
		return fibu_questions

	def question(self, **values):
		return frappe.get_doc("Task", self.api().create_question("FiBu Package", self.package.name,
			{"subject": "Missing statement", **values}))

	def action(self, task, action, **values):
		self.api().update_question(task.name, action, values, str(task.modified))
		return task.reload()

	def request(self, task):
		return self.action(task, "request", recipient=self.contact.name, requested_on=nowdate(),
			expected_response_on=nowdate(), note="Requested by phone", reminder_on=nowdate())

	def test_full_cycle_partial_answer_repeat_request_and_explicit_resolution(self):
		self.assertTrue(hasattr(self.api(), "create_question"))
		task = self.question()
		self.assertEqual(task.kanzlei_question_state, "Draft")
		self.assertEqual(task.kanzlei_question_responsible, "Administrator")
		self.request(task)
		self.assertEqual(task.status, "Working")
		self.action(task, "response", answered_on=nowdate(), answer="Only 16–20 September",
			is_partial=1, remaining="21–30 September still missing")
		self.assertEqual(task.kanzlei_question_state, "Answer Received")
		self.assertEqual(task.status, "Pending Review")
		self.assertFalse(task.kanzlei_question_reminder_on)
		with self.assertRaisesRegex(frappe.ValidationError, "unfinished"):
			self.package.reload().close("Too early")
		self.package.reload()
		self.request(task)
		self.action(task, "response", answered_on=nowdate(), answer="All remaining dates", is_partial=0)
		self.action(task, "resolve", decision="Coverage reviewed")
		self.assertEqual(task.status, "Completed")
		self.assertEqual(task.kanzlei_question_decided_by, "Administrator")
		self.assertEqual(len(task.kanzlei_question_events), 6)
		self.package.reload().close("Complete")

	def test_direct_save_bulk_completion_history_forgery_and_context_change_are_rejected(self):
		task = self.question()
		for field, value in [("status", "Completed"), ("kanzlei_question_state", "Resolved"),
			("kanzlei_work_kind", "Task"), ("kanzlei_fibu_package", ""),
			("kanzlei_question_decided_by", "Administrator")]:
			task.set(field, value)
			with self.assertRaises(frappe.ValidationError):
				task.save()
			task.reload()
		from erpnext.projects.doctype.task.task import set_multiple_status
		with self.assertRaises(frappe.ValidationError):
			set_multiple_status(json.dumps([task.name]), "Completed")
		task.reload().kanzlei_question_events[0].recorded_by = "Guest"
		with self.assertRaises(frappe.ValidationError):
			task.save()
		task.reload().kanzlei_question_events = []
		with self.assertRaises(frappe.ValidationError):
			task.save()

	def test_required_values_and_cancel_reopen_preserve_history(self):
		task = self.question()
		for action, values in [("request", {}), ("resolve", {}), ("cancel", {}),
			("response", {"answer": "Partial", "answered_on": nowdate(), "is_partial": 1})]:
			with self.assertRaises(frappe.ValidationError):
				self.action(task, action, **values)
			task.reload()
		self.action(task, "resolve", decision="Found in existing documents")
		self.action(task, "reopen", reason="New clarification")
		self.assertEqual(task.kanzlei_question_state, "Draft")
		self.assertFalse(task.kanzlei_question_decided_at)
		self.assertIn("Found in existing documents", task.kanzlei_question_events[1].after_json)
		self.action(task, "cancel", reason="Duplicate question")
		self.assertEqual(task.status, "Cancelled")

	def test_stale_versions_and_idempotent_creation(self):
		task = self.question(creation_key="test-click")
		self.assertEqual(task.name, self.question(creation_key="test-click").name)
		old = str(task.modified)
		self.action(task, "edit", operation="Payment 123")
		with self.assertRaises(frappe.ValidationError):
			self.api().update_question(task.name, "resolve", {"decision": "Done"}, old)

	def test_reminders_are_once_per_cycle_and_never_email(self):
		from unittest.mock import patch
		task = self.question()
		self.request(task)
		with patch("frappe.sendmail") as sendmail:
			self.api().send_question_reminders()
			self.api().send_question_reminders()
			sendmail.assert_not_called()
		self.assertEqual(frappe.db.count("Notification Log", {"document_type": "Task", "document_name": task.name}), 1)
		task.reload()
		self.assertEqual([e.action for e in task.kanzlei_question_events].count("Reminder Sent"), 1)
		self.action(task, "edit", reminder_on="")
		self.action(task, "edit", reminder_on=nowdate())
		self.api().send_question_reminders()
		self.assertEqual(frappe.db.count("Notification Log", {"document_type": "Task", "document_name": task.name}), 2)
		task.reload()
		self.action(task, "response", answered_on=nowdate(), answer="Received", is_partial=0)
		self.api().send_question_reminders()
		self.assertEqual(frappe.db.count("Notification Log", {"document_type": "Task", "document_name": task.name}), 2)

	def test_overdue_is_independent_and_copy_starts_new_work(self):
		task = self.question()
		self.request(task)
		task.db_set("status", "Overdue", update_modified=False)
		self.assertEqual(task.reload().kanzlei_question_state, "Waiting")
		copied = frappe.copy_doc(task).insert()
		self.assertEqual(copied.kanzlei_question_state, "Draft")
		self.assertEqual(copied.status, "Open")
		self.assertFalse(copied.kanzlei_question_requested_on)
		self.assertEqual(len(copied.kanzlei_question_events), 1)

	def test_unrelated_edit_preserves_sent_reminder_cycle(self):
		task = self.question(reminder_on=nowdate())
		self.api().send_question_reminders()
		task.reload()
		cycle = task.kanzlei_question_reminder_cycle
		self.action(task, "edit", operation="Updated bookkeeping reference")
		self.assertEqual(task.kanzlei_question_reminder_cycle, cycle)
		self.api().send_question_reminders()
		self.assertEqual(frappe.db.count("Notification Log", {"document_type": "Task", "document_name": task.name}), 1)

	def test_legacy_initialization_is_explicit(self):
		task = frappe.get_doc({"doctype": "Task", "subject": "Old question", "kanzlei_work_kind": "Question",
			"kanzlei_fibu_package": self.package.name}).insert()
		# Simulate a persisted pre-migration Task without inferring its prior requests.
		frappe.db.set_value("Task", task.name, "kanzlei_question_state", "")
		frappe.db.delete("FiBu Question Event", {"parent": task.name})
		task.reload()
		self.assertFalse(task.get("kanzlei_question_state"))
		self.action(task, "initialize")
		self.assertEqual(task.kanzlei_question_state, "Draft")
		self.assertFalse(task.kanzlei_question_requested_on)

	def test_wrong_contact_source_and_public_file_are_rejected(self):
		contact = frappe.get_doc({"doctype": "Contact", "first_name": "Unrelated"}).insert()
		with self.assertRaises(frappe.ValidationError):
			self.question(recipient=contact.name)
		with self.assertRaises(frappe.ValidationError):
			self.question(entry_key="wrong-source")
		from frappe.utils.file_manager import save_file
		file = save_file("question-public.txt", b"test", "Customer", self.customer.name, is_private=0)
		with self.assertRaises(frappe.ValidationError):
			self.question(file=file.name)

	def test_legacy_initialization_rejects_existing_foreign_assignment_access(self):
		from frappe.desk.form.assign_to import add
		staff = frappe.get_doc({"doctype": "User", "email": f"old-q-{frappe.generate_hash(length=8)}@example.invalid",
			"first_name": "Legacy question participant", "send_welcome_email": 0,
			"roles": [{"role": "Projects User"}, {"role": "Sales User"}]}).insert()
		other = frappe.get_doc({"doctype": "Customer", "customer_name": "_Legacy other " + frappe.generate_hash(length=8),
			"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories"}).insert()
		frappe.get_doc({"doctype": "User Permission", "user": staff.name, "allow": "Customer", "for_value": other.name}).insert()
		task = self.question()
		frappe.db.set_value("Task", task.name, "kanzlei_question_state", "")
		frappe.db.delete("FiBu Question Event", {"parent": task.name})
		add({"doctype": "Task", "name": task.name, "assign_to": [staff.name]})
		self.assertTrue(frappe.db.exists("DocShare", {"share_doctype": "Task", "share_name": task.name, "user": staff.name}))
		task.reload()
		with self.assertRaises(frappe.ValidationError):
			self.action(task, "initialize")
		frappe.share.remove("Task", task.name, staff.name)
		for todo_name in frappe.get_all("ToDo", filters={"reference_type": "Task", "reference_name": task.name}, pluck="name"):
			todo = frappe.get_doc("ToDo", todo_name)
			todo.status = "Cancelled"
			todo.save()
		task.reload()
		self.action(task, "initialize")
		self.assertEqual(task.kanzlei_question_state, "Draft")

	def test_default_overview_contains_all_active_states(self):
		tasks = [self.question(subject=f"Question {i}") for i in range(3)]
		self.request(tasks[1])
		self.request(tasks[2])
		self.action(tasks[2], "response", answered_on=nowdate(), answer="Received", is_partial=0)
		result = self.api().get_questions({"package": self.package.name})
		self.assertEqual({row["name"] for row in result["rows"]}, {t.name for t in tasks})
		self.assertEqual(result["summary"]["Waiting"], 1)
		self.assertEqual(self.api().get_questions({"package": self.package.name}, page_length=1)["total"], 3)

	def test_ui_exposes_question_actions_overview_and_checklist_creation(self):
		from frappe.desk.form.meta import FormMeta
		self.assertIn("fibuQuestionActions", FormMeta("Task").get("__js"))
		self.assertTrue(frappe.db.exists("Page", "fibu-questions"))
		from pathlib import Path
		self.assertIn("New Question", Path(frappe.get_app_path("kanzlei_erp", "public", "js", "fibu_checklist.js")).read_text())

	def test_staff_access_and_no_assignment_grants(self):
		staff = frappe.get_doc({"doctype": "User", "email": f"q-{frappe.generate_hash(length=8)}@example.invalid",
			"first_name": "Question staff", "send_welcome_email": 0,
			"roles": [{"role": "Projects User"}, {"role": "Sales User"}, {"role": "Inbox User"}]}).insert()
		frappe.get_doc({"doctype": "User Permission", "user": staff.name, "allow": "Customer", "for_value": self.customer.name}).insert()
		other = frappe.get_doc({"doctype": "Customer", "customer_name": "_Question other " + frappe.generate_hash(length=8),
			"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories"}).insert()
		other_package = test_fibu_package.TestFiBuPackage.make_package(self, customer=other.name)
		task = self.question(responsible=staff.name)
		other_task = frappe.get_doc("Task", self.api().create_question("FiBu Package", other_package.name, {"subject": "Other"}))
		frappe.set_user(staff.name)
		self.request(task)
		self.assertIn(task.name, [r.name for r in self.api().get_questions()["rows"]])
		self.assertNotIn(other_task.name, [r.name for r in self.api().get_questions()["rows"]])
		with self.assertRaises(frappe.PermissionError):
			self.api().update_question(other_task.name, "edit", {}, str(other_task.modified))
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			self.api().update_question(other_task.name, "edit", {"responsible": staff.name}, str(other_task.modified))
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": "Task", "share_name": other_task.name, "user": staff.name}))
		from frappe.desk.form.assign_to import add
		with self.assertRaises(frappe.ValidationError):
			add({"doctype": "Task", "name": other_task.name, "assign_to": [staff.name]})
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": "Task", "share_name": other_task.name, "user": staff.name}))
		from frappe.utils.file_manager import save_file
		file = save_file("question-restricted.txt", b"Private evidence", "Customer", other.name, is_private=1)
		communication = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
			"communication_medium": "Phone", "subject": "Restricted", "content": "Other Mandant",
			"reference_doctype": "Customer", "reference_name": other.name}).insert()
		frappe.set_user(staff.name)
		with self.assertRaises(frappe.PermissionError):
			self.action(task, "edit", file=file.name)
		task.reload()
		with self.assertRaises(frappe.PermissionError):
			self.action(task, "request", recipient=self.contact.name, requested_on=nowdate(),
				expected_response_on=nowdate(), communication=communication.name)
		frappe.set_user("Administrator")
		task.reload()
		self.action(task, "edit", reminder_on=nowdate())
		staff.enabled = 0
		staff.save()
		self.api().send_question_reminders()
		self.assertFalse(frappe.db.exists("Notification Log", {"document_type": "Task", "document_name": task.name}))
		row = next(r for r in self.api().get_questions({"package": self.package.name})["rows"] if r.name == task.name)
		self.assertTrue(row.reminder_issue)

	def test_standard_assignments_and_dependencies_remain_independent(self):
		from frappe.desk.form.assign_to import add
		task = self.question()
		dependency = frappe.get_doc({"doctype": "Task", "subject": "Required preparation",
			"kanzlei_fibu_package": self.package.name}).insert()
		task.append("depends_on", {"task": dependency.name})
		task.save()
		add({"doctype": "Task", "name": task.name, "assign_to": ["Administrator"], "notify": 0})
		task.reload()
		with self.assertRaises(frappe.ValidationError):
			self.action(task, "resolve", decision="Cannot bypass dependency")
		dependency.status = "Completed"
		dependency.save()
		task.reload()
		self.action(task, "resolve", decision="Dependency completed")
		self.assertEqual(task.kanzlei_question_responsible, "Administrator")
		self.assertEqual(task.depends_on[0].task, dependency.name)
		self.assertFalse(frappe.db.exists("ToDo", {"reference_type": "Task", "reference_name": task.name, "status": "Open"}))

	def test_supplement_and_same_communication_questions_are_independent(self):
		self.package.close("Initial closed work")
		supplement = frappe.get_doc({"doctype": "FiBu Supplement", "package": self.package.name, "reason": "Late receipt"}).insert()
		communication = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
			"communication_medium": "Phone", "subject": "Shared response", "content": "Two issues",
			"reference_doctype": "Customer", "reference_name": self.customer.name}).insert()
		first, second = [frappe.get_doc("Task", self.api().create_question("FiBu Supplement", supplement.name, {"subject": str(i)})) for i in range(2)]
		for task in (first, second):
			self.request(task)
			self.action(task, "response", answered_on=nowdate(), answer="Response", communication=communication.name, is_partial=0)
		self.action(first, "resolve", decision="Verified first issue")
		self.assertEqual(second.reload().kanzlei_question_state, "Answer Received")
		with self.assertRaises(frappe.ValidationError):
			supplement.close("Too early")
		self.action(second, "cancel", reason="No longer relevant")
		supplement.reload().close("All late issues handled")
		with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
			self.action(first, "reopen", reason="Cannot change closed history")

	def test_direct_child_apis_cannot_forge_or_delete_history(self):
		from frappe.client import save
		task = self.question()
		event = frappe.get_doc("FiBu Question Event", task.kanzlei_question_events[0].name)
		event.recorded_by = "Guest"
		with self.assertRaises(frappe.ValidationError):
			save(event.as_json())
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("FiBu Question Event", event.name)
		self.assertEqual(task.reload().kanzlei_question_events[0].recorded_by, "Administrator")
		other = self.question()
		for replacement in (frappe.generate_hash(length=10), other.kanzlei_question_events[0].name):
			task.kanzlei_question_events[0].name = replacement
			with self.assertRaises(frappe.ValidationError):
				task.save()
			task.reload()

	def test_standard_form_question_is_managed_from_creation(self):
		task = frappe.get_doc({"doctype": "Task", "subject": "Standard form", "kanzlei_work_kind": "Question",
			"kanzlei_fibu_package": self.package.name}).insert()
		self.assertEqual(task.kanzlei_question_state, "Draft")
		task.status = "Completed"
		with self.assertRaises(frappe.ValidationError):
			task.save()

	def test_project_bulk_completion_cannot_bypass_decision_and_active_state(self):
		task = self.question()
		project = frappe.get_doc({"doctype": "Project", "project_name": "Question project " + frappe.generate_hash(length=8)}).insert()
		task.project = project.name
		task.save()
		method = frappe.get_attr(frappe.override_whitelisted_method("erpnext.projects.doctype.project.project.set_project_status"))
		with self.assertRaises(frappe.ValidationError):
			method(project.name, "Completed")
		# Even a corrupted external status cannot hide or close an active question.
		task.db_set("status", "Completed")
		with self.assertRaisesRegex(frappe.ValidationError, "unfinished"):
			self.package.reload().close("Too early")
		self.assertIn(task.name, [r.name for r in self.api().get_questions({"package": self.package.name})["rows"]])

	def test_database_snapshot_conflict_becomes_a_reload_error(self):
		from unittest.mock import patch
		task = self.question()
		original = frappe.get_doc
		def read(*args, **kwargs):
			if args == ("Task", task.name) and kwargs.get("for_update"):
				raise frappe.QueryDeadlockError("Snapshot conflict")
			return original(*args, **kwargs)
		with patch("frappe.get_doc", side_effect=read):
			with self.assertRaisesRegex(frappe.ValidationError, "record changed"):
				self.action(task, "resolve", decision="Concurrent change")

	def test_template_mode_cannot_break_question_state(self):
		task = self.question()
		task.is_template = 1
		with self.assertRaises(frappe.ValidationError):
			task.save()

	def test_reminder_failure_rolls_back_marker_and_notification(self):
		from unittest.mock import patch
		task = self.question()
		self.request(task)
		with patch("kanzlei_erp.fibu_questions._event", side_effect=frappe.ValidationError("Injected event failure")):
			self.api().send_question_reminders()
		self.assertFalse(task.reload().kanzlei_question_notified_cycle)
		self.assertEqual(frappe.db.count("Notification Log", {"document_type": "Task", "document_name": task.name}), 0)
		self.api().send_question_reminders()
		self.assertEqual(frappe.db.count("Notification Log", {"document_type": "Task", "document_name": task.name}), 1)
