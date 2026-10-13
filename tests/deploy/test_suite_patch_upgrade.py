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
            (repo / "second.txt").write_text("v2\n")
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
            self.assertEqual((self.root / "apps" / app / "binding.txt").read_text(), "v2\n")
            self.assertEqual((self.root / "apps" / app / "unrelated.txt").read_text(), "keep this work\n")

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
