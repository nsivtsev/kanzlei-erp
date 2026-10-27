"""Offline AP10 arithmetic. Human semantic judgments are inputs, never inferred labels."""

import hashlib
import json
import math
import re
import statistics
import unicodedata
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "docs/plans/ai-posteingang/evaluation"


class EvaluationError(ValueError):
	"""Safe tooling failure; not a provider or ERP exception."""


def require(condition, message):
	if not condition:
		raise EvaluationError(message)


def loads(value):
	def pairs(rows):
		result = {}
		for key, item in rows:
			require(key not in result, "duplicate JSON key")
			result[key] = item
		return result

	def constant(_value):
		raise EvaluationError("non-finite JSON constant")

	def finite_number(text):
		number = float(text)
		require(math.isfinite(number), "non-finite JSON number")
		return number

	try:
		return json.loads(value, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_number)
	except (ValueError, TypeError) as error:
		raise EvaluationError("invalid JSON") from error


def read(path):
	try:
		return loads(Path(path).read_text(encoding="utf-8"))
	except OSError as error:
		raise EvaluationError("input file unavailable") from error


def digest(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
	).hexdigest()


def validate(value, name):
	schema = read(SCHEMAS / f"{name}.schema.json")
	# These tool schemas are self contained. No registry retrieval/network fallback.
	checker = FormatChecker()

	@checker.checks("date-time", raises=(ValueError, TypeError))
	def calendar_timestamp(value):
		return (
			isinstance(value, str)
			and bool(
				re.fullmatch(
					r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
					value,
				)
			)
			and datetime.fromisoformat(value).tzinfo is not None
		)

	validator = Draft202012Validator(schema, format_checker=checker)
	require(not list(validator.iter_errors(value)), f"invalid {name} input")


def safe_path(root, relative):
	path = Path(relative)
	target = (root / path).resolve()
	require(
		not path.is_absolute()
		and ".." not in path.parts
		and target.is_relative_to(root.resolve())
		and target.is_file(),
		"unsafe dataset path",
	)
	return target


def pointer(value, path):
	require(isinstance(path, str) and path.startswith("/"), "invalid JSON pointer")
	try:
		for token in path[1:].split("/"):
			require(not re.search(r"~(?![01])", token), "invalid pointer escape")
			token = token.replace("~1", "/").replace("~0", "~")
			if isinstance(value, list):
				require(bool(re.fullmatch(r"0|[1-9][0-9]*", token)), "invalid array pointer")
				value = value[int(token)]
			else:
				value = value[token]
		return value
	except (KeyError, IndexError, TypeError) as error:
		raise EvaluationError("pointer outside document") from error


def ratio(numerator, denominator, threshold, errors=None):
	require(
		type(numerator) is int and type(denominator) is int and 0 <= numerator <= denominator,
		"invalid metric counts",
	)
	status = (
		"NOT MEASURED"
		if not denominator
		else ("PASS" if Decimal(numerator) >= Decimal(threshold) * denominator else "FAIL")
	)
	return {
		"numerator": numerator,
		"denominator": denominator,
		"threshold": threshold,
		"value": numerator / denominator if denominator else None,
		"status": status,
		"errors": sorted(set(errors or [])),
	}


def action_map(rows, gold, proposed_count):
	allowed = {action["gold_action_id"] for action in gold}
	result, used = {}, set()
	for row in rows:
		key, index = row["gold_action_id"], row["proposed_action_index"]
		require(
			key in allowed and type(index) is int and 0 <= index < proposed_count,
			"unknown gold action or proposal index",
		)
		require(key not in result and index not in used, "action matching must be one-to-one")
		result[key] = index
		used.add(index)
	return result


def action_counts(gold, proposed, rows):
	matched = action_map(rows, gold, len(proposed))
	explicit = [a for a in gold if a["deadline"]["kind"] == "explicit_date"]
	return {
		"found": len(matched),
		"gold": len(gold),
		"proposed": len(proposed),
		"explicit_found": sum(a["gold_action_id"] in matched for a in explicit),
		"explicit_gold": len(explicit),
		"dates_correct": sum(
			a["gold_action_id"] in matched
			and proposed[matched[a["gold_action_id"]]]["deadline"]["kind"] == "explicit_date"
			and proposed[matched[a["gold_action_id"]]]["deadline"]["date"] == a["deadline"]["date"]
			for a in explicit
		),
		"relative_violations": len(
			{
				index
				for index, action in enumerate(proposed)
				if action["deadline"]["kind"] == "relative" and action["deadline"]["date"] is not None
			}
			| {
				matched[a["gold_action_id"]]
				for a in gold
				if a["deadline"]["kind"] == "relative"
				and a["gold_action_id"] in matched
				and proposed[matched[a["gold_action_id"]]]["deadline"]["date"] is not None
			}
		),
	}


def verdict(provider, metrics):
	if provider != "mistral":
		return "INCOMPLETE"
	if any(m["status"] == "FAIL" for m in metrics.values()):
		return "NO_GO"
	return (
		"GO_SYNTHETIC_PILOT"
		if metrics and all(m["status"] == "PASS" for m in metrics.values())
		else "INCOMPLETE"
	)


def timing_metric(data):
	if not data:
		return {
			"status": "NOT MEASURED",
			"value": None,
			"threshold": "0.70",
			"reason": "No registered timing study",
		}
	assignments = data["assignments"]
	lookup = {(a["participant"], a["case_id"]): a for a in assignments}
	require(len(lookup) == len(assignments), "participant must see a case in only one mode")
	samples = {(s["participant"], s["case_id"]): s for s in data["samples"]}
	require(
		len(samples) == len(data["samples"]) and samples.keys() <= lookup.keys(),
		"duplicate or unplanned timing sample",
	)
	for sample in samples.values():
		require(0 < sample["active_seconds"] <= sample["total_seconds"], "invalid active/total time")
	participants = {a["participant"] for a in assignments}
	complete = (
		len(participants) >= 2
		and samples.keys() == lookup.keys()
		and all(
			{a["mode"] for a in assignments if a["participant"] == p} == {"manual", "assisted"}
			for p in participants
		)
	)
	groups = {}
	for key, sample in samples.items():
		assignment = lookup[key]
		groups.setdefault(assignment["group"], {}).setdefault(assignment["mode"], []).append(
			Decimal(str(sample["active_seconds"]))
		)
	complete = complete and bool(groups) and all(set(g) == {"manual", "assisted"} for g in groups.values())
	ratios = [
		statistics.median(g["assisted"]) / statistics.median(g["manual"])
		for g in groups.values()
		if set(g) == {"manual", "assisted"}
	]
	value = statistics.median(ratios) if complete else None
	return {
		"status": ("PASS" if value <= Decimal(".70") else "FAIL") if value is not None else "NOT MEASURED",
		"value": float(value) if value is not None else None,
		"threshold": "0.70",
		"participants": len(participants),
		"groups": len(groups),
		"group_ratios": [float(x) for x in ratios],
	}


def evidence_rows(value):
	if isinstance(value, dict):
		if {"page", "quote", "verification"} <= value.keys():
			yield value
		for child in value.values():
			yield from evidence_rows(child)
	elif isinstance(value, list):
		for child in value:
			yield from evidence_rows(child)


def normalized(text):
	return " ".join(unicodedata.normalize("NFKC", text).split())


def verified(evidence, pages):
	page = evidence["page"]
	return (
		type(page) is int
		and 1 <= page <= len(pages)
		and isinstance(evidence["quote"], str)
		and bool(normalized(evidence["quote"]))
		and normalized(evidence["quote"]) in normalized(pages[page - 1]["text"])
	)


def load_dataset(manifest_path):
	"""Freeze every manifest dependency, including gold and the catalog, before collection."""
	manifest_path = Path(manifest_path).resolve()
	root, manifest = manifest_path.parent, read(manifest_path)
	import validate_posteingang_fixtures as contracts

	try:
		contracts.validate_dataset(root)
		contracts.validate_pilot(root)
	except (contracts.ContractError, OSError, ValueError) as error:
		raise EvaluationError("invalid pilot dataset") from error
	require(manifest["synthetic"] is True, "only synthetic evaluation is supported")
	paths = {manifest["catalog"], manifest["sources"], manifest["scenarios"]}
	paths.update(
		case[key]
		for case in manifest["cases"]
		for key in ("pdf", "source_text", "ocr", "extraction", "review", "annotations")
	)
	artifacts = {
		path: hashlib.sha256(safe_path(root, path).read_bytes()).hexdigest() for path in sorted(paths)
	}
	facts = read(SCHEMAS / "gold-facts.json")
	gold = {case["case_id"]: read(safe_path(root, case["annotations"])) for case in manifest["cases"]}
	require(len(gold) == len(manifest["cases"]), "duplicate dataset case")
	schema_hashes = {
		p.name: hashlib.sha256(p.read_bytes()).hexdigest()
		for p in sorted(contracts.SCHEMAS.glob("*.schema.json"))
	}
	profile = read(SCHEMAS / "ap10-v1.json")
	schema_hashes["pilot_profile"] = digest(profile)
	schema_hashes["original_baseline"] = digest(read(SCHEMAS / profile["baseline"]))
	dataset_hash = digest(
		{
			"manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
			"artifacts": artifacts,
			"schemas": schema_hashes,
		}
	)
	return manifest, gold, facts, dataset_hash, digest({"gold": gold, "facts": facts})


def unique_cases(rows, allowed):
	result = {r["case_id"]: r for r in rows}
	require(len(result) == len(rows) and result.keys() <= allowed, "duplicate or unknown case ID")
	return result


def score_group(cases, gold, facts, observations, judgments):
	import validate_posteingang_fixtures as contracts

	counts = {
		k: 0
		for k in (
			"found",
			"gold",
			"proposed",
			"explicit_found",
			"explicit_gold",
			"dates_correct",
			"relative_violations",
			"top1",
			"top1_total",
			"top3",
			"top3_total",
			"ambiguity",
			"ambiguity_total",
			"verified",
			"verified_total",
			"evidence",
			"evidence_total",
			"success",
			"manual",
			"pending",
			"relative_unknown",
		)
	}
	errors = {key: [] for key in counts}
	durations, edits = [], []
	for case in cases:
		cid = case["case_id"]
		expected, fact_case = gold[cid], facts[cid]
		result = observations.get(cid)
		review = judgments.get(cid)
		extraction = result["extraction"] if result else None
		proposed = extraction["actions"] if extraction else []
		if result:
			require(result["sha256"] == case["sha256"], "PDF hash differs from dataset")
			if result["status"] == "succeeded":
				require(
					extraction is not None
					and result["ocr"] is not None
					and result["matching"] is not None
					and result["error_code"] is None,
					"successful observation incomplete",
				)
				contracts.validate_document(result["ocr"], "support.schema.json", "pages")
				pages = result["ocr"]["pages"]
				require(
					[p["page"] for p in pages] == list(range(1, case["page_count"] + 1))
					and sum(len(p["text"]) for p in pages) <= 2000000,
					"OCR page set or limit differs",
				)
				contracts.validate_document(extraction, "extraction.schema.json")
				require(
					all(1 <= e["page"] <= case["page_count"] for e in evidence_rows(extraction)),
					"evidence page outside PDF",
				)
				counts["success"] += 1
				counts["manual"] += extraction["readability"] == "unreadable"
			else:
				require(
					extraction is None and result["matching"] is None and result["error_code"] is not None,
					"failed observation cannot contain successful predictions",
				)
			if result["duration_seconds"] is not None:
				durations.append(result["duration_seconds"])
		reviewed = bool(review and review["status"] == "adjudicated")
		if review:
			require(
				not review["disputed"]
				or (review["adjudicator"] and review["adjudicator"] != review["reviewer"]),
				"disputed judgment needs independent adjudicator",
			)
			require(
				not reviewed or (review["reviewer"] and review["reason"]),
				"reviewed judgment needs author and reason",
			)
			# Validate even pending rows; no stale or forged correspondences tolerated.
			action_map(review["matches"], expected["actions"], len(proposed))
		if extraction and not reviewed:
			counts["pending"] += 1
		matched = review["matches"] if reviewed else []
		values = action_counts(expected["actions"], proposed, matched)
		for key, value in values.items():
			counts[key] += value
		for key, good, total in [
			("found", values["found"], values["gold"]),
			("proposed", values["found"], values["proposed"]),
			("dates_correct", values["dates_correct"], values["explicit_gold"]),
			("explicit_found", values["explicit_found"], values["explicit_gold"]),
		]:
			if good != total:
				errors[key].append(cid)
		if result and result["raw_relative_date_violations"] is not None:
			counts["relative_violations"] += max(
				0, result["raw_relative_date_violations"] - values["relative_violations"]
			)
		elif not result or result["status"] != "succeeded":
			counts["relative_unknown"] += 1
		if values["relative_violations"] or (result and result["raw_relative_date_violations"]):
			errors["relative_violations"].append(cid)
		candidates = result["matching"]["candidates"] if result and result["matching"] else []
		require(
			len(candidates) == len(set(candidates)) and set(candidates) <= set(case["allowed_customers"]),
			"candidate outside current case scope",
		)
		eligible = expected["customer_candidates"] if fact_case["ambiguity"] != "multiple_addressees" else []
		if len(eligible) == 1:
			counts["top1_total"] += 1
			counts["top1"] += bool(candidates and candidates[0] == eligible[0])
			if not candidates or candidates[0] != eligible[0]:
				errors["top1"].append(cid)
		if eligible:
			counts["top3_total"] += 1
			counts["top3"] += bool(set(candidates) & set(eligible))
			if not set(candidates) & set(eligible):
				errors["top3"].append(cid)
		if fact_case["ambiguity"] in ("multiple_matches", "multiple_addressees"):
			counts["ambiguity_total"] += 1
			correct = bool(
				result and result["matching"] and result["matching"]["ambiguity"] == fact_case["ambiguity"]
			)
			counts["ambiguity"] += correct
			if not correct:
				errors["ambiguity"].append(cid)
		for evidence in evidence_rows(extraction):
			if evidence["verification"] == "matched":
				counts["verified_total"] += 1
				ok = verified(evidence, result["ocr"]["pages"])
				counts["verified"] += ok
				if not ok:
					errors["verified"].append(cid)
		expected_facts = {f["fact_id"]: f for f in fact_case["facts"]} if fact_case["readable"] else {}
		counts["evidence_total"] += len(expected_facts)
		used = set()
		fact_count_before = counts["evidence"]
		for row in review["facts"] if review else []:
			require(
				row["fact_id"] in expected_facts and row["fact_id"] not in used,
				"unknown or repeated gold fact",
			)
			used.add(row["fact_id"])
			target = pointer(extraction, row["proposed_pointer"])
			original = pointer(expected, expected_facts[row["fact_id"]]["pointer"])
			require(
				isinstance(target, dict) and isinstance(original, dict),
				"fact pointer must address a fact object",
			)
			# The human verifies meaning; server verification and same gold page
			# additionally prevent unrelated checked citations from earning coverage.
			good = (
				reviewed
				and row["correct"]
				and any(
					e["verification"] == "matched"
					and verified(e, result["ocr"]["pages"])
					and e["page"] in {g["page"] for g in original["evidence"]}
					for e in target.get("evidence", [])
				)
			)
			counts["evidence"] += bool(good)
		if counts["evidence"] - fact_count_before < len(expected_facts):
			errors["evidence"].append(cid)
		if reviewed and review["field_edits"] is not None:
			edits.append(
				{
					"case_id": cid,
					"field_edits": review["field_edits"],
					"actions_edited": review["actions_edited"],
				}
			)

	def metric(n, d, threshold="1"):
		return ratio(counts[n], counts[d], threshold, errors[n])

	metrics = {
		"action_recall": metric("found", "gold", ".95"),
		"action_precision": metric("found", "proposed", ".90"),
		"explicit_action_recall": metric("explicit_found", "explicit_gold"),
		"explicit_dates": metric("dates_correct", "explicit_gold"),
		"customer_top1": metric("top1", "top1_total", ".95"),
		"customer_top3": metric("top3", "top3_total", ".95"),
		"ambiguity": metric("ambiguity", "ambiguity_total"),
		"verified_quotes": metric("verified", "verified_total"),
		"evidence_coverage": metric("evidence", "evidence_total", ".95"),
	}
	metrics["relative_dates"] = {
		"numerator": counts["relative_violations"],
		"denominator": len(cases),
		"threshold": "0",
		"status": "FAIL"
		if counts["relative_violations"]
		else ("NOT MEASURED" if counts["relative_unknown"] or counts["pending"] else "PASS"),
		"errors": errors["relative_violations"],
	}
	metrics["semantic_review"] = {
		"status": "NOT MEASURED" if counts["pending"] else "PASS",
		"pending_cases": counts["pending"],
	}
	if counts["pending"]:
		for key in (
			"action_recall",
			"action_precision",
			"explicit_action_recall",
			"explicit_dates",
			"evidence_coverage",
		):
			metrics[key]["status"] = "NOT MEASURED"
			metrics[key]["reason"] = "Semantic judgments pending; numerator is a lower bound"
	return {
		"cases": len(cases),
		"metrics": metrics,
		"counts": counts,
		"duration_seconds": {
			"median": statistics.median(durations) if durations else None,
			"measured_cases": len(durations),
		},
		"edits": edits,
	}


def evaluate(manifest_path, observations, judgments, *, timings=None, billing=None):
	validate(observations, "observations")
	validate(judgments, "judgments")
	if observations["provider"] == "mistral":
		require(
			all(
				isinstance(model, str)
				and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,139}", model)
				and "latest" not in model.lower()
				and re.search(r"-(?:[0-9]{4}|[0-9]+-[0-9]+)$", model)
				for model in observations["models"].values()
			),
			"concrete Mistral models required",
		)
		require(
			observations["prompt_version"]
			in ("ap04-extraction-v1", "ap04-extraction-v2", "ap04-extraction-v3"),
			"unknown live prompt version",
		)
	manifest, gold, facts, data_hash, gold_hash = load_dataset(manifest_path)
	require(
		observations["dataset_hash"] == data_hash and observations["gold_hash"] == gold_hash,
		"dataset/gold freeze mismatch",
	)
	require(
		judgments["observations_hash"] == digest(observations)
		and judgments["gold_hash"] == gold_hash
		and judgments["run_id"] == observations["run_id"],
		"judgment provenance mismatch",
	)
	allowed = {c["case_id"] for c in manifest["cases"]}
	obs = unique_cases(observations["cases"], allowed)
	judged = unique_cases(judgments["cases"], allowed)
	fact_cases = unique_cases(facts["cases"], allowed)
	require(fact_cases.keys() == allowed, "gold fact coverage incomplete")
	new_cases = set(read(SCHEMAS / "ap10-v1.json")["holdout_history"]["new_cases"])
	groups = {
		name: score_group([c for c in manifest["cases"] if predicate(c)], gold, fact_cases, obs, judged)
		for name, predicate in [
			("development", lambda c: c["split"] == "development"),
			("holdout", lambda c: c["split"] == "holdout"),
			("new_holdout", lambda c: c["split"] == "holdout" and c["case_id"] in new_cases),
		]
	}
	metrics = dict(groups["holdout"]["metrics"])
	metrics["gold_review"] = {
		"status": "PASS"
		if facts["review_status"] == "independently_reviewed"
		and facts.get("independent_review", {}).get("reviewer")
		and facts.get("independent_review", {}).get("reviewed_at")
		and facts.get("independent_review", {}).get("reason")
		else "NOT MEASURED",
		"reason": facts["review_status"],
	}
	if timings:
		validate(timings, "timings")
		require(
			timings["run_id"] == observations["run_id"]
			and timings["assignment_plan_hash"] == digest(timings["assignments"]),
			"timing registration mismatch",
		)
		require(
			all(
				datetime.fromisoformat(s["started_at"])
				>= datetime.fromisoformat(timings["pre_registered_at"])
				for s in timings["samples"]
			),
			"timing study was not pre-registered",
		)
		require(all(a["case_id"] in allowed for a in timings["assignments"]), "unknown timing case")
	metrics["active_time"] = timing_metric(timings)
	metrics["cost"] = {"status": "NOT MEASURED", "value": None}
	if billing:
		validate(billing, "billing")
		require(
			billing["run_id"] == observations["run_id"] and billing["period_start"] <= billing["period_end"],
			"billing provenance mismatch",
		)
		allocations = unique_cases(billing["cases"], set(obs))
		require(
			bool(obs)
			and allocations.keys() == obs.keys()
			and sum(Decimal(str(c["amount"])) for c in allocations.values())
			== Decimal(str(billing["amount"])),
			"incomplete billing allocation",
		)
		metrics["cost"] = {
			"status": "PASS",
			"amount": billing["amount"],
			"currency": billing["currency"],
			"source": billing["source"],
			"allocations": billing["cases"],
			"request_count": sum(c["requests"] for c in allocations.values())
			if all(c["requests"] is not None for c in allocations.values())
			else None,
			"per_letter": billing["amount"] / len(obs) if obs else None,
			"per_page": billing["amount"]
			/ sum(c["page_count"] for c in manifest["cases"] if c["case_id"] in obs)
			if obs
			else None,
		}
	safety = observations["safety"]
	metrics["runtime_safety"] = {"status": "NOT MEASURED"}
	if safety:
		require(
			safety["code_revision"] == observations["code_revision"] and bool(safety["scenarios"]),
			"safety evidence provenance mismatch",
		)
		scenario_ids = [r["id"] for r in safety["scenarios"]]
		require(len(scenario_ids) == len(set(scenario_ids)), "duplicate safety scenario")
		required_scenarios = set(read(SCHEMAS / "ap10-v1.json")["required_safety_scenarios"])
		failures = [
			r["id"]
			for r in safety["scenarios"]
			if any(r[k] for k in ("leaks", "auto_confirmations", "duplicate_tasks"))
		]
		metrics["runtime_safety"] = {
			"status": "FAIL"
			if failures
			else "PASS"
			if required_scenarios <= set(scenario_ids)
			else "NOT MEASURED",
			"missing_scenarios": sorted(required_scenarios - set(scenario_ids)),
			"errors": failures,
			"tested_scenarios": len(safety["scenarios"]),
			"source": safety["source"],
		}
	if observations["provider"] == "fake":
		metrics["provider_quality"] = {"status": "NOT MEASURED", "reason": "Fake exercises contracts only"}
	return {
		"schema_version": "1.0",
		"run_id": observations["run_id"],
		"purpose": "contract_process" if observations["provider"] == "fake" else "provider_quality",
		"provider": observations["provider"],
		"dataset_hash": data_hash,
		"gold_hash": gold_hash,
		"observations_hash": digest(observations),
		"code_revision": observations["code_revision"],
		"models": observations["models"],
		"prompt_version": observations["prompt_version"],
		"groups": groups,
		"gates": metrics,
		"verdict": verdict(observations["provider"], metrics),
		"owner_decision_required": True,
	}


def markdown(report):
	lines = [
		"# Posteingang evaluation",
		"",
		f"Run: {report['run_id']} · Provider: {report['provider']} · Purpose: {report['purpose']}",
		"",
		f"Verdict: **{report['verdict']}** (owner decision required)",
		"",
		"| Gate | Numerator | Denominator | Status |",
		"|---|---:|---:|---|",
	]
	for key, value in report["gates"].items():
		lines.append(
			f"| {key} | {value.get('numerator', '—')} | {value.get('denominator', '—')} | {value['status']} |"
		)
	lines.extend(
		[
			"",
			"Fake results never establish Mistral quality. Missing cases remain in recall denominators.",
			"",
			"See report.json for case IDs, group denominators, provenance, timings and costs.",
		]
	)
	return "\n".join(lines) + "\n"
