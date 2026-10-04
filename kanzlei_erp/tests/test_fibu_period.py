import unittest
from datetime import date

from kanzlei_erp.fibu_period import period_bounds, period_label


class TestFiBuPeriod(unittest.TestCase):
	def test_month_is_independent_of_work_date(self):
		self.assertEqual(period_bounds("Monthly", 2026, 9), (date(2026, 9, 1), date(2026, 9, 30)))

	def test_quarter_and_year(self):
		self.assertEqual(period_bounds("Quarterly", 2026, 3), (date(2026, 7, 1), date(2026, 9, 30)))
		self.assertEqual(period_bounds("Yearly", 2026, None), (date(2026, 1, 1), date(2026, 12, 31)))

	def test_leap_february_and_invalid_numbers(self):
		self.assertEqual(period_bounds("Monthly", 2028, 2), (date(2028, 2, 1), date(2028, 2, 29)))
		for args in (("Monthly", 2026, 13), ("Quarterly", 2026, 0), ("Yearly", 2026, 1)):
			with self.assertRaises(ValueError):
				period_bounds(*args)

	def test_period_titles_are_unambiguous_in_german_interface(self):
		self.assertEqual(period_label("Monthly", 2026, 9), "09/2026")
		self.assertEqual(period_label("Quarterly", 2026, 3), "Q3/2026")
		self.assertEqual(period_label("Yearly", 2026, None), "2026")
