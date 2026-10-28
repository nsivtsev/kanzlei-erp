"""Independent DB connections and real RQ. Disposable site only."""

import multiprocessing
from pathlib import Path
from unittest.mock import patch

import frappe

from kanzlei_erp.tests import test_posteingang_ai as ai_tests
from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


def _connect(site, sites_path, actor):
	frappe.init(site, sites_path=sites_path, force=True)
	frappe.connect()
	frappe.set_user(actor)


def _queue_worker(site, sites_path, actor, letter, key, barrier, results):
	_connect(site, sites_path, actor)
	try:
		from kanzlei_erp import posteingang_jobs as jobs

		frappe.db.sql("SELECT COUNT(*) FROM `tabIncoming Letter Event`")
		barrier.wait(timeout=25)
		# Enter the same transaction-owning HTTP boundary as a routed request.
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		frappe.local.request = Request(EnvironBuilder(method="POST").get_environ())
		with patch.object(jobs, "_dispatch"):
			result = jobs.queue_analysis(letter, 1, key)
			frappe.db.commit()
		results.put(result)
	except Exception as error:
		frappe.db.rollback()
		results.put({"code": str(error)})
	finally:
		frappe.destroy()


def _analysis_worker(site, sites_path, actor, run, stage, ready, proceed, results):
	_connect(site, sites_path, actor)
	try:
		from kanzlei_erp import posteingang_ai as ai
		from kanzlei_erp import posteingang_jobs as jobs

		original = getattr(ai, stage)

		def paused(*args):
			value = original(*args)
			ready.set()
			if not proceed.wait(timeout=35):
				raise RuntimeError("test synchronization timed out")
			return value

		with patch.object(ai, stage, side_effect=paused):
			results.put(jobs.process_run(run))
	finally:
		frappe.destroy()


def _rq_worker(site, sites_path, actor, queue_name):
	_connect(site, sites_path, actor)
	try:
		from frappe.utils.background_jobs import generate_qname, get_redis_conn
		from rq import Queue, Worker

		queue = Queue(generate_qname(queue_name), connection=get_redis_conn())
		# Vanilla RQ worker deliberately does not start Frappe's global scheduler.
		Worker([queue], connection=queue.connection).work(burst=True, with_scheduler=False, logging_level="WARNING")
	finally:
		frappe.destroy()


class TestPosteingangJobsConcurrency(PosteingangTestCase):
	def setUp(self):
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		from kanzlei_erp import posteingang_jobs

		self.jobs = posteingang_jobs
		self.doc = ai_tests.TestPosteingangAi.fixture_letter(self)
		self.context = multiprocessing.get_context("spawn")
		self.dispatch = patch.object(self.jobs, "_dispatch")
		self.dispatch.start()
		self.addCleanup(self.dispatch.stop)
		frappe.db.commit()

	def args(self):
		return frappe.local.site, str(Path(frappe.local.sites_path).resolve()), self.operator.name

	def process(self, target, args):
		process = self.context.Process(target=target, args=args)
		process.start()
		def cleanup():
			if process.is_alive():
				process.kill()
			process.join(timeout=10)
		self.addCleanup(cleanup)
		return process

	def test_two_old_snapshots_same_key_replay_one_run(self):
		barrier, results = self.context.Barrier(2), self.context.Queue()
		processes = [self.process(_queue_worker, (*self.args(), self.doc.name, "same", barrier, results)) for _ in range(2)]
		out = [results.get(timeout=45) for _ in processes]
		for process in processes:
			process.join(timeout=10)
			self.assertEqual(process.exitcode, 0)
		self.assertEqual({r.get("status") for r in out}, {"Queued"}, out)
		self.assertEqual(sorted(r["replayed"] for r in out), [False, True])
		frappe.db.rollback()
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": self.doc.name}), 1)

	def test_duplicate_delivery_and_manual_during_request(self):
		queued = self.jobs.queue_analysis(self.doc.name, 1, "start")
		frappe.db.commit()
		ready, proceed, results = self.context.Event(), self.context.Event(), self.context.Queue()
		worker = self.process(_analysis_worker, (*self.args(), queued["run"], "extract", ready, proceed, results))
		self.assertTrue(ready.wait(timeout=35))
		self.assertEqual(self.jobs.process_run(queued["run"])["code"], "STALE_RUN")
		frappe.db.rollback()
		letter = frappe.get_doc("Incoming Letter", self.doc.name)
		self.jobs.manual_review(letter.name, letter.revision, "manual")
		frappe.db.commit()  # Must finish while the remote call is still blocked.
		proceed.set()
		self.assertEqual(results.get(timeout=35)["code"], "STALE_RUN")
		worker.join(timeout=10)
		self.assertEqual(worker.exitcode, 0)
		frappe.db.rollback()
		run = frappe.get_doc("Incoming Letter Run", queued["run"])
		self.assertEqual((run.status, run.attempt_count), ("Superseded", 1))
		self.assertFalse(run.result_json)

	def test_killed_worker_keeps_ocr_and_requires_explicit_retry(self):
		from kanzlei_erp import posteingang_ai as ai

		queued = self.jobs.queue_analysis(self.doc.name, 1, "start")
		frappe.db.commit()
		ready, proceed, results = self.context.Event(), self.context.Event(), self.context.Queue()
		worker = self.process(_analysis_worker, (*self.args(), queued["run"], "extract", ready, proceed, results))
		self.assertTrue(ready.wait(timeout=35))
		worker.kill()
		worker.join(timeout=10)
		frappe.db.rollback()
		run = frappe.get_doc("Incoming Letter Run", queued["run"])
		self.assertTrue(run.ocr_pages_json)
		frappe.cache.delete_value("ap05-recovery-tick")
		with patch.object(self.jobs, "now_datetime", return_value=frappe.utils.add_to_date(run.lease_expires_at, seconds=1)):
			self.jobs.recover_runs()
		run.reload()
		self.assertEqual((run.status, run.error_code), ("Failed", "WORKER_INTERRUPTED"))
		letter = frappe.get_doc("Incoming Letter", self.doc.name)
		new = self.jobs.queue_analysis(letter.name, letter.revision, "explicit")
		frappe.db.commit()
		with patch.object(ai, "recognize", side_effect=AssertionError("paid OCR repeated")):
			self.assertEqual(self.jobs.process_run(new["run"])["code"], "SUCCEEDED")

	def test_real_rq_after_commit_ids_only(self):
		from frappe.utils.background_jobs import generate_qname, get_redis_conn
		from rq import Queue

		self.dispatch.stop()
		queue_name = "ap05-acceptance-" + frappe.generate_hash(length=12)
		queue = Queue(generate_qname(queue_name), connection=get_redis_conn())
		try:
			# Redirect only the queue factory to a real, isolated RQ queue. No
			# Bench config change, fake Redis or synchronous worker substitution.
			with patch("frappe.utils.background_jobs.get_queue", return_value=queue):
				result = self.jobs.queue_analysis(self.doc.name, 1, "real-rq")
				self.assertEqual(queue.count, 0)
				frappe.db.commit()
			self.assertEqual(queue.count, 1)
			job = queue.jobs[0]
			self.assertEqual(job.kwargs["kwargs"], {"run_name": result["run"]})
			worker = self.process(_rq_worker, (*self.args(), queue_name))
			worker.join(timeout=60)
			self.assertEqual(worker.exitcode, 0)
			frappe.db.rollback()
			self.assertEqual(frappe.db.get_value("Incoming Letter Run", result["run"], "status"), "Succeeded")
			job.refresh()
			self.assertEqual(job.return_value(), {"code": "SUCCEEDED"})
		finally:
			queue.delete(delete_jobs=True)
