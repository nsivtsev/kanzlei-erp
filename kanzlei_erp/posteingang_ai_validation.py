"""Pure AP-04 validation. No Frappe, provider calls or remote schema retrieval."""

import copy
import json
import unicodedata
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

BASE = "https://posteingang.invalid/v1/"


class InvalidResult(ValueError):
	def __init__(self, code="INVALID_RESULT"):
		self.code = code
		super().__init__(code)


def parse_json(text):
	def pairs(items):
		result = {}
		for key, value in items:
			if key in result:
				raise InvalidResult()
			result[key] = value
		return result

	def constant(_):
		raise InvalidResult()

	try:
		value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
		# Escaped lone surrogates are accepted by Python's JSON decoder but cannot
		# safely be sent as UTF-8. Reject them before they reach any request frame.
		json.dumps(value, ensure_ascii=False).encode("utf-8")
	except (ValueError, TypeError, RecursionError):
		raise InvalidResult() from None
	if not isinstance(value, dict):
		raise InvalidResult()
	return value


@lru_cache(maxsize=3)
def _schema(name):
	if name not in ("common", "extraction", "support"):
		raise InvalidResult()
	return parse_json(
		files("kanzlei_erp").joinpath("posteingang_contracts", "v1", name + ".schema.json").read_text()
	)


def schema(name):
	return copy.deepcopy(_schema(name))


@lru_cache(maxsize=2)
def _validators(raw=False):
	schemas = {name: schema(name) for name in ("common", "extraction", "support")}
	if raw:
		evidence = schemas["common"]["$defs"]["evidence"]
		evidence["properties"].pop("verification")
		evidence["required"].remove("verification")
		evidence.pop("allOf")
	for name, value in schemas.items():
		value["$id"] = BASE + name + ".schema.json"
		Draft202012Validator.check_schema(value)
	registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas.values())
	return {
		name: Draft202012Validator(value, registry=registry, format_checker=FormatChecker())
		for name, value in schemas.items()
	}


def _check(value, validator):
	for error in validator.iter_errors(value):
		# maxItems=0 expresses a semantic prohibition (e.g. none_found), not a capacity limit.
		code = (
			"LIMIT_EXCEEDED"
			if error.validator in ("maxLength", "maxItems") and error.validator_value
			else "INVALID_RESULT"
		)
		raise InvalidResult(code)


def normalized(text):
	return " ".join(unicodedata.normalize("NFKC", text).split())


def validate_pages(pages, page_count):
	if type(page_count) is not int or not 1 <= page_count <= 50 or not isinstance(pages, list):
		raise InvalidResult("OCR_FAILED")
	if len(pages) != page_count:
		raise InvalidResult("OCR_FAILED")
	total = 0
	for number, page in enumerate(pages, 1):
		if (
			not isinstance(page, dict)
			or set(page) != {"page", "text"}
			or type(page["page"]) is not int
			or page["page"] != number
			or not isinstance(page["text"], str)
		):
			raise InvalidResult("OCR_FAILED")
		try:
			page["text"].encode("utf-8")
		except UnicodeError:
			raise InvalidResult("OCR_FAILED") from None
		total += len(page["text"])
		if len(page["text"]) > 100000 or total > 2000000:
			raise InvalidResult("LIMIT_EXCEEDED")
	return copy.deepcopy(pages)


def validate_ocr(value, page_count):
	if not isinstance(value, dict) or not isinstance(value.get("pages"), list):
		raise InvalidResult("OCR_FAILED")
	pages = value["pages"]
	if (
		len(pages) != page_count
		or any(not isinstance(p, dict) or type(p.get("index")) is not int for p in pages)
		or {p["index"] for p in pages} != set(range(page_count))
	):
		raise InvalidResult("OCR_FAILED")
	return validate_pages(
		[
			{"page": p["index"] + 1, "text": p.get("markdown")}
			for p in sorted(pages, key=lambda p: p["index"])
		],
		page_count,
	)


def validate_extraction(value, pages, *, max_actions):
	_check(value, _validators(raw=True)["extraction"])
	if len(value["actions"]) > max_actions:
		raise InvalidResult("LIMIT_EXCEEDED")
	pages = validate_pages(pages, len(pages))
	texts = {p["page"]: normalized(p["text"]) for p in pages}
	result = copy.deepcopy(value)

	def visit(node):
		if isinstance(node, dict):
			if "evidence" in node:
				for evidence in node["evidence"]:
					if evidence["page"] not in texts:
						raise InvalidResult()
					quote = evidence["quote"]
					if quote is None:
						evidence["verification"] = "missing"
					elif not normalized(quote):
						raise InvalidResult()
					else:
						evidence["verification"] = (
							"matched" if normalized(quote) in texts[evidence["page"]] else "unverified"
						)
			for child in node.values():
				visit(child)
		elif isinstance(node, list):
			for child in node:
				visit(child)

	visit(result)
	_check(result, _validators()["extraction"])
	return result


def without_verification(value):
	result = copy.deepcopy(value)

	def visit(node):
		if isinstance(node, dict):
			node.pop("verification", None)
			for child in node.values():
				visit(child)
		elif isinstance(node, list):
			for child in node:
				visit(child)

	visit(result)
	return result


def provider_schema():
	"""Structural grammar only; the complete raw/canonical validators remain authoritative."""
	root = copy.deepcopy(_validators(raw=True)["extraction"].schema)
	common = copy.deepcopy(_validators(raw=True)["common"].schema)

	def expand(node, current):
		if isinstance(node, list):
			return [expand(item, current) for item in node]
		if not isinstance(node, dict):
			return node
		if "$ref" in node:
			file, _, fragment = node["$ref"].partition("#")
			target = common if file == "common.schema.json" else current
			for part in fragment.lstrip("/").split("/"):
				target = target[part]
			return expand(target, common if file else current)
		return {
			key: expand(value, current)
			for key, value in node.items()
			if key not in ("$id", "$schema", "$defs", "allOf", "if", "then", "else")
		}

	return expand(root, root)
