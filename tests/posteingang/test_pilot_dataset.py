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


class MediumV2DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("medium_v2_validator", ROOT / "scripts/validate_posteingang_fixtures.py")
        cls.validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.validator)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dataset = Path(self.temp.name) / "fixtures"
        shutil.copytree(ROOT / "kanzlei_erp/tests/fixtures/posteingang_v2", self.dataset)
        self.profile = json.loads((ROOT / "docs/plans/ai-posteingang/evaluation/ap10-medium-v2.json").read_text())

    def test_v2_generator_uses_explicit_sources(self):
        import subprocess
        import sys
        output = Path(self.temp.name) / "rendered"
        result = subprocess.run([sys.executable, str(ROOT / "scripts/generate_posteingang_fixtures.py"), "--sources", str(self.dataset / "sources.json"), "--output", str(output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        import hashlib
        manifest = json.loads((self.dataset / "manifest.json").read_text())
        for case in manifest["cases"]:
            with self.subTest(case=case["case_id"]):
                self.assertEqual(hashlib.sha256((output / case["pdf"]).read_bytes()).hexdigest(), case["sha256"])

    def test_v2_counts_observable_context_and_new_holdout(self):
        result = self.validator.validate_pilot(self.dataset, self.profile)
        self.assertEqual(result["cases"], 42)
        self.assertEqual(result["new_holdout_cases"], 30)
        self.assertGreaterEqual(result["explicit_holdout_actions"], 15)

    def test_v2_rejects_unobservable_gold_context(self):
        case = json.loads((self.dataset / "manifest.json").read_text())["cases"][0]
        path = self.dataset / case["annotations"]
        gold = json.loads(path.read_text())
        gold["recipients"][0]["identifiers"][0]["authority"] = "Unstated synthetic issuer"
        path.write_text(json.dumps(gold))
        with self.assertRaisesRegex(self.validator.ContractError, "context absent"):
            self.validator.validate_pilot(self.dataset, self.profile)

    def test_v2_rejects_family_already_seen_in_development(self):
        path = self.dataset / "manifest.json"
        manifest = json.loads(path.read_text())
        source_path = self.dataset / "sources.json"
        sources = json.loads(source_path.read_text())
        family = json.loads((FIXTURES / "manifest.json").read_text())["cases"][0]["source_family"]
        case = next(c for c in manifest["cases"] if c["split"] == "holdout")
        case["source_family"] = family
        next(c for c in sources["cases"] if c["case_id"] == case["case_id"])["source_family"] = family
        path.write_text(json.dumps(manifest))
        source_path.write_text(json.dumps(sources))
        with self.assertRaisesRegex(self.validator.ContractError, "source family was already used"):
            self.validator.validate_pilot(self.dataset, self.profile)
