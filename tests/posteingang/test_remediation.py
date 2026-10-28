"""Observable synthetic development cases; never an independent quality holdout."""

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "kanzlei_erp/tests/fixtures/posteingang_remediation.json"


class RemediationCorpusTests(unittest.TestCase):
	def test_sources_and_gold_are_explicit_and_contract_valid(self):
		self.assertTrue(PACK.is_file(), "Observable remediation corpus missing")
		from jsonschema import Draft202012Validator

		from kanzlei_erp.posteingang_ai_validation import provider_schema, validate_extraction

		spec = importlib.util.spec_from_file_location("remediation_contracts", ROOT / "scripts/validate_posteingang_fixtures.py")
		contracts = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(contracts)
		pack = json.loads(PACK.read_text())
		self.assertTrue(pack["synthetic"])
		self.assertEqual(pack["purpose"], "development_regression")
		self.assertFalse(pack["independent_holdout"])
		self.assertEqual(len(pack["cases"]), 10)
		self.assertEqual(len({c["case_id"] for c in pack["cases"]}), 10)
		kinds = set()
		for case in pack["cases"]:
			with self.subTest(case=case["case_id"]):
				self.assertEqual(case["split"], "development")
				self.assertIn("SYNTHETISCH", case["pages"][0]["text"])
				self.assertEqual(contracts.audit_identifier_context(case["extraction"], case["pages"]), [])
				self.assertTrue(Draft202012Validator(provider_schema()).is_valid(case["extraction"]))
				result = validate_extraction(case["extraction"], case["pages"], max_actions=30)
				self.assertEqual(len(result["actions"]), case["expected"]["action_count"])
				self.assertEqual([a["deadline"]["kind"] for a in result["actions"]], case["expected"]["deadline_kinds"])
				kinds.update(a["deadline"]["kind"] for a in result["actions"])
		self.assertEqual(kinds, {"not_found", "explicit_date", "relative", "uncertain"})


if __name__ == "__main__":
	unittest.main()
