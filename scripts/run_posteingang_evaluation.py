#!/usr/bin/env python3
"""Collect synthetic AP10 observations on an explicitly marked disposable Bench site."""

import argparse
import os
import sys
from pathlib import Path


def parser():
	result = argparse.ArgumentParser(description=__doc__)
	result.add_argument("--site", required=True)
	result.add_argument("--actor", required=True)
	result.add_argument("--provider", choices=("fake", "mistral"), default="fake")
	result.add_argument("--split", choices=("development", "holdout", "all"), default="development")
	result.add_argument("--cohort", choices=("ap10-v1", "ap10-medium-v2"), default="ap10-v1")
	result.add_argument("--output", type=Path, required=True)
	result.add_argument("--resume", action="store_true")
	result.add_argument("--allow-external", action="store_true")
	result.add_argument("--max-requests", type=int)
	result.add_argument("--max-documents", type=int, default=1)
	return result


def cohort_manifest(root, cohort):
	paths = {"ap10-v1": "posteingang", "ap10-medium-v2": "posteingang_v2"}
	if cohort not in paths:
		raise ValueError("Unknown fixed synthetic cohort")
	return Path(root) / "kanzlei_erp/tests/fixtures" / paths[cohort] / "manifest.json"


def check_invocation(args, config):
	cohort = getattr(args, "cohort", "ap10-v1")
	cohort_manifest(Path(), cohort)
	if cohort == "ap10-medium-v2" and args.provider != "mistral":
		raise ValueError("New quality cohort requires explicit Mistral selection")
	if (
		not config.get("posteingang_acceptance_site")
		or not args.site.startswith("posteingang-ap10")
		or not args.site.endswith(".localhost")
	):
		raise ValueError("Marked disposable AP10 site required")
	if not args.actor.startswith("ap10-") or not args.actor.endswith("@example.invalid"):
		raise ValueError("Only synthetic AP10 actors are accepted")
	if args.provider == "mistral" and (
		not args.allow_external or not args.max_requests or args.max_requests < 1
	):
		raise ValueError("Live collection requires explicit external permission and request limit")
	if args.max_documents < 1 or args.max_documents > 42:
		raise ValueError("Document limit must be 1..42")


def check_configuration(args, settings, case_count):
	if args.provider == "mistral" and (
		not args.allow_external
		or not args.max_requests
		or args.max_requests < 2 * case_count
		or not settings.get("enabled")
		or not settings.get("external_analysis_enabled")
		or settings.get("provider") != "mistral"
		or settings.get("max_retries") != 0
		or settings.get("auto_analysis_enabled")
	):
		raise ValueError(
			"Live requires enabled zero-retry configuration, disabled autostart and request budget"
		)


def check_receipt(receipt):
	if receipt.get("outcome") != "created":
		raise ValueError("Existing bytes belong to another measurement; use a fresh disposable site")


def check_actor(owned, roles):
	required = {"Projects User", "Posteingang Operator"}
	if not owned or not required <= set(roles) or set(roles) - required - {"All", "Guest", "Desk User"}:
		raise ValueError("Actor must be journal-owned with only expected roles")


def check_run(run, letter, actor, provider, configuration):
	if (
		run.get("queued_by") != actor
		or letter.get("received_by") != actor
		or run.get("letter") != letter.get("name")
		or run.get("generation") != letter.get("analysis_generation")
		or run.get("provider") != provider
		or any(run.get("configuration", {}).get(k) != v for k, v in configuration.items())
		or any(
			run.get("snapshot", {}).get(k) != letter.get(k)
			for k in ("original_file", "content_sha256", "page_count", "received_date_known", "received_date")
		)
	):
		raise ValueError("Run source, actor or configuration differs from this measurement")


def check_source(letter, case):
	if letter.get("content_sha256") != case["sha256"] or any(
		letter.get(k) != case.get(k) for k in ("page_count", "received_date_known", "received_date")
	):
		raise ValueError("Journal source does not match manifest case")


def resume_action(status, attempts, run_epoch, current_epoch):
	if status in ("Succeeded", "Failed", "Cancelled", "Superseded"):
		return "observe"
	if status == "Queued" and attempts == 0 and run_epoch == current_epoch:
		return "dispatch"
	raise ValueError("Unknown or stopped worker outcome requires explicit recovery, never resend")


def code_hash(root):
	import hashlib
	import json

	paths = list((root / "kanzlei_erp").glob("*.py"))
	paths += [
		root / path
		for path in (
			"kanzlei_erp/tests/posteingang_acceptance_helpers.py",
			"scripts/run_posteingang_evaluation.py",
			"scripts/evaluate-posteingang.py",
			"scripts/posteingang_evaluation.py",
			"scripts/posteingang_evaluation_budget.py",
		)
	]
	content = {
		str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
		for path in sorted(set(paths))
		if path.is_file()
	}
	return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main(argv=None):
	args = parser().parse_args(argv)
	# Lazy imports: --help works with ordinary Python, without Bench or a secret.
	try:
		import frappe

		from kanzlei_erp.tests.posteingang_acceptance_helpers import collect

		sites = Path.cwd() / "sites"
		if not sites.is_dir():
			raise ValueError("Run collection from the Bench directory using its Python")
		sites = sites.resolve()
		os.chdir(sites)
		frappe.init(site=args.site, sites_path=str(sites), force=True)
		frappe.connect()
		check_invocation(args, frappe.conf)
		collect(args)
		print("Synthetic observations collected; quality gates require independent judgments.")
		return 0
	except Exception:
		print(
			"Collection refused or incomplete; check marked site, settings, budget and journal.",
			file=sys.stderr,
		)
		return 2
	finally:
		if "frappe" in locals() and getattr(frappe.local, "db", None):
			frappe.destroy()


if __name__ == "__main__":
	sys.exit(main())
