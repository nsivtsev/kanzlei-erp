"""Pure AP-07 review validation; identities and OCR are supplied by the server.

This module does not authorize mutations or select their source Run. The service
must load stored_actions and pages from the locked Letter and validated Run.
"""

import copy
import json
from functools import lru_cache

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from kanzlei_erp.posteingang_ai_validation import (
	BASE,
	InvalidResult,
	_check,
	normalized,
	parse_json,
	schema,
	validate_pages,
)

DOCUMENT_FIELDS = ("sender", "document_date", "letter_type", "summary")


@lru_cache(maxsize=1)
def _validators():
	common = schema("common")
	review = schema("review")
	common["$id"] = BASE + "common.schema.json"
	review["$id"] = BASE + "review.schema.json"

	def validator(common_schema):
		registry = Registry().with_resources(
			(value["$id"], Resource.from_contents(value)) for value in (common_schema, review)
		)
		return Draft202012Validator(review, registry=registry, format_checker=FormatChecker())

	# The model's evidence stamp is never authoritative. Check its shape first,
	# recompute it, then apply the unchanged canonical conditional constraints.
	raw_common = copy.deepcopy(common)
	raw_common["$defs"]["evidence"].pop("allOf")
	fields = {
		"type": "object",
		"properties": {
			field: {"type": ["string", "null"], "minLength": 1, "maxLength": limit}
			for field, limit in (("sender", 255), ("letter_type", 255), ("summary", 4000))
		},
		"required": list(DOCUMENT_FIELDS),
		"additionalProperties": False,
	}
	fields["properties"]["document_date"] = common["$defs"]["date"]
	return validator(raw_common), validator(common), Draft202012Validator(fields, format_checker=FormatChecker())


def _object(value):
	if isinstance(value, str):
		return parse_json(value)
	try:
		return parse_json(json.dumps(value, ensure_ascii=False, allow_nan=False))
	except (TypeError, ValueError, RecursionError):
		raise InvalidResult() from None


def _nonblank(node):
	if isinstance(node, str) and not node.strip():
		raise InvalidResult()
	if isinstance(node, dict):
		for child in node.values():
			_nonblank(child)
	elif isinstance(node, list):
		for child in node:
			_nonblank(child)


def _identities(actions, stored_actions):
	if not isinstance(stored_actions, (list, tuple)):
		raise InvalidResult()
	stored = {}
	indices = set()
	for row in stored_actions:
		identity, index = row.get("action_id"), row.get("source_index")
		if not isinstance(identity, str) or identity in stored:
			raise InvalidResult()
		if index is not None:
			if type(index) is not int or not 0 <= index <= 29 or index in indices:
				raise InvalidResult()
			indices.add(index)
		stored[identity] = index
	seen = set()
	for row in actions:
		identity = row["action_id"]
		if identity in seen or identity not in stored or row["source_index"] != stored[identity]:
			raise InvalidResult()
		seen.add(identity)
	if seen != set(stored):
		raise InvalidResult()


def _semantics(review, confirm):
	if (review["mode"] == "confirm") != confirm:
		raise InvalidResult()
	if review["mode"] == "clarification" and not review["clarification_reason"]:
		raise InvalidResult()
	for action in review["actions"]:
		if action["decision"] == "reject" and not action["rejection_reason"]:
			raise InvalidResult()
	if not confirm:
		return
	if not review["human_verified"] or not review["selected_customer"] or not review["customer_reason"]:
		raise InvalidResult()
	if any(row["decision"] == "pending" for row in review["actions"]):
		raise InvalidResult()
	accepted = [row for row in review["actions"] if row["decision"] == "accept"]
	if (not accepted and not review["no_actions_reason"]) or (accepted and review["no_actions_reason"] is not None):
		raise InvalidResult()
	for row in accepted:
		if row["assigned_to"] is None:
			if row["assignment_decision"] != "unassigned" or not row["assignment_reason"]:
				raise InvalidResult()
		elif row["assignment_decision"] != "assigned":
			raise InvalidResult()
		if row["internal_date"] is None:
			if row["scheduling_decision"] != "unscheduled" or not row["scheduling_reason"]:
				raise InvalidResult()
		elif row["scheduling_decision"] != "scheduled":
			raise InvalidResult()
		if row["external_date"] is not None and not row["external_date_basis"]:
			raise InvalidResult()
		if row["needs_planning"] != (row["assigned_to"] is None or row["internal_date"] is None):
			raise InvalidResult()


def validate_review(review, document_fields, *, stored_actions, pages, page_count, confirm=False):
	"""Return independent canonical (review, document_fields), or a safe error.

	None/empty pages means a manual review without saved OCR, never a verified
	quote. Full OCR, when available, must match the accepted PDF's page count.
	"""
	result, fields = _object(review), _object(document_fields)
	raw_validator, canonical_validator, fields_validator = _validators()
	_check(result, raw_validator)
	_check(fields, fields_validator)
	_nonblank(result)
	_nonblank(fields)
	if type(page_count) is not int or not 1 <= page_count <= 50:
		raise InvalidResult()
	if type(confirm) is not bool:
		raise InvalidResult()
	try:
		_identities(result["actions"], stored_actions)
	except (AttributeError, TypeError):
		raise InvalidResult() from None
	texts = {} if pages is None or pages == [] else {
		page["page"]: normalized(page["text"]) for page in validate_pages(pages, page_count)
	}
	for row in result["actions"]:
		for evidence in row["evidence"]:
			if type(evidence["page"]) is not int or evidence["page"] > page_count:
				raise InvalidResult()
			quote = evidence["quote"]
			evidence["verification"] = (
				"missing" if quote is None else
				"matched" if evidence["page"] in texts and normalized(quote) in texts[evidence["page"]] else
				"unverified"
			)
	_check(result, canonical_validator)
	_semantics(result, confirm)
	return result, fields
