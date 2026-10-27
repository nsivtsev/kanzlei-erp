"""AP-10 synthetic vertical acceptance; isolated RQ and post-commit transport loss.

These tests deliberately commit, so use only a marked disposable AP-10 or CI site.
No production policy is replaced. Cleanup is limited to records created here.
"""

import copy
import io
import json
import multiprocessing
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase, acceptance_test_site


class TestAcceptanceSiteGuard(TestCase):
	def test_ci_site_requires_both_explicit_test_flags(self):
		from kanzlei_erp.tests.posteingang_helpers import acceptance_test_site

		self.assertTrue(acceptance_test_site("ci.localhost", {"allow_tests": True, "posteingang_acceptance_site": True}))
		self.assertTrue(acceptance_test_site("posteingang-ap10.localhost", {"allow_tests": True, "posteingang_acceptance_site": 1}))
		for site, config in (
			("ci.localhost", {"allow_tests": True}),
			("ci.localhost", {"posteingang_acceptance_site": True}),
			("production.example", {"allow_tests": True, "posteingang_acceptance_site": True}),
			("posteingang-ap10.localhost.evil", {"allow_tests": True, "posteingang_acceptance_site": True}),
		):
			self.assertFalse(acceptance_test_site(site, config))


class LostResponse(ConnectionError):
	"""The application has already committed; transport discards its response."""


class TestPosteingangAcceptance(PosteingangTestCase):
	def setUp(self):
		self.assertTrue(acceptance_test_site(frappe.local.site, frappe.conf), "Marked disposable test site required")
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		from kanzlei_erp import posteingang_confirmation as review
		from kanzlei_erp import posteingang_jobs as jobs
		from kanzlei_erp import posteingang_matching as matching

		self.review, self.jobs, self.matching = review, jobs, matching
		self.owned = []
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		frappe.share.add("Customer", self.customer.name, self.manager.name, read=1, write=1)
		self.manager.add_roles("Projects User")
		frappe.db.commit()
		frappe.set_user(self.operator.name)

	def fixture(self, case_id):
		root = Path(__file__).parent / "fixtures/posteingang"
		case = next(c for c in json.loads((root / "manifest.json").read_text())["cases"] if c["case_id"] == case_id)
		return case, (root / case["pdf"]).read_bytes()

	def post(self, method, *args, lose_response=False, request=None, **kwargs):
		"""Call a routed mutation with its actual fresh POST boundary, then commit."""
		request = request or Request(EnvironBuilder(method="POST").get_environ())
		with patch.object(frappe.local, "request", request, create=True):
			result = method(*args, **kwargs)
			frappe.db.commit()
			if lose_response:
				raise LostResponse("Synthetic response lost after commit")
			return result

	def receive(self, case_id, key=None, lose_response=False):
		from kanzlei_erp.posteingang import get_intake_result, receive

		_case, content = self.fixture(case_id)
		key = key or frappe.generate_hash(length=30)
		request = Request(EnvironBuilder(method="POST", data={"file": (io.BytesIO(content), f"AP10-{case_id}.pdf")}).get_environ())
		try:
			result = self.post(receive, key, "false", request=request, lose_response=lose_response)
		finally:
			# Recovering a lost response reads a durable receipt; it does not upload again.
			recovered = get_intake_result(key)
			if recovered.get("letter") and recovered["letter"] not in self.owned:
				self.owned.append(recovered["letter"])
		return result, key

	def letter(self, case_id="AP01-01"):
		from kanzlei_erp.posteingang_model import create_received_letter

		case, content = self.fixture(case_id)
		doc = create_received_letter(actor=self.operator.name,
			original={"content": content, "file_name": f"AP10-{case_id}.pdf", "page_count": case["page_count"]},
			receipt={"received_date_known": False, "received_date": None},
			operation={"key": frappe.generate_hash(length=64), "fingerprint": frappe.generate_hash(length=64)})
		self.owned.append(doc.name)
		frappe.db.commit()
		return doc

	def revision(self, name):
		return frappe.db.get_value("Incoming Letter", name, "revision")

	def manual(self, letter):
		self.post(self.matching.select_customer, letter, self.customer.name, "Synthetic original reviewed", self.revision(letter), frappe.generate_hash())
		self.post(self.jobs.manual_review, letter, self.revision(letter), frappe.generate_hash())
		return self.review.get_review_state(letter)

	def final(self, letter, *, action=False):
		if action:
			self.post(self.review.add_manual_action, letter, "Synthetic AP10 work", "Synthetic description", self.revision(letter), frappe.generate_hash())
		state = self.review.get_review_state(letter)
		state["review"].update(mode="confirm", human_verified=True, no_actions_reason=None if state["review"]["actions"] else "Original reviewed: no further actions")
		for row in state["review"]["actions"]:
			row.update(decision="accept", assigned_to=self.reader.name, assignment_decision="assigned", assignment_reason=None,
				internal_date="2026-11-18", scheduling_decision="scheduled", scheduling_reason=None, needs_planning=False,
				external_date="2026-11-20", external_date_basis="Explicit original wording checked on page 1")
		return state

	def test_vertical_real_rq_to_calendar_source_documents(self):
		from frappe.desk.calendar import get_events
		from frappe.utils.background_jobs import generate_qname, get_redis_conn
		from rq import Queue

		from kanzlei_erp import mandant_documents, posteingang_documents
		from kanzlei_erp.tests.test_posteingang_jobs_concurrency import _rq_worker

		first, key = self.receive("AP01-01")
		self.assertEqual(first["outcome"], "created")
		self.assertTrue(self.receive("AP01-01", key)[0]["replayed"])
		self.assertEqual(self.receive("AP01-01")[0]["outcome"], "duplicate")
		queue_name = "ap10-acceptance-" + frappe.generate_hash(length=12)
		queue = Queue(generate_qname(queue_name), connection=get_redis_conn())
		worker = None
		try:
			with patch("frappe.utils.background_jobs.get_queue", return_value=queue):
				queued = self.post(self.jobs.queue_analysis, first["letter"], 1, "queue")
			self.assertEqual(queue.count, 1)
			job = queue.jobs[0]
			self.assertEqual(job.kwargs["kwargs"], {"run_name": queued["run"]})
			worker = multiprocessing.get_context("spawn").Process(target=_rq_worker,
				args=(frappe.local.site, str(Path(frappe.local.sites_path).resolve()), self.operator.name, queue_name))
			worker.start()
			worker.join(timeout=70)
			self.assertFalse(worker.is_alive())
			self.assertEqual(worker.exitcode, 0)
			frappe.db.rollback()
			self.assertEqual(frappe.db.get_value("Incoming Letter Run", queued["run"], "status"), "Succeeded")
			state = self.review.get_review_state(first["letter"])
			self.post(self.review.apply_result, first["letter"], queued["run"], state["revision"], "apply", False)
			self.post(self.matching.select_customer, first["letter"], self.customer.name, "Original matched manually", self.revision(first["letter"]), "select")
			pending = self.review.get_review_state(first["letter"])
			pending["review"].update(mode="confirm", human_verified=True)
			with self.assertRaises(frappe.ValidationError):
				self.post(self.review.confirm, first["letter"], pending["review"], pending["document_fields"], "pending")
			state = self.final(first["letter"])
			result = self.post(self.review.confirm, first["letter"], state["review"], state["document_fields"], "confirm")
			task_name = result["actions"][0]["task"]
			self.assertTrue(self.post(self.review.confirm, first["letter"], state["review"], state["document_fields"], "confirm")["replayed"])
			self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": first["letter"]}), 1)
			self.assertEqual(frappe.db.count("ToDo", {"reference_type": "Task", "reference_name": task_name, "allocated_to": self.reader.name}), 1)
			frappe.set_user(self.reader.name)
			events = get_events("Task", "2026-11-18", "2026-11-19", json.dumps({"start": "exp_start_date", "end": "exp_end_date", "title": "kanzlei_calendar_title"}))
			self.assertIn(task_name, [row["name"] for row in events])
			source = posteingang_documents.get_task_source(task_name)
			self.assertEqual(source["action"]["external_date"], "2026-11-20")
			self.assertTrue(source["action"]["evidence"])
			rows = mandant_documents.get_documents(self.customer.name, filters={"source": "posteingang"})["items"]
			self.assertEqual([row["file_id"] for row in rows], [first["original_file"]])
			job.refresh()
			self.assertEqual(job.return_value(), {"code": "SUCCEEDED"})
		finally:
			if worker and worker.is_alive():
				worker.kill()
				worker.join(timeout=10)
			queue.delete(delete_jobs=True)

	def test_lost_post_commit_receive_and_confirmation_responses_recover_once(self):
		from kanzlei_erp.posteingang import get_intake_result

		key = frappe.generate_hash(length=30)
		with self.assertRaises(LostResponse):
			self.receive("AP01-08", key, lose_response=True)
		first = get_intake_result(key)
		self.assertEqual(first["outcome"], "created")
		self.assertTrue(self.receive("AP01-08", key)[0]["replayed"])
		self.manual(first["letter"])
		state = self.final(first["letter"], action=True)
		with self.assertRaises(LostResponse):
			self.post(self.review.confirm, first["letter"], state["review"], state["document_fields"], "lost-confirm", lose_response=True)
		self.assertEqual(frappe.db.get_value("Incoming Letter", first["letter"], "status"), "Completed")
		replayed = self.post(self.review.confirm, first["letter"], state["review"], state["document_fields"], "lost-confirm")
		self.assertTrue(replayed["replayed"])
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": first["letter"]}), 1)
		self.assertEqual(len(replayed["actions"]), 1)

	def test_manual_without_external_provider_requires_explicit_no_actions(self):
		doc = self.letter("AP01-03")
		frappe.db.set_single_value("Posteingang Settings", {"provider": "mistral", "external_analysis_enabled": 0})
		frappe.db.commit()
		with self.assertRaisesRegex(frappe.ValidationError, "ANALYSIS_STOPPED"):
			self.post(self.jobs.queue_analysis, doc.name, 1, "disabled-provider")
		self.manual(doc.name)
		state = self.final(doc.name)
		invalid = copy.deepcopy(state["review"])
		invalid["no_actions_reason"] = None
		with self.assertRaises(frappe.ValidationError):
			self.post(self.review.confirm, doc.name, invalid, state["document_fields"], "no-reason")
		self.post(self.review.confirm, doc.name, state["review"], state["document_fields"], "manual-confirm")
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": doc.name}), 0)
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": doc.name}), 0)
		self.assertIsNone(frappe.db.get_value("Incoming Letter", doc.name, "received_date"))

	def test_correction_keeps_completed_task_history_then_other_operator_confirms(self):
		from kanzlei_erp.posteingang_permissions import letter_access
		from kanzlei_erp.posteingang_task_access import task_access

		doc = self.letter("AP01-10")
		self.manual(doc.name)
		state = self.final(doc.name, action=True)
		confirmed = self.post(self.review.confirm, doc.name, state["review"], state["document_fields"], "initial")
		task = frappe.get_doc("Task", confirmed["actions"][0]["task"])
		task.status = "Completed"
		task.save()
		frappe.db.commit()
		frappe.set_user(self.manager.name)
		result = self.post(self.review.create_correction, doc.name, confirmed["revision"], "correction", "Synthetic corrected assignment", True, False, None)
		new = frappe.get_doc("Incoming Letter", result["replacement"])
		self.owned.append(new.name)
		self.assertNotEqual(doc.original_file, new.original_file)
		self.assertEqual(doc.content_sha256, new.content_sha256)
		self.assertFalse(self.review.get_review_state(new.name)["correction"]["ready"])
		self.post(self.review.correction_disposition, new.name, new.revision, confirmed["actions"][0]["action_id"], "historical", "Already completed; retain history", "disposition")
		frappe.set_user(self.operator.name)
		self.manual(new.name)
		state = self.final(new.name)
		self.post(self.review.confirm, new.name, state["review"], state["document_fields"], "replacement-confirm")
		self.assertEqual(frappe.db.get_value("Task", task.name, "status"), "Completed")
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": new.name}), 0)
		self.assertFalse(letter_access(frappe.get_doc("Incoming Letter", doc.name)))
		frappe.set_user(self.reader.name)
		self.assertFalse(task_access(task.name))

	def test_disable_between_ocr_and_extraction_stops_without_implicit_resume(self):
		from kanzlei_erp import posteingang_ai as ai

		doc = self.letter("AP01-15")
		with patch.object(self.jobs, "_dispatch"):
			queued = self.post(self.jobs.queue_analysis, doc.name, 1, "start")
		original = ai.recognize

		def disable_after_ocr(source):
			result = original(source)
			frappe.db.set_single_value("Posteingang Settings", "enabled", 0)
			frappe.db.commit()
			return result

		with patch.object(ai, "recognize", side_effect=disable_after_ocr), patch.object(ai, "extract", side_effect=AssertionError("Extraction must not be called")):
			self.jobs.process_run(queued["run"])
		run = frappe.get_doc("Incoming Letter Run", queued["run"])
		self.assertEqual((run.status, run.error_code), ("Cancelled", "ANALYSIS_STOPPED"))
		frappe.db.set_single_value("Posteingang Settings", "enabled", 1)
		frappe.db.commit()
		self.assertEqual(self.jobs.process_run(run.name)["code"], "STALE_RUN")
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": doc.name}), 1)

	def test_dispatch_outage_keeps_run_then_recovery_delivers_without_new_attempt(self):
		doc = self.letter("AP01-12")
		with patch("frappe.enqueue", side_effect=ConnectionError("Synthetic Redis outage")):
			queued = self.post(self.jobs.queue_analysis, doc.name, 1, "dispatch")
		run = frappe.get_doc("Incoming Letter Run", queued["run"])
		self.assertEqual((run.status, run.attempt_count), ("Queued", 0))
		self.assertTrue(frappe.db.exists("File", doc.original_file))
		frappe.cache.delete_value("ap05-recovery-tick")
		with patch.object(self.jobs, "_dispatch") as dispatch:
			self.jobs.recover_runs()
			self.assertIn((run.name,), [call.args for call in dispatch.call_args_list])
		run.reload()
		self.assertEqual(run.attempt_count, 0)
		self.assertEqual(self.jobs.process_run(run.name)["code"], "SUCCEEDED")

	def test_interrupted_worker_keeps_ocr_until_explicit_retry_without_second_ocr(self):
		from kanzlei_erp import posteingang_ai as ai
		from kanzlei_erp.tests.test_posteingang_jobs_concurrency import _analysis_worker

		doc = self.letter("AP01-07")
		with patch.object(self.jobs, "_dispatch"):
			queued = self.post(self.jobs.queue_analysis, doc.name, 1, "first")
		context = multiprocessing.get_context("spawn")
		ready, proceed, results = context.Event(), context.Event(), context.Queue()
		worker = context.Process(target=_analysis_worker,
			args=(frappe.local.site, str(Path(frappe.local.sites_path).resolve()), self.operator.name, queued["run"], "extract", ready, proceed, results))
		worker.start()
		try:
			self.assertTrue(ready.wait(timeout=35), "Worker must reach extraction after OCR checkpoint")
			worker.kill()
			worker.join(timeout=10)
			frappe.db.rollback()
			run = frappe.get_doc("Incoming Letter Run", queued["run"])
			self.assertTrue(run.ocr_pages_json)
			frappe.cache.delete_value("ap05-recovery-tick")
			with patch.object(self.jobs, "now_datetime", return_value=frappe.utils.add_to_date(run.lease_expires_at, seconds=1)), patch.object(ai, "recognize", side_effect=AssertionError("Recovery must not call OCR")):
				self.jobs.recover_runs()
			run.reload()
			self.assertEqual((run.status, run.error_code), ("Failed", "WORKER_INTERRUPTED"))
			with patch.object(self.jobs, "_dispatch"):
				new = self.post(self.jobs.queue_analysis, doc.name, self.revision(doc.name), "explicit-retry")
			with patch.object(ai, "recognize", side_effect=AssertionError("Checkpoint must prevent repeated OCR")):
				self.assertEqual(self.jobs.process_run(new["run"])["code"], "SUCCEEDED")
			self.assertEqual(frappe.db.get_value("Incoming Letter Run", new["run"], "reused_ocr_from"), run.name)
		finally:
			if worker.is_alive():
				worker.kill()
				worker.join(timeout=10)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		# Test-only SQL cleanup: immutable runtime records remain protected through CRUD.
		for name in getattr(self, "owned", []):
			tasks = frappe.get_all("Task", {"kanzlei_incoming_letter": name}, pluck="name")
			for task in tasks:
				for kind, fields in (("ToDo", ("reference_type", "reference_name")), ("Notification Log", ("document_type", "document_name")), ("Comment", ("reference_doctype", "reference_name")), ("Communication", ("reference_doctype", "reference_name")), ("DocShare", ("share_doctype", "share_name"))):
					frappe.db.delete(kind, {fields[0]: "Task", fields[1]: task})
				frappe.db.delete("Task Depends On", {"parent": task})
				frappe.db.delete("Task", {"name": task})
			for file_name in frappe.get_all("File", {"attached_to_doctype": "Incoming Letter", "attached_to_name": name}, pluck="name"):
				file = frappe.get_doc("File", file_name)
				Path(file.get_full_path()).unlink(missing_ok=True)
				frappe.db.delete("File", {"name": file_name})
			for kind in ("Incoming Letter Intake Receipt", "Incoming Letter Event", "Incoming Letter Run"):
				frappe.db.delete(kind, {"letter": name})
			frappe.db.delete("Incoming Letter Action", {"parent": name})
			frappe.db.delete("Incoming Letter", {"name": name})
		frappe.db.commit()
		super().tearDown()
