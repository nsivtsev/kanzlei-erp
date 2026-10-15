#!/usr/bin/env python3
"""Run real S3 ERP acceptance on the disposable s3-test.localhost site only."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("action", choices=("seed", "smoke", "verify_restored", "damage", "thumbnail"))
	action = parser.parse_args().action
	code = """import os, json
import frappe
os.chdir('/workspace/development/frappe-bench/sites')
frappe.init(site='s3-test.localhost', sites_path='.')
frappe.connect()
try:
    from kanzlei_erp.tests import s3_acceptance
    print(json.dumps(getattr(s3_acceptance, ACTION)()))
finally:
    frappe.destroy()
""".replace("ACTION", repr(action))
	result = subprocess.run(
		[
			"docker",
			"compose",
			"--env-file",
			str(ROOT / ".env"),
			"-f",
			str(ROOT / ".devcontainer/docker-compose.yml"),
			"exec",
			"-T",
			"frappe",
			"/workspace/development/frappe-bench/env/bin/python",
			"-c",
			code,
		],
		capture_output=True,
		text=True,
	)
	# Direct Python tracebacks do not include local variables; still redact all
	# generated credentials before reporting errors from third-party libraries.
	output = result.stdout + result.stderr
	for line in (ROOT / ".env").read_text().splitlines():
		key, sep, value = line.partition("=")
		if sep and key.startswith(("LOCAL_MINIO_", "LOCAL_S3_")) and value:
			output = output.replace(value, "[hidden]")
	print(output.strip())
	raise SystemExit(result.returncode)


if __name__ == "__main__":
	main()
