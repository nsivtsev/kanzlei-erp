import unittest
from datetime import date

from kanzlei_erp.fibu_checklist import build_checklist_snapshot, validate_entry


class TestFiBuChecklistRules(unittest.TestCase):
	def test_snapshot_selects_common_and_service_sources_and_clips_validity(self):
		sources = [
			{
				"name": "bank-current",
				"category": "Bank Account",
				"source_name": "Current account",
				"service": "",
				"valid_from": "2026-09-15",
				"valid_until": "",
			},
			{
				"name": "bank-other-service",
				"category": "Bank Account",
				"source_name": "Other account",
				"service": "Other Service",
				"valid_from": "",
				"valid_until": "",
			},
		]

		rows = build_checklist_snapshot(sources, "FiBu", date(2026, 9, 1), date(2026, 9, 30))

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["source_id"], "bank-current")
		self.assertEqual(rows[0]["expected_from"], "2026-09-15")
		self.assertEqual(rows[0]["expected_through"], "2026-09-30")
		self.assertEqual(rows[0]["status"], "Expected")

	def test_annual_snapshot_handles_leap_day_and_respects_end_date(self):
		rows = build_checklist_snapshot(
			[
				{
					"name": "assets",
					"category": "Assets",
					"source_name": "Asset register",
					"valid_from": "2024-02-29",
					"valid_until": "2024-08-31",
				}
			],
			"Annual FiBu",
			date(2024, 1, 1),
			date(2024, 12, 31),
		)

		self.assertEqual(rows[0]["expected_from"], "2024-02-29")
		self.assertEqual(rows[0]["expected_through"], "2024-08-31")
		self.assertEqual(
			build_checklist_snapshot(
				[{"name": "future", "category": "Contract", "source_name": "Future contract", "valid_from": "2025-01-01"}],
				"Annual FiBu",
				date(2024, 1, 1),
				date(2024, 12, 31),
			),
			[],
		)

	def test_quarter_snapshot_clips_source_start_to_the_quarter(self):
		rows = build_checklist_snapshot(
			[
				{
					"name": "card",
					"category": "Card",
					"source_name": "Company card",
					"valid_from": "2026-05-10",
					"valid_until": "2026-12-31",
				}
			],
			"FiBu",
			date(2026, 4, 1),
			date(2026, 6, 30),
		)

		self.assertEqual(rows[0]["expected_from"], "2026-05-10")
		self.assertEqual(rows[0]["expected_through"], "2026-06-30")

	def test_received_status_requires_evidence_covering_entire_expected_period(self):
		entry = {"status": "Received in Full", "expected_from": "2026-09-01", "expected_through": "2026-09-30"}
		evidence = [
			{"covered_from": "2026-09-01", "covered_through": "2026-09-14"},
			{"covered_from": "2026-09-15", "covered_through": "2026-09-30"},
		]

		validate_entry(entry, evidence, previous_status="Expected", reason="")
		validate_entry(entry, [evidence[0], {**evidence[1], "covered_from": "2026-09-14"}], "Expected", "")

		with self.assertRaisesRegex(ValueError, "cover the expected period"):
			validate_entry(entry, [evidence[0], {**evidence[1], "covered_from": "2026-09-16"}], "Expected", "")

	def test_reviewed_requires_evidence_and_not_applicable_requires_reason(self):
		with self.assertRaisesRegex(ValueError, "Evidence"):
			validate_entry(
				{"status": "Reviewed", "expected_from": "2026-09-01", "expected_through": "2026-09-30"},
				[],
				"Expected",
				"",
			)
		with self.assertRaisesRegex(ValueError, "reason"):
			validate_entry(
				{"status": "Not Applicable", "expected_from": "2026-09-01", "expected_through": "2026-09-30"},
				[],
				"Expected",
				"",
			)

	def test_status_rollback_requires_reason_and_evidence_must_be_in_period(self):
		entry = {"status": "Partially Received", "expected_from": "2026-09-01", "expected_through": "2026-09-30"}
		with self.assertRaisesRegex(ValueError, "reason"):
			validate_entry({**entry, "status": "Expected"}, [], "Partially Received", "")
		with self.assertRaisesRegex(ValueError, "within the expected period"):
			validate_entry(
				entry,
				[{"covered_from": "2026-08-31", "covered_through": "2026-09-03"}],
				"Expected",
				"",
			)

	def test_immutable_evidence_remains_valid_after_expected_dates_are_corrected(self):
		entry = {"status": "Reviewed", "expected_from": "2026-09-15", "expected_through": "2026-09-30"}
		proof = [{"covered_from": "2026-09-01", "covered_through": "2026-09-30"}]

		validate_entry(entry, proof, "Reviewed", "Corrected source activation date", check_evidence_within_period=False)
		with self.assertRaisesRegex(ValueError, "within the expected period"):
			validate_entry(entry, proof, "Reviewed", "Corrected source activation date")


if __name__ == "__main__":
	unittest.main()
