"""AP-04 service contract. Only synthetic documents and mocked provider HTTP."""

import copy
import io
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock, patch

import frappe
import requests

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase

FIXTURES = Path(__file__).parent / "fixtures/posteingang"


class TestPosteingangAi(PosteingangTestCase):
	def setUp(self):
		super().setUp()
		self.assertTrue(
			Path(frappe.get_app_path("kanzlei_erp", "posteingang_ai.py")).exists(), "AP-04 adapter missing"
		)
		from kanzlei_erp import posteingang_ai

		self.ai = posteingang_ai
		self.doc = self.fixture_letter()

	def fixture_letter(self, case_id="AP01-01"):
		from kanzlei_erp.posteingang_model import create_received_letter

		case = next(
			c
			for c in json.loads((FIXTURES / "manifest.json").read_text())["cases"]
			if c["case_id"] == case_id
		)
		frappe.set_user(self.operator.name)
		doc = create_received_letter(
			actor=self.operator.name,
			original={
				"content": (FIXTURES / case["pdf"]).read_bytes(),
				"file_name": case_id + "-" + frappe.generate_hash(length=10) + ".pdf",
				"page_count": case["page_count"],
			},
			receipt={
				"received_date_known": case["received_date_known"],
				"received_date": case["received_date"],
			},
			operation={
				"key": frappe.generate_hash(length=64),
				"fingerprint": frappe.generate_hash(length=64),
			},
		)
		file = frappe.get_doc("File", doc.original_file)
		from kanzlei_erp.object_storage import stored

		if not stored(file):
			self.paths.append(Path(file.get_full_path()))
		return doc

	def real_config(self):
		frappe.db.set_single_value(
			"Posteingang Settings",
			{
				"provider": "mistral",
				"external_analysis_enabled": 1,
				"ocr_model": "mistral-ocr-4-1",
				"extraction_model": "mistral-small-2603",
			},
		)

	def source(self):
		return self.ai.prepare_source(self.doc.name, actor=frappe.session.user)

	def responses(self, case_id="AP01-01"):
		from kanzlei_erp.posteingang_ai_validation import without_verification

		pages = json.loads((FIXTURES / f"ocr/{case_id}.json").read_text())["pages"]
		gold = json.loads((FIXTURES / f"extraction/{case_id}.json").read_text())
		ocr = {
			"model": "mistral-ocr-4-1",
			"pages": [{"index": p["page"] - 1, "markdown": p["text"]} for p in pages],
		}
		chat = {
			"model": "mistral-small-2603",
			"choices": [
				{"finish_reason": "stop", "message": {"content": json.dumps(without_verification(gold))}}
			],
		}
		return ocr, chat

	def transport(self, *responses):
		session = Mock()
		session.__enter__ = Mock(return_value=session)
		session.__exit__ = Mock(return_value=False)
		returned = []
		for value in responses:
			if isinstance(value, BaseException):
				returned.append(value)
				continue
			response = Mock(status_code=200, headers={})
			response.iter_content.return_value = [json.dumps(value).encode()]
			returned.append(response)
		session.post.side_effect = returned
		return session, patch("kanzlei_erp.posteingang_ai.requests.Session", return_value=session), returned

	def test_source_uses_original_and_rejects_forgery(self):
		source = self.source()
		self.assertNotIsInstance(source, self.ai.Failure)
		self.assertEqual(sha256(source.content).hexdigest(), self.doc.content_sha256)
		self.assertNotIn("%PDF", repr(source))
		for name, actor in (
			("https://example.invalid/a.pdf", self.operator.name),
			(self.doc.original_file, self.operator.name),
			(self.doc.name, self.manager.name),
		):
			self.assertIsInstance(self.ai.prepare_source(name, actor=actor), self.ai.Failure)
		self.assertIsInstance(self.ai.recognize({"content": b"forged"}), self.ai.Failure)
		self.assertIsInstance(self.ai.recognize(replace(source, content=b"forged")), self.ai.Failure)

	def test_all_fake_cases_and_no_database_writes(self):
		with patch.object(requests.sessions.Session, "request", side_effect=AssertionError("no network")):
			for case in json.loads((FIXTURES / "manifest.json").read_text())["cases"]:
				doc = self.fixture_letter(case["case_id"])
				before = {
					d: frappe.db.count(d)
					for d in (
						"Incoming Letter",
						"Incoming Letter Run",
						"Incoming Letter Event",
						"Incoming Letter Action",
						"Task",
						"File",
					)
				}
				result = self.ai.analyze_letter(doc.name, actor=self.operator.name)
				self.assertIsInstance(result, self.ai.AnalysisResult, case["case_id"])
				self.assertEqual(result.schema_version, "1.0")
				self.assertEqual(result.prompt_version, "fake-v1")
				self.assertEqual(before, {d: frappe.db.count(d) for d in before})
				self.assertEqual(
					frappe.db.get_value(
						"Incoming Letter", doc.name, ["status", "revision", "analysis_generation"]
					),
					("Received", 1, 0),
				)

	def test_fake_never_reads_ground_truth_or_reviews(self):
		read = Path.read_text

		def guarded(path, *args, **kwargs):
			self.assertNotIn(path.parent.name, ("texts", "reviews", "annotations"))
			self.assertNotIn(path.name, ("catalog.json", "sources.json", "scenarios.json"))
			return read(path, *args, **kwargs)

		with patch.object(Path, "read_text", guarded):
			self.assertIsInstance(
				self.ai.analyze_letter(self.doc.name, actor=self.operator.name), self.ai.AnalysisResult
			)
		unknown = self.letter()
		self.assertEqual(
			self.ai.analyze_letter(unknown.name, actor=self.operator.name).code, "FAKE_FIXTURE_NOT_FOUND"
		)

	def test_http_parameters_and_valid_canonical_result(self):
		self.real_config()
		session, transport, responses = self.transport(*self.responses())
		with (
			transport,
			patch.object(frappe.model.document.Document, "get_password", return_value="synthetic-key"),
		):
			result = self.ai.analyze_letter(self.doc.name, actor=self.operator.name)
		self.assertIsInstance(result, self.ai.AnalysisResult)
		self.assertEqual(result.extraction["actions"][0]["deadline"]["date"], "2026-11-20")
		self.assertEqual(session.post.call_count, 2)
		ocr, chat = session.post.call_args_list
		self.assertEqual(ocr.args[0], "https://api.mistral.ai/v1/ocr")
		self.assertTrue(
			json.loads(ocr.kwargs["data"])["document"]["document_url"].startswith(
				"data:application/pdf;base64,"
			)
		)
		payload = json.loads(chat.kwargs["data"])
		self.assertEqual(payload["response_format"]["type"], "json_schema")
		self.assertEqual(payload["tool_choice"], "none")
		self.assertNotIn("tools", payload)
		self.assertNotIn("verification", json.dumps(payload["response_format"]))
		for call in (ocr, chat):
			self.assertFalse(call.kwargs["allow_redirects"])
			self.assertTrue(call.kwargs["verify"])
		for response in responses:
			response.close.assert_called_once()
		self.assertNotIn("synthetic-key", repr(result))
		self.assertNotIn("SYN-0007", repr(result))

	def test_empty_ocr_is_unknown_without_extraction_call(self):
		self.real_config()
		session, transport, _ = self.transport(
			{"pages": [{"index": 0, "markdown": ""}], "model": "mistral-ocr-4-1"}
		)
		with (
			transport,
			patch.object(frappe.model.document.Document, "get_password", return_value="synthetic-key"),
		):
			result = self.ai.analyze_letter(self.doc.name, actor=self.operator.name)
		self.assertIsInstance(result, self.ai.AnalysisResult)
		self.assertEqual(
			(result.extraction["readability"], result.extraction["action_assessment"]),
			("unreadable", "unknown"),
		)
		self.assertEqual(session.post.call_count, 1)

	def test_transport_failures_are_safe_and_not_retried(self):
		self.real_config()
		vectors = json.loads((FIXTURES / "provider/ap04-errors.json").read_text())
		for vector in vectors:
			with self.subTest(vector=vector["id"]):
				session, transport, responses = self.transport({"secret": "document sentinel"})
				responses[0].status_code = vector["status"]
				responses[0].headers = {"Retry-After": "999999"}
				with (
					transport,
					patch.object(
						frappe.model.document.Document, "get_password", return_value="synthetic-key"
					),
				):
					result = self.ai.recognize(self.source())
				self.assertEqual(result.code, vector["code"])
				self.assertEqual(result.retryable, vector["retryable"])
				self.assertLessEqual(result.retry_after_seconds or 0, 120)
				self.assertEqual(session.post.call_count, 1)
				self.assertNotIn("sentinel", repr(result))
		for error in (
			requests.Timeout("synthetic-key document sentinel"),
			requests.ConnectionError("document sentinel"),
		):
			session, transport, _ = self.transport(error)
			with (
				transport,
				patch.object(frappe.model.document.Document, "get_password", return_value="synthetic-key"),
			):
				result = self.ai.recognize(self.source())
			self.assertEqual(result.code, "PROVIDER_UNAVAILABLE")
			self.assertTrue(result.retryable)
			self.assertNotIn("sentinel", repr(result))

	def test_invalid_extraction_preserves_ocr(self):
		self.real_config()
		ocr, chat = self.responses()
		for change, code in (
			(lambda r: r["choices"][0].update(finish_reason="length"), "LIMIT_EXCEEDED"),
			(lambda r: r["choices"][0]["message"].update(content="not JSON"), "INVALID_RESULT"),
			(
				lambda r: r["choices"][0]["message"].update(tool_calls=[{"name": "anything"}]),
				"INVALID_RESULT",
			),
		):
			response = copy.deepcopy(chat)
			change(response)
			_, transport, _ = self.transport(ocr, response)
			with (
				transport,
				patch.object(frappe.model.document.Document, "get_password", return_value="synthetic-key"),
			):
				result = self.ai.analyze_letter(self.doc.name, actor=self.operator.name)
			self.assertEqual(result.code, code)
			self.assertEqual(result.ocr_result.content_sha256, self.doc.content_sha256)
			self.assertNotIn("SYN-0007", repr(result))

	def test_rechecks_role_user_access_settings_between_steps(self):
		mutations = (
			lambda: frappe.db.set_single_value("Posteingang Settings", "enabled", 0),
			lambda: frappe.db.set_single_value("Posteingang Settings", "external_analysis_enabled", 0),
			lambda: frappe.db.set_single_value(
				"Posteingang Settings", "extraction_model", "mistral-small-2506"
			),
			lambda: frappe.db.set_value("User", self.operator.name, "enabled", 0),
			lambda: frappe.db.delete(
				"Has Role", {"parent": self.operator.name, "role": "Posteingang Operator"}
			),
			lambda: frappe.db.set_value(
				"Incoming Letter", self.doc.name, "confirmed_customer", self.customer.name
			),
		)
		for mutation in mutations:
			frappe.db.savepoint("ap04_revoke")
			self.real_config()
			source = self.source()
			_, transport, _ = self.transport(self.responses()[0])
			with (
				transport,
				patch.object(frappe.model.document.Document, "get_password", return_value="synthetic-key"),
			):
				ocr = self.ai.recognize(source)
			mutation()
			with patch.object(requests.sessions.Session, "request", side_effect=AssertionError("revoked")):
				self.assertEqual(self.ai.extract(source, ocr).code, "ANALYSIS_STOPPED")
			frappe.db.rollback(save_point="ap04_revoke")

	def test_terminal_quarantine_and_missing_key(self):
		for values in ({"status": "Completed"}, {"status": "Cancelled"}, {"correction_quarantined": 1}):
			frappe.db.savepoint("ap04_terminal")
			frappe.db.set_value("Incoming Letter", self.doc.name, values)
			self.assertEqual(self.source().code, "ANALYSIS_STOPPED")
			frappe.db.rollback(save_point="ap04_terminal")
		self.real_config()
		with patch.object(frappe.model.document.Document, "get_password", return_value=None):
			self.assertEqual(self.ai.recognize(self.source()).code, "CONFIGURATION_INVALID")

	def test_reusable_ocr_must_match_source_and_models(self):
		source = self.source()
		ocr = self.ai.recognize(source)
		self.assertIsInstance(self.ai.extract(source, replace(ocr, content_sha256="0" * 64)), self.ai.Failure)
		self.assertIsInstance(
			self.ai.extract(source, replace(ocr, pages=[{"page": 2, "text": "bad"}])), self.ai.Failure
		)

	def test_payload_limits_and_stream_read_failures(self):
		self.real_config()
		source = self.source()
		_, transport, _ = self.transport(self.responses()[0])
		with (
			transport,
			patch.object(frappe.model.document.Document, "get_password", return_value="synthetic-key"),
		):
			ocr = self.ai.recognize(source)
		with (
			patch.object(self.ai, "MAX_EXTRACTION_REQUEST_BYTES", 1),
			patch.object(requests.sessions.Session, "request", side_effect=AssertionError("over budget")),
		):
			self.assertEqual(self.ai.extract(source, ocr).code, "LIMIT_EXCEEDED")
		for body, code in (
			([b"x" * (16 * 1024 * 1024 + 1)], "LIMIT_EXCEEDED"),
			([b"not JSON"], "OCR_FAILED"),
		):
			_, transport, responses = self.transport({})
			responses[0].iter_content.return_value = body
			with (
				transport,
				patch.object(frappe.model.document.Document, "get_password", return_value="synthetic-key"),
			):
				self.assertEqual(self.ai.recognize(self.source()).code, code)
		_, transport, responses = self.transport({})
		responses[0].iter_content.side_effect = requests.ConnectionError("private document")
		with (
			transport,
			patch.object(frappe.model.document.Document, "get_password", return_value="synthetic-key"),
		):
			self.assertEqual(self.ai.recognize(self.source()).code, "PROVIDER_UNAVAILABLE")
		responses[0].close.assert_called_once()

	def test_settings_require_concrete_models_and_key_without_network(self):
		frappe.set_user("Administrator")
		for model in ("mistral-ocr-latest", "https://example.invalid/model", "model\nname", "model name"):
			settings = frappe.get_single("Posteingang Settings")
			settings.update(
				{
					"provider": "mistral",
					"external_analysis_enabled": 1,
					"ocr_model": model,
					"extraction_model": "mistral-small-2603",
					"api_key": "synthetic-key",
				}
			)
			with self.assertRaises(frappe.ValidationError):
				settings.save()
		settings = frappe.get_single("Posteingang Settings")
		settings.update(
			{
				"provider": "mistral",
				"external_analysis_enabled": 1,
				"ocr_model": "mistral-ocr-4-1",
				"extraction_model": "mistral-small-2603",
				"api_key": "synthetic-key",
			}
		)
		with patch.object(
			requests.sessions.Session, "request", side_effect=AssertionError("settings must not call API")
		):
			settings.save()
		self.assertNotIn("synthetic-key", frappe.as_json(settings.as_dict()))
		settings.reload()
		self.assertEqual(settings.get_password("api_key"), "synthetic-key")

	def test_methods_are_not_whitelisted(self):
		for name in ("prepare_source", "recognize", "extract", "analyze_letter"):
			self.assertNotIn(getattr(self.ai, name), frappe.whitelisted)

	def test_invalid_settings_do_not_snapshot_serialized_secret(self):
		from frappe.utils.error import log_error_snapshot

		frappe.set_user("Administrator")
		for invalid in ({"ocr_model": "mistral-ocr-latest"}, {"max_pages": 51}, {"extraction_model": None}):
			payload = frappe.get_single("Posteingang Settings").as_dict()
			payload.update(
				{
					"provider": "mistral",
					"external_analysis_enabled": 1,
					"ocr_model": "mistral-ocr-4-1",
					"extraction_model": "mistral-small-2603",
					"api_key": "AP04-secret-form-sentinel",
					**invalid,
				}
			)
			form = frappe._dict(doc=json.dumps(payload, default=str), action="Save")
			with patch.object(frappe.local, "form_dict", form), patch("frappe.utils.error.log_error") as log:
				with self.assertRaises(frappe.ValidationError) as caught:
					frappe.call("frappe.desk.form.save.savedocs", **form)
				log_error_snapshot(caught.exception)
				log.assert_not_called()
				self.assertEqual(caught.exception.http_status_code, 417)

	def test_unsaved_settings_serialization_masks_secret(self):
		settings = frappe.get_single("Posteingang Settings")
		settings.api_key = "AP04-unsaved-secret-sentinel"
		self.assertNotIn("AP04-unsaved-secret-sentinel", frappe.as_json(settings.as_dict()))

	def test_guard_revalidates_private_file_binding(self):
		source = self.source()
		for values in ({"is_private": 0}, {"attached_to_name": "other-letter"}):
			frappe.db.savepoint("ap04_binding")
			frappe.db.set_value("File", self.doc.original_file, values)
			self.assertEqual(self.ai.recognize(source).code, "ANALYSIS_STOPPED")
			frappe.db.rollback(save_point="ap04_binding")

	def test_source_limits_and_hash_mismatch(self):
		with patch("kanzlei_erp.object_storage.open_file", return_value=io.BytesIO(b"forged")):
			self.assertEqual(self.source().code, "OCR_FAILED")
		frappe.db.set_single_value("Posteingang Settings", "max_file_bytes", 1)
		self.assertEqual(self.source().code, "LIMIT_EXCEEDED")
		frappe.set_user("Guest")
		self.assertEqual(self.source().code, "ANALYSIS_STOPPED")

	def test_surrogate_ocr_is_rejected_without_traceback(self):
		self.real_config()
		_, transport, _ = self.transport(
			{
				"pages": [{"index": 0, "markdown": b"private\\ud800".decode("unicode_escape")}],
				"model": "mistral-ocr-4-1",
			}
		)
		with (
			transport,
			patch.object(frappe.model.document.Document, "get_password", return_value="synthetic-key"),
			patch.object(frappe, "log_error") as log,
		):
			result = self.ai.analyze_letter(self.doc.name, actor=self.operator.name)
		self.assertEqual(result.code, "OCR_FAILED")
		log.assert_not_called()

	def test_invalid_secret_never_reaches_http_headers(self):
		self.real_config()
		for key in ("synthetic-🔑", "invalid key", "invalid\nkey"):
			session, transport, _ = self.transport(self.responses()[0])
			with transport, patch.object(frappe.model.document.Document, "get_password", return_value=key):
				result = self.ai.recognize(self.source())
			self.assertIsInstance(result, self.ai.Failure)
			self.assertEqual(result.code, "CONFIGURATION_INVALID")
			session.post.assert_not_called()

	def test_z_reads_memory_s3_original_without_materialization(self):
		if "suite" not in frappe.get_installed_apps():
			self.skipTest("Suite object storage is optional")
		from kanzlei_erp import object_storage
		from kanzlei_erp.tests.test_object_storage import TestObjectStorage

		frappe.set_user("Administrator")
		before = object_storage.settings().as_dict()
		TestObjectStorage.setUp(self)
		try:
			doc = self.fixture_letter()
			self.assertTrue(object_storage.stored(frappe.get_doc("File", doc.original_file)))
			with patch.object(object_storage, "materialize", side_effect=AssertionError("stream only")):
				result = self.ai.analyze_letter(doc.name, actor=self.operator.name)
			self.assertIsInstance(result, self.ai.AnalysisResult)
		finally:
			frappe.set_user("Administrator")
			with object_storage.configuration_change():
				settings = object_storage.settings()
				settings.update(
					{
						f.fieldname: before.get(f.fieldname)
						for f in settings.meta.fields
						if f.fieldname in before
					}
				)
				settings.save()
