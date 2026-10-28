import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))


class VerificationTests(unittest.TestCase):
    def module(self):
        self.assertTrue((SCRIPTS / "ci_verification.py").is_file(), "Verification receipt implementation is missing")
        spec = importlib.util.spec_from_file_location("ci_verification", SCRIPTS / "ci_verification.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def receipt(self):
        return {
            "schema_version": 1, "revision": "1" * 40,
            "image": "ghcr.io/nsivtsev/kanzlei-erp@sha256:" + "a" * 64,
            "platform": "linux/amd64", "run_id": 123, "run_attempt": 2,
            "suites": {"deployment": "passed", "application": "passed", "upgrade": "passed"},
        }

    def workflow_run(self):
        return {"id": 123, "run_attempt": 2, "event": "push", "head_branch": "main",
                "head_sha": "1" * 40, "conclusion": "success", "status": "completed",
                "head_repository": {"full_name": "nsivtsev/kanzlei-erp"},
                "path": ".github/workflows/ci.yml"}

    def test_app_image_installs_declared_dependencies(self):
        dockerfile = (SCRIPTS.parent / "deploy/Dockerfile").read_text()
        install = next(line for line in dockerfile.splitlines() if "bench pip install" in line and "apps/kanzlei_erp" in line)
        self.assertNotIn("--no-deps", install, "Runtime jsonschema must be installed from pyproject.toml")

    def test_smoke_does_not_run_full_acceptance_or_enable_test_mode(self):
        source = (SCRIPTS / "ci-image.sh").read_text()
        smoke = source.split('if [[ $scenario == smoke ]]; then', 1)[1].split('if [[ $scenario == upgrade ]]; then', 1)[0]
        for command in ("install-app erpnext", "install-app suite", "install-app kanzlei_erp", " migrate", "module.smoke_check(compose)", "compose.running()", "exit 0"):
            self.assertIn(command, smoke)
        for command in ("run-tests", "allow_tests true", "backup --", "restore_snapshot", "ci-site.py"):
            self.assertNotIn(command, smoke)

    def test_full_acceptance_is_manual_and_pr_runs_quick_checks(self):
        workflows = SCRIPTS.parent / ".github/workflows"
        ci = (workflows / "ci.yml").read_text()
        pr = ci.split("  verify-main:")[0]
        self.assertIn("uses: ./.github/workflows/checks.yml", pr)
        self.assertNotIn("uses: ./.github/workflows/verify.yml", pr)
        manual = (workflows / "acceptance.yml").read_text()
        self.assertIn("workflow_dispatch:", manual)
        self.assertIn("publish: false", manual)
        self.assertIn("profile: full", manual)
        self.assertNotIn("pull_request:", manual)
        self.assertNotIn("push:", manual)

    def test_patched_framework_copy_guard_is_compiled_and_verified(self):
        dockerfile = (SCRIPTS.parent / "deploy/Dockerfile").read_text()
        self.assertIn("bench build --app frappe --production", dockerfile)
        self.assertIn('assets["desk.bundle.js"]', dockerfile)
        self.assertIn('"document_copy_guard"', dockerfile)

    def test_ci_marks_only_its_disposable_site_for_acceptance(self):
        source = (SCRIPTS / "ci-image.sh").read_text()
        enable = source.index("bench --site ci.localhost set-config posteingang_acceptance_site true")
        tests = source.index("bench --site ci.localhost run-tests --app kanzlei_erp")
        disable = source.index("bench --site ci.localhost set-config posteingang_acceptance_site false")
        self.assertLess(enable, tests)
        self.assertLess(tests, disable)

    def test_receipt_requires_the_exact_successful_main_run(self):
        module = self.module()
        receipt = self.receipt()
        self.assertEqual(module.validate_receipt(receipt, self.workflow_run(), "1" * 40), receipt["image"])
        for key, value in (("event", "pull_request"), ("head_branch", "other"),
                           ("conclusion", "failure"), ("head_sha", "2" * 40), ("run_attempt", 3)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                module.validate_receipt(receipt, {**self.workflow_run(), key: value}, "1" * 40)

    def test_partial_or_mismatched_receipt_cannot_promote(self):
        module = self.module()
        for changes in ({"revision": "2" * 40}, {"run_id": 124}, {"platform": "linux/arm64"},
                        {"schema_version": 99}, {"image": "ghcr.io/nsivtsev/kanzlei-erp:latest"},
                        {"suites": {"application": "passed"}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                module.validate_receipt({**self.receipt(), **changes}, self.workflow_run(), "1" * 40)

    def test_full_metrics_cannot_be_mislabeled_as_minimal_receipt(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            metrics = Path(directory)
            for name in ("application", "upgrade"):
                (metrics / (name + ".json")).write_text(json.dumps({"name": name, "exit_code": 0, "seconds": 1}))
            with self.assertRaises(ValueError):
                module.make_receipt("1" * 40, self.receipt()["image"], 123, 2, metrics)

    def test_minimal_receipt_names_only_checks_that_ran(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            metrics = Path(directory)
            (metrics / "smoke.json").write_text(json.dumps({"name": "smoke", "exit_code": 0, "seconds": 1}))
            receipt = module.make_receipt("1" * 40, self.receipt()["image"], 123, 2, metrics)
            self.assertEqual(receipt["schema_version"], 2)
            self.assertEqual(receipt["suites"], {"deployment": "passed", "smoke": "passed"})
            self.assertEqual(module.validate_receipt(receipt, self.workflow_run(), "1" * 40), receipt["image"])
            for suites in ({"deployment": "passed"}, {"deployment": "passed", "smoke": "failed"}, self.receipt()["suites"]):
                with self.subTest(suites=suites), self.assertRaises(ValueError):
                    module.validate_receipt({**receipt, "suites": suites}, self.workflow_run(), "1" * 40)
            (metrics / "smoke.json").write_text(json.dumps({"name": "smoke", "exit_code": 1}))
            with self.assertRaises(ValueError):
                module.make_receipt("1" * 40, self.receipt()["image"], 123, 2, metrics)


if __name__ == "__main__":
    unittest.main()
