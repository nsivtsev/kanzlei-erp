import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


class ScenarioTests(unittest.TestCase):
    def module(self):
        self.assertTrue((SCRIPTS / "ci_scenarios.py").is_file(), "Scenario orchestrator implementation is missing")
        spec = importlib.util.spec_from_file_location("ci_scenarios", SCRIPTS / "ci_scenarios.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_minimal_profile_runs_only_smoke_and_records_failure(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, "argv", ["ci_scenarios.py", "local:test", "--profile", "minimal"]), \
                    patch.dict(os.environ, {"CI_METRICS_DIR": directory, "CI_SCENARIO_JOBS": "1"}), \
                    patch.object(module, "run_scenarios", return_value=7) as run:
                self.assertEqual(module.main(), 7)
            commands = run.call_args.args[0]
            self.assertEqual(list(commands), ["smoke"])
            self.assertEqual(commands["smoke"][-2:], ["local:test", "smoke"])

    def test_full_profile_retains_application_and_upgrade(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, "argv", ["ci_scenarios.py", "local:test", "--profile", "full"]), \
                    patch.dict(os.environ, {"CI_METRICS_DIR": directory, "CI_SCENARIO_JOBS": "1"}), \
                    patch.object(module, "run_scenarios", return_value=0) as run:
                self.assertEqual(module.main(), 0)
            self.assertEqual(list(run.call_args.args[0]), ["application", "upgrade"])

    def test_independent_scenarios_overlap_and_record_results(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child.py"
            child.write_text("import sys,time\nfrom pathlib import Path\np=Path(sys.argv[1]); name=sys.argv[2]\n"
                             "(p/name).touch()\nend=time.monotonic()+3\n"
                             "while not all((p/n).exists() for n in ('application','upgrade')):\n"
                             " if time.monotonic()>end: sys.exit(9)\n time.sleep(.01)\n")
            commands = {name: [sys.executable, str(child), str(root), name] for name in ("application", "upgrade")}
            self.assertEqual(module.run_scenarios(commands, root / "metrics", jobs=2), 0)
            for name in commands:
                data = json.loads((root / "metrics" / (name + ".json")).read_text())
                self.assertEqual(data["exit_code"], 0)
                self.assertGreater(data["seconds"], 0)

    def test_failure_cancels_other_scenario_and_waits_for_cleanup(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "cleanup.py"
            child.write_text("import signal,time,sys\nfrom pathlib import Path\n"
                             "def stop(*args):\n Path(sys.argv[1]).touch(); sys.exit(143)\n"
                             "signal.signal(signal.SIGTERM,stop)\nPath(sys.argv[2]).touch()\n"
                             "while True: time.sleep(.01)\n")
            fail = root / "fail.py"
            fail.write_text("import sys,time\nfrom pathlib import Path\n"
                            "while not Path(sys.argv[1]).exists(): time.sleep(.01)\nsys.exit(7)\n")
            commands = {"application": [sys.executable, str(fail), str(root / "started")],
                        "upgrade": [sys.executable, str(child), str(root / "cleaned"), str(root / "started")]}
            self.assertEqual(module.run_scenarios(commands, root / "metrics", jobs=2), 7)
            self.assertTrue((root / "cleaned").exists())

    def test_serial_mode_does_not_start_next_scenario_after_failure(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = {"application": [sys.executable, "-c", "raise SystemExit(8)"],
                        "upgrade": [sys.executable, "-c", "raise SystemExit(0)"]}
            self.assertEqual(module.run_scenarios(commands, root, jobs=1), 8)
            self.assertFalse((root / "upgrade.json").exists())

    def test_successive_cancellation_signals_do_not_interrupt_child_cleanup(self):
        self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child.py"
            child.write_text("import os,signal,sys,time\nfrom pathlib import Path\np=Path(sys.argv[1])\n"
                             "def stop(*args):\n (p/'stopping').touch(); time.sleep(.4); (p/'cleaned').touch(); sys.exit(143)\n"
                             "signal.signal(signal.SIGTERM,stop)\n(p/'pid').write_text(str(os.getpid()))\n"
                             "while True: time.sleep(.01)\n")
            driver = root / "driver.py"
            commands = {"application": [sys.executable, str(child), str(root)]}
            driver.write_text(f"import sys\nsys.path.insert(0,{str(SCRIPTS)!r})\n"
                              "from ci_scenarios import run_scenarios\n"
                              f"sys.exit(run_scenarios({commands!r},"
                              f"{str(root / 'metrics')!r},jobs=1))\n")
            process = subprocess.Popen([sys.executable, str(driver)], start_new_session=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not (root / "pid").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / "pid").exists(), "Child did not start")
                os.kill(process.pid, signal.SIGINT)
                while not (root / "stopping").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / "stopping").exists(), "Child cleanup did not start")
                os.kill(process.pid, signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 130)
                self.assertTrue((root / "cleaned").exists())
                result = json.loads((root / "metrics/application.json").read_text())
                self.assertEqual(result["exit_code"], 143)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                if (root / "pid").exists():
                    try:
                        os.kill(int((root / "pid").read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
