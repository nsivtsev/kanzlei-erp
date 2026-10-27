"""No request may start without reserving its conservative maximum tariff."""

import importlib
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


class EvaluationBudgetTests(unittest.TestCase):
	def setUp(self):
		self.b = importlib.import_module("posteingang_evaluation_budget")

	def test_reserves_before_call_and_settles_reported_tokens(self):
		ledger = self.b.new_ledger("1.00", 60)
		reserved = self.b.reserve(ledger, "extraction", request_bytes=10000, max_tokens=32768)
		self.assertEqual(ledger["requests"], 0)
		self.assertGreater(Decimal(reserved["pending_usd"]), Decimal("0.24"))
		with self.assertRaises(self.b.BudgetError):
			self.b.reserve(reserved, "ocr", page_count=1)
		settled = self.b.settle(reserved, {"prompt_tokens": 1000, "completion_tokens": 1000})
		self.assertEqual(Decimal(settled["spent_usd"]), Decimal("0.009"))
		self.assertIsNone(settled["pending_usd"])

	def test_unknown_outcome_keeps_maximum_charge_and_cannot_overspend(self):
		ledger = self.b.new_ledger("0.30", 2)
		ledger = self.b.reserve(ledger, "extraction", request_bytes=10000, max_tokens=32768)
		reserved = Decimal(ledger["pending_usd"])
		ledger = self.b.settle(ledger, None)
		self.assertEqual(Decimal(ledger["spent_usd"]), reserved)
		with self.assertRaises(self.b.BudgetError):
			self.b.reserve(ledger, "extraction", request_bytes=10000, max_tokens=32768)

	def test_ocr_is_charged_by_all_pages_and_request_cap_is_enforced(self):
		ledger = self.b.new_ledger("1", 1)
		ledger = self.b.reserve(ledger, "ocr", page_count=3)
		ledger = self.b.settle(ledger, {"pages_processed": 3})
		self.assertEqual(Decimal(ledger["spent_usd"]), Decimal("0.012"))
		with self.assertRaises(self.b.BudgetError):
			self.b.reserve(ledger, "ocr", page_count=1)

	def test_ocr_does_not_release_known_page_cost_on_incomplete_usage(self):
		ledger = self.b.reserve(self.b.new_ledger("1", 60), "ocr", page_count=3)
		self.assertEqual(Decimal(self.b.settle(ledger, {"pages_processed": 0})["spent_usd"]), Decimal("0.012"))

	def test_invalid_usage_and_nonfinite_budget_are_refused(self):
		for amount in ("NaN", "Infinity", "-1", "0"):
			with self.assertRaises(self.b.BudgetError):
				self.b.new_ledger(amount, 60)
		for tokens in (True, -1, "100"):
			ledger = self.b.reserve(self.b.new_ledger("1", 60), "extraction", request_bytes=10000, max_tokens=32768)
			with self.assertRaises(self.b.BudgetError):
				self.b.settle(ledger, {"prompt_tokens": tokens, "completion_tokens": 1})


if __name__ == "__main__":
	unittest.main()
