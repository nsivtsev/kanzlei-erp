"""AP-10 dataset freeze and pilot coverage; no ERP site or provider required."""

import copy
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "kanzlei_erp/tests/fixtures/posteingang"


class PilotDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("pilot_validator", ROOT / "scripts/validate_posteingang_fixtures.py")
        cls.validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.validator)

    def setUp(self):
        self.assertTrue(hasattr(self.validator, "validate_pilot"), "AP-10 pilot validator is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dataset = Path(self.temp.name) / "fixtures"
        shutil.copytree(FIXTURES, self.dataset)
        self.profile = json.loads((ROOT / "docs/plans/ai-posteingang/evaluation/ap10-v1.json").read_text())

    def manifest(self):
        return json.loads((self.dataset / "manifest.json").read_text())

    def write_manifest(self, value):
        (self.dataset / "manifest.json").write_text(json.dumps(value))

    def test_full_profile_counts_and_original_freeze(self):
        result = self.validator.validate_pilot(self.dataset, self.profile)
        self.assertEqual(result["cases"], 42)
        self.assertEqual(result["splits"], {"development": 12, "holdout": 30})
        self.assertGreaterEqual(result["explicit_holdout_actions"], 15)
        self.assertEqual(result["new_holdout_cases"], 26)

    def test_original_pdf_change_is_detected_even_if_hash_is_updated(self):
        manifest = self.manifest()
        item = manifest["cases"][0]
        import hashlib
        path = self.dataset / item["pdf"]
        path.write_bytes(path.read_bytes() + b"\n% altered")
        item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write_manifest(manifest)
        with self.assertRaisesRegex(self.validator.ContractError, "baseline"):
            self.validator.validate_pilot(self.dataset, self.profile)

    def test_original_case_metadata_is_frozen(self):
        manifest = self.manifest()
        manifest["cases"][0]["description"] = "changed original"
        self.write_manifest(manifest)
        with self.assertRaisesRegex(self.validator.ContractError, "baseline"):
            self.validator.validate_pilot(self.dataset, self.profile)

    def test_missing_new_case_does_not_pass_pilot(self):
        manifest = self.manifest()
        manifest["cases"].pop()
        self.write_manifest(manifest)
        with self.assertRaisesRegex(self.validator.ContractError, "42"):
            self.validator.validate_pilot(self.dataset, self.profile)

    def test_wrong_split_cannot_pass_pilot(self):
        manifest = self.manifest()
        manifest["cases"][-1]["split"] = "development"
        self.write_manifest(manifest)
        with self.assertRaisesRegex(self.validator.ContractError, "split"):
            self.validator.validate_pilot(self.dataset, self.profile)

    def test_missing_required_class_is_detected(self):
        profile = copy.deepcopy(self.profile)
        profile["required_tags"].append("missing-required-class")
        with self.assertRaisesRegex(self.validator.ContractError, "coverage"):
            self.validator.validate_pilot(self.dataset, profile)

    def test_minimum_explicit_actions_is_enforced(self):
        profile = copy.deepcopy(self.profile)
        profile["minimum_explicit_holdout_actions"] = 1000
        with self.assertRaisesRegex(self.validator.ContractError, "explicit"):
            self.validator.validate_pilot(self.dataset, profile)


if __name__ == "__main__":
    unittest.main()
