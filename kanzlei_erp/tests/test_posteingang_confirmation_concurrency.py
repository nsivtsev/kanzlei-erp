"""AP-07 confirmation races on independent connections, including old snapshots.

Disposable site only. No scheduler, live provider, shared queue cleanup or Settings
changes: all competing transactions use their own synthetic Letter and Customer.
"""

import copy
import multiprocessing
import traceback
from pathlib import Path
from unittest.mock import patch

import frappe

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


def _race(site, sites, actor, operation, args, barrier, results):
	from werkzeug.test import EnvironBuilder
	from werkzeug.wrappers import Request

	frappe.init(site, sites_path=sites, force=True)
	frappe.connect()
	frappe.set_user(actor)
	try:
		from kanzlei_erp import posteingang_confirmation as confirmation
		from kanzlei_erp import posteingang_jobs as jobs
		from kanzlei_erp import posteingang_matching as matching

		# Establish a repeatable-read snapshot before either HTTP transaction
		# starts. The service must discard it instead of trusting stale reads.
		snapshot = frappe.db.sql("SELECT revision FROM `tabIncoming Letter` WHERE name=%s", (args[0],))[0][0]
		barrier.wait(timeout=30)
		frappe.local.request = Request(EnvironBuilder(method="POST").get_environ())
		with patch("kanzlei_erp.drive_realtime._deliver"), patch.object(jobs, "_dispatch"):
			if operation == "confirm":
				result = confirmation.confirm(*args)
			elif operation == "select_customer":
				result = matching.select_customer(*args)
			elif operation == "queue_analysis":
				result = jobs.queue_analysis(*args)
			elif operation == "cancel":
				result = jobs.cancel(*args)
			else:
				raise AssertionError("Unknown test operation")
			frappe.db.commit()
		results.put({"ok": result, "operation": operation, "snapshot_revision": snapshot})
	except Exception as error:
		frappe.db.rollback()
		results.put({"error": str(error), "operation": operation, "snapshot_revision": locals().get("snapshot"),
			"where": [(Path(frame.filename).name, frame.lineno, frame.name) for frame in traceback.extract_tb(error.__traceback__)]})
	finally:
		frappe.destroy()


class TestPosteingangConfirmationConcurrency(PosteingangTestCase):
	def setUp(self):
		# Unlike the common fixture, do not reset site-wide Settings while
		# other isolated AP-07 suites may be exercising their own transactions.
		frappe.set_user("Administrator")
		self.paths = []
		for target in ("kanzlei_erp.drive_realtime._deliver", "frappe.core.doctype.user.user.throttle_user_creation"):
			mock = patch(target)
			mock.start()
			self.addCleanup(mock.stop)
		self.assertTrue(frappe.db.get_single_value("Posteingang Settings", "enabled"), "Disposable AP-07 test site must have the function enabled")
		self.assertEqual(frappe.db.get_single_value("Posteingang Settings", "provider"), "fake")
		self.customer = frappe.get_doc({
			"doctype": "Customer", "customer_name": "AP07 race " + frappe.generate_hash(length=10),
			"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories",
		}).insert()
		self.operator = self.user("ap07-race", ["Projects User", "Posteingang Operator"])
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		self.doc = self.letter()
		from kanzlei_erp import posteingang_confirmation as confirmation
		from kanzlei_erp import posteingang_jobs as jobs
		from kanzlei_erp import posteingang_matching as matching

		matching.select_customer(self.doc.name, self.customer.name, "Synthetic original reviewed", 1, "customer")
		jobs.manual_review(self.doc.name, 2, "manual")
		confirmation.add_manual_action(self.doc.name, "Synthetic race work", "Synthetic race description", 3, "action")
		state = confirmation.get_review_state(self.doc.name)
		self.review, self.fields = state["review"], state["document_fields"]
		self.review.update(mode="confirm", human_verified=True)
		self.review["actions"][0].update(decision="accept", assigned_to=None, assignment_decision="unassigned",
			assignment_reason="Planning follows after review", internal_date=None, scheduling_decision="unscheduled",
			scheduling_reason="Planning follows after review", needs_planning=True)
		self.revision = state["revision"]
		self.context = multiprocessing.get_context("spawn")
		frappe.db.commit()

	def race(self, operations):
		barrier, results = self.context.Barrier(2), self.context.Queue()
		processes = []
		for operation, args in operations:
			process = self.context.Process(target=_race, args=(frappe.local.site,
				str(Path(frappe.local.sites_path).resolve()), self.operator.name, operation, args, barrier, results))
			process.start()
			processes.append(process)
		try:
			output = [results.get(timeout=60) for _ in processes]
			for process in processes:
				process.join(timeout=15)
				self.assertEqual(process.exitcode, 0, output)
			self.assertEqual([item["snapshot_revision"] for item in output], [self.revision] * 2, output)
			frappe.db.rollback()
			return output
		finally:
			for process in processes:
				if process.is_alive():
					process.kill()
				process.join(timeout=10)
			results.close()
			results.join_thread()

	def confirm_args(self, key, review=None):
		return self.doc.name, review or self.review, self.fields, key

	def assert_one_confirmation(self):
		self.doc.reload()
		self.assertEqual(self.doc.status, "Completed")
		self.assertEqual(self.doc.revision, self.revision + 1)
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": self.doc.name}), 1)
		self.assertEqual(frappe.db.count("Incoming Letter Event", {"letter": self.doc.name, "event_type": "confirm"}), 1)
		actions = frappe.get_all("Incoming Letter Action", filters={"parent": self.doc.name}, fields=["created_task"])
		self.assertEqual(len(actions), 1)
		self.assertTrue(actions[0].created_task)

	def test_same_key_and_old_snapshots_replay_one_task_and_event(self):
		args = self.confirm_args("same")
		output = self.race([("confirm", args), ("confirm", args)])
		self.assertTrue(all("ok" in item for item in output), output)
		self.assertEqual(sorted(item["ok"]["replayed"] for item in output), [False, True])
		self.assertEqual(output[0]["ok"]["actions"], output[1]["ok"]["actions"])
		self.assertEqual(output[0]["ok"]["event"], output[1]["ok"]["event"])
		self.assert_one_confirmation()

	def test_different_keys_same_final_payload_replay_completed_receipt(self):
		output = self.race([("confirm", self.confirm_args(key)) for key in ("first", "second")])
		self.assertTrue(all("ok" in item for item in output), output)
		self.assertEqual(sorted(item["ok"]["replayed"] for item in output), [False, True])
		self.assertEqual(output[0]["ok"]["event"], output[1]["ok"]["event"])
		self.assert_one_confirmation()

	def test_same_key_different_payload_cannot_publish_a_second_result(self):
		changed = copy.deepcopy(self.review)
		changed["actions"][0]["title"] = "Another synthetic decision"
		output = self.race([("confirm", self.confirm_args("same")), ("confirm", self.confirm_args("same", changed))])
		self.assertEqual(sum("ok" in item for item in output), 1, output)
		self.assertEqual([item["error"] for item in output if "error" in item], ["IDEMPOTENCY_CONFLICT"], output)
		self.assert_one_confirmation()

	def assert_competing_mutation(self, operation, args, other_status):
		output = self.race([("confirm", self.confirm_args("confirmation")), (operation, args)])
		self.assertEqual(sum("ok" in item for item in output), 1, output)
		self.assertEqual([item["error"] for item in output if "error" in item], ["REVISION_CONFLICT"], output)
		self.doc.reload()
		self.assertEqual(self.doc.revision, self.revision + 1)
		if self.doc.status == "Completed":
			self.assert_one_confirmation()
		else:
			self.assertEqual(self.doc.status, other_status)
			self.assertFalse(self.doc.confirmed_customer)
			self.assertFalse(self.doc.confirmation_receipt_json)
			self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": self.doc.name}), 0)
			self.assertEqual(frappe.db.count("Incoming Letter Event", {"letter": self.doc.name, "event_type": "confirm"}), 0)
			self.assertFalse(frappe.db.get_value("Incoming Letter Action", {"parent": self.doc.name}, "created_task"))

	def test_confirmation_against_customer_selection_is_serialized(self):
		self.assert_competing_mutation("select_customer", (self.doc.name, self.customer.name, "New explicit review", self.revision, "selection"), "Needs Review")

	def test_confirmation_against_new_analysis_is_serialized(self):
		self.assert_competing_mutation("queue_analysis", (self.doc.name, self.revision, "analysis"), "Queued")
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": self.doc.name}), int(self.doc.status == "Queued"))

	def test_confirmation_against_cancellation_is_serialized(self):
		self.assert_competing_mutation("cancel", (self.doc.name, self.revision, "cancel", "Synthetic cancellation"), "Cancelled")
