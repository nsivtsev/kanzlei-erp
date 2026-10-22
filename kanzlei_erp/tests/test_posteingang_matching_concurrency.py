"""Independent transactions, old snapshots and Customer/Letter write races."""

import multiprocessing
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
		from kanzlei_erp import mandant_identifiers as ids
		from kanzlei_erp import posteingang_jobs as jobs
		from kanzlei_erp import posteingang_matching as matching

		frappe.db.sql("SELECT COUNT(*) FROM `tabCustomer`")
		barrier.wait(timeout=25)
		frappe.local.request = Request(EnvironBuilder(method="POST").get_environ())
		with patch("kanzlei_erp.drive_realtime._deliver"):
			if operation == "select":
				result = matching.select_customer(*args)
			elif operation == "verify":
				result = ids.verify_identifier(*args)
			else:
				jobs._http_boundary()
				doc = frappe.get_doc("Customer", args[0])
				doc.modified = args[1]
				doc.kanzlei_identifiers[0].raw_value = "changed-reference"
				doc.save()
				result = {"edited": True}
			frappe.db.commit()
		results.put({"ok": result})
	except frappe.TimestampMismatchError:
		frappe.db.rollback()
		results.put({"error": "CUSTOMER_CHANGED"})
	except Exception as error:
		frappe.db.rollback()
		# Ordinary ERP Customer.save may report its optimistic conflict directly
		# as MariaDB 1020. The new verification endpoint must use a safe code.
		code = "CUSTOMER_CHANGED" if operation == "edit" and isinstance(error, frappe.QueryDeadlockError) else str(error)
		results.put({"error": code, "operation": operation})
	finally:
		frappe.destroy()


class TestMatchingConcurrency(PosteingangTestCase):
	def setUp(self):
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		self.customer.append("kanzlei_identifiers", {"identifier_type": "other", "raw_value": "reference", "authority": "Synthetic", "other_type": "reference"})
		self.customer.save()
		self.doc = self.letter()
		frappe.db.commit()
		self.context = multiprocessing.get_context("spawn")

	def race(self, operations):
		barrier, results = self.context.Barrier(2), self.context.Queue()
		processes = []
		for operation, args in operations:
			process = self.context.Process(target=_race, args=(frappe.local.site, str(Path(frappe.local.sites_path).resolve()), self.operator.name, operation, args, barrier, results))
			process.start()
			processes.append(process)
		try:
			output = [results.get(timeout=45) for _ in processes]
			for process in processes:
				process.join(timeout=10)
				self.assertEqual(process.exitcode, 0)
			frappe.db.rollback()
			return output
		finally:
			for process in processes:
				if process.is_alive():
					process.kill()
				process.join(timeout=10)

	def test_same_key_old_snapshots_return_one_selection_and_replay(self):
		args = (self.doc.name, self.customer.name, "reviewed", 1, "same")
		output = self.race([("select", args), ("select", args)])
		self.assertTrue(all("ok" in item for item in output), output)
		self.assertEqual(sorted(item["ok"]["replayed"] for item in output), [False, True])
		self.assertEqual(frappe.db.count("Incoming Letter Event", {"letter": self.doc.name}), 2)

	def test_two_intents_from_one_revision_commit_only_one(self):
		output = self.race([("select", (self.doc.name, self.customer.name, reason, 1, key)) for reason, key in (("first", "one"), ("second", "two"))])
		self.assertEqual(sum("ok" in item for item in output), 1, output)
		self.assertEqual([item["error"] for item in output if "error" in item], ["REVISION_CONFLICT"])
		self.assertEqual(frappe.db.get_value("Incoming Letter", self.doc.name, "revision"), 2)

	def test_verification_and_edit_cannot_attest_stale_value(self):
		output = self.race([("verify", (self.customer.name, self.customer.kanzlei_identifiers[0].name, str(self.customer.modified), "source")), ("edit", (self.customer.name, str(self.customer.modified)))])
		self.assertEqual(sum("ok" in item for item in output), 1, output)
		self.assertEqual([item["error"] for item in output if "error" in item], ["CUSTOMER_CHANGED"], output)
		row = frappe.get_doc("Mandant Identifier", self.customer.kanzlei_identifiers[0].name)
		if row.raw_value == "changed-reference":
			self.assertFalse(row.verified_by)
		else:
			self.assertEqual(row.raw_value, "reference")
			self.assertEqual(row.verified_by, self.operator.name)
