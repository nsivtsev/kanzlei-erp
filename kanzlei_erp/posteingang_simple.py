"""One-click Posteingang helpers: internal working date for a proposed task."""

from datetime import date, timedelta

import frappe

WORKING_DAYS_BEFORE_DEADLINE = 5
DAYS_WITHOUT_DEADLINE = 7


def working_date(deadline: date | None, today: date, holidays: frozenset[date]) -> date:
	"""Deadline minus 5 working days (Mon-Fri, not a holiday), never before today.

	Without a deadline the date is today plus 7 days.
	"""
	if deadline is None:
		return today + timedelta(days=DAYS_WITHOUT_DEADLINE)
	result = deadline
	remaining = WORKING_DAYS_BEFORE_DEADLINE
	while remaining:
		result -= timedelta(days=1)
		if result.weekday() < 5 and result not in holidays:
			remaining -= 1
	return max(result, today)


def company_holidays() -> frozenset[date]:
	"""Holiday dates of the default company's holiday list; empty if any link is missing."""
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	holiday_list = company and frappe.db.get_value("Company", company, "default_holiday_list")
	if not holiday_list:
		return frozenset()
	dates = frappe.get_all("Holiday", filters={"parent": holiday_list}, pluck="holiday_date")
	return frozenset(frappe.utils.getdate(value) for value in dates)
