"""AP-04 validation without Frappe, external services or a live provider key."""

import copy
import importlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "kanzlei_erp/tests/fixtures/posteingang"


def raw_result(value):
	value = copy.deepcopy(value)

	def visit(node):
		if isinstance(node, dict):
			node.pop("verification", None)
			for child in node.values():
				visit(child)
		elif isinstance(node, list):
			for child in node:
				visit(child)

	visit(value)
	return value


class AiValidationTests(unittest.TestCase):
	def setUp(self):
		self.assertTrue(
			(ROOT / "kanzlei_erp/posteingang_ai_validation.py").exists(), "AP-04 validator missing"
		)
		self.v = importlib.import_module("kanzlei_erp.posteingang_ai_validation")
		self.gold = json.loads((FIXTURES / "extraction/AP01-01.json").read_text())
		self.raw = raw_result(self.gold)
		self.pages = json.loads((FIXTURES / "ocr/AP01-01.json").read_text())["pages"]

	def invalid(self, change, code="INVALID_RESULT"):
		change(self.raw)
		with self.assertRaises(self.v.InvalidResult) as caught:
			self.v.validate_extraction(self.raw, self.pages, max_actions=30)
		self.assertEqual(caught.exception.code, code)
		self.assertNotIn("SYN-0007", str(caught.exception))

	def test_runtime_schemas_match_contract(self):
		for name in ("common", "extraction", "support"):
			expected = json.loads(
				(ROOT / f"docs/plans/ai-posteingang/schemas/{name}.schema.json").read_text()
			)
			self.assertEqual(self.v.schema(name), expected)

	def test_all_synthetic_extractions(self):
		manifest = json.loads((FIXTURES / "manifest.json").read_text())
		for case in manifest["cases"]:
			with self.subTest(case=case["case_id"]):
				gold = json.loads((FIXTURES / case["extraction"]).read_text())
				pages = json.loads((FIXTURES / case["ocr"]).read_text())["pages"]
				self.assertEqual(self.v.validate_extraction(raw_result(gold), pages, max_actions=30), gold)

	def test_rejects_impossible_date(self):
		self.invalid(lambda v: v["document_date"].update(value="2026-02-30"))

	def test_other_identifier_without_authority_requires_explicit_incomplete_warning(self):
		self.raw["recipients"][0]["identifiers"][0]["authority"] = None
		with self.assertRaises(self.v.InvalidResult):
			self.v.validate_extraction(self.raw, self.pages, max_actions=30)
		identifier = self.raw["recipients"][0]["identifiers"].pop()
		self.raw["warnings"].append(
			{
				"code": "incomplete_text",
				"message": "Mandantenreferenz SYN-0007: authority not stated; identifier incomplete.",
				"evidence": identifier["evidence"],
			}
		)
		result = self.v.validate_extraction(self.raw, self.pages, max_actions=30)
		self.assertEqual(result["recipients"][0]["identifiers"], [])
		self.assertEqual(result["warnings"][-1]["evidence"][0]["verification"], "matched")

	def test_rejects_unknown_fields_enums_and_verification(self):
		for change in (
			lambda v: v.update(customer="ERP-ID"),
			lambda v: v.update(readability="perfect"),
			lambda v: v["sender"]["evidence"][0].update(verification="matched"),
		):
			self.raw = raw_result(self.gold)
			self.invalid(change)

	def test_rejects_relative_date_and_inconsistent_assessment(self):
		self.invalid(lambda v: v["actions"][0]["deadline"].update(kind="relative", date="2026-11-20"))
		self.raw = raw_result(self.gold)
		self.invalid(lambda v: v.update(action_assessment="none_found"))

	def test_wrong_page_is_not_guessed(self):
		for page in (0, 2, True):
			self.raw = raw_result(self.gold)
			self.invalid(lambda v: v["sender"]["evidence"][0].update(page=page))

	def test_exact_unicode_evidence_only(self):
		self.raw["sender"]["evidence"][0]["quote"] = "\uff21\u00a0\nB"
		self.pages[0]["text"] = "A B"
		result = self.v.validate_extraction(self.raw, self.pages, max_actions=30)
		self.assertEqual(result["sender"]["evidence"][0]["verification"], "matched")
		self.raw["sender"]["evidence"][0]["quote"] = "A C"
		result = self.v.validate_extraction(self.raw, self.pages, max_actions=30)
		self.assertEqual(result["sender"]["evidence"][0]["verification"], "unverified")
		self.raw["sender"]["evidence"][0]["quote"] = None
		result = self.v.validate_extraction(self.raw, self.pages, max_actions=30)
		self.assertEqual(result["sender"]["evidence"][0]["verification"], "missing")

	def test_quote_on_other_page_is_unverified(self):
		self.pages = [{"page": 1, "text": "other"}, {"page": 2, "text": self.pages[0]["text"]}]
		result = self.v.validate_extraction(self.raw, self.pages, max_actions=30)
		self.assertEqual(result["sender"]["evidence"][0]["verification"], "unverified")

	def test_limits_are_not_truncated(self):
		self.raw["actions"] *= 2
		with self.assertRaises(self.v.InvalidResult) as caught:
			self.v.validate_extraction(self.raw, self.pages, max_actions=1)
		self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")
		self.assertEqual(len(self.raw["actions"]), 2)
		self.raw = raw_result(self.gold)
		self.invalid(lambda v: v["actions"][0].update(title="x" * 141), "LIMIT_EXCEEDED")

	def test_strict_json_parser(self):
		for text in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', "[]", "{} {}", '{"x":"\\ud800"}'):
			with self.subTest(text=text), self.assertRaises(self.v.InvalidResult):
				self.v.parse_json(text)

	def test_ocr_indices_and_limits(self):
		result = self.v.validate_ocr(
			{"pages": [{"index": 1, "markdown": "second"}, {"index": 0, "markdown": "first"}]}, 2
		)
		self.assertEqual(result, [{"page": 1, "text": "first"}, {"page": 2, "text": "second"}])
		for pages in (
			[],
			[{"index": 1, "markdown": "x"}],
			[{"index": 0, "markdown": "x"}] * 2,
			[{"index": True, "markdown": "x"}],
			[{"index": 0, "markdown": None}],
		):
			with self.subTest(pages=pages), self.assertRaises(self.v.InvalidResult):
				self.v.validate_ocr({"pages": pages}, 1)
		with self.assertRaises(self.v.InvalidResult) as caught:
			self.v.validate_ocr({"pages": [{"index": 0, "markdown": "x" * 100001}]}, 1)
		self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")

	def test_provider_schema_is_self_contained_and_has_no_server_fields(self):
		schema = self.v.provider_schema()
		serialized = json.dumps(schema)
		for forbidden in ('"$ref"', '"verification"', '"if"', '"then"', '"else"'):
			self.assertNotIn(forbidden, serialized)
		self.assertFalse(schema["additionalProperties"])

	def test_no_remote_schema_resolution(self):
		with self.assertRaises(self.v.InvalidResult):
			self.v.validate_extraction({"$ref": "https://example.invalid/secret"}, self.pages, max_actions=30)

	def test_total_ocr_limit_and_boundary(self):
		pages = [{"page": i + 1, "text": "x" * 100000} for i in range(20)]
		self.assertEqual(len(self.v.validate_pages(pages, 20)), 20)
		pages.append({"page": 21, "text": "x"})
		with self.assertRaises(self.v.InvalidResult) as caught:
			self.v.validate_pages(pages, 21)
		self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")


if __name__ == "__main__":
	unittest.main()
