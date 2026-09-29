import unittest
from datetime import date

from kanzlei_erp.recurrence import occurrence_dates


class TestOccurrenceDates(unittest.TestCase):
    def test_month_end_stays_at_month_end(self):
        self.assertEqual(
            occurrence_dates(date(2026, 1, 31), "Monthly", date(2026, 1, 1), date(2026, 4, 30)),
            [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)],
        )

    def test_non_month_end_restores_original_day_after_short_month(self):
        self.assertEqual(
            occurrence_dates(date(2026, 1, 30), "Monthly", date(2026, 2, 1), date(2026, 3, 31)),
            [date(2026, 2, 28), date(2026, 3, 30)],
        )

    def test_quarterly_and_last_due_date_are_inclusive(self):
        self.assertEqual(
            occurrence_dates(
                date(2026, 11, 30),
                "Quarterly",
                date(2026, 11, 1),
                date(2027, 9, 1),
                date(2027, 5, 31),
            ),
            [date(2026, 11, 30), date(2027, 2, 28), date(2027, 5, 31)],
        )

    def test_yearly_leap_day_returns_on_next_leap_year(self):
        self.assertEqual(
            occurrence_dates(date(2024, 2, 29), "Yearly", date(2025, 1, 1), date(2028, 12, 31)),
            [date(2025, 2, 28), date(2026, 2, 28), date(2027, 2, 28), date(2028, 2, 29)],
        )

    def test_weekly_obeys_window(self):
        self.assertEqual(
            occurrence_dates(date(2026, 9, 1), "Weekly", date(2026, 9, 8), date(2026, 9, 22)),
            [date(2026, 9, 8), date(2026, 9, 15), date(2026, 9, 22)],
        )
