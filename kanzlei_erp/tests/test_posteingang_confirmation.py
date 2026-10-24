"""AP-07 real transactions and synthetic documents; no provider network."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

import frappe

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangConfirmation(PosteingangTestCase):
	def setUp(self):
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		self.assertTrue(Path(frappe.get_app_path("kanzlei_erp", "posteingang_confirmation.py")).exists(), "AP-07 confirmation missing")
		from kanzlei_erp import posteingang_confirmation as service
		from kanzlei_erp import posteingang_jobs as jobs
		from kanzlei_erp import posteingang_matching as matching

		self.service, self.jobs = service, jobs
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		frappe.share.add("Customer", self.customer.name, self.manager.name, read=1, write=1)
		self.manager.add_roles("Projects User")
		self.doc = self.letter()
		matching.select_customer(self.doc.name, self.customer.name, "Original reviewed", 1, "customer")
		jobs.manual_review(self.doc.name, 2, "manual")
		self.doc.reload()

	def state(self):
		return self.service.get_review_state(self.doc.name)

	def add(self):
		state = self.state()
		self.service.add_manual_action(self.doc.name, "Synthetic work", "Synthetic description", state["revision"], frappe.generate_hash())
		return self.state()

	def final(self, assigned=True, scheduled=True):
		state = self.add()
		review = state["review"]
		review.update(mode="confirm", human_verified=True)
		action = review["actions"][0]
		action.update(decision="accept", assigned_to=self.reader.name if assigned else None,
			assignment_decision="assigned" if assigned else "unassigned", assignment_reason=None if assigned else "Plan assignment later",
			internal_date="2026-11-18" if scheduled else None, scheduling_decision="scheduled" if scheduled else "unscheduled",
			scheduling_reason=None if scheduled else "Plan date later", needs_planning=not (assigned and scheduled))
		return review, state["document_fields"]

	def test_pending_and_unknown_customer_never_confirm(self):
		state = self.add()
		review = state["review"]
		review.update(mode="confirm", human_verified=True)
		with self.assertRaises(frappe.ValidationError):
			self.service.confirm(self.doc.name, review, state["document_fields"], "pending")
		self.assertFalse(frappe.db.get_value("Incoming Letter", self.doc.name, "confirmed_customer"))

	def test_no_actions_requires_explicit_reason_and_replays_completed(self):
		state = self.state()
		review = state["review"]
		review.update(mode="confirm", human_verified=True)
		with self.assertRaises(frappe.ValidationError):
			self.service.confirm(self.doc.name, review, state["document_fields"], "empty")
		review["no_actions_reason"] = "Informational original reviewed"
		result = self.service.confirm(self.doc.name, review, state["document_fields"], "empty")
		self.assertEqual(result["status"], "Completed")
		self.assertEqual(result["actions"], [])
		self.assertTrue(self.service.confirm(self.doc.name, review, state["document_fields"], "new-key")["replayed"])
		review["no_actions_reason"] = "different"
		with self.assertRaisesRegex(frappe.ValidationError, "ALREADY_COMPLETED"):
			self.service.confirm(self.doc.name, review, state["document_fields"], "third-key")

	def test_confirm_creates_one_task_and_standard_assignment(self):
		review, fields = self.final()
		result = self.service.confirm(self.doc.name, review, fields, "confirm")
		task = frappe.get_doc("Task", result["actions"][0]["task"])
		self.assertEqual(task.kanzlei_customer, self.customer.name)
		self.assertEqual(str(frappe.utils.getdate(task.exp_start_date)), "2026-11-18")
		self.assertEqual(str(frappe.utils.getdate(task.exp_end_date)), "2026-11-18")
		self.assertEqual(task.kanzlei_incoming_action_id, review["actions"][0]["action_id"])
		self.assertEqual(frappe.db.count("ToDo", {"reference_type": "Task", "reference_name": task.name, "allocated_to": self.reader.name}), 1)
		self.assertTrue(self.service.confirm(self.doc.name, review, fields, "confirm")["replayed"])
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": self.doc.name}), 1)

	def test_explicit_unplanned_task_has_no_invented_dates(self):
		review, fields = self.final(False, False)
		result = self.service.confirm(self.doc.name, review, fields, "unplanned")
		task = frappe.get_doc("Task", result["actions"][0]["task"])
		self.assertIsNone(task.exp_start_date)
		self.assertIsNone(task.exp_end_date)
		self.assertTrue(task.kanzlei_incoming_needs_planning)

	def test_assignment_failure_rolls_back_entire_confirmation(self):
		review, fields = self.final()
		before = self.state()["revision"]
		with patch("frappe.desk.form.assign_to.add", side_effect=frappe.ValidationError("unsafe synthetic error")):
			with self.assertRaisesRegex(frappe.ValidationError, "TASK_CREATION_FAILED"):
				self.service.confirm(self.doc.name, review, fields, "rollback")
		self.assertEqual(self.state()["revision"], before)
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": self.doc.name}), 0)
		self.assertEqual(self.service.confirm(self.doc.name, review, fields, "rollback")["status"], "Completed")

	def test_full_snapshot_and_customer_selection_are_server_bound(self):
		state = self.add()
		review = copy.deepcopy(state["review"])
		review["actions"] = []
		with self.assertRaises(frappe.ValidationError):
			self.service.save_review(self.doc.name, review, state["document_fields"], "omit")
		review = state["review"]
		review["customer_reason"] = "Bypass AP06"
		with self.assertRaises(frappe.ValidationError):
			self.service.save_review(self.doc.name, review, state["document_fields"], "selection")

	def test_revision_conflict_and_rights_before_replay(self):
		state = self.add()
		self.service.save_review(self.doc.name, state["review"], state["document_fields"], "save")
		with self.assertRaisesRegex(frappe.ValidationError, "REVISION_CONFLICT"):
			self.service.save_review(self.doc.name, state["review"], state["document_fields"], "other")
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.operator.name)
		frappe.set_user(self.operator.name)
		with self.assertRaises(frappe.PermissionError):
			self.service.save_review(self.doc.name, state["review"], state["document_fields"], "save")

	def test_apply_result_creates_new_ids_and_preserves_old_snapshot(self):
		from kanzlei_erp.tests.test_posteingang_matching import TestPosteingangMatching

		self.doc = TestPosteingangMatching.successful_letter(self)
		state = self.state()
		self.service.apply_result(self.doc.name, state["proposal"]["run"], state["revision"], "apply", False)
		first = self.state()
		self.assertEqual(len(first["review"]["actions"]), 1)
		self.assertIsNone(first["review"]["actions"][0]["external_date"])
		with self.assertRaisesRegex(frappe.ValidationError, "REPLACE_ACKNOWLEDGEMENT_REQUIRED"):
			self.service.apply_result(self.doc.name, first["proposal"]["run"], first["revision"], "replace", False)
		self.service.apply_result(self.doc.name, first["proposal"]["run"], first["revision"], "replace", True)
		self.assertNotEqual(first["review"]["actions"][0]["action_id"], self.state()["review"]["actions"][0]["action_id"])
		event = frappe.db.get_value("Incoming Letter Event", {"letter": self.doc.name, "event_type": "apply_result"}, "payload_json", order_by="creation desc")
		self.assertTrue(json.loads(event)["before"]["review"]["actions"])

	def correction(self):
		self.assertTrue(hasattr(self.service, "create_correction"), "AP-07 correction missing")
		review, fields = self.final(False, False)
		confirmed = self.service.confirm(self.doc.name, review, fields, "initial")
		frappe.set_user(self.manager.name)
		return self.service.create_correction(self.doc.name, confirmed["revision"], "correction", "Wrong synthetic Customer", True, False, None), confirmed

	def test_correction_quarantines_original_and_preserves_tasks(self):
		result, confirmed = self.correction()
		old = frappe.get_doc("Incoming Letter", self.doc.name)
		new = frappe.get_doc("Incoming Letter", result["replacement"])
		self.assertEqual((old.status, old.correction_quarantined, new.status), ("Cancelled", 1, "Received"))
		self.assertNotEqual(old.original_file, new.original_file)
		self.assertEqual(old.content_sha256, new.content_sha256)
		self.assertFalse(new.selected_customer)
		self.assertFalse(new.current_run)
		self.assertEqual(new.actions, [])
		self.assertEqual(frappe.db.get_value("Task", confirmed["actions"][0]["task"], "status"), "Open")
		again = self.service.create_correction(self.doc.name, confirmed["revision"], "correction", "Wrong synthetic Customer", True, False, None)
		self.assertTrue(again["replayed"])
		self.assertEqual(again["replacement"], new.name)
		frappe.set_user(self.reader.name)
		from kanzlei_erp.posteingang_task_access import task_access
		self.assertFalse(task_access(confirmed["actions"][0]["task"]))

	def test_replacement_requires_admin_disposition_then_operator_can_confirm(self):
		from kanzlei_erp import posteingang_matching as matching

		result, confirmed = self.correction()
		new = frappe.get_doc("Incoming Letter", result["replacement"])
		disposition = self.service.correction_disposition(new.name, new.revision, confirmed["actions"][0]["action_id"], "historical", "Preserve synthetic historical work", "resolve")
		self.assertEqual(disposition["status"], "Received")
		self.assertTrue(self.service.get_review_state(new.name)["correction"]["ready"])
		frappe.set_user(self.operator.name)
		matching.select_customer(new.name, self.customer.name, "New original reviewed", disposition["revision"], "replacement-customer")
		new.reload()
		self.jobs.manual_review(new.name, new.revision, "replacement-manual")
		state = self.service.get_review_state(new.name)
		self.assertNotIn("tasks", state["correction"])
		state["review"].update(mode="confirm", human_verified=True, no_actions_reason="No new work")
		self.assertEqual(self.service.confirm(new.name, state["review"], state["document_fields"], "replacement-confirm")["status"], "Completed")
		frappe.set_user(self.manager.name)
		replayed = self.service.correction_disposition(new.name, new.revision, confirmed["actions"][0]["action_id"], "historical", "Preserve synthetic historical work", "resolve")
		self.assertTrue(replayed["replayed"])

	def test_correction_works_with_disabled_feature_and_no_auto_analysis(self):
		self.assertTrue(hasattr(self.service, "create_correction"), "AP-07 correction missing")
		review, fields = self.final(False, False)
		confirmed = self.service.confirm(self.doc.name, review, fields, "initial")
		frappe.db.set_single_value("Posteingang Settings", {"enabled": 0, "auto_analysis_enabled": 1})
		frappe.set_user(self.manager.name)
		result = self.service.create_correction(self.doc.name, confirmed["revision"], "disabled-correction", "Wrong document", True, False, None)
		self.assertEqual(frappe.db.get_value("Incoming Letter", result["replacement"], "status"), "Received")
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": result["replacement"]}), 0)

	def test_all_sixteen_gold_reviews_through_runtime(self):
		from kanzlei_erp import posteingang_matching as matching
		from kanzlei_erp.tests.test_posteingang_ai import FIXTURES
		from kanzlei_erp.tests.test_posteingang_matching import TestPosteingangMatching

		for case in json.loads((FIXTURES / "manifest.json").read_text())["cases"]:
			with self.subTest(case=case["case_id"]):
				self.doc = TestPosteingangMatching.successful_letter(self, case["case_id"])
				state = self.state()
				self.service.apply_result(self.doc.name, state["proposal"]["run"], state["revision"], "apply-" + case["case_id"], False)
				gold = json.loads((FIXTURES / case["review"]).read_text())
				if gold["selected_customer"]:
					state = self.state()
					matching.select_customer(self.doc.name, self.customer.name, gold["customer_reason"], state["revision"], "select-" + case["case_id"])
				state = self.state()
				ids = {row["source_index"]: row["action_id"] for row in state["review"]["actions"]}
				gold.update(expected_revision=state["revision"], selected_customer=state["review"]["selected_customer"], customer_reason=state["review"]["customer_reason"])
				for row in gold["actions"]:
					row["action_id"] = ids[row["source_index"]]
					if row["assigned_to"]:
						row["assigned_to"] = self.reader.name
				operation = self.service.confirm if gold["mode"] == "confirm" else self.service.save_review
				result = operation(self.doc.name, gold, state["document_fields"], "gold-" + case["case_id"])
				self.assertEqual(result["status"], case["expected"]["status"])
				self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": self.doc.name}), len(case["expected"]["tasks"]))

	def test_unapplied_result_requires_current_explicit_retention(self):
		from kanzlei_erp import posteingang_matching as matching
		from kanzlei_erp.tests.test_posteingang_matching import TestPosteingangMatching

		self.doc = TestPosteingangMatching.successful_letter(self)
		state = self.state()
		matching.select_customer(self.doc.name, self.customer.name, "Reviewed original", state["revision"], "retained-customer")
		state = self.state()
		state["review"].update(mode="confirm", human_verified=True, no_actions_reason="Original checked manually")
		with self.assertRaisesRegex(frappe.ValidationError, "RESULT_NOT_APPLIED"):
			self.service.confirm(self.doc.name, state["review"], state["document_fields"], "retain")
		result = self.service.confirm(self.doc.name, state["review"], state["document_fields"], "retain", {"run": state["proposal"]["run"], "reason": "Human draft retained"})
		self.assertEqual(result["status"], "Completed")

	def test_final_event_failure_rolls_back_tasks_and_callbacks(self):
		review, fields = self.final()
		before = len(frappe.db.after_commit._functions)
		original = self.jobs._insert

		def reject(doctype, values):
			if doctype == "Incoming Letter Event" and values.get("event_type") == "confirm":
				raise RuntimeError("synthetic receipt failure")
			return original(doctype, values)

		with patch.object(self.jobs, "_insert", side_effect=reject):
			with self.assertRaisesRegex(RuntimeError, "synthetic receipt failure"):
				self.service.confirm(self.doc.name, review, fields, "event-failed")
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": self.doc.name}), 0)
		self.assertEqual(len(frappe.db.after_commit._functions), before)
		self.assertEqual(self.state()["status"], "Needs Review")

	def test_malformed_action_container_returns_safe_validation(self):
		state = self.state()
		for actions in ([None], 1):
			review = {**state["review"], "actions": actions}
			with self.assertRaisesRegex(frappe.ValidationError, "INVALID_RESULT"):
				self.service.save_review(self.doc.name, review, state["document_fields"], frappe.generate_hash())

	def test_long_extracted_metadata_fails_before_database_write(self):
		from kanzlei_erp.tests.test_posteingang_matching import TestPosteingangMatching

		self.doc = TestPosteingangMatching.successful_letter(self)
		state = self.state()
		source = state["proposal"]["extraction"]
		source["sender"]["value"] = "S" * 256
		frappe.db.set_value("Incoming Letter Run", state["proposal"]["run"], "result_json", json.dumps(source))
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_RESULT"):
			self.service.apply_result(self.doc.name, state["proposal"]["run"], state["revision"], "long-sender", False)
		self.assertFalse(frappe.db.get_value("Incoming Letter", self.doc.name, "applied_run"))

	def test_correction_preserves_pdf_extension_for_long_accepted_name(self):
		frappe.db.set_value("File", self.doc.original_file, "file_name", "a" * 130 + ".pdf")
		result, _ = self.correction()
		name = frappe.db.get_value("File", frappe.db.get_value("Incoming Letter", result["replacement"], "original_file"), "file_name")
		self.assertLessEqual(len(name), 140)
		self.assertTrue(name.endswith(".pdf"))
