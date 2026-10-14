import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
REVISION = "1" * 40
IMAGE = "ghcr.io/nsivtsev/kanzlei-erp@sha256:" + "a" * 64


def run_fixture(**changes):
    return {"id": 123, "run_attempt": 1, "event": "push", "head_branch": "main",
            "head_sha": REVISION, "conclusion": "success", "status": "completed",
            "head_repository": {"full_name": "nsivtsev/kanzlei-erp"},
            "path": ".github/workflows/ci.yml", **changes}


def receipt_fixture(**changes):
    return {"schema_version": 1, "revision": REVISION, "image": IMAGE, "platform": "linux/amd64",
            "run_id": 123, "run_attempt": 1,
            "suites": {"deployment": "passed", "application": "passed", "upgrade": "passed"}, **changes}


class GitHubFixture:
    def __init__(self, runs=None, receipt=None):
        self.results = [run_fixture()] if runs is None else runs
        self.record = receipt_fixture() if receipt is None else receipt
        self.dispatched = []

    def runs(self, revision):
        return self.results

    def receipt(self, run, revision):
        return self.record

    def run(self, run_id):
        return self.results[0]

    def dispatch(self, tag):
        self.dispatched.append(tag)


class RegistryFixture:
    def __init__(self, images=None):
        self.images = {"sha-" + REVISION: IMAGE} if images is None else images
        self.inspected = []

    def existing(self, tag):
        return self.images.get(tag)

    def inspect(self, image, revision):
        self.inspected.append((image, revision))
        return {"compressed_bytes": 100}


class ReleaseCoordinationTests(unittest.TestCase):
    def module(self):
        self.assertTrue((SCRIPTS / "ci_release.py").is_file(), "Release coordination implementation is missing")
        spec = importlib.util.spec_from_file_location("ci_release", SCRIPTS / "ci_release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_ready_release_uses_exact_receipt_digest(self):
        registry = RegistryFixture()
        result = self.module().resolve_release(GitHubFixture(), registry, "v1.0.0", REVISION)
        self.assertTrue(result["ready"])
        self.assertEqual(result["image"], IMAGE)
        self.assertFalse(result["existing"])
        self.assertEqual(registry.inspected, [(IMAGE, REVISION)])

    def test_early_tag_waits_without_a_polling_loop(self):
        module = self.module()
        for runs in ([], [run_fixture(status="in_progress", conclusion=None)]):
            with self.subTest(runs=runs):
                result = module.resolve_release(GitHubFixture(runs), RegistryFixture(), "v1.0.0", REVISION)
                self.assertFalse(result["ready"])

    def test_failed_ci_does_not_queue_a_deploy(self):
        with self.assertRaisesRegex(ValueError, "CI"):
            self.module().resolve_release(GitHubFixture([run_fixture(conclusion="failure")]),
                                          RegistryFixture(), "v1.0.0", REVISION)

    def test_receipt_and_sha_tag_must_resolve_to_the_same_digest(self):
        with self.assertRaisesRegex(ValueError, "digest"):
            self.module().resolve_release(GitHubFixture(), RegistryFixture({"sha-" + REVISION: IMAGE[:-1] + "b"}),
                                          "v1.0.0", REVISION)

    def test_legacy_release_retry_does_not_require_a_new_receipt(self):
        registry = RegistryFixture({"v1.0.0": IMAGE})
        result = self.module().resolve_release(GitHubFixture([]), registry, "v1.0.0", REVISION)
        self.assertTrue(result["ready"])
        self.assertTrue(result["existing"])
        self.assertEqual(registry.inspected, [(IMAGE, REVISION)])

    def test_coordinator_dispatches_only_release_tags_after_trusted_main_success(self):
        github = GitHubFixture()
        event = {"workflow_run": run_fixture()}
        self.module().coordinate(event, github, ["noise", "v1.1.0", "v1.0.0"])
        self.assertEqual(github.dispatched, ["v1.0.0", "v1.1.0"])
        for run in (run_fixture(event="pull_request"), run_fixture(head_branch="other"),
                    run_fixture(conclusion="failure"),
                    run_fixture(head_repository={"full_name": "other/repository"})):
            github = GitHubFixture([run])
            with self.assertRaises(ValueError):
                self.module().coordinate({"workflow_run": run}, github, ["v1.0.0"])
            self.assertEqual(github.dispatched, [])

    def test_archive_is_read_as_data_and_cannot_supply_executable_paths(self):
        module = self.module()
        for name in ("verified-image.json", "../../verified-image.json"):
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr(name, json.dumps(receipt_fixture()))
            if name == "verified-image.json":
                self.assertEqual(module.read_receipt_archive(output.getvalue()), receipt_fixture())
            else:
                with self.assertRaises(ValueError):
                    module.read_receipt_archive(output.getvalue())

    def test_published_index_is_bound_to_the_locally_tested_platform_config(self):
        module = self.module()
        config_digest = "sha256:" + "b" * 64
        registry = RegistryFixture({"sha-" + REVISION: IMAGE})
        registry.inspect = lambda image, revision: {"body": b'{"manifests":[{}]}',
            "config_digest": config_digest, "compressed_bytes": 100}
        local = [{"Os": "linux", "Architecture": "amd64", "Id": config_digest, "Size": 123,
                  "Config": {"Labels": {"org.opencontainers.image.revision": REVISION}}}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("application", "upgrade"):
                (root / (name + ".json")).write_text(json.dumps({"name": name, "exit_code": 0, "seconds": 1}))
            env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
                   "GITHUB_REPOSITORY": "nsivtsev/kanzlei-erp", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                   "GITHUB_OUTPUT": "", "GITHUB_STEP_SUMMARY": ""}
            with patch.dict(os.environ, env), patch.object(module.subprocess, "check_output", return_value=json.dumps(local)):
                module.publish_receipt(registry, "local:test", REVISION, root)
            receipt = json.loads((root / "verified-image.json").read_text())
            self.assertEqual(receipt["image"], IMAGE)
            local[0]["Id"] = "sha256:" + "c" * 64
            with patch.dict(os.environ, env), patch.object(module.subprocess, "check_output", return_value=json.dumps(local)):
                with self.assertRaisesRegex(ValueError, "exact locally verified"):
                    module.publish_receipt(registry, "local:test", REVISION, root)


if __name__ == "__main__":
    unittest.main()
