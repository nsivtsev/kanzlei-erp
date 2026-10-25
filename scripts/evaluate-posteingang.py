#!/usr/bin/env python3
"""Evaluate frozen synthetic results offline; never contacts a provider or ERP."""

import argparse
import json
import sys
from pathlib import Path

from posteingang_evaluation import EvaluationError, evaluate, markdown, read


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--manifest", type=Path, required=True)
	parser.add_argument("--observations", type=Path, required=True)
	parser.add_argument("--judgments", type=Path, required=True)
	parser.add_argument("--timings", type=Path)
	parser.add_argument("--billing", type=Path)
	parser.add_argument("--output", type=Path, required=True)
	parser.add_argument("--require-gates", action="store_true")
	args = parser.parse_args(argv)
	try:
		report = evaluate(
			args.manifest,
			read(args.observations),
			read(args.judgments),
			timings=read(args.timings) if args.timings else None,
			billing=read(args.billing) if args.billing else None,
		)
		args.output.mkdir(parents=True, exist_ok=True)
		(args.output / "report.json").write_text(
			json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
		)
		(args.output / "report.md").write_text(markdown(report))
	except (EvaluationError, OSError, ValueError):
		print("Evaluation rejected: invalid, incomplete or mismatched input contracts.", file=sys.stderr)
		return 2
	print(f"Evaluation {report['purpose']}: {report['verdict']}")
	return 1 if args.require_gates and report["verdict"] != "GO_SYNTHETIC_PILOT" else 0


if __name__ == "__main__":
	sys.exit(main())
