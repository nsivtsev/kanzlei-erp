"""Working-date rule and company holiday lookup for the one-click Posteingang."""

import unittest
from datetime import date

import frappe
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp.posteingang_simple import company_holidays, working_date


class TestWorkingDate(unittest.TestCase):
	def test_five_working_days_before_deadline(self):
		self.assertEqual(working_date(date(2026, 12, 15), date(2026, 11, 26), frozenset()), date(2026, 12, 8))

	def test_skips_weekend_and_holidays(self):
		self.assertEqual(working_date(date(2027, 1, 15), date(2026, 11, 26), frozenset()), date(2027, 1, 8))
		self.assertEqual(
			working_date(date(2027, 1, 15), date(2026, 11, 26), frozenset({date(2027, 1, 12)})),
			date(2027, 1, 7),
		)

	def test_never_before_today(self):
		self.assertEqual(working_date(date(2026, 11, 30), date(2026, 11, 26), frozenset()), date(2026, 11, 26))
		self.assertEqual(working_date(date(2026, 11, 1), date(2026, 11, 26), frozenset()), date(2026, 11, 26))

	def test_no_deadline_is_one_week(self):
		self.assertEqual(working_date(None, date(2026, 11, 26), frozenset()), date(2026, 12, 3))


class TestCompanyHolidays(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.previous_default_company = frappe.db.get_single_value("Global Defaults", "default_company")

	def tearDown(self):
		frappe.db.set_single_value("Global Defaults", "default_company", self.previous_default_company)
		frappe.db.rollback()

	def test_reads_holidays_of_default_company(self):
		holiday_list = frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": "Posteingang Simple Test",
				"from_date": "2027-01-01",
				"to_date": "2027-12-31",
				"holidays": [{"holiday_date": "2027-01-12", "description": "Test"}],
			}
		).insert()
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "Posteingang Simple Test GmbH",
				"abbr": "PST",
				"default_currency": "EUR",
				"country": "Germany",
				"default_holiday_list": holiday_list.name,
			}
		)
		# Company.insert() builds warehouses and accounts a fresh site lacks; only the link is needed.
		company.db_insert()
		frappe.db.set_single_value("Global Defaults", "default_company", company.name)
		self.assertIn(date(2027, 1, 12), company_holidays())

	def test_empty_without_default_company(self):
		frappe.db.set_single_value("Global Defaults", "default_company", None)
		self.assertEqual(company_holidays(), frozenset())
