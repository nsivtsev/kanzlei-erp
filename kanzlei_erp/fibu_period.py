"""Canonical calendar periods for FiBu work packages."""

from calendar import monthrange
from datetime import date


def period_bounds(period_type: str, year: int, number: int | None) -> tuple[date, date]:
	try:
		year = int(year)
		number = int(number) if number not in (None, "") else None
		if period_type == "Monthly" and number is not None and 1 <= number <= 12:
			first_month, last_month = number, number
		elif period_type == "Quarterly" and number is not None and 1 <= number <= 4:
			first_month, last_month = 3 * number - 2, 3 * number
		elif period_type == "Yearly" and number is None:
			first_month, last_month = 1, 12
		else:
			raise ValueError("Invalid FiBu period")
		return date(year, first_month, 1), date(year, last_month, monthrange(year, last_month)[1])
	except (TypeError, ValueError, OverflowError) as error:
		raise ValueError("Invalid FiBu period") from error


def period_label(period_type: str, year: int, number: int | None) -> str:
	"""Return a concise, language-independent title for a calendar period."""
	period_bounds(period_type, year, number)
	if period_type == "Monthly":
		return f"{int(number):02d}/{int(year)}"
	if period_type == "Quarterly":
		return f"Q{int(number)}/{int(year)}"
	return str(int(year))
