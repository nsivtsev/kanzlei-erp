"""Independent arithmetic and adversarial input checks for AP10's offline scorer."""

import copy
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


class EvaluationTests(unittest.TestCase):
	def setUp(self):
		self.assertTrue((ROOT / "scripts/posteingang_evaluation.py").exists(), "AP10 scorer missing")
		import posteingang_evaluation as evaluation

		self.e = evaluation

	def test_exact_thresholds_and_empty_denominator(self):
		self.assertEqual(self.e.ratio(19, 20, "0.95")["status"], "PASS")
		self.assertEqual(self.e.ratio(18, 20, "0.95")["status"], "FAIL")
		self.assertEqual(self.e.ratio(9, 10, "0.90")["status"], "PASS")
		self.assertEqual(self.e.ratio(0, 0, "1")["status"], "NOT MEASURED")

	def test_duplicate_action_mapping_is_rejected(self):
		actions = [{"gold_action_id": "g1"}, {"gold_action_id": "g2"}]
		rows = [
			{"gold_action_id": "g1", "proposed_action_index": 0},
			{"gold_action_id": "g2", "proposed_action_index": 0},
		]
		with self.assertRaisesRegex(self.e.EvaluationError, "one-to-one"):
			self.e.action_map(rows, actions, 2)
		rows[1]["proposed_action_index"] = 1
		self.assertEqual(self.e.action_map(rows, actions, 2), {"g1": 0, "g2": 1})

	def test_unknown_action_and_proposal_index_rejected(self):
		for row in (
			{"gold_action_id": "missing", "proposed_action_index": 0},
			{"gold_action_id": "g", "proposed_action_index": 3},
		):
			with self.assertRaises(self.e.EvaluationError):
				self.e.action_map([row], [{"gold_action_id": "g"}], 1)

	def test_parser_rejects_duplicate_keys_and_nan(self):
		for value in ('{"case_id":"a","case_id":"b"}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'):
			with self.assertRaises(self.e.EvaluationError):
				self.e.loads(value)

	def test_pointer_does_not_allow_negative_array_indices(self):
		with self.assertRaises(self.e.EvaluationError):
			self.e.pointer({"a": [1]}, "/a/-1")
		self.assertEqual(self.e.pointer({"a": [1]}, "/a/0"), 1)

	def test_missing_case_counts_as_false_negative_and_duplicate_is_fp(self):
		gold = [
			{"gold_action_id": "g1", "deadline": {"kind": "explicit_date", "date": "2026-11-20"}},
			{"gold_action_id": "g2", "deadline": {"kind": "not_found", "date": None}},
		]
		proposed = [
			{"deadline": {"kind": "explicit_date", "date": "2026-11-21"}},
			{"deadline": {"kind": "not_found", "date": None}},
		]
		result = self.e.action_counts(gold, proposed, [{"gold_action_id": "g1", "proposed_action_index": 0}])
		self.assertEqual(
			result,
			{
				"found": 1,
				"gold": 2,
				"proposed": 2,
				"explicit_found": 1,
				"explicit_gold": 1,
				"dates_correct": 0,
				"relative_violations": 0,
			},
		)
		missing = self.e.action_counts(gold, [], [])
		self.assertEqual((missing["gold"], missing["found"], missing["explicit_gold"]), (2, 0, 1))

	def test_swapped_dates_do_not_count_as_correct(self):
		gold = [
			{"gold_action_id": "a", "deadline": {"kind": "explicit_date", "date": "2026-11-20"}},
			{"gold_action_id": "b", "deadline": {"kind": "explicit_date", "date": "2026-11-21"}},
		]
		proposed = [{"deadline": gold[1]["deadline"]}, {"deadline": gold[0]["deadline"]}]
		self.assertEqual(
			self.e.action_counts(
				gold,
				proposed,
				[
					{"gold_action_id": "a", "proposed_action_index": 0},
					{"gold_action_id": "b", "proposed_action_index": 1},
				],
			)["dates_correct"],
			0,
		)

	def test_fake_can_never_open_live_gate(self):
		self.assertEqual(self.e.verdict("fake", {"recall": {"status": "PASS"}}), "INCOMPLETE")
		self.assertEqual(self.e.verdict("mistral", {"recall": {"status": "NOT MEASURED"}}), "INCOMPLETE")
		self.assertEqual(self.e.verdict("mistral", {"recall": {"status": "FAIL"}}), "NO_GO")
		self.assertEqual(self.e.verdict("mistral", {"recall": {"status": "PASS"}}), "GO_SYNTHETIC_PILOT")

	def test_one_participant_cannot_pass_time_gate(self):
		assignments = [
			{"participant": "p1", "case_id": "a", "mode": "manual", "group": "x"},
			{"participant": "p1", "case_id": "b", "mode": "assisted", "group": "x"},
		]
		data = {
			"assignments": assignments,
			"samples": [
				{"participant": "p1", "case_id": "a", "active_seconds": 100, "total_seconds": 100},
				{"participant": "p1", "case_id": "b", "active_seconds": 60, "total_seconds": 90},
			],
		}
		self.assertEqual(self.e.timing_metric(data)["status"], "NOT MEASURED")

	def test_two_participants_rotate_and_time_boundary(self):
		assignments = [
			{"participant": p, "case_id": f"{p}-{mode}", "mode": mode, "group": "x"}
			for p in ("one", "two")
			for mode in ("manual", "assisted")
		]
		data = {
			"assignments": assignments,
			"samples": [
				{
					"participant": row["participant"],
					"case_id": row["case_id"],
					"active_seconds": 100 if row["mode"] == "manual" else 70,
					"total_seconds": 110,
				}
				for row in assignments
			],
		}
		self.assertEqual(self.e.timing_metric(data)["status"], "PASS")
		data["samples"][1]["active_seconds"] = 72
		self.assertEqual(self.e.timing_metric(data)["status"], "FAIL")
		data["assignments"][1]["case_id"] = data["assignments"][0]["case_id"]
		with self.assertRaises(self.e.EvaluationError):
			self.e.timing_metric(data)


if __name__ == "__main__":
	unittest.main()


class EvaluationIntegrationTests(unittest.TestCase):
	def setUp(self):
		import posteingang_evaluation as e

		self.e = e
		self.manifest_path = ROOT / "kanzlei_erp/tests/fixtures/posteingang/manifest.json"
		self.manifest, self.gold, self.facts, dataset_hash, gold_hash = e.load_dataset(self.manifest_path)
		self.obs = {
			"schema_version": "1.0",
			"run_id": "test-arithmetic",
			"dataset_hash": dataset_hash,
			"gold_hash": gold_hash,
			"provider": "fake",
			"prompt_version": "fake-v1",
			"code_revision": "test-only",
			"configuration_hash": "1" * 64,
			"models": {"ocr": None, "extraction": None},
			"started_at": "2026-10-25T10:00:00Z",
			"cases": [],
			"safety": None,
		}
		self.jud = {
			"schema_version": "1.0",
			"run_id": "test-arithmetic",
			"observations_hash": e.digest(self.obs),
			"gold_hash": gold_hash,
			"cases": [],
		}

	def finish(self):
		self.jud["observations_hash"] = self.e.digest(self.obs)
		return self.e.evaluate(self.manifest_path, self.obs, self.jud)

	def observation(self, cid):
		case = next(c for c in self.manifest["cases"] if c["case_id"] == cid)
		root = self.manifest_path.parent
		row = {
			"case_id": cid,
			"sha256": case["sha256"],
			"letter": "synthetic",
			"run": "synthetic-run",
			"status": "succeeded",
			"error_code": None,
			"ocr": self.e.read(root / case["ocr"]),
			"extraction": self.e.read(root / case["extraction"]),
			"matching": {
				"candidates": case["expected"]["candidate_customers"],
				"ambiguity": next(f["ambiguity"] for f in self.facts["cases"] if f["case_id"] == cid),
			},
			"duration_seconds": 1,
			"attempt_count": 1,
			"raw_relative_date_violations": 0,
		}
		self.obs["cases"].append(row)
		return row

	def test_missing_results_remain_in_denominator_and_new_cohort_is_26(self):
		report = self.finish()
		expected = sum(
			len(self.gold[c["case_id"]]["actions"]) for c in self.manifest["cases"] if c["split"] == "holdout"
		)
		self.assertEqual(report["gates"]["action_recall"]["denominator"], expected)
		self.assertEqual(report["gates"]["action_recall"]["numerator"], 0)
		self.assertEqual(report["groups"]["new_holdout"]["cases"], 26)
		self.assertEqual(report["gates"]["gold_review"]["status"], "NOT MEASURED")
		self.assertEqual(report["verdict"], "INCOMPLETE")

	def test_known_live_prompt_versions_are_evaluable_without_assuming_quality(self):
		self.obs.update(
			provider="mistral", models={"ocr": "mistral-ocr-4-1", "extraction": "mistral-small-2603"}
		)
		for version in ("ap04-extraction-v1", "ap04-extraction-v2", "ap04-extraction-v3"):
			with self.subTest(version=version):
				self.obs["prompt_version"] = version
				self.assertNotEqual(self.finish()["verdict"], "GO_SYNTHETIC_PILOT")
		self.obs["prompt_version"] = "unknown-future-prompt"
		with self.assertRaises(self.e.EvaluationError):
			self.finish()

	def test_wrong_verified_quote_fails_and_missing_evidence_is_not_removed(self):
		row = self.observation("AP01-01")
		row["extraction"]["sender"]["evidence"][0]["quote"] = "NOT ON THE PAGE"
		report = self.finish()
		self.assertEqual(report["groups"]["development"]["metrics"]["verified_quotes"]["status"], "FAIL")
		self.assertGreater(report["groups"]["development"]["counts"]["evidence_total"], 0)

	def test_unknown_fields_impossible_dates_and_stale_fingerprint_rejected(self):
		self.obs["secret"] = "no secret permitted"
		with self.assertRaises(self.e.EvaluationError):
			self.finish()
		del self.obs["secret"]
		self.obs["started_at"] = "2026-02-30T10:00:00Z"
		with self.assertRaises(self.e.EvaluationError):
			self.finish()
		self.obs["started_at"] = "2026-10-25T10:00:00Z"
		self.obs["dataset_hash"] = "0" * 64
		with self.assertRaises(self.e.EvaluationError):
			self.finish()

	def test_unknown_page_and_duplicate_case_are_rejected(self):
		row = self.observation("AP01-01")
		row["ocr"]["pages"][0]["page"] = 0
		with self.assertRaises(self.e.EvaluationError):
			self.finish()
		row["ocr"]["pages"][0]["page"] = 1
		self.obs["cases"].append(copy.deepcopy(row))
		with self.assertRaises(self.e.EvaluationError):
			self.finish()

	def test_raw_relative_violation_is_visible_when_result_was_rejected(self):
		row = self.observation("AP01-07")
		row.update(
			status="failed",
			error_code="INVALID_RESULT",
			extraction=None,
			matching=None,
			raw_relative_date_violations=1,
		)
		report = self.finish()
		self.assertEqual(report["gates"]["relative_dates"]["status"], "FAIL")
		self.assertIn("AP01-07", report["gates"]["relative_dates"]["errors"])


class EvaluationReviewRegressionTests(EvaluationIntegrationTests):
	def test_relative_gold_cannot_be_relabelled_explicit_to_evade_gate(self):
		row = self.observation("AP01-07")
		row["extraction"]["actions"][0]["deadline"].update(
			kind="explicit_date", date="2026-11-01", anchor=None
		)
		self.jud["cases"] = [
			{
				"case_id": "AP01-07",
				"status": "adjudicated",
				"reviewer": "test-reviewer",
				"adjudicator": None,
				"disputed": False,
				"reason": "Unit test semantic match",
				"matches": [
					{
						"gold_action_id": self.gold["AP01-07"]["actions"][0]["gold_action_id"],
						"proposed_action_index": 0,
					}
				],
				"facts": [],
				"field_edits": 0,
				"actions_edited": 0,
			}
		]
		self.assertEqual(self.finish()["gates"]["relative_dates"]["status"], "FAIL")

	def test_safety_requires_complete_unique_scenarios(self):
		self.obs["safety"] = {
			"source": "unit-only",
			"code_revision": "test-only",
			"scenarios": [
				{"id": "one-arbitrary-check", "leaks": 0, "auto_confirmations": 0, "duplicate_tasks": 0}
			],
		}
		self.assertEqual(self.finish()["gates"]["runtime_safety"]["status"], "NOT MEASURED")
		self.obs["safety"]["scenarios"] *= 2
		with self.assertRaises(self.e.EvaluationError):
			self.finish()

	def test_live_metadata_cannot_omit_concrete_models(self):
		self.obs["provider"] = "mistral"
		with self.assertRaises(self.e.EvaluationError):
			self.finish()

	def test_missing_explicit_action_reports_case_id(self):
		metric = self.finish()["gates"]["explicit_action_recall"]
		self.assertEqual(metric["status"], "FAIL")
		self.assertIn("AP01-04", metric["errors"])

	def test_registration_compares_instants_not_iso_strings(self):
		data = {
			"schema_version": "1.0",
			"run_id": self.obs["run_id"],
			"assignment_plan_hash": "0" * 64,
			"pre_registered_at": "2026-10-25T10:30:00Z",
			"assignments": [{"participant": "one", "case_id": "AP01-01", "mode": "manual", "group": "x"}],
			"samples": [
				{
					"participant": "one",
					"case_id": "AP01-01",
					"started_at": "2026-10-25T11:00:00+02:00",
					"active_seconds": 10,
					"total_seconds": 10,
				}
			],
		}
		data["assignment_plan_hash"] = self.e.digest(data["assignments"])
		self.jud["observations_hash"] = self.e.digest(self.obs)
		with self.assertRaises(self.e.EvaluationError):
			self.e.evaluate(self.manifest_path, self.obs, self.jud, timings=data)


class StrictTimestampTests(EvaluationIntegrationTests):
	def test_invalid_offset_minutes_rejected(self):
		self.obs["started_at"] = "2026-10-25T10:30:00+00:99"
		with self.assertRaises(self.e.EvaluationError):
			self.finish()

	def test_exponent_overflow_rejected(self):
		with self.assertRaises(self.e.EvaluationError):
			self.e.loads('{"x":1e999}')


class PendingJudgmentTests(EvaluationIntegrationTests):
	def test_pending_semantic_labels_are_not_reported_as_measured_quality(self):
		self.observation("AP01-01")
		report = self.finish()
		self.assertEqual(
			report["groups"]["development"]["metrics"]["action_recall"]["status"], "NOT MEASURED"
		)
		self.assertEqual(
			report["groups"]["development"]["metrics"]["relative_dates"]["status"], "NOT MEASURED"
		)
