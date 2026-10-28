"""Tariff reservation for explicit synthetic evals; no HTTP, secrets or ERP imports.

Rates verified 2026-10-27: Medium3.5 input/output USD1.5/7.5 per million
and OCR4.1 USD4/1000 pages. This is a conservative meter, not actual billing.
"""

from decimal import Decimal, InvalidOperation


class BudgetError(ValueError):
	def __init__(self):
		super().__init__("Evaluation budget refused")


def _money(value):
	try:
		amount = Decimal(str(value))
	except (InvalidOperation, ValueError):
		raise BudgetError() from None
	if not amount.is_finite() or amount < 0:
		raise BudgetError()
	return amount


def _integer(value, maximum):
	if type(value) is not int or not 0 <= value <= maximum:
		raise BudgetError()
	return value


def new_ledger(limit_usd, max_requests):
	limit = _money(limit_usd)
	if not limit or not _integer(max_requests, 60):
		raise BudgetError()
	return {"schema_version": "1.0", "limit_usd": str(limit), "spent_usd": "0", "max_requests": max_requests, "requests": 0, "pending_usd": None, "pending_stage": None}


def reserve(ledger, stage, *, request_bytes=0, max_tokens=0, page_count=0):
	if ledger["pending_usd"] is not None or ledger["requests"] >= ledger["max_requests"]:
		raise BudgetError()
	if stage == "ocr":
		if not _integer(page_count, 50):
			raise BudgetError()
		amount = Decimal(page_count) * Decimal("0.004")
	elif stage == "extraction":
		if not _integer(request_bytes, 192000) or not _integer(max_tokens, 32768):
			raise BudgetError()
		# One token per UTF-8 byte plus explicit protocol margin overestimates
		# input usage. Reserve the full output cap, never the expected answer.
		amount = (Decimal(request_bytes + 4096) * Decimal("1.5") + Decimal(max_tokens) * Decimal("7.5")) / 1000000
	else:
		raise BudgetError()
	if _money(ledger["spent_usd"]) + amount > _money(ledger["limit_usd"]):
		raise BudgetError()
	return {**ledger, "requests": ledger["requests"] + 1, "pending_usd": str(amount), "pending_stage": stage}


def settle(ledger, usage):
	if ledger["pending_usd"] is None:
		raise BudgetError()
	amount = _money(ledger["pending_usd"])
	if isinstance(usage, dict):
		if ledger["pending_stage"] == "extraction" and {"prompt_tokens", "completion_tokens"} <= usage.keys():
			prompt = _integer(usage["prompt_tokens"], 10000000)
			completion = _integer(usage["completion_tokens"], 10000000)
			amount = (Decimal(prompt) * Decimal("1.5") + Decimal(completion) * Decimal("7.5")) / 1000000
		elif ledger["pending_stage"] == "ocr" and "pages_processed" in usage:
			amount = max(amount, Decimal(_integer(usage["pages_processed"], 50)) * Decimal("0.004"))
	return {**ledger, "spent_usd": str(_money(ledger["spent_usd"]) + amount), "pending_usd": None, "pending_stage": None}
