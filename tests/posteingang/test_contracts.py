"""Offline AP-01 contract checks; these do not exercise ERP permissions or workers."""

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "kanzlei_erp/tests/fixtures/posteingang"
SCRIPT = ROOT / "scripts/validate_posteingang_fixtures.py"


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCRIPT.is_file():
            return
        spec = importlib.util.spec_from_file_location("posteingang_contracts", SCRIPT)
        cls.validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.validator)

    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "AP-01 validator has not been implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dataset = Path(self.temp.name) / "fixtures"
        shutil.copytree(FIXTURES, self.dataset)

    def read(self, path):
        return json.loads((self.dataset / path).read_text())

    def write(self, path, value):
        (self.dataset / path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def invalid(self, fragment):
        with self.assertRaisesRegex(self.validator.ContractError, fragment):
            self.validator.validate_dataset(self.dataset)

    def mutate_extraction(self, change):
        path = self.read("manifest.json")["cases"][0]["extraction"]
        value = self.read(path)
        change(value)
        self.write(path, value)

    def mutate_review(self, change):
        path = self.read("manifest.json")["cases"][0]["review"]
        value = self.read(path)
        change(value)
        self.write(path, value)

    def test_entire_dataset_is_valid(self):
        result = self.validator.validate_dataset(self.dataset)
        manifest = self.read("manifest.json")
        self.assertEqual(result["cases"], len(manifest["cases"]))
        self.assertEqual(result["splits"]["development"], 12)
        self.assertGreaterEqual(result["splits"]["holdout"], 4)

    def test_dataset_revision_does_not_require_schema_revision(self):
        manifest = self.read("manifest.json")
        manifest["dataset_version"] = "1.1"
        self.write("manifest.json", manifest)
        self.validator.validate_dataset(self.dataset)

    def test_unknown_model_field_is_rejected(self):
        self.mutate_extraction(lambda v: v.update(customer="SYN-CUSTOMER-A"))
        self.invalid("Additional properties")

    def test_calendar_date_is_validated_not_just_its_shape(self):
        self.mutate_extraction(lambda v: v["document_date"].update(value="2026-02-30"))
        self.invalid("date")

    def test_zero_page_is_rejected(self):
        self.mutate_extraction(lambda v: v["sender"]["evidence"][0].update(page=0))
        self.invalid("minimum")

    def test_page_outside_pdf_is_rejected(self):
        self.mutate_extraction(lambda v: v["sender"]["evidence"][0].update(page=2))
        self.invalid("page outside PDF")

    def test_unknown_enum_is_rejected(self):
        self.mutate_extraction(lambda v: v.update(readability="perfect"))
        self.invalid("not one of")

    def test_action_limit_is_not_silently_truncated(self):
        self.mutate_extraction(lambda v: v.update(actions=v["actions"] * 31))
        self.invalid("too long")

    def test_title_limit(self):
        self.mutate_extraction(lambda v: v["actions"][0].update(title="a" * 141))
        self.invalid("too long")

    def test_sender_fits_declared_storage_field(self):
        self.mutate_extraction(lambda v: v["sender"].update(value="a" * 256))
        self.invalid("too long")

    def test_worker_publication_checks_analysis_switch(self):
        workflow = self.validator.read_json(self.validator.WORKFLOW)
        publication = next(t for t in workflow["transitions"] if t["operation"] == "stage_result")
        self.assertIn("analysis_enabled", publication["requires"])

    def test_schema_accepts_nullable_values_and_extraction_enums(self):
        value = self.read(self.read("manifest.json")["cases"][0]["extraction"])
        for readability in ("readable", "partial", "unreadable"):
            for assessment in ("actions_found", "none_found", "unknown"):
                if readability == "unreadable" and assessment != "unknown":
                    continue
                candidate = copy.deepcopy(value)
                candidate.update(readability=readability, action_assessment=assessment)
                if assessment != "actions_found":
                    candidate["actions"] = []
                self.validator.validate_document(candidate, "extraction.schema.json")
        for holder in ("addressee", "sender", "representative", "other", "unknown"):
            for kind in ("de_steuernummer", "de_tax_id", "de_vat_id", "other"):
                candidate = copy.deepcopy(value)
                candidate["recipients"][0]["identifiers"][0].update(type=kind, holder_role=holder)
                self.validator.validate_document(candidate, "extraction.schema.json")
        for verification, quote in (("matched", "Datum"), ("unverified", "nicht geprüft"), ("missing", None)):
            candidate = copy.deepcopy(value)
            candidate["sender"] = {"value": None, "evidence": [{"page": 1, "quote": quote, "verification": verification}]}
            self.validator.validate_document(candidate, "extraction.schema.json")

    def test_draft_can_keep_pending_decisions(self):
        value = self.read(self.read("manifest.json")["cases"][0]["review"])
        extraction = self.read(self.read("manifest.json")["cases"][0]["extraction"])
        value.update(mode="draft", selected_customer=None, human_verified=False)
        value["actions"][0].update(decision="pending", assignment_decision="pending", scheduling_decision="pending")
        self.validator.validate_document(value, "review.schema.json")
        self.validator.validate_review(value, extraction)

    def test_relative_deadline_cannot_contain_computed_date(self):
        self.mutate_extraction(lambda v: v["actions"][0]["deadline"].update(kind="relative"))
        self.invalid("not of type 'null'")

    def test_matched_quote_must_exist_on_its_page(self):
        self.mutate_extraction(lambda v: v["sender"]["evidence"][0].update(quote="fabricated quotation"))
        self.invalid("matched quote")

    def test_quote_normalization_accepts_unicode_and_whitespace(self):
        self.mutate_extraction(lambda v: v["sender"]["evidence"][0].update(quote="Fiktives\u00a0Finanzamt   Beispielstadt"))
        self.validator.validate_dataset(self.dataset)

    def test_duplicate_action_uuid(self):
        self.mutate_review(lambda v: v["actions"].append(copy.deepcopy(v["actions"][0])))
        self.invalid("duplicate action_id")

    def test_pending_action_cannot_be_confirmed(self):
        self.mutate_review(lambda v: v["actions"][0].update(decision="pending"))
        self.invalid("pending action")

    def test_missing_customer_cannot_be_confirmed(self):
        self.mutate_review(lambda v: v.update(selected_customer=None))
        self.invalid("selected_customer")

    def test_confirmation_requires_human_review(self):
        self.mutate_review(lambda v: v.update(human_verified=False))
        self.invalid("human_verified")

    def test_unassigned_action_requires_explicit_reason(self):
        self.mutate_review(lambda v: v["actions"][0].update(assigned_to=None, assignment_decision="unassigned", assignment_reason=None, needs_planning=True))
        self.invalid("assignment_reason")

    def test_unknown_work_date_requires_explicit_reason(self):
        self.mutate_review(lambda v: v["actions"][0].update(internal_date=None, scheduling_decision="unscheduled", scheduling_reason=None, needs_planning=True))
        self.invalid("scheduling_reason")

    def test_external_date_requires_basis(self):
        self.mutate_review(lambda v: v["actions"][0].update(external_date_basis=None))
        self.invalid("external_date_basis")

    def test_no_actions_requires_explicit_reason(self):
        self.mutate_review(lambda v: v.update(actions=[], no_actions_reason=None))
        self.invalid("no_actions_reason")

    def test_rejection_requires_reason(self):
        self.mutate_review(lambda v: (v.update(no_actions_reason="Kein Handlungsbedarf nach Prüfung."), v["actions"][0].update(decision="reject", rejection_reason=None)))
        self.invalid("rejection_reason")

    def test_unknown_receipt_date_is_not_upload_date(self):
        manifest = self.read("manifest.json")
        manifest["cases"][0]["received_date_known"] = False
        self.write("manifest.json", manifest)
        self.invalid("received_date")

    def test_hash_mismatch_is_rejected(self):
        manifest = self.read("manifest.json")
        manifest["cases"][0]["sha256"] = "0" * 64
        self.write("manifest.json", manifest)
        self.invalid("SHA-256")

    def test_cross_split_duplicate_pdf_is_rejected(self):
        manifest = self.read("manifest.json")
        a = manifest["cases"][0]
        b = next(c for c in manifest["cases"] if c["split"] != a["split"])
        b.update(pdf=a["pdf"], sha256=a["sha256"], page_count=a["page_count"])
        self.write("manifest.json", manifest)
        self.invalid("duplicate PDF")

    def test_fixture_path_cannot_escape_dataset(self):
        manifest = self.read("manifest.json")
        manifest["cases"][0]["pdf"] = "../outside.pdf"
        self.write("manifest.json", manifest)
        self.invalid("unsafe path")

    def test_hidden_customer_cannot_appear_in_candidates(self):
        manifest = self.read("manifest.json")
        manifest["cases"][0]["expected"]["candidate_customers"].append("SYN-CUSTOMER-HIDDEN")
        self.write("manifest.json", manifest)
        self.invalid("inaccessible customer")

    def test_accessible_but_unrelated_candidate_is_rejected(self):
        manifest = self.read("manifest.json")
        manifest["cases"][0]["expected"]["candidate_customers"] = ["SYN-CUSTOMER-C"]
        self.write("manifest.json", manifest)
        self.invalid("semantic gold candidates")

    def test_unreadable_scan_retains_independent_semantic_gold(self):
        manifest = self.read("manifest.json")
        case = next(c for c in manifest["cases"] if "poor_scan" in c["tags"])
        self.assertIn("annotations", case)
        gold = self.read(case["annotations"])
        self.assertEqual(len(gold["actions"]), 1)
        self.assertEqual(gold["actions"][0]["deadline"]["date"], "2026-11-20")
        self.assertEqual(self.read(case["extraction"])["actions"], [])

    def test_correction_does_not_require_a_premature_new_customer(self):
        workflow = self.validator.read_json(self.validator.WORKFLOW)
        correction = next(t for t in workflow["transitions"] if t["operation"] == "create_correction")
        self.assertIn("old_customer_read_write", correction["requires"])
        self.assertNotIn("old_new_customer_read_write", correction["requires"])

    def test_correction_scenario_blocks_old_access_and_preserves_completed_task(self):
        scenarios = self.read("scenarios.json")
        case = next(s for s in scenarios["operations"] if s["kind"] == "correction")
        self.assertIn("correction", case)
        self.assertEqual(case["correction"]["original_task_before"]["status"], "Completed")
        self.assertEqual(case["correction"]["original_task_before"], case["correction"]["original_task_after"])
        case["correction"]["normal_reader_can_read_old_file"] = True
        self.write("scenarios.json", scenarios)
        self.invalid("correction quarantine")

    def test_gold_candidate_must_have_matching_source_provenance(self):
        manifest = self.read("manifest.json")
        case = manifest["cases"][0]
        gold = self.read(case["annotations"])
        gold["customer_candidates"] = ["SYN-CUSTOMER-C"]
        gold["candidate_evidence"][0]["customer"] = "SYN-CUSTOMER-C"
        self.write(case["annotations"], gold)
        case["expected"]["candidate_customers"] = ["SYN-CUSTOMER-C"]
        self.write("manifest.json", manifest)
        self.invalid("candidate provenance")

    def test_typed_identifier_normalization_preserves_zeroes_and_context(self):
        base = {"type": "de_steuernummer", "value": " 00 / 012  ", "jurisdiction": "TEST", "authority": "TEST", "other_type": None}
        key = self.validator.identifier_key(base)
        self.assertEqual(key[1], "00012")
        self.assertNotEqual(key, self.validator.identifier_key({**base, "authority": "OTHER"}))
        self.assertIsNone(self.validator.identifier_key({**base, "jurisdiction": None}))
        self.assertEqual(self.validator.identifier_key({**base, "type": "de_tax_id", "value": " 00 123 "})[1], "00123")
        self.assertEqual(self.validator.identifier_key({**base, "type": "de_vat_id", "value": "de 00123"})[1], "DE00123")
        self.assertIsNone(self.validator.identifier_key({**base, "type": "de_vat_id", "value": "DE-00123"}))
        other = {**base, "type": "other", "value": " Ref-00 / A ", "other_type": "synthetic"}
        self.assertEqual(self.validator.identifier_key(other)[1], "Ref-00 / A")
        self.assertNotEqual(self.validator.identifier_key(other), self.validator.identifier_key({**other, "value": "ref-00 / A"}))

    def test_expected_task_dates_must_match_review(self):
        manifest = self.read("manifest.json")
        manifest["cases"][0]["expected"]["tasks"][0]["exp_end_date"] = "2026-12-31"
        self.write("manifest.json", manifest)
        self.invalid("expected tasks")

    def test_illegal_completed_transition_is_rejected(self):
        scenarios = self.read("scenarios.json")
        scenarios["transitions"].append({"id": "illegal-reopen", "from": "Completed", "to": "Needs Review", "operation": "manual_review", "allowed": True})
        self.write("scenarios.json", scenarios)
        self.invalid("transition expectation")

    def test_retry_scenario_cannot_expect_duplicate_tasks(self):
        scenarios = self.read("scenarios.json")
        case = next(s for s in scenarios["operations"] if s["kind"] == "completed_retry")
        case["expected"]["tasks_delta"] = 1
        self.write("scenarios.json", scenarios)
        self.invalid("scenario expectation")

    def test_variants_cannot_cross_evaluation_split(self):
        manifest = self.read("manifest.json")
        next(case for case in manifest["cases"] if case["case_id"] == "AP01-16")["split"] = "holdout"
        self.write("manifest.json", manifest)
        self.invalid("source family")

    def test_omitting_an_ai_action_is_not_rejection(self):
        manifest = self.read("manifest.json")
        path = manifest["cases"][1]["review"]
        review = self.read(path)
        review["actions"].pop()
        self.write(path, review)
        self.invalid("every source action")

    def test_partial_scan_has_no_hidden_text_layer(self):
        from pypdf import PdfReader
        manifest = self.read("manifest.json")
        case = next(c for c in manifest["cases"] if "poor_scan" in c["tags"])
        self.assertTrue(all(not p.extract_text().strip() for p in PdfReader(self.dataset / case["pdf"]).pages))

    def test_generator_reproduces_pdf_bytes_without_touching_gold(self):
        target = Path(self.temp.name) / "generated"
        subprocess.run([sys.executable, str(ROOT / "scripts/generate_posteingang_fixtures.py"), "--output", str(target)], check=True, capture_output=True)
        manifest = self.read("manifest.json")
        for case in manifest["cases"]:
            with self.subTest(case=case["case_id"]):
                self.assertEqual(hashlib.sha256((target / case["pdf"]).read_bytes()).hexdigest(), case["sha256"])
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["pdfs"])


if __name__ == "__main__":
    unittest.main()
