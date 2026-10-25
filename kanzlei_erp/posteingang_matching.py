"""Current-permission matching, separate from AI facts and human selection."""

import copy
import re
import unicodedata
from hashlib import sha256

import frappe

from kanzlei_erp import posteingang_jobs as workflow
from kanzlei_erp.mandant_identifiers import identifier_key, readable_customers
from kanzlei_erp.posteingang import canonical
from kanzlei_erp.posteingang_ai_validation import (
	InvalidResult,
	normalized,
	parse_json,
	validate_extraction,
	validate_pages,
	without_verification,
)
from kanzlei_erp.posteingang_permissions import letter_access

EDITABLE = {"Received", "Needs Review", "Needs Clarification", "Error"}


def _boundary(operation, extra):
	workflow._http_boundary()
	cmd = extra.pop("cmd", None)
	if extra or cmd not in (None, "kanzlei_erp.posteingang_matching." + operation):
		workflow.fail("INVALID_REQUEST")


def _read_letter(name):
	if not workflow._text(name, 140):
		workflow.fail("INVALID_REQUEST")
	row = frappe.db.get_value("Incoming Letter", name, "*", as_dict=True)
	if not row or not workflow._operator(frappe.session.user) or not letter_access(row):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	return row


def _customer(name):
	label = frappe.db.get_value("Customer", name, "customer_name") if name else None
	if label is None or not frappe.has_permission("Customer", "read", doc=name):
		return None
	return {"customer": name, "customer_name": label, "can_select": bool(frappe.has_permission("Customer", "write", doc=name))}


def _source(letter):
	if not letter.current_run:
		return None
	run = frappe.db.get_value("Incoming Letter Run", letter.current_run, "*", as_dict=True)
	if not run or run.status != "Succeeded":
		return None
	try:
		if run.letter != letter.name or run.generation != letter.analysis_generation or run.schema_version != "1.0" or parse_json(run.source_snapshot_json) != workflow._snapshot(letter):
			raise InvalidResult()
		pages = validate_pages(parse_json(run.ocr_pages_json)["pages"], letter.page_count)
		return validate_extraction(without_verification(parse_json(run.result_json)), pages, max_actions=30)
	except (InvalidResult, KeyError, TypeError):
		workflow.fail("INVALID_RESULT")


def _name(text):
	return normalized(text or "").casefold()


def _evidence(value, evidence, kind=None):
	"""A real quoted value, not just any matched quote on the page."""
	proof = []
	for item in evidence:
		if item["verification"] != "matched":
			continue
		text = unicodedata.normalize("NFKC", item["quote"]).strip()
		if kind:
			needle = identifier_key(kind, value, "context", "context", "context")[1]
			if kind == "de_vat_id":
				text = text.upper()
			separator = r"[\s/]*" if kind == "de_steuernummer" else r"\s*"
			pattern = re.escape(needle) if kind == "other" else separator.join(re.escape(char) for char in needle)
			matched = re.search(r"(?<!\w)" + pattern + r"(?!\w)", text)
		else:
			matched = _name(value) and re.search(r"(?<!\w)" + re.escape(_name(value)) + r"(?!\w)", _name(text))
		if matched:
			proof.append(item)
	return proof


def _identifier(item):
	try:
		return identifier_key(item["type"], item["value"], item["jurisdiction"], item["authority"], item["other_type"])
	except workflow.WorkflowError:
		return None


def _address_score(customer, recipients):
	def words(text):
		return " ".join(re.findall(r"\w+", _name(text)))

	for link in frappe.get_all("Dynamic Link", filters={"parenttype": "Address", "link_doctype": "Customer", "link_name": customer}, fields=["parent"]):
		if not frappe.has_permission("Address", "read", doc=link.parent):
			continue
		address = frappe.db.get_value("Address", link.parent, ["address_line1", "pincode", "city", "disabled"], as_dict=True)
		if not address or address.disabled or not all((address.address_line1, address.pincode, address.city)):
			continue
		parts = [words(address[k]) for k in ("address_line1", "pincode", "city")]
		for recipient in recipients:
			fact = recipient["address"]
			if fact["value"] and _evidence(fact["value"], fact["evidence"]):
				text = " " + words(fact["value"]) + " "
				if all(" " + part + " " in text for part in parts):
					return 1
	return 0


def _recipients(extraction):
	by_identity = {}
	for recipient in extraction["recipients"]:
		def fact(item):
			return canonical({k: v for k, v in item.items() if k != "evidence"})

		identity = canonical({"name": _name(recipient["name"]["value"]), "address": _name(recipient["address"]["value"]), "identifiers": sorted(fact(item) for item in recipient["identifiers"])})
		if identity not in by_identity:
			by_identity[identity] = copy.deepcopy(recipient)
			continue
		existing = by_identity[identity]
		for field in ("name", "address"):
			existing[field]["evidence"].extend(recipient[field]["evidence"])
		identifiers = {fact(item): item for item in existing["identifiers"]}
		for item in recipient["identifiers"]:
			identifiers[fact(item)]["evidence"].extend(item["evidence"])
	return list(by_identity.values())


def _match(extraction):
	recipients = _recipients(extraction)
	identifiers, names = [], []
	for recipient in recipients:
		fact = recipient["name"]
		if fact["value"] and (proof := _evidence(fact["value"], fact["evidence"])):
			names.append((_name(fact["value"]), proof))
		for item in recipient["identifiers"]:
			key = _identifier(item)
			if item["holder_role"] == "addressee" and key and (proof := _evidence(item["value"], item["evidence"], item["type"])):
				identifiers.append((key, proof))
	candidates = []
	for name in readable_customers():
		customer = _customer(name)
		keys = set()
		for row in frappe.get_all("Mandant Identifier", filters={"parent": name, "parenttype": "Customer", "parentfield": "kanzlei_identifiers"}, fields=["*"]):
			if row.verified_by and row.verified_at and row.verification_source:
				key = identifier_key(row.identifier_type, row.raw_value, row.jurisdiction, row.authority, row.other_type)
				if key:
					keys.add(key)
		exact = {key for key, _ in identifiers if key in keys}
		name_proof = [proof for value, proof in names if value == _name(customer["customer_name"])]
		if not exact and not name_proof:
			continue
		reasons = [{"kind": "exact_identifier", "identifier_type": key[0], "evidence": proof} for key, proof in identifiers if key in exact]
		if name_proof:
			reasons.append({"kind": "name_address", "identifier_type": None, "evidence": name_proof[0]})
		conflicts = []
		for key, proof in identifiers:
			if key not in keys and any((key[0], *key[2:]) == (own[0], *own[2:]) for own in keys):
				conflicts.append({"code": "identifier_conflict", "identifier_type": key[0], "evidence": proof})
		if exact and names and not name_proof:
			conflicts.append({"code": "name_mismatch", "identifier_type": None, "evidence": names[0][1]})
		candidates.append({**customer, "reasons": reasons, "conflicts": conflicts, "_score": (len(exact), bool(name_proof), _address_score(name, recipients))})
	if any(c["_score"][0] for c in candidates):
		candidates = [c for c in candidates if c["_score"][0]]
	candidates.sort(key=lambda c: (*(-int(x) for x in c["_score"]), _name(c["customer_name"]), c["customer"]))
	ambiguity = "multiple_addressees" if len(recipients) > 1 else "multiple_matches" if len(candidates) > 1 else "none" if candidates else "not_found"
	return {"schema_version": "1.0", "candidates": [{k: v for k, v in c.items() if k != "_score"} for c in candidates[:3]], "ambiguity": ambiguity}


@frappe.whitelist(methods=["GET", "POST"])
def get_matching_state(letter=None, **extra):
	_boundary("get_matching_state", extra)
	doc = _read_letter(letter)
	return matching_projection(doc)


def matching_projection(doc):
	"""Current-permission projection without starting another transaction."""
	source = _source(doc)
	matching = _match(source) if source is not None else None
	selected = _customer(doc.selected_customer)
	allowed = bool(doc.status in EDITABLE and workflow._authorized(doc, frappe.session.user) and frappe.db.get_single_value("Posteingang Settings", "enabled"))
	operations = ["select_customer"] if allowed and (not matching or matching["ambiguity"] != "multiple_addressees") else []
	if allowed and selected:
		operations.append("clear_customer")
	return {"schema_version": "1.0", "letter": doc.name, "revision": doc.revision, "source_run": doc.current_run if source is not None else None, "matching": matching, "selected_customer": selected, "customer_reason": doc.customer_reason if selected else None, "allowed_operations": operations}


@frappe.whitelist(methods=["GET", "POST"])
def search_customers(letter=None, text="", start=0, page_length=20, **extra):
	_boundary("search_customers", extra)
	_read_letter(letter)
	if not isinstance(text, str) or (text and not workflow._text(text, 140)):
		workflow.fail("INVALID_REQUEST")
	for value in (start, page_length):
		if type(value) not in (int, str) or (isinstance(value, str) and (not value.isascii() or not value.isdecimal() or len(value) > 9)):
			workflow.fail("INVALID_REQUEST")
	start, page_length = int(start), int(page_length)
	if start < 0 or not 1 <= page_length <= 50:
		workflow.fail("INVALID_REQUEST")
	rows = []
	for name in readable_customers():
		customer = _customer(name)
		if _name(text) in _name(customer["customer_name"]) or _name(text) in _name(name):
			rows.append(customer)
	rows.sort(key=lambda c: (_name(c["customer_name"]), c["customer"]))
	return {"rows": rows[start:start + page_length], "has_next": len(rows) > start + page_length, "next_start": start + page_length if len(rows) > start + page_length else None}


@frappe.whitelist(methods=["POST"])
def select_customer(letter=None, customer=None, reason=None, expected_revision=None, request_key=None, **extra):
	_boundary("select_customer", extra)
	if not workflow._text(reason, 1000) or not workflow._text(request_key, 80) or (customer is not None and not workflow._text(customer, 140)):
		workflow.fail("INVALID_REQUEST")
	if isinstance(expected_revision, str) and expected_revision.isascii() and expected_revision.isdecimal() and len(expected_revision) <= 10:
		expected_revision = int(expected_revision)
	if type(expected_revision) is not int or not 1 <= expected_revision <= 2147483647:
		workflow.fail("INVALID_REQUEST")
	doc = workflow._locked_letter(letter)
	actor = frappe.session.user
	if not workflow._authorized(doc, actor) or (doc.selected_customer and not _customer(doc.selected_customer)) or (customer and not ((_customer(customer) or {}).get("can_select"))):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	key = sha256(canonical([actor, letter, "select_customer", request_key]).encode()).hexdigest()
	fingerprint = sha256(canonical({"schema_version": "1.0", "letter": letter, "customer": customer, "reason": reason}).encode()).hexdigest()
	old = frappe.db.get_value("Incoming Letter Event", {"operation_key": key}, ["payload_fingerprint", "result_json"], as_dict=True, for_update=True)
	if old:
		if old.payload_fingerprint != fingerprint:
			workflow.fail("IDEMPOTENCY_CONFLICT")
		return {**parse_json(old.result_json), "replayed": True}
	if expected_revision != doc.revision:
		workflow.fail("REVISION_CONFLICT")
	if doc.status not in EDITABLE or not frappe.db.get_single_value("Posteingang Settings", "enabled"):
		workflow.fail("INVALID_TRANSITION")
	source = _source(doc)
	if customer and source is not None and _match(source)["ambiguity"] == "multiple_addressees":
		workflow.fail("MULTIPLE_ADDRESSEES")
	return workflow._change(doc, actor, "select_customer", {"selected_customer": customer, "customer_reason": reason}, payload={"before_customer": doc.selected_customer, "customer": customer, "reason": reason}, receipt=(key, fingerprint))
