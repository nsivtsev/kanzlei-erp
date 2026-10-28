"""A failed rerun must not erase an earlier successful proposal's constraints."""

import json
from pathlib import Path
from unittest.mock import patch

import frappe

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangReviewHistory(PosteingangTestCase):
	def setUp(self):
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		from kanzlei_erp import posteingang_confirmation, posteingang_jobs, posteingang_matching

		self.service, self.jobs, self.matching = posteingang_confirmation, posteingang_jobs, posteingang_matching
		self.fixtures = Path(frappe.get_app_path("kanzlei_erp", "tests", "fixtures", "posteingang"))

	def successful(self, case_id):
		from kanzlei_erp.object_storage import stored
		from kanzlei_erp.posteingang_model import create_received_letter

		case = next(row for row in json.loads((self.fixtures / "manifest.json").read_text())["cases"] if row["case_id"] == case_id)
		frappe.set_user(self.operator.name)
		self.doc = create_received_letter(actor=self.operator.name,
			original={"content": (self.fixtures / case["pdf"]).read_bytes(), "file_name": case_id + "-" + frappe.generate_hash(length=10) + ".pdf", "page_count": case["page_count"]},
			receipt={"received_date_known": case["received_date_known"], "received_date": case["received_date"]},
			operation={"key": frappe.generate_hash(length=64), "fingerprint": frappe.generate_hash(length=64)})
		file = frappe.get_doc("File", self.doc.original_file)
		if not stored(file):
			self.paths.append(Path(file.get_full_path()))
		self.matching.select_customer(self.doc.name, self.customer.name, "Original reviewed before analysis", 1, "customer")
		with patch.object(self.jobs, "_dispatch"):
			result = self.jobs.queue_analysis(self.doc.name, 2, "first-analysis")
			frappe.db.commit()
		self.assertEqual(self.jobs.process_run(result["run"]), {"code": "SUCCEEDED"})
		self.doc.reload()
		return result["run"]

	def fail_next_run(self):
		from kanzlei_erp import posteingang_ai as ai

		self.doc.reload()
		with patch.object(self.jobs, "_dispatch"):
			queued = self.jobs.queue_analysis(self.doc.name, self.doc.revision, "failed-rerun")
			frappe.db.commit()
		with patch.object(ai, "extract", return_value=ai.Failure("INVALID_RESULT", "extraction")):
			self.assertEqual(self.jobs.process_run(queued["run"]), {"code": "INVALID_RESULT"})
		self.doc.reload()
		self.assertEqual(self.doc.status, "Error")
		self.jobs.manual_review(self.doc.name, self.doc.revision, "review-after-failed-rerun")
		return queued["run"]

	def final_no_actions(self):
		state = self.service.get_review_state(self.doc.name)
		review = state["review"]
		review.update(mode="confirm", human_verified=True, no_actions_reason="No new work after manual review")
		for action in review["actions"]:
			action.update(decision="reject", rejection_reason="Handled elsewhere")
		return review, state["document_fields"]

	def test_failed_rerun_cannot_remove_applied_multiple_addressee_block(self):
		run = self.successful("AP01-06")
		state = self.service.get_review_state(self.doc.name)
		self.service.apply_result(self.doc.name, run, state["revision"], "apply-multiple", False)
		self.fail_next_run()
		review, fields = self.final_no_actions()
		with self.assertRaisesRegex(frappe.ValidationError, "MULTIPLE_ADDRESSEES"):
			self.service.confirm(self.doc.name, review, fields, "confirm-after-failed-rerun")
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": self.doc.name}), 0)

	def test_failed_rerun_cannot_silently_discard_unapplied_success(self):
		self.successful("AP01-01")
		self.fail_next_run()
		review, fields = self.final_no_actions()
		with self.assertRaisesRegex(frappe.ValidationError, "RESULT_NOT_APPLIED"):
			self.service.confirm(self.doc.name, review, fields, "discard-last-success")
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": self.doc.name}), 0)
