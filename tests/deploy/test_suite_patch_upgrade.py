"""Exercise the real patch installer using disposable Git checkouts."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestSuitePatchUpgrade(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ("apps", "sites", "scripts", "deploy", "bin"):
            (self.root / directory).mkdir()
        (self.root / "sites/apps.txt").write_text("frappe\n")
        shutil.copy(ROOT / "scripts/fetch-suite.sh", self.root / "scripts/fetch-suite.sh")
        for command in ("bench", "yarn"):
            path = self.root / "bin" / command
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.root / "bin") + os.pathsep + os.environ["PATH"]}
        for app in ("frappe", "suite"):
            repo = self.root / "apps" / app
            repo.mkdir()
            self.git(app, "init", "-q")
            (repo / "binding.txt").write_text("original\n")
            (repo / "second.txt").write_text("original\n")
            (repo / "unrelated.txt").write_text("original\n")
            self.git(app, "add", ".")
            self.git(app, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
            (repo / "binding.txt").write_text("v1\n")
            old = self.git(app, "diff")
            (self.root / "deploy" / (app + "-mandant-access-v1.patch")).write_text(old)
            (repo / "binding.txt").write_text("v2\n")
            if app == "suite":
                previous = self.git(app, "diff")
                (self.root / "deploy/suite-mandant-access-v2.patch").write_text(previous)
                (repo / "binding.txt").write_text("v3\n")
                previous = self.git(app, "diff")
                (self.root / "deploy/suite-mandant-access-v3.patch").write_text(previous)
            (repo / "binding.txt").write_text("v4\n")
            if app == "suite":
                (self.root / "deploy/suite-mandant-access-v4.patch").write_text(self.git(app, "diff"))
            if app == "suite":
                (repo / "binding.txt").write_text("v5\n")
                (self.root / "deploy/suite-mandant-access-v5.patch").write_text(self.git(app, "diff"))
            (repo / "binding.txt").write_text("current\n")
            (repo / "second.txt").write_text("current\n")
            new = self.git(app, "diff")
            (self.root / "deploy" / (app + "-mandant-access.patch")).write_text(new)
            self.git(app, "checkout", "--", ".")
            self.git(app, "apply", str(self.root / "deploy" / (app + "-mandant-access-v1.patch")))
            (repo / "unrelated.txt").write_text("keep this work\n")
            (repo / "untracked.txt").write_text("keep this new file\n")
        commit = self.git("suite", "rev-parse", "HEAD").strip()
        (self.root / "deploy/suite.env").write_text("SUITE_COMMIT=" + commit + "\nSUITE_REPOSITORY=unused\n")

    def git(self, app, *args):
        return subprocess.check_output(["git", "-C", str(self.root / "apps" / app), *args], text=True)

    def install(self):
        return subprocess.run(["bash", str(self.root / "scripts/fetch-suite.sh")], cwd=self.root,
                              env=self.env, capture_output=True, text=True)

    def test_upgrade_and_repeat_preserve_unrelated_work(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.install().returncode, 0)
        for app in ("frappe", "suite"):
            self.assertEqual((self.root / "apps" / app / "binding.txt").read_text(), "current\n")
            self.assertEqual((self.root / "apps" / app / "unrelated.txt").read_text(), "keep this work\n")

    def test_framework_v7_upgrade_preserves_unrelated_work(self):
        self.git("frappe", "apply", "--reverse", str(self.root / "deploy/frappe-mandant-access-v1.patch"))
        (self.root / "apps/frappe/binding.txt").write_text("v7\n")
        previous = self.git("frappe", "diff", "--", "binding.txt")
        (self.root / "deploy/frappe-mandant-access-v7.patch").write_text(previous)
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "apps/frappe/binding.txt").read_text(), "current\n")
        self.assertEqual((self.root / "apps/frappe/unrelated.txt").read_text(), "keep this work\n")

    def test_framework_v8_upgrade_and_child_query_context(self):
        self.git("frappe", "apply", "--reverse", str(self.root / "deploy/frappe-mandant-access-v1.patch"))
        (self.root / "apps/frappe/binding.txt").write_text("v8\n")
        (self.root / "deploy/frappe-mandant-access-v8.patch").write_text(self.git("frappe", "diff", "--", "binding.txt"))
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "apps/frappe/unrelated.txt").read_text(), "keep this work\n")
        self.assertIn("!deploy/frappe-mandant-access-v8.patch", (ROOT / ".dockerignore").read_text().splitlines())
        self.assertIn("query_doctype=self.doctype", (ROOT / "deploy/frappe-mandant-access.patch").read_text())

    def test_framework_v9_upgrade_repeat_and_exact_rollback(self):
        self.git("frappe", "apply", "--reverse", str(self.root / "deploy/frappe-mandant-access-v1.patch"))
        (self.root / "apps/frappe/binding.txt").write_text("v9\n")
        (self.root / "deploy/frappe-mandant-access-v9.patch").write_text(self.git("frappe", "diff", "--", "binding.txt"))
        (self.root / "apps/suite/second.txt").write_text("user edit\n")
        before = {app: self.git(app, "diff") for app in ("frappe", "suite")}
        self.assertNotEqual(self.install().returncode, 0)
        for app in ("frappe", "suite"):
            self.assertEqual(self.git(app, "diff"), before[app])
        (self.root / "apps/suite/second.txt").write_text("original\n")
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("!deploy/frappe-mandant-access-v9.patch", (ROOT / ".dockerignore").read_text().splitlines())
        self.assertEqual(hashlib.sha256((ROOT / "deploy/frappe-mandant-access-v9.patch").read_bytes()).hexdigest(), "bf11cc6d84b66ceba00c9126745bfe276e1634548c23fb81785e59a86d3f9570")

    def test_v7_predecessors_are_exact_and_included_in_image(self):
        for app in ("frappe", "suite"):
            filename = f"deploy/{app}-mandant-access-v7.patch"
            self.assertIn("!" + filename, (ROOT / ".dockerignore").read_text().splitlines())
            expected = {
                "frappe": "8d5be96b61ebbf9bb1c4e84d18dd93773d2c735d177f4831c2d2661d6a97d177",
                "suite": "741a76bcb8f9992a290e0d8331375521fc5bb38b316a8a436d98a0c0693ed839",
            }
            self.assertEqual(hashlib.sha256((ROOT / filename).read_bytes()).hexdigest(), expected[app])

    def use_suite_v2(self):
        self.git("suite", "apply", "--reverse", str(self.root / "deploy/suite-mandant-access-v1.patch"))
        self.git("suite", "apply", str(self.root / "deploy/suite-mandant-access-v2.patch"))

    def use_suite_v3(self):
        self.git("suite", "apply", "--reverse", str(self.root / "deploy/suite-mandant-access-v1.patch"))
        self.git("suite", "apply", str(self.root / "deploy/suite-mandant-access-v3.patch"))

    def test_v3_upgrade_and_repeat_preserve_unrelated_work(self):
        self.use_suite_v3()
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        for app in ("frappe", "suite"):
            self.assertEqual((self.root / "apps" / app / "binding.txt").read_text(), "current\n")
            self.assertEqual((self.root / "apps" / app / "unrelated.txt").read_text(), "keep this work\n")
            self.assertEqual((self.root / "apps" / app / "untracked.txt").read_text(), "keep this new file\n")

    def test_v3_conflict_restores_exact_predecessors_for_both_apps(self):
        self.use_suite_v3()
        (self.root / "apps/suite/second.txt").write_text("user edit\n")
        before = {app: self.git(app, "diff") for app in ("frappe", "suite")}
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Unknown suite patch state", result.stderr)
        for app in ("frappe", "suite"):
            self.assertEqual(self.git(app, "diff"), before[app])
            self.assertEqual((self.root / "apps" / app / "unrelated.txt").read_text(), "keep this work\n")
            self.assertEqual((self.root / "apps" / app / "untracked.txt").read_text(), "keep this new file\n")

    def test_v3_conflict_does_not_downgrade_already_current_frappe(self):
        self.git("frappe", "apply", "--reverse", str(self.root / "deploy/frappe-mandant-access-v1.patch"))
        self.git("frappe", "apply", str(self.root / "deploy/frappe-mandant-access.patch"))
        self.use_suite_v3()
        (self.root / "apps/suite/second.txt").write_text("user edit\n")
        before = {app: self.git(app, "diff") for app in ("frappe", "suite")}
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Unknown suite patch state", result.stderr)
        for app in ("frappe", "suite"):
            self.assertEqual(self.git(app, "diff"), before[app])

    def test_modified_v3_patch_preserves_both_checkout_diffs(self):
        self.use_suite_v3()
        (self.root / "apps/suite/binding.txt").write_text("user edit\n")
        before = {app: self.git(app, "diff") for app in ("frappe", "suite")}
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown suite patch state", result.stderr)
        for app in ("frappe", "suite"):
            self.assertEqual(self.git(app, "diff"), before[app])

    def test_v3_predecessor_is_included_in_image_context(self):
        self.assertIn("!deploy/suite-mandant-access-v3.patch", (ROOT / ".dockerignore").read_text().splitlines())
        self.assertIn("deploy/*-mandant-access*.patch", (ROOT / "deploy/Dockerfile").read_text())

    def test_v3_snapshot_is_exact_committed_predecessor(self):
        # Exact Suite patch from ad1f339, before BL-046. Pin bytes rather than
        # HEAD so later feature commits and shallow CI checkouts remain valid.
        predecessor = (ROOT / "deploy/suite-mandant-access-v3.patch").read_bytes()
        self.assertEqual(hashlib.sha256(predecessor).hexdigest(),
                         "5eda2093e0e3e5579a7e7b7257c1ec8b0ea80f61f4cbe1ef9a7aafeea9a4a065")

    def use_suite_v4(self):
        self.git("suite", "apply", "--reverse", str(self.root / "deploy/suite-mandant-access-v1.patch"))
        self.git("suite", "apply", str(self.root / "deploy/suite-mandant-access-v4.patch"))

    def test_v4_upgrade_repeat_and_atomic_conflict(self):
        self.use_suite_v4()
        (self.root / "apps/suite/second.txt").write_text("user edit\n")
        before = {app: self.git(app, "diff") for app in ("frappe", "suite")}
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Unknown suite patch state", result.stderr)
        for app in ("frappe", "suite"):
            self.assertEqual(self.git(app, "diff"), before[app])
        (self.root / "apps/suite/second.txt").write_text("original\n")
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "apps/suite/binding.txt").read_text(), "current\n")
        self.assertEqual((self.root / "apps/suite/unrelated.txt").read_text(), "keep this work\n")
        self.assertEqual((self.root / "apps/suite/untracked.txt").read_text(), "keep this new file\n")

    def test_v4_predecessor_is_exact_and_included_in_image(self):
        self.assertIn("!deploy/suite-mandant-access-v4.patch", (ROOT / ".dockerignore").read_text().splitlines())
        self.assertEqual(hashlib.sha256((ROOT / "deploy/suite-mandant-access-v4.patch").read_bytes()).hexdigest(),
                         "6a4d15b8fc09294a2a2b4c08d664358452dd9b488e524ae3b9a1d106ea860ccc")

    def test_v2_upgrade_and_repeat_preserve_unrelated_work(self):
        self.use_suite_v2()
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        for app in ("frappe", "suite"):
            self.assertEqual((self.root / "apps" / app / "binding.txt").read_text(), "current\n")
            self.assertEqual((self.root / "apps" / app / "unrelated.txt").read_text(), "keep this work\n")

    def test_v2_conflict_restores_exact_predecessors_for_both_apps(self):
        self.use_suite_v2()
        (self.root / "apps/suite/second.txt").write_text("user edit\n")
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual((self.root / "apps/frappe/binding.txt").read_text(), "v1\n")
        self.assertEqual((self.root / "apps/suite/binding.txt").read_text(), "v2\n")
        self.assertEqual((self.root / "apps/suite/second.txt").read_text(), "user edit\n")
        for app in ("frappe", "suite"):
            self.assertEqual((self.root / "apps" / app / "unrelated.txt").read_text(), "keep this work\n")

    def test_clean_install_and_repeat(self):
        for app in ("frappe", "suite"):
            self.git(app, "apply", "--reverse", str(self.root / "deploy" / (app + "-mandant-access-v1.patch")))
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        for app in ("frappe", "suite"):
            self.assertEqual((self.root / "apps" / app / "binding.txt").read_text(), "current\n")
            self.assertEqual((self.root / "apps" / app / "unrelated.txt").read_text(), "keep this work\n")

    def test_v2_predecessor_is_included_in_image_context(self):
        self.assertIn("!deploy/suite-mandant-access-v2.patch", (ROOT / ".dockerignore").read_text().splitlines())

    def test_conflict_in_second_app_restores_both_previous_patches(self):
        (self.root / "apps/suite/second.txt").write_text("user edit\n")
        self.assertNotEqual(self.install().returncode, 0)
        for app in ("frappe", "suite"):
            self.assertEqual((self.root / "apps" / app / "binding.txt").read_text(), "v1\n")
        self.assertEqual((self.root / "apps/suite/second.txt").read_text(), "user edit\n")

    def test_unknown_first_patch_stops_without_changing_either_checkout(self):
        (self.root / "apps/frappe/binding.txt").write_text("user edit\n")
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual((self.root / "apps/frappe/binding.txt").read_text(), "user edit\n")
        self.assertEqual((self.root / "apps/suite/binding.txt").read_text(), "v1\n")

    def test_unknown_second_patch_preserves_both_checkout_diffs(self):
        (self.root / "apps/suite/binding.txt").write_text("user edit\n")
        before = {app: self.git(app, "diff") for app in ("frappe", "suite")}
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown suite patch state", result.stderr)
        for app in ("frappe", "suite"):
            self.assertEqual(self.git(app, "diff"), before[app])
            self.assertEqual((self.root / "apps" / app / "unrelated.txt").read_text(), "keep this work\n")

    def test_v2_conflict_does_not_downgrade_already_current_frappe(self):
        self.git("frappe", "apply", "--reverse", str(self.root / "deploy/frappe-mandant-access-v1.patch"))
        self.git("frappe", "apply", str(self.root / "deploy/frappe-mandant-access.patch"))
        self.use_suite_v2()
        (self.root / "apps/suite/second.txt").write_text("user edit\n")
        before = {app: self.git(app, "diff") for app in ("frappe", "suite")}
        self.assertNotEqual(self.install().returncode, 0)
        for app in ("frappe", "suite"):
            self.assertEqual(self.git(app, "diff"), before[app])

    def test_v5_upgrade_repeat_and_atomic_conflict(self):
        self.git("suite", "apply", "--reverse", str(self.root / "deploy/suite-mandant-access-v1.patch"))
        self.git("suite", "apply", str(self.root / "deploy/suite-mandant-access-v5.patch"))
        (self.root / "apps/suite/second.txt").write_text("user edit\n")
        before = {app: self.git(app, "diff") for app in ("frappe", "suite")}
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Unknown suite patch state", result.stderr)
        for app in ("frappe", "suite"):
            self.assertEqual(self.git(app, "diff"), before[app])
        (self.root / "apps/suite/second.txt").write_text("original\n")
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "apps/suite/binding.txt").read_text(), "current\n")
        self.assertEqual((self.root / "apps/suite/unrelated.txt").read_text(), "keep this work\n")
        self.assertEqual((self.root / "apps/suite/untracked.txt").read_text(), "keep this new file\n")

    def test_v5_predecessor_is_exact_and_included_in_image(self):
        self.assertIn("!deploy/suite-mandant-access-v5.patch", (ROOT / ".dockerignore").read_text().splitlines())
        self.assertEqual(hashlib.sha256((ROOT / "deploy/suite-mandant-access-v5.patch").read_bytes()).hexdigest(),
                         "4a26b79378e0819f217a6991273316811ed55a277b63feabe7050f9b341c3b21")
