"""Date calculation for recurring Kanzlei work."""

from calendar import monthrange
from datetime import date, timedelta


def _add_months(first_date: date, months: int) -> date:
	month_index = first_date.year * 12 + first_date.month - 1 + months
	year, month_zero_based = divmod(month_index, 12)
	month = month_zero_based + 1
	last_day = monthrange(year, month)[1]
	first_is_month_end = first_date.day == monthrange(first_date.year, first_date.month)[1]
	day = last_day if first_is_month_end else min(first_date.day, last_day)
	return date(year, month, day)


def occurrence_dates(
	first_due_date: date,
	frequency: str,
	window_start: date,
	window_end: date,
	last_due_date: date | None = None,
) -> list[date]:
	"""Return due dates within an inclusive window, anchored to the first date."""
	if frequency not in {"Weekly", "Monthly", "Quarterly", "Yearly"}:
		raise ValueError(f"Unsupported frequency: {frequency}")

	result = []
	index = 0
	while True:
		if frequency == "Weekly":
			current = first_due_date + timedelta(weeks=index)
		else:
			months = {"Monthly": 1, "Quarterly": 3, "Yearly": 12}[frequency] * index
			current = _add_months(first_due_date, months)
		if current > window_end or (last_due_date and current > last_due_date):
			break
		if current >= window_start:
			result.append(current)
		index += 1
	return result
