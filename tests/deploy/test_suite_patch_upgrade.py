"""Exercise the real patch installer using disposable Git checkouts."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


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
            self.git(app, "add", ".")
            self.git(app, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
            (repo / "binding.txt").write_text("v1\n")
            old = self.git(app, "diff")
            (self.root / "deploy" / (app + "-mandant-access-v1.patch")).write_text(old)
            (repo / "binding.txt").write_text("v2\n")
            if app == "suite":
                previous = self.git(app, "diff")
                (self.root / "deploy/suite-mandant-access-v2.patch").write_text(previous)
            (repo / "binding.txt").write_text("current\n")
            (repo / "second.txt").write_text("current\n")
            new = self.git(app, "diff")
            (self.root / "deploy" / (app + "-mandant-access.patch")).write_text(new)
            self.git(app, "checkout", "--", ".")
            self.git(app, "apply", str(self.root / "deploy" / (app + "-mandant-access-v1.patch")))
            (repo / "unrelated.txt").write_text("keep this work\n")
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

    def use_suite_v2(self):
        self.git("suite", "apply", "--reverse", str(self.root / "deploy/suite-mandant-access-v1.patch"))
        self.git("suite", "apply", str(self.root / "deploy/suite-mandant-access-v2.patch"))

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
