"""AP-07 review contract and semantics without Frappe or a provider."""

import copy
import importlib
import json
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "kanzlei_erp/tests/fixtures/posteingang"


class ReviewValidationTests(unittest.TestCase):
	def setUp(self):
		self.assertTrue((ROOT / "kanzlei_erp/posteingang_review_validation.py").exists(), "AP-07 review validator missing")
		self.v = importlib.import_module("kanzlei_erp.posteingang_review_validation")
		self.review = json.loads((FIXTURES / "reviews/AP01-01.json").read_text())
		self.fields = {"sender": None, "document_date": None, "letter_type": None, "summary": None}
		self.pages = json.loads((FIXTURES / "ocr/AP01-01.json").read_text())["pages"]
		self.stored = [{"action_id": row["action_id"], "source_index": row["source_index"]} for row in self.review["actions"]]

	def validate(self, *, confirm=True, review=None, fields=None, stored=None, pages="default", page_count=1):
		return self.v.validate_review(
			self.review if review is None else review, self.fields if fields is None else fields,
			stored_actions=self.stored if stored is None else stored,
			pages=self.pages if pages == "default" else pages, page_count=page_count, confirm=confirm,
		)

	def invalid(self, change, *, confirm=True, code=None):
		change(self.review)
		with self.assertRaises(self.v.InvalidResult) as caught:
			self.validate(confirm=confirm)
		if code:
			self.assertEqual(caught.exception.code, code)
		self.assertEqual(str(caught.exception), caught.exception.code)

	def test_all_sixteen_gold_reviews_with_server_uuid_mapping(self):
		for case in json.loads((FIXTURES / "manifest.json").read_text())["cases"]:
			with self.subTest(case=case["case_id"]):
				review = json.loads((FIXTURES / case["review"]).read_text())
				for row in review["actions"]:
					row["action_id"] = str(uuid4())
				stored = [{"action_id": row["action_id"], "source_index": row["source_index"]} for row in review["actions"]]
				pages = json.loads((FIXTURES / case["ocr"]).read_text())["pages"]
				result, fields = self.validate(review=review, stored=stored, pages=pages, page_count=case["page_count"], confirm=review["mode"] == "confirm")
				self.assertEqual(result, review)
				self.assertEqual(fields, self.fields)

	def test_runtime_schema_is_exact_ap01_copy(self):
		from kanzlei_erp.posteingang_ai_validation import schema

		self.assertEqual(schema("review"), json.loads((ROOT / "docs/plans/ai-posteingang/schemas/review.schema.json").read_text()))

	def test_cannot_drop_add_or_forge_actions(self):
		for mutate in (lambda r: r.update(actions=[]), lambda r: r["actions"][0].update(action_id=str(uuid4())), lambda r: r["actions"][0].update(source_index=1), lambda r: r["actions"].append(copy.deepcopy(r["actions"][0]))):
			original = copy.deepcopy(self.review)
			self.invalid(mutate)
			self.review = original

	def test_source_index_is_not_a_boolean(self):
		self.invalid(lambda r: r["actions"][0].update(source_index=True))

	def test_unknown_fields_and_enum_are_rejected(self):
		for mutate in (lambda r: r.update(created_task="TASK-1"), lambda r: r["actions"][0].update(created_task="TASK-1"), lambda r: r["actions"][0].update(decision="autoaccept")):
			original = copy.deepcopy(self.review)
			self.invalid(mutate)
			self.review = original

	def test_bad_dates_and_limits_are_rejected(self):
		for mutate in (lambda r: r["actions"][0].update(external_date="2026-02-30"), lambda r: r["actions"][0].update(internal_date=""), lambda r: r["actions"][0].update(title="x" * 141)):
			original = copy.deepcopy(self.review)
			self.invalid(mutate)
			self.review = original

	def test_pending_human_verification_and_customer_block_confirm(self):
		for mutate in (lambda r: r.update(human_verified=False), lambda r: r.update(selected_customer=None), lambda r: r.update(customer_reason=None), lambda r: r["actions"][0].update(decision="pending")):
			original = copy.deepcopy(self.review)
			self.invalid(mutate)
			self.review = original

	def test_reject_needs_reason_even_in_draft(self):
		self.review["mode"] = "draft"
		self.invalid(lambda r: r["actions"][0].update(decision="reject"), confirm=False)

	def test_zero_actions_requires_explicit_reason(self):
		self.review["actions"] = []
		self.stored = []
		self.invalid(lambda r: None)
		self.review["no_actions_reason"] = "Reviewed manually; no work is necessary."
		self.assertEqual(self.validate()[0], self.review)

	def test_accepted_actions_cannot_also_be_no_actions(self):
		self.invalid(lambda r: r.update(no_actions_reason="There is work"))

	def test_assignment_and_scheduling_require_explicit_decisions(self):
		for mutate in (lambda a: a.update(assigned_to=None), lambda a: a.update(assignment_decision="unassigned"), lambda a: a.update(internal_date=None), lambda a: a.update(scheduling_decision="unscheduled"), lambda a: a.update(external_date_basis=None), lambda a: a.update(needs_planning=True)):
			original = copy.deepcopy(self.review)
			self.invalid(lambda r: mutate(r["actions"][0]))
			self.review = original

	def test_explicit_unassigned_and_unscheduled_are_valid(self):
		self.review["actions"][0].update(assigned_to=None, assignment_decision="unassigned", assignment_reason="Plan the responsible person later.", internal_date=None, scheduling_decision="unscheduled", scheduling_reason="Plan the date later.", needs_planning=True)
		self.assertEqual(self.validate()[0], self.review)

	def test_draft_allows_incomplete_decisions(self):
		self.review.update(mode="draft", selected_customer=None, customer_reason=None, human_verified=False)
		self.review["actions"][0].update(decision="pending", assigned_to=None, assignment_decision="pending", internal_date=None, scheduling_decision="pending", external_date=None, external_date_basis=None)
		self.assertEqual(self.validate(confirm=False)[0], self.review)

	def test_clarification_requires_reason(self):
		self.review["mode"] = "clarification"
		self.invalid(lambda r: r.update(clarification_reason=None), confirm=False)
		self.review["clarification_reason"] = "Read the scan again."
		self.assertEqual(self.validate(confirm=False)[0], self.review)

	def test_mode_must_match_operation(self):
		self.invalid(lambda r: r.update(mode="draft"))
		self.review["mode"] = "confirm"
		with self.assertRaises(self.v.InvalidResult):
			self.validate(confirm=False)

	def test_evidence_is_recomputed_and_not_mutated_in_place(self):
		self.pages[0]["text"] = "A B"
		proof = self.review["actions"][0]["evidence"][0]
		proof.update(quote="\uff21\u00a0B", verification="unverified")
		self.assertEqual(self.validate()[0]["actions"][0]["evidence"][0]["verification"], "matched")
		self.assertEqual(proof["verification"], "unverified")
		proof.update(quote="A C", verification="matched")
		self.assertEqual(self.validate()[0]["actions"][0]["evidence"][0]["verification"], "unverified")
		proof.update(quote=None, verification="matched")
		self.assertEqual(self.validate()[0]["actions"][0]["evidence"][0]["verification"], "missing")

	def test_same_page_and_page_bounds(self):
		self.pages = [{"page": 1, "text": "other"}, {"page": 2, "text": self.pages[0]["text"]}]
		self.assertEqual(self.validate(page_count=2)[0]["actions"][0]["evidence"][0]["verification"], "unverified")
		for page in (0, 2, True):
			self.review["actions"][0]["evidence"][0]["page"] = page
			with self.assertRaises(self.v.InvalidResult):
				self.validate(pages=None, page_count=1)

	def test_manual_evidence_cannot_claim_matched_without_ocr(self):
		self.assertEqual(self.validate(pages=None)[0]["actions"][0]["evidence"][0]["verification"], "unverified")
		self.review["actions"][0]["source_index"] = None
		self.stored[0]["source_index"] = None
		self.assertEqual(self.validate(pages=[])[0]["actions"][0]["source_index"], None)

	def test_fields_are_full_strict_and_calendar_checked(self):
		for fields in ({}, {**self.fields, "actor": "Administrator"}, {**self.fields, "document_date": "2026-02-30"}, {**self.fields, "sender": ""}, {**self.fields, "summary": "x" * 4001}):
			with self.subTest(fields=tuple(fields)), self.assertRaises(self.v.InvalidResult):
				self.validate(fields=fields)

	def test_whitespace_and_surrogates_are_not_valid_text(self):
		for value in ("   ", "\ud800"):
			original = copy.deepcopy(self.review)
			self.invalid(lambda r: r["actions"][0].update(title=value))
			self.review = original

	def test_duplicate_json_keys_and_nonfinite_constants_are_rejected(self):
		for value in ('{"mode":"draft","mode":"confirm"}', '{"x":NaN}', '{"x":Infinity}', "[]"):
			with self.assertRaises(self.v.InvalidResult):
				self.validate(review=value)
		self.assertEqual(self.validate(review=json.dumps(self.review), fields=json.dumps(self.fields))[0], self.review)


if __name__ == "__main__":
	unittest.main()
