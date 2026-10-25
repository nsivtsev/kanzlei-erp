"""AP-08 protected projections over synthetic originals and real confirmations."""

import json
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp import mandant_documents as documents
from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangDocuments(PosteingangTestCase):
	def setUp(self):
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)

	def completed(self, count=0):
		from kanzlei_erp import posteingang_confirmation as review
		from kanzlei_erp import posteingang_jobs as jobs
		from kanzlei_erp import posteingang_matching as matching

		letter = self.letter()
		matching.select_customer(letter.name, self.customer.name, "Synthetic original reviewed", 1, frappe.generate_hash())
		jobs.manual_review(letter.name, 2, frappe.generate_hash())
		for index in range(count):
			state = review.get_review_state(letter.name)
			review.add_manual_action(letter.name, "Synthetic action " + str(index), "Synthetic description", state["revision"], frappe.generate_hash())
		state = review.get_review_state(letter.name)
		state["review"].update(mode="confirm", human_verified=True, no_actions_reason=None if count else "Synthetic information reviewed")
		for row in state["review"]["actions"]:
			row.update(decision="accept", assignment_decision="unassigned", assignment_reason="Plan later", scheduling_decision="unscheduled", scheduling_reason="Plan later", needs_planning=True,
				external_date="2026-11-20", external_date_basis="Original reviewed on page 1")
		result = review.confirm(letter.name, state["review"], state["document_fields"], frappe.generate_hash())
		letter.reload()
		return letter, [row["task"] for row in result["actions"]]

	def rows(self, **filters):
		return documents.get_documents(self.customer.name, filters=filters)["items"]

	def test_completed_without_tasks_is_visible_to_customer_reader_without_operator(self):
		letter, _ = self.completed()
		frappe.set_user(self.reader.name)
		rows = self.rows()
		self.assertEqual([r["file_id"] for r in rows], [letter.original_file])
		self.assertEqual(rows[0]["contexts"][0]["source"], "posteingang")
		self.assertEqual(rows[0]["date_kind"], "uploaded")

	def test_pending_and_quarantined_letters_never_join_documents(self):
		letter, _ = self.completed()
		for status in ("Received", "Queued", "Processing", "Needs Review", "Needs Clarification", "Error", "Cancelled"):
			frappe.db.set_value("Incoming Letter", letter.name, "status", status)
			self.assertEqual(self.rows(), [], status)
		frappe.db.set_value("Incoming Letter", letter.name, {"status": "Completed", "correction_quarantined": 1})
		frappe.set_user("Administrator")
		self.assertEqual(self.rows(), [])

	def test_one_original_many_tasks_and_source_filters(self):
		letter, tasks = self.completed(2)
		rows = self.rows(source="posteingang")
		self.assertEqual(len(rows), 1)
		self.assertEqual({c["name"] for c in rows[0]["contexts"] if c["source"] == "task"}, set(tasks))
		self.assertEqual([r["file_id"] for r in self.rows(source="task")], [letter.original_file])

	def test_received_date_controls_sort_and_filter_without_changing_file_creation(self):
		letter, _ = self.completed()
		creation = frappe.db.get_value("File", letter.original_file, "creation")
		frappe.db.set_value("Incoming Letter", letter.name, {"received_date_known": 1, "received_date": "2026-09-15", "document_date": "2026-08-01"})
		row = self.rows(date_from="2026-09-15", date_to="2026-09-15")[0]
		self.assertEqual((row["date_kind"], row["document_date"]), ("received", "2026-09-15 00:00:00"))
		self.assertEqual(self.rows(date_to="2026-09-14"), [])
		self.assertEqual(frappe.db.get_value("File", letter.original_file, "creation"), creation)

	def test_snapshot_excludes_a_later_confirmation(self):
		first, _ = self.completed()
		second, _ = self.completed()
		page = documents.get_documents(self.customer.name, page_length=1)
		future = frappe.utils.now_datetime() + timedelta(minutes=5)
		# An existing upload confirmed after the first page must await refresh.
		later, _ = self.completed()
		frappe.db.set_value("File", later.original_file, "creation", frappe.utils.add_days(first.creation, -1))
		frappe.db.set_value("Incoming Letter", later.name, "confirmed_at", future)
		last = documents.get_documents(self.customer.name, cursor=page["next_cursor"], page_length=100)
		self.assertNotIn(later.original_file, [r["file_id"] for r in last["items"]])
		self.assertEqual({r["file_id"] for r in page["items"] + last["items"]}, {first.original_file, second.original_file})

	def source(self, task, **extra):
		from kanzlei_erp import posteingang_documents as source
		self.assertTrue(hasattr(source, "get_task_source"), "AP-08 Task source endpoint missing")
		return source.get_task_source(task=task, **extra)

	def test_task_projection_has_only_its_confirmed_action_and_no_ocr(self):
		letter, tasks = self.completed(2)
		frappe.set_user(self.reader.name)
		result = self.source(tasks[0])
		self.assertTrue(result["available"])
		self.assertEqual(result["letter"]["name"], letter.name)
		self.assertEqual(result["action"]["external_date"], "2026-11-20")
		self.assertEqual(result["action"]["external_date_basis"], "Original reviewed on page 1")
		self.assertEqual(set(result["action"]), {"action_id", "external_date", "external_date_basis", "evidence"})
		self.assertNotIn(tasks[1], json.dumps(result))
		self.assertEqual(result["original"]["file_id"], letter.original_file)
		self.assertNotIn("file_url", result["original"])

	def test_manual_evidence_is_recomputed_and_never_claims_verified_without_ocr(self):
		letter, tasks = self.completed(1)
		row = frappe.db.get_value("Incoming Letter Action", {"parent": letter.name}, "name")
		frappe.db.set_value("Incoming Letter Action", row, "evidence_json", json.dumps([{"page": 1, "quote": "Synthetic original", "verification": "matched"}]))
		self.assertEqual(self.source(tasks[0])["action"]["evidence"][0]["verification"], "unverified")
		frappe.db.set_value("Incoming Letter Action", row, "evidence_json", json.dumps([{"page": 2, "quote": None, "verification": "missing"}]))
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_RESULT"):
			self.source(tasks[0])

	def test_task_source_rejects_extra_parameters_and_broken_reverse_link(self):
		letter, tasks = self.completed(1)
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_REQUEST"):
			self.source(tasks[0], file_id=letter.original_file)
		row = frappe.db.get_value("Incoming Letter Action", {"parent": letter.name}, "name")
		frappe.db.set_value("Incoming Letter Action", row, "created_task", None)
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_RESULT"):
			self.source(tasks[0])

	def test_ordinary_task_returns_unavailable_and_revoked_source_is_denied(self):
		frappe.set_user(self.operator.name)
		task = frappe.get_doc({"doctype": "Task", "subject": "Ordinary synthetic task"}).insert()
		self.assertEqual(self.source(task.name), {"schema_version": "1.0", "available": False})
		_letter, tasks = self.completed(1)
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user(self.reader.name)
		with self.assertRaises(frappe.PermissionError):
			self.source(tasks[0])

	def test_original_pdf_disposition_range_and_head(self):
		from kanzlei_erp import posteingang_documents as source
		self.assertTrue(hasattr(source, "download_task_source"), "AP-08 protected download endpoint missing")
		letter, tasks = self.completed(1)
		file = frappe.get_doc("File", letter.original_file)
		with open(file.get_full_path(), "rb") as stream:
			original = stream.read()
		for method, headers, status in (("GET", {}, 200), ("HEAD", {}, 200), ("GET", {"Range": "bytes=0-9"}, 206)):
			env = EnvironBuilder(path="/api/method/kanzlei_erp.posteingang_documents.download_task_source", method=method, headers=headers).get_environ()
			with patch.object(frappe.local, "request", Request(env), create=True):
				response = source.download_task_source(tasks[0], disposition="attachment")
				try:
					self.assertEqual(response.status_code, status)
					self.assertIn("attachment", response.headers["Content-Disposition"])
					self.assertIn("no-store", response.headers["Cache-Control"])
					data = b"".join(response.get_app_iter(env))
					self.assertEqual(data, b"" if method == "HEAD" else original[:10] if status == 206 else original)
				finally:
					response.close()

	def test_names_are_not_identity_and_selected_customer_is_not_ownership(self):
		first, _ = self.completed()
		second, _ = self.completed()
		for letter in (first, second):
			frappe.db.set_value("File", letter.original_file, "file_name", "same.pdf")
			frappe.db.set_value("Incoming Letter", letter.name, "selected_customer", "unavailable-selected-customer")
		self.assertEqual({r["file_id"] for r in self.rows(query="same.pdf")}, {first.original_file, second.original_file})
		self.assertEqual(len(self.rows(query=first.name)), 1)
		for order in ("oldest", "newest"):
			rows = self.rows(sort=order)
			keys = [(r["document_date"], r["file_id"]) for r in rows]
			self.assertEqual(keys, sorted(keys, reverse=order == "newest"))

	def test_hidden_task_never_changes_contexts_or_search(self):
		letter, tasks = self.completed(2)
		frappe.db.set_value("Task", tasks[0], "subject", "Hidden unique phrase")
		permission = frappe.has_permission
		def filtered(doctype, ptype="read", doc=None, **kwargs):
			if doctype == "Task" and (doc if isinstance(doc, str) else getattr(doc, "name", None)) == tasks[0]:
				return False
			return permission(doctype, ptype, doc=doc, **kwargs)
		with patch.object(frappe, "has_permission", side_effect=filtered):
			row = self.rows()[0]
			self.assertEqual([c["name"] for c in row["contexts"] if c["source"] == "task"], [tasks[1]])
			self.assertEqual(self.rows(query="Hidden unique phrase"), [])
			self.assertEqual(row["file_id"], letter.original_file)

	def test_zip_deduplicates_ids_and_same_names_and_rejects_revoked_access(self):
		first, _ = self.completed()
		second, _ = self.completed()
		for letter in (first, second):
			frappe.db.set_value("File", letter.original_file, "file_name", "same.pdf")
		documents.download_documents_zip(self.customer.name, [first.original_file, first.original_file, second.original_file])
		with ZipFile(BytesIO(frappe.local.response.filecontent)) as archive:
			self.assertEqual(archive.namelist(), ["same.pdf", "same (2).pdf"])
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user(self.reader.name)
		for operation in (lambda: documents.download_document(self.customer.name, first.original_file), lambda: documents.download_documents_zip(self.customer.name, [first.original_file, second.original_file])):
			with self.assertRaises(frappe.PermissionError):
				operation()

	def test_correction_hides_original_even_from_admin_and_replacement_appears_only_after_confirmation(self):
		from kanzlei_erp import posteingang_confirmation as service
		from kanzlei_erp import posteingang_jobs as jobs
		from kanzlei_erp import posteingang_matching as matching
		letter, tasks = self.completed(1)
		frappe.set_user("Administrator")
		frappe.share.add("Customer", self.customer.name, self.manager.name, read=1, write=1)
		self.manager.add_roles("Projects User")
		frappe.set_user(self.manager.name)
		result = service.create_correction(letter.name, letter.revision, "correction", "Wrong synthetic original", True, False, None)
		self.assertEqual(self.rows(), [])
		self.assertTrue(self.source(tasks[0])["letter"]["quarantined"])
		new = frappe.get_doc("Incoming Letter", result["replacement"])
		action = frappe.db.get_value("Incoming Letter Action", {"parent": letter.name}, "action_id")
		service.correction_disposition(new.name, new.revision, action, "historical", "Retain prior work", "dispose")
		new.reload()
		frappe.set_user("Administrator")
		customer = frappe.get_doc({"doctype": "Customer", "customer_name": "Synthetic replacement " + frappe.generate_hash(), "customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories"}).insert()
		frappe.share.add("Customer", customer.name, self.manager.name, read=1, write=1)
		frappe.set_user(self.manager.name)
		matching.select_customer(new.name, customer.name, "Replacement reviewed", new.revision, "select")
		new.reload()
		jobs.manual_review(new.name, new.revision, "manual")
		state = service.get_review_state(new.name)
		state["review"].update(mode="confirm", human_verified=True, no_actions_reason="No new work")
		service.confirm(new.name, state["review"], state["document_fields"], "confirm-new")
		self.assertEqual(self.rows(), [])
		self.assertEqual([r["file_id"] for r in documents.get_documents(customer.name)["items"]], [new.original_file])
		with self.assertRaises(frappe.PermissionError):
			documents.download_document(self.customer.name, letter.original_file)

	def test_safe_errors_are_not_generic_traceback_errors(self):
		_, tasks = self.completed(1)
		with self.assertRaises(frappe.ValidationError) as error:
			self.source(tasks[0], actor="Administrator")
		self.assertIsInstance(error.exception, frappe.SecurityException)

	def test_disabled_feature_keeps_reads_but_disabled_or_website_user_cannot_read(self):
		letter, tasks = self.completed(1)
		frappe.db.set_single_value("Posteingang Settings", "enabled", 0)
		frappe.set_user(self.reader.name)
		self.assertTrue(self.source(tasks[0])["available"])
		self.assertEqual(self.rows()[0]["file_id"], letter.original_file)
		for user in ("Guest", self.website.name):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError):
				self.source(tasks[0])
		frappe.set_user("Administrator")
		frappe.db.set_value("User", self.reader.name, "enabled", 0)
		frappe.set_user(self.reader.name)
		with self.assertRaises(frappe.PermissionError):
			self.source(tasks[0])

	def test_reads_do_not_write_and_null_external_date_stays_unknown(self):
		letter, tasks = self.completed(1)
		frappe.db.set_value("Incoming Letter Action", {"parent": letter.name}, {"external_date": None, "external_date_basis": None})
		before = {kind: frappe.db.count(kind) for kind in ("File", "Incoming Letter", "Incoming Letter Event", "Incoming Letter Run", "Task", "ToDo")}
		with patch("kanzlei_erp.posteingang_jobs._dispatch", side_effect=AssertionError("Unexpected enqueue")), patch("kanzlei_erp.posteingang_ai.analyze_letter", side_effect=AssertionError("Unexpected AI")):
			self.assertIsNone(self.source(tasks[0])["action"]["external_date"])
			self.rows()
		self.assertEqual(before, {kind: frappe.db.count(kind) for kind in before})

	def test_old_cursor_and_bad_source_binding_are_rejected(self):
		import hashlib
		from base64 import urlsafe_b64encode
		letter, tasks = self.completed(1)
		filters = documents._normalize_filters()
		old_fingerprint = hashlib.sha256(json.dumps([self.customer.name, filters], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
		cursor = urlsafe_b64encode(json.dumps({"customer": self.customer.name, "filters": old_fingerprint, "sort": "newest", "snapshot": str(frappe.utils.now_datetime()), "date": "2026-01-01", "name": "file"}).encode()).decode()
		with self.assertRaises(frappe.ValidationError):
			documents.get_documents(self.customer.name, cursor=cursor)
		frappe.db.set_value("File", letter.original_file, "is_private", 0)
		self.assertEqual(self.rows(), [])
		with self.assertRaises(frappe.PermissionError):
			self.source(tasks[0])

	def test_action_cannot_claim_a_different_analysis_origin(self):
		letter, tasks = self.completed(1)
		frappe.db.set_value("Incoming Letter Action", {"parent": letter.name}, {"source_run": "foreign-run", "source_index": 0})
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_RESULT"):
			self.source(tasks[0])

	def test_failed_applied_run_cannot_make_saved_ocr_verified(self):
		from kanzlei_erp.posteingang_jobs import _insert, _snapshot
		letter, tasks = self.completed(1)
		run = _insert("Incoming Letter Run", {"letter": letter.name, "generation": letter.analysis_generation, "status": "Failed", "schema_version": "1.0", "source_snapshot_json": json.dumps(_snapshot(letter), default=str), "ocr_pages_json": json.dumps({"pages": [{"page": 1, "text": "Synthetic OCR"}]})})
		frappe.db.set_value("Incoming Letter", letter.name, {"current_run": run.name, "applied_run": run.name})
		frappe.db.set_value("Incoming Letter Action", {"parent": letter.name}, "evidence_json", json.dumps([{"page": 1, "quote": "Synthetic OCR", "verification": "unverified"}]))
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_RESULT"):
			self.source(tasks[0])

	def test_http_errors_do_not_reach_development_traceback_handler(self):
		_, tasks = self.completed(1)
		env = EnvironBuilder(path="/api/method/kanzlei_erp.posteingang_documents.get_task_source").get_environ()
		with patch.object(frappe.local, "request", Request(env), create=True), patch.object(frappe, "_dev_server", True):
			response = self.source(tasks[0], file_id="forged")
			self.assertEqual(response.status_code, 417)
			body = response.get_data()
			self.assertIn(b"INVALID_REQUEST", body)
			self.assertNotIn(b"Traceback", body)


class TestPosteingangDocumentsS3(PosteingangTestCase):
	completed = TestPosteingangDocuments.completed
	source = TestPosteingangDocuments.source
	rows = TestPosteingangDocuments.rows

	def setUp(self):
		if "suite" not in frappe.get_installed_apps():
			self.skipTest("Suite MemoryS3 integration")
		from kanzlei_erp.tests.test_object_storage import TestObjectStorage
		TestObjectStorage.setUp(self)
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)

	def test_s3_original_uses_guarded_response_and_zip_bytes(self):
		from kanzlei_erp.posteingang_documents import download_task_source
		letter, tasks = self.completed(1)
		file = frappe.get_doc("File", letter.original_file)
		expected = self.s3.objects[self.storage.get_binding(file.file_url).object_key]
		env = EnvironBuilder(path="/api/method/kanzlei_erp.posteingang_documents.download_task_source").get_environ()
		with patch.object(frappe.local, "request", Request(env), create=True):
			response = download_task_source(tasks[0])
			try:
				self.assertEqual(b"".join(response.iter_encoded()), expected)
			finally:
				response.close()
		documents.download_documents_zip(self.customer.name, [letter.original_file])
		with ZipFile(BytesIO(frappe.local.response.filecontent)) as archive:
			self.assertEqual(archive.read(archive.namelist()[0]), expected)

	def test_s3_failure_never_publishes_a_partial_zip(self):
		first, _ = self.completed()
		second, _ = self.completed()
		file = frappe.get_doc("File", second.original_file)
		self.s3.objects.pop(self.storage.get_binding(file.file_url).object_key)
		frappe.local.response.pop("filecontent", None)
		with self.assertRaises(frappe.PermissionError):
			documents.download_documents_zip(self.customer.name, [first.original_file, second.original_file])
		self.assertNotIn("filecontent", frappe.local.response)
