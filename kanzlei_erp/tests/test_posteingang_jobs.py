"""AP-05 workflow and worker acceptance; synthetic PDFs, never live AI."""

import io
import json
from datetime import timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe

from kanzlei_erp.tests import test_posteingang_ai as ai_tests
from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangJobs(PosteingangTestCase):
	def setUp(self):
		# Drive broadcasts are unrelated to AP-05 and scan all accumulated fixture
		# users/files. Keep actual SQL/commit/RQ, isolate notification delivery only.
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		self.assertTrue(Path(frappe.get_app_path("kanzlei_erp", "posteingang_jobs.py")).exists(), "AP-05 workflow missing")
		from kanzlei_erp import posteingang_jobs

		self.jobs = posteingang_jobs
		frappe.cache.delete_value("ap05-recovery-tick")
		self.doc = ai_tests.TestPosteingangAi.fixture_letter(self)
		self.dispatch = patch("kanzlei_erp.posteingang_jobs._dispatch")
		self.sent = self.dispatch.start()
		self.addCleanup(self.dispatch.stop)

	def queue(self, key="intent", revision=None):
		return self.jobs.queue_analysis(self.doc.name, revision or self.doc.revision, key)

	def run_doc(self, name):
		return frappe.get_doc("Incoming Letter Run", name)

	def test_queue_replay_precedes_revision_and_preserves_draft(self):
		frappe.db.set_value("Incoming Letter", self.doc.name, "summary", "Human draft")
		result = self.queue()
		self.assertEqual((result["status"], result["revision"], result["generation"]), ("Queued", 2, 1))
		self.assertEqual(self.jobs.queue_analysis(self.doc.name, 1, "intent"), {**result, "replayed": True})
		with self.assertRaisesRegex(frappe.ValidationError, "ANALYSIS_IN_PROGRESS"):
			self.queue("another", revision=2)
		self.assertEqual(frappe.db.get_value("Incoming Letter", self.doc.name, "summary"), "Human draft")
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": self.doc.name}), 1)

	def test_unknown_fields_and_bad_revision_are_rejected(self):
		for value in (True, 0, "1.0", None):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				self.jobs.queue_analysis(self.doc.name, value, "intent")
		with self.assertRaises(frappe.ValidationError):
			self.jobs.queue_analysis(self.doc.name, 1, "intent", actor="Administrator")
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": self.doc.name}), 0)

	def test_http_routing_cmd_is_allowed_but_not_other_business_fields(self):
		result = self.jobs.queue_analysis(self.doc.name, 1, "http", cmd="kanzlei_erp.posteingang_jobs.queue_analysis")
		self.assertEqual(result["status"], "Queued")
		self.assertEqual(self.jobs.get_analysis_state(self.doc.name, cmd="kanzlei_erp.posteingang_jobs.get_analysis_state")["status"], "Queued")

	def test_role_revoked_between_steps_prevents_extraction(self):
		from kanzlei_erp import posteingang_ai as ai

		result = self.queue()
		frappe.db.commit()
		recognize = ai.recognize

		def revoked(source):
			ocr = recognize(source)
			frappe.db.delete("Has Role", {"parent": self.operator.name, "role": "Posteingang Operator"})
			frappe.db.commit()
			return ocr

		with patch.object(ai, "recognize", side_effect=revoked), patch.object(ai, "extract", side_effect=AssertionError("must not call")):
			self.jobs.process_run(result["run"])
		run = self.run_doc(result["run"])
		self.assertEqual((run.status, run.error_code), ("Cancelled", "ANALYSIS_STOPPED"))
		with self.assertRaises(frappe.PermissionError):
			self.jobs.queue_analysis(self.doc.name, 1, "intent")

	def test_retry_budget_is_bounded(self):
		from kanzlei_erp import posteingang_ai as ai

		result = self.queue()
		frappe.db.commit()
		with patch.object(ai, "extract", return_value=ai.Failure("PROVIDER_UNAVAILABLE", "extraction", True)):
			for _attempt in range(3):
				run = self.run_doc(result["run"])
				with patch.object(self.jobs, "now_datetime", return_value=run.next_attempt_at + timedelta(seconds=1)):
					self.jobs.process_run(run.name)
		self.assertEqual((self.run_doc(result["run"]).status, self.run_doc(result["run"]).attempt_count), ("Failed", 3))

	def test_worker_db_outage_does_not_escape_safe_boundary(self):
		from kanzlei_erp import posteingang_ai as ai

		result = self.queue()
		frappe.db.commit()
		rollback = frappe.db.rollback
		down = False

		def broken_rollback(*args, **kwargs):
			if down:
				raise ConnectionError("private OCR and secret")
			return rollback(*args, **kwargs)

		def broken_extract(*args):
			nonlocal down
			down = True
			raise RuntimeError("private OCR and secret")

		with patch.object(ai, "extract", side_effect=broken_extract), patch.object(frappe.db, "rollback", side_effect=broken_rollback), patch("frappe.log_error") as logged:
			self.assertEqual(self.jobs.process_run(result["run"]), {"code": "INTERNAL_ERROR"})
			logged.assert_not_called()

	def test_rq_timeout_is_unknown_outcome_without_retry(self):
		from rq.timeouts import JobTimeoutException

		from kanzlei_erp import posteingang_ai as ai

		result = self.queue()
		frappe.db.commit()
		with patch.object(ai, "extract", side_effect=JobTimeoutException("private data")):
			self.assertEqual(self.jobs.process_run(result["run"]), {"code": "WORKER_INTERRUPTED"})
		self.assertEqual(self.run_doc(result["run"]).status, "Failed")

	def test_expired_lease_has_priority_over_queued_backlog(self):
		from frappe.utils import add_to_date

		self.queue()
		# Historical stale Running must not occupy expired-first forever.
		self.jobs._insert("Incoming Letter Run", {"letter": self.doc.name, "generation": 1000,
			"status": "Running", "queued_at": "2000-01-01", "lease_expires_at": "2000-01-01"})
		frappe.db.commit()
		other = ai_tests.TestPosteingangAi.fixture_letter(self)
		second = self.jobs.queue_analysis(other.name, 1, "second")
		frappe.db.commit()
		self.jobs._claim(second["run"])
		frappe.db.set_value("Incoming Letter Run", second["run"], "lease_expires_at", "2001-01-01")
		frappe.db.set_single_value("Posteingang Settings", "recovery_batch_size", 1)
		frappe.db.commit()
		self.jobs.recover_runs()
		self.assertEqual(self.run_doc(second["run"]).error_code, "WORKER_INTERRUPTED")

	def test_failed_dispatch_keeps_durable_intake(self):
		from kanzlei_erp import posteingang

		self.dispatch.stop()
		frappe.db.set_single_value("Posteingang Settings", "auto_analysis_enabled", 1)
		data = (ai_tests.FIXTURES / "pdfs/AP01-01.pdf").read_bytes() + ("\n%" + frappe.generate_hash()).encode()
		with patch("frappe.enqueue", side_effect=RuntimeError("private provider context")):
			result = posteingang._receive(io.BytesIO(data), "queue-down.pdf", "queue-down", "false")
			frappe.db.commit()
		letter = frappe.get_doc("Incoming Letter", result["letter"])
		self.assertEqual(letter.status, "Queued")
		self.assertTrue(frappe.db.exists("Incoming Letter Intake Receipt", posteingang.operation_key(self.operator.name, "queue-down")))
		self.assertTrue(frappe.get_doc("File", letter.original_file).get_content())

	def test_commit_dispatches_and_rollback_does_not(self):
		result = self.queue()
		self.sent.assert_not_called()
		frappe.db.commit()
		self.sent.assert_called_once_with(result["run"])
		doc = ai_tests.TestPosteingangAi.fixture_letter(self)
		self.jobs.queue_analysis(doc.name, 1, "rolled-back")
		frappe.db.rollback()
		self.assertEqual(self.sent.call_count, 1)

	def test_success_stages_result_without_applying_or_tasks(self):
		from kanzlei_erp import posteingang_ai

		tasks = frappe.db.count("Task")
		result = self.queue()
		frappe.db.commit()
		with patch.object(posteingang_ai.requests, "Session", side_effect=AssertionError("network")):
			self.jobs.process_run(result["run"])
		run = self.run_doc(result["run"])
		letter = frappe.get_doc("Incoming Letter", self.doc.name)
		self.assertEqual((run.status, letter.status, run.attempt_count), ("Succeeded", "Needs Review", 1))
		self.assertTrue(run.ocr_pages_json)
		self.assertTrue(run.result_json)
		self.assertFalse(letter.applied_run)
		self.assertFalse(letter.actions)
		self.assertEqual(letter.review_outcome, "pending")
		self.assertEqual(frappe.db.count("Task"), tasks)
		before = run.as_dict()
		self.jobs.process_run(run.name)
		self.assertEqual(self.run_doc(run.name).as_dict(), before)
		events = frappe.get_all("Incoming Letter Event", filters={"letter": letter.name}, fields=["revision_before", "revision_after"], order_by="revision_after")
		self.assertEqual([(e.revision_before, e.revision_after) for e in events], [(i, i + 1) for i in range(letter.revision)])

	def test_retry_uses_checkpoint_and_due_time(self):
		from kanzlei_erp import posteingang_ai as ai

		result = self.queue()
		frappe.db.commit()
		with patch.object(ai, "extract", return_value=ai.Failure("PROVIDER_UNAVAILABLE", "extraction", True, 120)):
			self.jobs.process_run(result["run"])
		run = self.run_doc(result["run"])
		self.assertEqual((run.status, run.attempt_count), ("Queued", 1))
		self.assertEqual(frappe.db.get_value("Incoming Letter", self.doc.name, "status"), "Processing")
		self.assertTrue(run.ocr_pages_json)
		self.jobs.process_run(run.name)
		self.assertEqual(self.run_doc(run.name).attempt_count, 1)
		with patch.object(self.jobs, "now_datetime", return_value=run.next_attempt_at + timedelta(seconds=1)), patch.object(ai, "recognize", side_effect=AssertionError("OCR repeated")):
			self.jobs.process_run(run.name)
		self.assertEqual(self.run_doc(run.name).status, "Succeeded")

	def test_corrupt_checkpoint_is_rejected_without_new_ocr(self):
		from kanzlei_erp import posteingang_ai as ai

		result = self.queue()
		frappe.db.commit()
		with patch.object(ai, "extract", return_value=ai.Failure("PROVIDER_UNAVAILABLE", "extraction", True)):
			self.jobs.process_run(result["run"])
		run = self.run_doc(result["run"])
		frappe.db.set_value("Incoming Letter Run", run.name, "ocr_pages_json", '{"broken": []}')
		frappe.db.commit()
		with patch.object(self.jobs, "now_datetime", return_value=run.next_attempt_at + timedelta(seconds=1)), patch.object(ai, "recognize", side_effect=AssertionError("no paid fallback")):
			self.jobs.process_run(run.name)
		run.reload()
		self.assertEqual((run.status, run.error_code, run.error_stage), ("Failed", "OCR_FAILED", "ocr"))

	def test_recovery_cursor_does_not_touch_settings_modified_or_epoch(self):
		self.queue()
		frappe.db.commit()
		before = frappe.db.get_singles_dict("Posteingang Settings")
		self.jobs.recover_runs()
		after = frappe.db.get_singles_dict("Posteingang Settings")
		self.assertEqual((before["modified"], before["analysis_configuration_epoch"]), (after["modified"], after["analysis_configuration_epoch"]))

	def test_manual_review_fences_late_response_and_reuses_ocr(self):
		from kanzlei_erp import posteingang_ai as ai

		result = self.queue()
		frappe.db.commit()
		extract = ai.extract

		def manual_during_extraction(source, ocr):
			value = extract(source, ocr)
			revision = frappe.db.get_value("Incoming Letter", self.doc.name, "revision")
			self.jobs.manual_review(self.doc.name, revision, "manual")
			frappe.db.commit()
			return value

		with patch.object(ai, "extract", side_effect=manual_during_extraction):
			self.jobs.process_run(result["run"])
		run = self.run_doc(result["run"])
		self.assertEqual(run.status, "Superseded")
		self.assertFalse(run.result_json)
		letter = frappe.get_doc("Incoming Letter", self.doc.name)
		self.assertEqual((letter.status, letter.analysis_generation), ("Needs Review", 2))
		new = self.jobs.queue_analysis(letter.name, letter.revision, "again")
		frappe.db.commit()
		with patch.object(ai, "recognize", side_effect=AssertionError("OCR repeated")):
			self.jobs.process_run(new["run"])
		self.assertEqual(self.run_doc(new["run"]).reused_ocr_from, run.name)
		self.assertEqual(self.run_doc(new["run"]).status, "Succeeded")

	def test_stopped_run_does_not_resume(self):
		result = self.queue()
		frappe.db.commit()
		frappe.db.set_single_value("Posteingang Settings", "enabled", 0)
		frappe.db.commit()
		self.jobs.process_run(result["run"])
		run = self.run_doc(result["run"])
		self.assertEqual((run.status, run.error_code), ("Cancelled", "ANALYSIS_STOPPED"))
		frappe.db.set_single_value("Posteingang Settings", "enabled", 1)
		frappe.db.commit()
		self.jobs.process_run(run.name)
		self.assertEqual(self.run_doc(run.name).status, "Cancelled")

	def test_empty_ocr_is_uncertainty_not_no_actions(self):
		from kanzlei_erp import posteingang_ai as ai

		result = self.queue()
		frappe.db.commit()
		def empty(source):
			return ai.OcrResult(source.content_sha256, None, "fake", [{"page": 1, "text": ""}])

		with patch.object(ai, "recognize", side_effect=empty), patch.object(ai.requests, "Session", side_effect=AssertionError("network")):
			self.jobs.process_run(result["run"])
		extraction = json.loads(self.run_doc(result["run"]).result_json)
		self.assertEqual((extraction["readability"], extraction["action_assessment"], extraction["actions"]), ("unreadable", "unknown", []))
		self.assertEqual(frappe.db.get_value("Incoming Letter", self.doc.name, "review_outcome"), "pending")

	def test_disabled_initiator_is_stopped_before_provider(self):
		from kanzlei_erp import posteingang_ai as ai

		result = self.queue()
		frappe.db.commit()
		frappe.db.set_value("User", self.operator.name, "enabled", 0)
		frappe.db.commit()
		with patch.object(ai, "prepare_source", side_effect=AssertionError("must not read source")):
			self.jobs.process_run(result["run"])
		self.assertEqual(self.run_doc(result["run"]).error_code, "ANALYSIS_STOPPED")

	def test_configuration_off_on_invalidates_queued_run(self):
		result = self.queue()
		frappe.db.commit()
		frappe.set_user(self.manager.name)
		for enabled in (0, 1):
			settings = frappe.get_single("Posteingang Settings")
			settings.enabled = enabled
			settings.save()
			frappe.db.commit()
		self.jobs.process_run(result["run"])
		self.assertEqual((self.run_doc(result["run"]).status, self.run_doc(result["run"]).error_code), ("Cancelled", "ANALYSIS_STOPPED"))

	def test_recovery_expired_running_requires_explicit_retry(self):
		result = self.queue()
		frappe.db.commit()
		claim = self.jobs._claim(result["run"])
		self.assertTrue(claim)
		run = self.run_doc(result["run"])
		with patch.object(self.jobs, "now_datetime", return_value=run.lease_expires_at + timedelta(seconds=1)):
			self.jobs.recover_runs()
		run.reload()
		self.assertEqual((run.status, run.error_code), ("Failed", "WORKER_INTERRUPTED"))
		self.assertEqual(frappe.db.get_value("Incoming Letter", self.doc.name, "status"), "Error")

	def test_cancel_is_idempotent_when_disabled_and_conflicts_on_reason(self):
		frappe.db.set_single_value("Posteingang Settings", "enabled", 0)
		result = self.jobs.cancel(self.doc.name, 1, "cancel", "Wrong upload")
		self.assertEqual(self.jobs.cancel(self.doc.name, 1, "cancel", "Wrong upload"), {**result, "replayed": True})
		with self.assertRaisesRegex(frappe.ValidationError, "IDEMPOTENCY_CONFLICT"):
			self.jobs.cancel(self.doc.name, 1, "cancel", "Other reason")

	def test_status_and_run_projection_omit_private_state(self):
		result = self.queue()
		state = self.jobs.get_analysis_state(self.doc.name)
		self.assertEqual(state["allowed_operations"], ["manual_review", "cancel"])
		text = json.dumps(state, default=str)
		for private in ("configuration_json", "source_snapshot_json", "lease_token", "ocr_pages_json", "result_json", "api_key"):
			self.assertNotIn(private, text)
			self.assertFalse(self.run_doc(result["run"]).as_dict().get(private))

	def test_generic_write_and_terminal_write_remain_forbidden(self):
		result = self.queue()
		run = self.run_doc(result["run"])
		with self.assertRaises(frappe.PermissionError):
			run.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			run.db_set("status", "Succeeded")
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": "Incoming Letter Run", "letter": self.doc.name}).insert(ignore_permissions=True)

	def test_epoch_is_server_owned_and_mask_is_not_key_rotation(self):
		frappe.set_user(self.manager.name)
		settings = frappe.get_single("Posteingang Settings")
		settings.api_key = "synthetic-ap05-key"
		settings.save()
		epoch = settings.analysis_configuration_epoch
		settings.reload()
		settings.analysis_configuration_epoch = 99999
		settings.auto_analysis_enabled = 1
		settings.save()
		self.assertEqual(settings.analysis_configuration_epoch, epoch)
		settings.enabled = 0
		settings.save()
		self.assertEqual(settings.analysis_configuration_epoch, epoch + 1)
		settings.enabled = 1
		settings.save()
		self.assertEqual(settings.analysis_configuration_epoch, epoch + 2)

	def test_autostart_is_atomic_with_historical_intake_receipt(self):
		from kanzlei_erp import posteingang

		frappe.db.set_single_value("Posteingang Settings", "auto_analysis_enabled", 1)
		# Unique accepted PDF bytes, no provider invocation in intake.
		data = (ai_tests.FIXTURES / "pdfs/AP01-01.pdf").read_bytes() + ("\n%" + frappe.generate_hash()).encode()
		result = posteingang._receive(io.BytesIO(data), "auto.pdf", "auto", "false")
		self.assertEqual((result["status"], result["revision"]), ("Received", 1))
		letter = frappe.get_doc("Incoming Letter", result["letter"])
		self.assertEqual((letter.status, letter.revision, letter.analysis_generation), ("Queued", 2, 1))
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": letter.name}), 1)
		duplicate = posteingang._receive(io.BytesIO(data), "auto.pdf", "auto2", "false")
		self.assertEqual(duplicate["outcome"], "duplicate")
		replayed = posteingang._receive(io.BytesIO(data), "renamed.pdf", "auto", "false")
		self.assertTrue(replayed["replayed"])
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": letter.name}), 1)
		file = frappe.get_doc("File", letter.original_file)
		path = Path(file.get_full_path())
		frappe.db.rollback()
		self.sent.assert_not_called()
		for doctype, name in (("Incoming Letter", letter.name), ("File", file.name), ("Incoming Letter Run", letter.current_run)):
			self.assertFalse(frappe.db.exists(doctype, name))
		for doctype in ("Incoming Letter Event", "Incoming Letter Intake Receipt"):
			self.assertEqual(frappe.db.count(doctype, {"letter": letter.name}), 0)
		self.assertFalse(path.exists())


class TestPosteingangJobInputs(TestCase):
	def test_malformed_unicode_and_revision_use_safe_errors(self):
		from kanzlei_erp import posteingang_jobs as jobs

		for name, revision, key in (("missing", "9" * 5000, "key"), ("missing", 1, "bad\ud800"), ("bad\ud800", 1, "key")):
			with self.subTest(revision_length=len(str(revision))), self.assertRaises(frappe.ValidationError):
				jobs.queue_analysis(name, revision, key)
