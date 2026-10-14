import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PreflightTests(unittest.TestCase):
    def module(self):
        source = ROOT / "scripts/deploy_preflight.py"
        self.assertTrue(source.exists(), "Deployment preflight implementation is missing")
        spec = importlib.util.spec_from_file_location("deploy_preflight", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_matching_current_release_requires_health_and_preserves_state(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared").mkdir()
            state = {"tag": "v1.0.0", "revision": "1" * 40,
                     "image": "ghcr.io/nsivtsev/kanzlei-erp@sha256:" + "a" * 64,
                     "site": "ci.localhost", "release": str(root / "releases/old")}
            current = root / "shared/current.json"
            current.write_text(json.dumps(state))
            observed = []
            self.assertTrue(module.check_current(root, state["image"], state["revision"], state["tag"],
                health=lambda saved: observed.append(saved)))
            self.assertEqual(observed, [state])
            self.assertEqual(json.loads(current.read_text()), state)
            (root / "shared/pending.json").write_text("{}")
            self.assertFalse(module.check_current(root, state["image"], state["revision"], state["tag"],
                health=lambda saved: self.fail("Pending deployments must not be bypassed")))

    def test_different_release_or_unhealthy_current_is_not_silently_skipped(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared").mkdir()
            state = {"tag": "v1.0.0", "revision": "1" * 40,
                     "image": "ghcr.io/nsivtsev/kanzlei-erp@sha256:" + "a" * 64}
            (root / "shared/current.json").write_text(json.dumps(state))
            self.assertFalse(module.check_current(root, state["image"], "2" * 40, "v1.1.0",
                                                  health=lambda saved: self.fail("Different release")))
            def unhealthy(saved):
                raise RuntimeError("Service is not running")
            with self.assertRaisesRegex(RuntimeError, "not running"):
                module.check_current(root, state["image"], state["revision"], state["tag"], health=unhealthy)


if __name__ == "__main__":
    unittest.main()
