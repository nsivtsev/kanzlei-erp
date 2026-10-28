"""AP-03 intake contracts, using only synthetic documents."""

import io
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import frappe
from werkzeug.datastructures import FileStorage, MultiDict

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangIntake(PosteingangTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user(self.operator.name)
		self.content = (
			Path(frappe.get_app_path("kanzlei_erp")) / "tests/fixtures/posteingang/pdfs/AP01-01.pdf"
		).read_bytes()
		self.content += ("\n% " + frappe.generate_hash(length=16)).encode()

	def receive(self, key=None, content=None, known="false", date=None, **extra):
		from kanzlei_erp.posteingang import _receive, fail

		if extra:
			fail("INVALID_REQUEST")
		return _receive(
			io.BytesIO(content or self.content),
			"../../synthetic.pdf",
			key or frappe.generate_hash(length=30),
			known,
			date,
		)

	def test_accept_replay_and_duplicate(self):
		key = frappe.generate_hash(length=30)
		first = self.receive(key)
		self.assertEqual(first["outcome"], "created")
		self.assertEqual(self.receive(key), {**first, "replayed": True})
		duplicate = self.receive(known="true", date="2026-10-20")
		self.assertEqual(duplicate["outcome"], "duplicate")
		self.assertEqual(duplicate["letter"], first["letter"])
		doc = frappe.get_doc("Incoming Letter", first["letter"])
		self.assertEqual((doc.status, doc.revision, doc.analysis_generation), ("Received", 1, 0))
		self.assertIsNone(doc.received_date)
		self.assertEqual(frappe.db.count("Incoming Letter Event", {"letter": doc.name}), 1)
		file = frappe.get_doc("File", doc.original_file)
		self.paths.append(Path(file.get_full_path()))
		data = file.get_content(encodings=[])
		self.assertEqual(sha256(data).hexdigest(), sha256(self.content).hexdigest())

	def test_rejects_bad_inputs_and_conflicting_key(self):
		for extra in (
			{"known": "maybe"},
			{"known": "true", "date": "2026-02-30"},
			{"date": "2026-10-20"},
			{"date": ""},
			{"customer": self.customer.name},
		):
			with self.subTest(extra=extra), self.assertRaisesRegex(frappe.ValidationError, "INVALID_REQUEST"):
				self.receive(**extra)
		key = frappe.generate_hash(length=30)
		self.receive(key)
		with self.assertRaisesRegex(frappe.ValidationError, "IDEMPOTENCY_CONFLICT"):
			self.receive(key, known="true", date="2026-10-20")

	def test_status_and_replay_when_disabled(self):
		from kanzlei_erp.posteingang import get_intake_result

		key = frappe.generate_hash(length=30)
		result = self.receive(key)
		frappe.db.set_single_value("Posteingang Settings", "enabled", 0)
		self.assertEqual(get_intake_result(key)["letter"], result["letter"])
		self.assertTrue(self.receive(key)["replayed"])
		with self.assertRaises(frappe.PermissionError):
			self.receive()

	def test_receipt_is_not_generic_readable_even_with_share(self):
		from kanzlei_erp.posteingang import operation_key

		key = frappe.generate_hash(length=30)
		self.receive(key)
		name = operation_key(self.operator.name, key)
		frappe.set_user("Administrator")
		frappe.share.add(
			"Incoming Letter Intake Receipt",
			name,
			self.operator.name,
			read=1,
			flags={"ignore_share_permission": True},
		)
		for user in (self.operator.name, "Administrator"):
			self.assertFalse(
				frappe.has_permission("Incoming Letter Intake Receipt", "read", doc=name, user=user)
			)

	def test_hidden_duplicate_and_revoked_replay(self):
		from kanzlei_erp.posteingang import get_intake_result

		key = frappe.generate_hash(length=30)
		first = self.receive(key)
		doc = frappe.get_doc("Incoming Letter", first["letter"])
		self.confirm_fixture(doc, quarantine=True)
		frappe.set_user(self.operator.name)
		self.assertEqual(get_intake_result(key), {"outcome": "unknown"})
		with self.assertRaisesRegex(frappe.ValidationError, "CANNOT_ACCEPT_FILE"):
			self.receive()
		with self.assertRaisesRegex(frappe.ValidationError, "CANNOT_ACCEPT_FILE"):
			self.receive(key, known="true", date="2026-10-20")

	def test_receipt_failure_cleans_all_new_records_and_bytes(self):
		from frappe.model.base_document import get_controller

		from kanzlei_erp.posteingang import _save_receipt

		File = get_controller("File")
		paths = []
		original = File.after_insert

		def capture(file):
			paths.append(Path(file.get_full_path()))
			return original(file)

		before = {
			d: frappe.db.count(d)
			for d in ("Incoming Letter", "File", "Incoming Letter Event", "Incoming Letter Intake Receipt")
		}
		with (
			patch(
				"kanzlei_erp.posteingang._save_receipt", side_effect=RuntimeError("synthetic receipt failure")
			),
			patch.object(File, "after_insert", capture),
		):
			with self.assertRaisesRegex(RuntimeError, "synthetic receipt failure"):
				self.receive()
		for d, count in before.items():
			self.assertEqual(frappe.db.count(d), count)
		self.assertTrue(paths)
		self.assertTrue(all(not p.exists() for p in paths))

	def test_duplicate_does_not_reparse_after_page_limit_decreases(self):
		from pypdf import PdfWriter

		writer = PdfWriter()
		for _ in range(2):
			writer.add_blank_page(width=100, height=100)
		out = io.BytesIO()
		writer.write(out)
		content = out.getvalue() + ("\n% " + frappe.generate_hash(length=16)).encode()
		first = self.receive(content=content)
		frappe.db.set_single_value("Posteingang Settings", "max_pages", 1)
		with patch(
			"kanzlei_erp.posteingang.validate_pdf", side_effect=AssertionError("must not parse duplicate")
		):
			duplicate = self.receive(content=content)
		self.assertEqual(duplicate["outcome"], "duplicate")
		self.assertEqual(duplicate["letter"], first["letter"])

	def test_disabled_user_and_revoked_operator_role(self):
		for field in ("enabled", "role"):
			with self.subTest(field=field):
				if field == "enabled":
					frappe.db.set_value("User", self.operator.name, "enabled", 0)
				else:
					frappe.db.set_value("User", self.operator.name, "enabled", 1)
					frappe.db.delete(
						"Has Role", {"parent": self.operator.name, "role": "Posteingang Operator"}
					)
				with self.assertRaises(frappe.PermissionError):
					self.receive()

	def test_lowered_limits_and_invalid_pdf_leave_no_records(self):
		before = frappe.db.count("Incoming Letter")
		frappe.db.set_single_value("Posteingang Settings", "max_file_bytes", len(self.content) - 1)
		with self.assertRaisesRegex(frappe.ValidationError, "FILE_TOO_LARGE"):
			self.receive()
		frappe.db.set_single_value("Posteingang Settings", "max_file_bytes", 20971520)
		with self.assertRaisesRegex(frappe.ValidationError, "PDF_INVALID"):
			self.receive(content=b"not a PDF")
		self.assertEqual(frappe.db.count("Incoming Letter"), before)

	def test_transport_rejects_multiple_files_and_unknown_business_arguments(self):
		from kanzlei_erp.posteingang import receive

		def file():
			return FileStorage(io.BytesIO(self.content), filename="synthetic.pdf")

		for files, extra in (
			([("file", file()), ("file", file())], {}),
			([("file", file())], {"customer": "forbidden"}),
		):
			with patch.object(frappe.local, "request", frappe._dict(files=MultiDict(files)), create=True):
				with self.assertRaisesRegex(frappe.ValidationError, "INVALID_REQUEST"):
					receive(request_key="test", received_date_known="false", **extra)


class TestIntakeS3(TestPosteingangIntake):
	"""Exercise the same contracts on MemoryS3 as well as the real local disk."""

	def setUp(self):
		if "suite" not in frappe.get_installed_apps():
			self.skipTest("Suite S3 integration")
		from kanzlei_erp.tests.test_object_storage import TestObjectStorage

		TestObjectStorage.setUp(self)
		super().setUp()

	def test_receipt_failure_cleans_all_new_records_and_bytes(self):
		before = dict(self.s3.objects)
		with patch(
			"kanzlei_erp.posteingang._save_receipt", side_effect=RuntimeError("synthetic receipt failure")
		):
			with self.assertRaisesRegex(RuntimeError, "synthetic receipt failure"):
				self.receive()
		self.assertEqual(self.s3.objects, before)

	def test_accept_replay_and_duplicate(self):
		first = self.receive()
		before = dict(self.s3.objects)
		self.assertEqual(self.receive()["letter"], first["letter"])
		self.assertEqual(self.s3.objects, before)
		file = frappe.get_doc("File", first["original_file"])
		self.assertEqual(file.get_content(encodings=[]), self.content)
