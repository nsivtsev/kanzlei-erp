"""HTTP correction commit outcomes and permission-filtered historical replay."""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangCorrectionTransactions(PosteingangTestCase):
	def setUp(self):
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		from kanzlei_erp import posteingang_confirmation, posteingang_jobs, posteingang_matching

		self.service, self.jobs, self.matching = posteingang_confirmation, posteingang_jobs, posteingang_matching
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		frappe.share.add("Customer", self.customer.name, self.manager.name, read=1, write=1)
		self.doc = self.letter()
		self.matching.select_customer(self.doc.name, self.customer.name, "Synthetic original reviewed", 1, "customer")
		self.jobs.manual_review(self.doc.name, 2, "manual")
		state = self.service.get_review_state(self.doc.name)
		state["review"].update(mode="confirm", human_verified=True, no_actions_reason="Synthetic informational letter")
		result = self.service.confirm(self.doc.name, state["review"], state["document_fields"], "initial-confirm")
		self.original_revision = result["revision"]
		self.original_file = frappe.get_doc("File", self.doc.original_file)
		self.original_path = Path(self.original_file.get_full_path())
		self.original_bytes = self.original_path.read_bytes()
		frappe.db.commit()
		frappe.set_user(self.manager.name)

	@contextmanager
	def http(self):
		previous = getattr(frappe.local, "request", None)
		frappe.local.request = Request(EnvironBuilder(method="POST").get_environ())
		try:
			yield
		finally:
			frappe.local.request = previous

	def correct(self):
		return self.service.create_correction(self.doc.name, self.original_revision, "correct", "Wrong synthetic Customer", True, False, None)

	def capture_replacement(self):
		name = frappe.db.get_value("Incoming Letter", {"replaces_letter": self.doc.name}, "name")
		self.assertTrue(name)
		file_id = frappe.db.get_value("Incoming Letter", name, "original_file")
		file = frappe.get_doc("File", file_id)
		return {"name": name, "file": file_id, "path": Path(file.get_full_path())}

	def test_definite_sql_commit_failure_removes_only_new_file(self):
		original_sql, captured = frappe.db.sql, {}

		def fail_commit(query, *args, **kwargs):
			if isinstance(query, str) and query.strip().lower() == "commit":
				captured.update(self.capture_replacement())
				raise RuntimeError("Synthetic SQL COMMIT did not execute")
			return original_sql(query, *args, **kwargs)

		with self.http(), patch.object(frappe.db, "sql", side_effect=fail_commit):
			with self.assertRaisesRegex(frappe.ValidationError, "CORRECTION_FAILED"):
				self.correct()
		self.assertTrue(captured, "The HTTP operation must own the SQL COMMIT boundary")
		self.assertFalse(frappe.db.exists("Incoming Letter", captured["name"]))
		self.assertFalse(frappe.db.exists("File", captured["file"]))
		self.assertFalse(captured["path"].exists())
		self.assertEqual(frappe.db.get_value("Incoming Letter", self.doc.name, "status"), "Completed")
		self.assertFalse(frappe.db.get_value("Incoming Letter", self.doc.name, "replacement_letter"))
		self.assertEqual(self.original_path.read_bytes(), self.original_bytes)
		with self.http():
			self.assertTrue(self.correct()["replacement"], "Retry after definite failure must succeed")

	def test_unknown_successful_commit_preserves_bytes_and_replays(self):
		original_sql, captured = frappe.db.sql, {}

		def committed_but_response_lost(query, *args, **kwargs):
			if isinstance(query, str) and query.strip().lower() == "commit" and not captured:
				captured.update(self.capture_replacement())
				original_sql(query, *args, **kwargs)
				raise RuntimeError("Synthetic connection lost after SQL COMMIT")
			return original_sql(query, *args, **kwargs)

		with self.http(), patch.object(frappe.db, "sql", side_effect=committed_but_response_lost):
			result = self.correct()
		self.assertTrue(captured, "The HTTP operation must own the SQL COMMIT boundary")
		self.assertEqual(result["replacement"], captured["name"])
		self.assertTrue(frappe.db.exists("Incoming Letter", captured["name"]))
		self.assertTrue(frappe.db.exists("File", captured["file"]))
		self.assertEqual(captured["path"].read_bytes(), self.original_bytes)
		self.assertEqual(self.original_path.read_bytes(), self.original_bytes)
		with self.http():
			replay = self.correct()
		self.assertTrue(replay["replayed"])
		self.assertEqual(replay["replacement"], captured["name"])
		self.assertEqual(frappe.db.count("Incoming Letter", {"replaces_letter": self.doc.name}), 1)

	def test_replay_does_not_disclose_now_inaccessible_replacement(self):
		with self.http():
			created = self.correct()
		frappe.db.commit()
		replacement = created["replacement"]
		frappe.set_user("Administrator")
		other = frappe.get_doc({"doctype": "Customer", "customer_name": "AP07 replacement " + frappe.generate_hash(length=10),
			"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories"}).insert()
		new_operator = self.user("replacement", ["Projects User", "Posteingang Operator"])
		frappe.share.add("Customer", other.name, new_operator.name, read=1, write=1)
		frappe.get_doc({"doctype": "User Permission", "user": self.manager.name, "allow": "Customer", "for_value": self.customer.name, "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
		frappe.set_user(new_operator.name)
		new = frappe.get_doc("Incoming Letter", replacement)
		self.matching.select_customer(new.name, other.name, "Correct synthetic Customer", new.revision, "replacement-customer")
		new.reload()
		self.jobs.manual_review(new.name, new.revision, "replacement-manual")
		state = self.service.get_review_state(new.name)
		state["review"].update(mode="confirm", human_verified=True, no_actions_reason="No new work")
		self.service.confirm(new.name, state["review"], state["document_fields"], "replacement-confirm")
		frappe.db.commit()
		frappe.set_user(self.manager.name)
		self.assertFalse(frappe.has_permission("Customer", "read", doc=other.name))
		self.assertFalse(frappe.has_permission("Incoming Letter", "read", doc=replacement))
		with self.http():
			replay = self.correct()
		self.assertTrue(replay["replayed"])
		self.assertIsNone(replay["replacement"])
		self.assertNotIn(replacement, json.dumps(replay))
