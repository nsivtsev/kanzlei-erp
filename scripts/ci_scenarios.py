#!/usr/bin/env python3
"""Run isolated acceptance scenarios on one runner and retain timings on failure."""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run_scenarios(commands, metrics, jobs=1):
    if jobs not in (1, 2):
        raise ValueError("CI_SCENARIO_JOBS must be 1 or 2")
    metrics = Path(metrics)
    metrics.mkdir(parents=True, exist_ok=True)
    pending = list(commands.items())
    running = {}
    failure = 0
    previous_handlers = {}

    def interrupted(signum, _frame):
        raise InterruptedError("CI interrupted by signal " + str(signum))

    def record(name, process, started):
        data = {"name": name, "exit_code": process.returncode, "seconds": round(time.monotonic() - started, 3)}
        (metrics / (name + ".json")).write_text(json.dumps(data) + "\n")
        print(f"Scenario {name}: exit={process.returncode}, seconds={data['seconds']}", flush=True)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        while pending or running:
            while pending and len(running) < jobs and not failure:
                name, command = pending.pop(0)
                started = time.monotonic()
                process = subprocess.Popen(command, start_new_session=True)
                running[name] = (process, started)
            for name, (process, started) in list(running.items()):
                if process.poll() is not None:
                    record(name, process, started)
                    del running[name]
                    if process.returncode and not failure:
                        failure = process.returncode if process.returncode > 0 else 128 - process.returncode
            if failure:
                break
            time.sleep(0.05)
    except InterruptedError:
        failure = 130
    finally:
        # GitHub cancellation escalates SIGINT to SIGTERM. Repeated signals must
        # not interrupt the EXIT traps or the wait for their cleanup to finish.
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        # Signal the entire group, including Compose clients; scenario EXIT traps
        # then remove their own containers and volumes before this parent exits.
        for process, _ in running.values():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 45
        for name, (process, started) in running.items():
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                failure = failure or 1
            record(name, process, started)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    args = parser.parse_args()
    script = Path(__file__).with_name("ci-image.sh")
    commands = {name: ["bash", str(script), args.image, name] for name in ("application", "upgrade")}
    directory = os.environ.get("CI_METRICS_DIR")
    if directory:
        return run_scenarios(commands, directory, int(os.environ.get("CI_SCENARIO_JOBS", "1")))
    with tempfile.TemporaryDirectory(prefix="kanzlei-ci-metrics-") as temporary:
        return run_scenarios(commands, temporary, int(os.environ.get("CI_SCENARIO_JOBS", "1")))


if __name__ == "__main__":
    sys.exit(main())
