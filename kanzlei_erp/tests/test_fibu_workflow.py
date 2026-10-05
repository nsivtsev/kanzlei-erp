import unittest

import frappe

from kanzlei_erp.fibu_workflow import (
	validate_stage_transition,
	validate_work_context,
	validate_workflow_document,
)


class TestFiBuWorkflow(unittest.TestCase):
	def test_transition_can_skip_clarification_when_no_questions_are_needed(self):
		validate_stage_transition("Review", "Ready")

	def test_return_to_earlier_stage_requires_a_reason(self):
		with self.assertRaisesRegex(ValueError, "reason"):
			validate_stage_transition("Ready", "Review")
		validate_stage_transition("Ready", "Review", "A statement is missing")

	def test_forward_transition_cannot_skip_required_work(self):
		with self.assertRaisesRegex(ValueError, "transition"):
			validate_stage_transition("Collection", "Ready")

	def test_receipt_confirmation_is_terminal(self):
		with self.assertRaisesRegex(ValueError, "terminal"):
			validate_stage_transition("Receipt Confirmed", "Review")

	def test_unknown_stage_is_rejected(self):
		with self.assertRaisesRegex(ValueError, "stage"):
			validate_stage_transition("Unknown", "Review")

	def test_waiting_for_mandant_requires_reason_and_clears_it_when_ended(self):
		with self.assertRaisesRegex(ValueError, "waiting reason"):
			validate_work_context("Open", "Review", True, "", "Check answer", "employee@example.com")
		context = validate_work_context("Open", "Review", False, "Old reason", "Check answer", "employee@example.com")
		self.assertEqual(context["waiting_reason"], "")

	def test_open_work_requires_an_assigned_next_action_before_receipt(self):
		with self.assertRaisesRegex(ValueError, "next action"):
			validate_work_context("Open", "Review", False, "", "", "")
		validate_work_context("Open", "Receipt Confirmed", False, "", "", "")
		validate_work_context("Closed", "Review", False, "", "", "")

	def test_ordinary_save_cannot_reorder_workflow_history(self):
		first_event = frappe._dict(name="event-1", idx=1, event_type="Stage Changed", from_stage="Collection", to_stage="Review")
		second_event = frappe._dict(name="event-2", idx=2, event_type="Stage Changed", from_stage="Review", to_stage="Ready")
		previous = frappe._dict(
			doctype="FiBu Package",
			preparation_stage="Ready",
			waiting_for_mandant=0,
			waiting_reason="",
			blocking_reason="",
			next_action="Send package",
			next_action_assignee="Administrator",
			next_action_task="",
			workflow_events=[first_event, second_event],
		)
		doc = frappe._dict(previous)
		doc.workflow_events = [second_event, first_event]
		doc.status = "Open"
		doc.flags = frappe._dict()
		doc.is_new = lambda: False
		doc.get_doc_before_save = lambda: previous

		with self.assertRaisesRegex(frappe.ValidationError, "Use the workflow actions"):
			validate_workflow_document(doc)

	def test_ordinary_save_cannot_append_fabricated_workflow_event(self):
		first_event = frappe._dict(name="event-1", idx=1, event_type="Stage Changed", from_stage="Collection", to_stage="Review")
		second_event = frappe._dict(name="event-2", idx=2, event_type="Stage Changed", from_stage="Review", to_stage="Ready")
		fabricated_event = frappe._dict(
			name="event-3",
			idx=3,
			event_type="Stage Changed",
			from_stage="Ready",
			to_stage="Transfer",
			recorded_by="not-the-current-user@example.invalid",
			recorded_at="2026-10-04 10:00:00",
		)
		previous = frappe._dict(
			doctype="FiBu Package",
			preparation_stage="Ready",
			waiting_for_mandant=0,
			waiting_reason="",
			blocking_reason="",
			next_action="Send package",
			next_action_assignee="Administrator",
			next_action_task="",
			workflow_events=[first_event, second_event],
		)
		doc = frappe._dict(previous)
		doc.workflow_events = [first_event, second_event, fabricated_event]
		doc.status = "Open"
		doc.flags = frappe._dict()
		doc.is_new = lambda: False
		doc.get_doc_before_save = lambda: previous

		with self.assertRaisesRegex(frappe.ValidationError, "Use the workflow actions"):
			validate_workflow_document(doc)
