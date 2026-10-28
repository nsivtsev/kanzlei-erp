"""AP-09 read workspace; synthetic sources and existing workflow services only."""

import importlib
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangWorkspace(PosteingangTestCase):
	def test_work_views_separate_human_review_from_running_analysis(self):
		service = self.service()
		letter = self.letter()
		for status, view in (("Received", "review"), ("Needs Review", "review"), ("Error", "review"), ("Queued", "processing"), ("Processing", "processing")):
			frappe.db.set_value("Incoming Letter", letter.name, "status", status, update_modified=False)
			for requested in ("review", "processing"):
				names = [row["letter"] for row in service.get_queue(filters={"state": requested, "query": letter.name})["items"]]
				self.assertEqual(letter.name in names, requested == view)
	def test_receipt_date_is_separate_from_upload_time_and_unknown_is_preserved(self):
		service = self.service()
		letter = self.letter()
		metadata = service.get_letter_workspace(letter.name)["original"]
		self.assertFalse(metadata["received_date_known"])
		self.assertIsNone(metadata["received_date"])
		self.assertEqual(metadata["uploaded_at"], str(letter.received_at))

	def service(self):
		self.assertTrue(Path(frappe.get_app_path("kanzlei_erp", "posteingang_workspace.py")).exists(), "AP-09 workspace API missing")
		return importlib.import_module("kanzlei_erp.posteingang_workspace")

	def test_default_queue_filters_terminal_states_and_returns_minimal_metadata(self):
		service = self.service()
		letter = self.letter()
		result = service.get_queue()
		row = next(r for r in result["items"] if r["letter"] == letter.name)
		self.assertEqual(result["user"], self.operator.name)
		self.assertEqual(row["status"], "Received")
		self.assertNotIn("content_sha256", json.dumps(row))
		self.confirm_fixture(letter)
		frappe.set_user(self.operator.name)
		self.assertNotIn(letter.name, [r["letter"] for r in service.get_queue()["items"]])

	def test_operator_is_mandatory_even_for_manager_or_customer_reader(self):
		service = self.service()
		letter = self.letter()
		for user in (self.reader.name, self.website.name, "Guest"):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError):
				service.get_queue()
			with self.assertRaises(frappe.PermissionError):
				service.get_letter_workspace(letter.name)

	def test_filters_and_cursor_are_strict_and_bound_to_user_and_filter(self):
		service = self.service()
		first, second = self.letter(), self.letter()
		page = service.get_queue(filters={"query": "synthetic-"}, page_length=1)
		self.assertTrue(page["next_cursor"])
		for kwargs in ({"actor": self.operator.name}, {"filters": False}, {"filters": {"state": "bogus"}}, {"filters": {"state": []}}, {"page_length": 51}, {"cursor": page["next_cursor"], "filters": {"sort": "newest"}}):
			with self.assertRaises(frappe.ValidationError):
				service.get_queue(**kwargs)
		self.assertNotEqual(first.name, second.name)

	def test_workspace_uses_one_read_boundary_and_hides_file_path(self):
		service = self.service()
		letter = self.letter()
		with patch("kanzlei_erp.posteingang_jobs._http_boundary") as boundary:
			state = service.get_letter_workspace(letter.name)
			self.assertEqual(boundary.call_count, 1)
		self.assertEqual({state[k]["revision"] for k in ("analysis", "matching", "review")}, {letter.revision})
		self.assertNotIn("file_url", json.dumps(state))
		self.assertNotIn("ocr_pages_json", json.dumps(state))
		self.assertIn("download_original", state["original"]["preview_url"])

	def test_status_is_lightweight_and_disabled_feature_keeps_history_readable(self):
		service = self.service()
		letter = self.letter()
		frappe.db.set_single_value("Posteingang Settings", "enabled", 0)
		with patch("kanzlei_erp.posteingang_matching._source", side_effect=AssertionError("status must not validate OCR")):
			state = service.get_workspace_status(letter.name)
		self.assertFalse(state["capabilities"]["enabled"])
		self.assertNotIn("proposal", json.dumps(state))
		self.assertIn("cancel", state["analysis"]["allowed_operations"])

	def test_pdf_uses_persisted_binding_and_preserves_range_and_bytes(self):
		service = self.service()
		letter = self.letter()
		file = frappe.get_doc("File", letter.original_file)
		from kanzlei_erp.object_storage import open_file
		with open_file(file) as stream:
			content = stream.read()
		for method, headers, status in (("GET", {}, 200), ("HEAD", {}, 200), ("GET", {"Range": "bytes=0-9"}, 206)):
			env = EnvironBuilder(method=method, path="/api/method/source", headers=headers).get_environ()
			with patch.object(frappe.local, "request", Request(env), create=True), patch("kanzlei_erp.posteingang_jobs._http_boundary"):
				response = service.download_original(letter.name, "attachment")
				self.assertEqual(response.status_code, status)
				self.assertIn("no-store", response.headers["Cache-Control"])
				self.assertIn("attachment", response.headers["Content-Disposition"])
				self.assertEqual(b"".join(response.get_app_iter(env)), b"" if method == "HEAD" else content[:10] if status == 206 else content)
				response.close()
		frappe.db.set_value("File", file.name, "attached_to_name", "forged")
		with self.assertRaises(frappe.PermissionError):
			service.download_original(letter.name)
		with self.assertRaises(frappe.PermissionError):
			service.get_workspace_status(letter.name)

	def test_snapshot_and_sort_keep_new_uploads_out_of_remaining_pages(self):
		service = self.service()
		letters = [self.letter() for _ in range(3)]
		for i, letter in enumerate(letters):
			frappe.db.set_value("Incoming Letter", letter.name, {"sender": "AP09 cursor unique", "received_at": f"2026-01-0{i + 1} 10:00:00"})
		page = service.get_queue(filters={"query": "AP09 cursor unique"}, page_length=1)
		self.assertEqual(page["items"][0]["letter"], letters[0].name)
		later = self.letter()
		frappe.db.set_value("Incoming Letter", later.name, {"sender": "AP09 cursor unique", "received_at": "2099-01-01 10:00:00"})
		remaining = service.get_queue(filters={"query": "AP09 cursor unique"}, cursor=page["next_cursor"])
		self.assertEqual([r["letter"] for r in remaining["items"]], [r.name for r in letters[1:]])
		newest = service.get_queue(filters={"query": "AP09 cursor unique", "sort": "newest"})
		self.assertEqual([r["letter"] for r in newest["items"]], [r.name for r in reversed(letters)])

	def test_current_permissions_and_explicit_role_are_checked_each_request(self):
		service = self.service()
		letter = self.letter()
		service.get_workspace_status(letter.name)
		frappe.db.delete("Has Role", {"parent": self.operator.name, "role": "Posteingang Operator"})
		for call in (lambda: service.get_queue(), lambda: service.get_workspace_status(letter.name)):
			with self.assertRaises(frappe.PermissionError):
				call()
		frappe.set_user("Administrator")
		manager = self.user("manager-no-operator", ["System Manager"])
		frappe.set_user(manager.name)
		with self.assertRaises(frappe.PermissionError):
			service.get_queue()

	def test_corrupt_run_isolated_to_one_queue_row_and_never_claims_no_actions(self):
		service = self.service()
		letter = self.letter()
		from kanzlei_erp.posteingang_jobs import _insert
		run = _insert("Incoming Letter Run", {"letter": letter.name, "generation": 0, "status": "Succeeded", "schema_version": "1.0", "source_snapshot_json": "{}", "result_json": "{}"})
		frappe.db.set_value("Incoming Letter", letter.name, "current_run", run.name)
		row = next(r for r in service.get_queue()["items"] if r["letter"] == letter.name)
		self.assertEqual(row["result_error"], "INVALID_RESULT")
		self.assertIsNone(row["matching"])
		self.assertIsNone(row["deadline"])

	def test_reads_do_not_write_or_invoke_provider_or_queue(self):
		service = self.service()
		letter = self.letter()
		kinds = ("File", "Incoming Letter", "Incoming Letter Run", "Incoming Letter Action", "Incoming Letter Event", "Task")
		before = {kind: frappe.db.count(kind) for kind in kinds}
		with patch("kanzlei_erp.posteingang_jobs._dispatch", side_effect=AssertionError("no dispatch")), patch("kanzlei_erp.posteingang_ai.analyze_letter", side_effect=AssertionError("no AI")):
			service.get_queue()
			service.get_letter_workspace(letter.name)
			service.get_workspace_status(letter.name)
		self.assertEqual(before, {kind: frappe.db.count(kind) for kind in kinds})

	def test_disabled_and_website_operator_do_not_get_the_page_data(self):
		service = self.service()
		self.letter()
		frappe.db.set_value("User", self.operator.name, "enabled", 0)
		with self.assertRaises(frappe.PermissionError):
			service.get_queue()

	def test_safe_http_error_has_no_traceback_or_injected_parameters(self):
		service = self.service()
		self.letter()
		env = EnvironBuilder(method="POST", path="/api/method/workspace").get_environ()
		with patch.object(frappe.local, "request", Request(env), create=True), patch("kanzlei_erp.posteingang_jobs._http_boundary"):
			response = service.get_queue(actor="SECRET SYNTHETIC INPUT")
			self.assertEqual(response.status_code, 417)
			self.assertNotIn(b"Traceback", response.get_data())
			self.assertNotIn(b"SECRET SYNTHETIC INPUT", response.get_data())

	def test_reference_visibility_changes_without_changing_the_letter_revision(self):
		service = self.service()
		letter = self.letter()
		frappe.set_user("Administrator")
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		frappe.set_user(self.operator.name)
		before = service.get_workspace_status(letter.name)
		self.assertIn("visibility_key", before)
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.operator.name)
		frappe.set_user(self.operator.name)
		after = service.get_workspace_status(letter.name)
		self.assertEqual(before["revision"], after["revision"])
		self.assertNotEqual(before["visibility_key"], after["visibility_key"])

	def test_guest_dispatch_is_rejected_inside_the_safe_http_boundary(self):
		from frappe.handler import execute_cmd
		frappe.set_user("Guest")
		env = EnvironBuilder(method="GET", path="/api/method/kanzlei_erp.posteingang_workspace.get_queue").get_environ()
		with patch.object(frappe.local, "request", Request(env), create=True), patch.object(frappe.local, "form_dict", frappe._dict(), create=True):
			response = execute_cmd("kanzlei_erp.posteingang_workspace.get_queue")
			self.assertEqual(response.status_code, 403)
			self.assertNotIn(b"Traceback", response.get_data())

	def test_confirmed_manual_results_with_zero_or_multiple_tasks_are_readonly(self):
		from kanzlei_erp.tests.test_posteingang_documents import TestPosteingangDocuments
		service = self.service()
		frappe.set_user("Administrator")
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		for count in (0, 2):
			letter, tasks = TestPosteingangDocuments.completed(self, count)
			state = service.get_letter_workspace(letter.name)
			self.assertEqual(state["status"], "Completed")
			self.assertNotIn("save_review", state["review"]["allowed_operations"])
			self.assertIsNone(state["review"]["proposal"])
			self.assertEqual([r["task"] for r in state["review"]["receipt"]["actions"]], tasks)

	def test_receipt_visibility_is_refreshed_without_letter_revision_changes(self):
		from kanzlei_erp.tests.test_posteingang_documents import TestPosteingangDocuments
		service = self.service()
		frappe.set_user("Administrator")
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		letter, tasks = TestPosteingangDocuments.completed(self, 1)
		before = service.get_workspace_status(letter.name)
		permission = frappe.has_permission

		def denied(doctype, *args, **kwargs):
			if doctype == "Task" and kwargs.get("doc") == tasks[0]:
				return False
			return permission(doctype, *args, **kwargs)

		with patch("frappe.has_permission", side_effect=denied):
			after = service.get_letter_workspace(letter.name)
		self.assertEqual(before["revision"], after["revision"])
		self.assertNotEqual(before["visibility_key"], after["visibility_key"])
		self.assertIsNone(after["review"]["receipt"]["actions"][0]["task"])


class TestPosteingangWorkspaceS3(PosteingangTestCase):
	service = TestPosteingangWorkspace.service
	test_pdf_uses_persisted_binding_and_preserves_range_and_bytes = TestPosteingangWorkspace.test_pdf_uses_persisted_binding_and_preserves_range_and_bytes

	def setUp(self):
		if "suite" not in frappe.get_installed_apps():
			self.skipTest("Suite MemoryS3 integration")
		from kanzlei_erp.tests.test_object_storage import TestObjectStorage
		TestObjectStorage.setUp(self)
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
