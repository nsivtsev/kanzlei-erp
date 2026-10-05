"""Browse private documents already linked to a Mandant."""

import hashlib
import json
import mimetypes
import re
import tempfile
import unicodedata
import zipfile
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import frappe

DOCUMENT_PAGE_SIZE = 50
DOCUMENT_MAX_PAGE_SIZE = 100
DOCUMENT_CANDIDATE_BATCH_SIZE = 200
DOCUMENT_MAX_ZIP_FILES = 100
DOCUMENT_MAX_ZIP_BYTES = 100 * 1024 * 1024
DOCUMENT_SEARCH_MAX_LENGTH = 200

_FILTER_DEFAULTS = {
	"query": "",
	"source": "all",
	"direction": "all",
	"file_group": "all",
	"date_from": None,
	"date_to": None,
	"package": None,
	"sort": "newest",
}
_FILTER_VALUES = {
	"source": {"all", "mandant", "email", "fibu", "task", "question"},
	"direction": {"all", "received", "sent"},
	"file_group": {"all", "pdf", "image", "spreadsheet", "document", "xml", "archive", "other"},
	"sort": {"newest", "oldest"},
}
_FILE_GROUPS = {
	"pdf": {"pdf"},
	"image": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "svg"},
	"spreadsheet": {"xls", "xlsx", "ods", "csv"},
	"document": {"doc", "docx", "odt", "rtf", "txt"},
	"xml": {"xml"},
	"archive": {"zip", "7z", "rar", "tar", "gz"},
}
_WINDOWS_RESERVED_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
_INLINE_MIME_TYPES = {
	"pdf": "application/pdf",
	"jpg": "image/jpeg",
	"jpeg": "image/jpeg",
	"png": "image/png",
	"gif": "image/gif",
	"webp": "image/webp",
}


def _invalid_filters(message):
	frappe.throw(message, frappe.ValidationError)


def _file_group(file_name):
	extension = PurePosixPath(str(file_name or "").replace("\\", "/")).suffix.lstrip(".").casefold()
	for group, extensions in _FILE_GROUPS.items():
		if extension in extensions:
			return group
	return "other"


def _normalize_filters(filters=None):
	if isinstance(filters, str):
		try:
			filters = frappe.parse_json(filters)
		except (ValueError, TypeError):
			_invalid_filters(frappe._("Document filters must be valid JSON"))
	if filters is None:
		filters = {}
	if not isinstance(filters, dict):
		_invalid_filters(frappe._("Document filters must be an object"))
	unknown = set(filters) - set(_FILTER_DEFAULTS)
	if unknown:
		_invalid_filters(frappe._("Unknown document filter: {0}").format(sorted(unknown)[0]))
	result = {**_FILTER_DEFAULTS, **filters}
	result["query"] = str(result["query"] or "").strip()
	if len(result["query"]) > DOCUMENT_SEARCH_MAX_LENGTH:
		_invalid_filters(frappe._("Document search can contain at most {0} characters").format(DOCUMENT_SEARCH_MAX_LENGTH))
	for field, allowed in _FILTER_VALUES.items():
		value = str(result[field] or "all").casefold()
		if value not in allowed:
			_invalid_filters(frappe._("Invalid value for document filter {0}").format(field))
		result[field] = value
	if result["source"] != "email" and result["direction"] != "all":
		_invalid_filters(frappe._("Email direction can only be filtered for email documents"))
	for field in ("date_from", "date_to"):
		value = result[field]
		if not value:
			result[field] = None
			continue
		try:
			parsed = date.fromisoformat(str(value))
		except ValueError:
			_invalid_filters(frappe._("Document dates must use YYYY-MM-DD format"))
		if parsed.isoformat() != str(value):
			_invalid_filters(frappe._("Document dates must use YYYY-MM-DD format"))
		result[field] = parsed.isoformat()
	if result["date_from"] and result["date_to"] and result["date_from"] > result["date_to"]:
		_invalid_filters(frappe._("Start date must not be after end date"))
	result["package"] = str(result["package"]).strip() if result["package"] else None
	return result


def _safe_archive_names(file_names):
	"""Return flat, unique ZIP names while preserving safe readable file names."""
	seen = set()
	output = []
	for file_name in file_names:
		name = unicodedata.normalize("NFC", str(file_name or "").replace("\\", "/").split("/")[-1])
		name = "".join(character for character in name if ord(character) >= 32 and ord(character) != 127)
		name = name.strip().rstrip(".") or "document"
		if name in {".", ".."}:
			name = "document"
		if _WINDOWS_RESERVED_NAME.match(name):
			name = f"_{name}"
		encoded = name.encode("utf-8")
		if len(encoded) > 240:
			suffix = PurePosixPath(name).suffix
			budget = max(1, 240 - len(suffix.encode("utf-8")))
			stem = PurePosixPath(name).stem
			while len(stem.encode("utf-8")) > budget:
				stem = stem[:-1]
			name = f"{stem}{suffix}"
		base = PurePosixPath(name).stem
		suffix = PurePosixPath(name).suffix
		candidate = name
		number = 2
		while candidate.casefold() in seen:
			candidate = f"{base} ({number}){suffix}"
			number += 1
		seen.add(candidate.casefold())
		output.append(candidate)
	return output


def _chunks(values, size=DOCUMENT_CANDIDATE_BATCH_SIZE):
	values = list(values)
	for offset in range(0, len(values), size):
		yield values[offset : offset + size]


def _readable(doctype, name, permission_cache=None):
	if not name:
		return False
	key = (doctype, name)
	if permission_cache is not None and key in permission_cache:
		return permission_cache[key]
	if not frappe.db.exists(doctype, name):
		return False
	try:
		result = bool(frappe.has_permission(doctype, ptype="read", doc=name))
	except (frappe.DoesNotExistError, frappe.PermissionError):
		result = False
	if permission_cache is not None:
		permission_cache[key] = result
	return result


def _require_customer(customer):
	if not customer:
		frappe.throw(frappe._("Client is required"), frappe.ValidationError)
	frappe.has_permission("Customer", ptype="read", doc=customer, throw=True)
	return frappe.get_doc("Customer", customer)


def _get_list(doctype, filters, fields):
	return frappe.get_list(doctype, filters=filters, fields=fields, limit=0)


def _documents_by_names(doctype, names, fields):
	output = {}
	for chunk in _chunks(sorted(set(filter(None, names)))):
		for doc in _get_list(doctype, {"name": ["in", chunk]}, fields):
			output[doc.name] = doc
	return output


def _append_context(file_contexts, file_name, context):
	if not file_name:
		return
	key = (context["source"], context["doctype"], context["name"])
	file_contexts.setdefault(file_name, {})[key] = context


def _customer_context(customer):
	return frappe._dict(
		{
			"source": "mandant",
			"doctype": "Customer",
			"name": customer.name,
			"title": customer.customer_name,
			"direction": None,
			"sender": None,
			"recipients": None,
			"package": None,
			"package_title": None,
		}
	)


def _package_context(package):
	return frappe._dict(
		{
			"source": "fibu",
			"doctype": "FiBu Package",
			"name": package.name,
			"title": package.display_title,
			"direction": None,
			"sender": None,
			"recipients": None,
			"package": package.name,
			"package_title": package.display_title,
		}
	)


def _supplement_context(supplement, package):
	return frappe._dict(
		{
			"source": "fibu",
			"doctype": "FiBu Supplement",
			"name": supplement.name,
			"title": supplement.display_title,
			"direction": None,
			"sender": None,
			"recipients": None,
			"package": package.name,
			"package_title": package.display_title,
		}
	)


def _task_context(task, package=None):
	question = task.get("kanzlei_work_kind") == "Question"
	return frappe._dict(
		{
			"source": "question" if question else "task",
			"doctype": "Task",
			"name": task.name,
			"title": task.subject,
			"direction": None,
			"sender": None,
			"recipients": None,
			"package": package.name if package else None,
			"package_title": package.display_title if package else None,
		}
	)


def _communication_context(communication, package=None):
	direction = str(communication.sent_or_received or "").casefold()
	return frappe._dict(
		{
			"source": "email",
			"doctype": "Communication",
			"name": communication.name,
			"title": communication.subject,
			"direction": direction,
			"sender": communication.sender if direction == "received" else None,
			"recipients": communication.recipients if direction == "sent" else None,
			"package": package.name if package else None,
			"package_title": package.display_title if package else None,
		}
	)


def _mail_context_matches(communication, customer, related_names):
	if communication.reference_doctype == "Customer" and communication.reference_name == customer:
		return True
	if (communication.reference_doctype, communication.reference_name) in related_names:
		return True
	return ("Customer", customer) in related_names


def _collect_documents(customer, filters, permission_cache):
	"""Build the explicitly supported Customer, mail, FiBu, and Task relation graph."""
	file_contexts = {}
	package_docs = _documents_by_names(
		"FiBu Package",
		[row.name for row in _get_list("FiBu Package", {"customer": customer.name}, ["name"])],
		["name", "customer", "display_title"],
	)
	if filters["package"] and filters["package"] not in package_docs:
		frappe.throw(frappe._("The selected accounting package is not available for this client"), frappe.PermissionError)
	if filters["package"]:
		package_docs = {filters["package"]: package_docs[filters["package"]]}
	package_names = set(package_docs)
	supplement_docs = _documents_by_names(
		"FiBu Supplement",
		[
			row.name
			for chunk in _chunks(package_names)
			for row in _get_list("FiBu Supplement", {"package": ["in", chunk]}, ["name"])
		],
		["name", "package", "display_title"],
	)
	if filters["package"]:
		supplement_docs = {name: doc for name, doc in supplement_docs.items() if doc.package == filters["package"]}

	task_names = set()
	task_filters = [{"kanzlei_customer": customer.name}]
	if package_names:
		task_filters.append({"kanzlei_fibu_package": ["in", sorted(package_names)]})
	if supplement_docs:
		task_filters.append({"kanzlei_fibu_supplement": ["in", sorted(supplement_docs)]})
	for task_filter in task_filters:
		task_names.update(
			row.name
			for row in _get_list(
				"Task",
				task_filter,
				["name", "subject", "kanzlei_customer", "kanzlei_work_kind", "kanzlei_fibu_package", "kanzlei_fibu_supplement"],
			)
		)
	task_docs = _documents_by_names(
		"Task",
		task_names,
		["name", "subject", "kanzlei_customer", "kanzlei_work_kind", "kanzlei_fibu_package", "kanzlei_fibu_supplement"],
	)

	customer_context = _customer_context(customer)
	package_contexts = {name: _package_context(doc) for name, doc in package_docs.items()}
	supplement_contexts = {
		name: _supplement_context(doc, package_docs[doc.package])
		for name, doc in supplement_docs.items()
		if doc.package in package_docs
	}
	task_contexts = {}
	task_package_contexts = {}
	task_supplement_contexts = {}
	for task_name, task in task_docs.items():
		supplement = supplement_docs.get(task.kanzlei_fibu_supplement)
		package = package_docs.get(task.kanzlei_fibu_package) or package_docs.get(supplement.package if supplement else None)
		task_contexts[task_name] = _task_context(task, package)
		if package:
			task_package_contexts[task_name] = package_contexts[package.name]
		if supplement:
			task_supplement_contexts[task_name] = supplement_contexts[supplement.name]

	for file in frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Customer", "attached_to_name": customer.name, "is_folder": 0},
			fields=["name", "attached_to_field"],
		):
		if file.attached_to_field != "image":
			_append_context(file_contexts, file.name, customer_context)

	for doctype, contexts in (
		("FiBu Package", package_contexts),
		("FiBu Supplement", supplement_contexts),
		("Task", task_contexts),
	):
		for chunk in _chunks(contexts):
			for file in frappe.get_all(
				"File",
				filters={"attached_to_doctype": doctype, "attached_to_name": ["in", chunk], "is_folder": 0},
				fields=["name", "attached_to_name"],
			):
				_append_context(file_contexts, file.name, contexts[file.attached_to_name])
				if doctype == "Task":
					if file.attached_to_name in task_package_contexts:
						_append_context(file_contexts, file.name, task_package_contexts[file.attached_to_name])
					if file.attached_to_name in task_supplement_contexts:
						_append_context(file_contexts, file.name, task_supplement_contexts[file.attached_to_name])

	communication_names = set()
	include_communication_files = set()
	fibu_attachment_communications = set()
	communication_contexts = {}
	communication_packages = {}
	fibu_file_links = []
	fibu_transfer_entries = []
	for doctype, contexts in (("FiBu Package", package_contexts), ("FiBu Supplement", supplement_contexts)):
		for chunk in _chunks(contexts):
			for row in frappe.get_all(
				"FiBu File Link",
				filters={"parent": ["in", chunk], "parenttype": doctype, "parentfield": "files"},
				fields=["parent", "file", "source_communication"],
			):
				fibu_file_links.append((doctype, row.parent, row.file, row.source_communication))
				if row.source_communication:
					communication_names.add(row.source_communication)
					fibu_attachment_communications.add(row.source_communication)
					communication_contexts.setdefault(row.source_communication, set()).add((doctype, row.parent))
					communication_packages.setdefault(row.source_communication, set()).add(contexts[row.parent].package)
			for row in frappe.get_all(
				"FiBu Transfer Entry",
				filters={"parent": ["in", chunk], "parenttype": doctype, "parentfield": "transfers"},
				fields=["parent", "evidence_file", "evidence_communication"],
			):
				fibu_transfer_entries.append((doctype, row.parent, row.evidence_file, row.evidence_communication))
				if row.evidence_communication:
					communication_names.add(row.evidence_communication)
					include_communication_files.add(row.evidence_communication)
					fibu_attachment_communications.add(row.evidence_communication)
					communication_contexts.setdefault(row.evidence_communication, set()).add((doctype, row.parent))
					communication_packages.setdefault(row.evidence_communication, set()).add(contexts[row.parent].package)
	for doctype, names in (
		("Customer", {customer.name}),
		("FiBu Package", set(package_docs)),
		("FiBu Supplement", set(supplement_docs)),
		("Task", set(task_docs)),
	):
		if not names:
			continue
		for row in frappe.get_all(
			"Communication Link",
			filters={"link_doctype": doctype, "link_name": ["in", sorted(names)] if names else ["=", ""]},
			fields=["parent", "parenttype", "parentfield", "link_name"],
		):
			if row.parenttype == "Communication" and row.parentfield == "timeline_links":
				communication_names.add(row.parent)
				include_communication_files.add(row.parent)
				communication_contexts.setdefault(row.parent, set()).add((doctype, row.link_name))
				if doctype == "FiBu Package":
					communication_packages.setdefault(row.parent, set()).add(row.link_name)
				elif doctype == "FiBu Supplement":
					communication_packages.setdefault(row.parent, set()).add(supplement_docs[row.link_name].package)
				elif doctype == "Task" and task_docs[row.link_name].kanzlei_fibu_package:
					communication_packages.setdefault(row.parent, set()).add(task_docs[row.link_name].kanzlei_fibu_package)

	for doctype, names in (
		("Customer", {customer.name}),
		("FiBu Package", set(package_docs)),
		("FiBu Supplement", set(supplement_docs)),
		("Task", set(task_docs)),
	):
		if not names:
			continue
		child_doctype = "FiBu Message Link"
		for chunk in _chunks(names):
			for row in frappe.get_all(
				"Communication",
				filters={
					"reference_doctype": doctype,
					"reference_name": ["in", chunk],
					"communication_medium": "Email",
					"communication_type": "Communication",
				},
				fields=["name", "reference_name"],
			):
				communication_names.add(row.name)
				include_communication_files.add(row.name)
				communication_contexts.setdefault(row.name, set()).add((doctype, row.reference_name))
				if doctype == "FiBu Package":
					communication_packages.setdefault(row.name, set()).add(row.reference_name)
				elif doctype == "FiBu Supplement":
					package_name = supplement_docs[row.reference_name].package
					communication_packages.setdefault(row.name, set()).add(package_name)
				elif doctype == "Task":
					task = task_docs.get(row.reference_name)
					if task:
						package = package_docs.get(task.kanzlei_fibu_package)
						if not package:
							supplement = supplement_docs.get(task.kanzlei_fibu_supplement)
							package = package_docs.get(supplement.package) if supplement else None
						if package:
							communication_packages.setdefault(row.name, set()).add(package.name)

	for doctype, names in (("FiBu Package", set(package_docs)), ("FiBu Supplement", set(supplement_docs))):
		if not names:
			continue
		for chunk in _chunks(names):
			for row in frappe.get_all(
				child_doctype,
				filters={"parent": ["in", chunk], "parenttype": doctype, "parentfield": "messages"},
				fields=["parent", "communication"],
			):
				communication_names.add(row.communication)
				include_communication_files.add(row.communication)
				fibu_attachment_communications.add(row.communication)
				communication_contexts.setdefault(row.communication, set()).add((doctype, row.parent))
				communication_packages.setdefault(row.communication, set()).add(
					row.parent if doctype == "FiBu Package" else supplement_docs[row.parent].package
				)

	for communication_name, package_names_for_mail in list(communication_packages.items()):
		for package_name in package_names_for_mail:
			if package_name in package_contexts:
				communication_contexts.setdefault(communication_name, set()).add(("FiBu Package", package_name))

	communication_docs = {}
	for chunk in _chunks(communication_names):
		for communication in frappe.get_all(
			"Communication",
			filters={"name": ["in", chunk]},
			fields=[
				"name", "subject", "sender", "recipients", "sent_or_received", "communication_medium",
				"communication_type", "communication_date", "creation", "reference_doctype", "reference_name",
			],
		):
			communication_docs[communication.name] = communication
	valid_communications = {}
	mail_file_contexts = {}
	for name, communication in communication_docs.items():
		if (
			communication.communication_medium != "Email"
			or communication.communication_type != "Communication"
			or not _readable("Communication", name, permission_cache)
		):
			continue
		related = communication_contexts.get(name, set())
		if not _mail_context_matches(communication, customer.name, related) and name not in fibu_attachment_communications:
			continue
		valid_communications[name] = communication
		package_names_for_mail = communication_packages.get(name, set())
		package_name = next((package for package in sorted(package_names_for_mail) if package in package_docs), None)
		context = _communication_context(communication, package_docs.get(package_name))
		communication_contexts_for_file = {("email", "Communication", name): context}
		for doctype, related_name in related:
			if doctype == "FiBu Package" and related_name in package_contexts:
				communication_contexts_for_file[("fibu", "FiBu Package", related_name)] = package_contexts[related_name]
			elif doctype == "FiBu Supplement" and related_name in supplement_contexts:
				communication_contexts_for_file[("fibu", "FiBu Supplement", related_name)] = supplement_contexts[related_name]
			elif doctype == "Task" and related_name in task_contexts:
				communication_contexts_for_file[(task_contexts[related_name].source, "Task", related_name)] = task_contexts[related_name]
				if related_name in task_package_contexts:
					package_context = task_package_contexts[related_name]
					communication_contexts_for_file[("fibu", "FiBu Package", package_context.name)] = package_context
				if related_name in task_supplement_contexts:
					supplement_context = task_supplement_contexts[related_name]
					communication_contexts_for_file[("fibu", "FiBu Supplement", supplement_context.name)] = supplement_context
		if name in include_communication_files:
			mail_file_contexts[name] = communication_contexts_for_file

	for chunk in _chunks(mail_file_contexts):
		for file in frappe.get_all(
				"File",
				filters={"attached_to_doctype": "Communication", "attached_to_name": ["in", chunk], "is_folder": 0},
				fields=["name", "attached_to_name"],
			):
			for file_context in mail_file_contexts[file.attached_to_name].values():
				_append_context(file_contexts, file.name, file_context)

	# FiBu file and transfer references are explicit associations; add only the named file.
	for doctype, parent, file_name, source_communication in fibu_file_links:
		context = package_contexts[parent] if doctype == "FiBu Package" else supplement_contexts[parent]
		_append_context(file_contexts, file_name, context)
		if source_communication in valid_communications:
			_append_context(
				file_contexts,
				file_name,
				_communication_context(valid_communications[source_communication], package_docs.get(context.package)),
			)
		elif source_communication in communication_docs:
			communication = communication_docs[source_communication]
			if (
				communication.communication_medium == "Email"
				and communication.communication_type == "Communication"
				and _readable("Communication", source_communication, permission_cache)
			):
				_append_context(
					file_contexts,
					file_name,
					_communication_context(communication, package_docs.get(context.package)),
				)

	for doctype, parent, evidence_file, evidence_communication in fibu_transfer_entries:
		context = package_contexts[parent] if doctype == "FiBu Package" else supplement_contexts[parent]
		_append_context(file_contexts, evidence_file, context)
		if evidence_communication in valid_communications:
			communication = valid_communications[evidence_communication]
			comm_context = _communication_context(communication, package_docs.get(context.package))
			_append_context(file_contexts, evidence_file, comm_context)
			for file in frappe.get_all(
				"File",
				filters={"attached_to_doctype": "Communication", "attached_to_name": communication.name, "is_folder": 0},
				fields=["name"],
			):
				_append_context(file_contexts, file.name, context)
				_append_context(file_contexts, file.name, comm_context)

	return file_contexts, valid_communications, package_docs


def _cursor_fingerprint(customer, filters):
	serialized = json.dumps([customer, filters], sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(serialized.encode()).hexdigest()


def _decode_cursor(cursor, customer, filters):
	if not cursor:
		return None
	if not isinstance(cursor, str) or len(cursor) > 2048:
		frappe.throw(frappe._("This document list changed. Refresh it and try again."), frappe.ValidationError)
	try:
		encoded = cursor.encode()
		payload = json.loads(urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
		if payload["customer"] != customer or payload["filters"] != _cursor_fingerprint(customer, filters):
			raise ValueError
		if payload["sort"] != filters["sort"]:
			raise ValueError
		if not isinstance(payload["date"], str) or not isinstance(payload["name"], str):
			raise ValueError
		frappe.utils.get_datetime(payload["snapshot"])
		return payload
	except (Base64Error, ValueError, KeyError, TypeError, json.JSONDecodeError):
		frappe.throw(frappe._("This document list changed. Refresh it and try again."), frappe.ValidationError)


def _encode_cursor(customer, filters, last_date, last_name, snapshot):
	payload = {
		"customer": customer,
		"filters": _cursor_fingerprint(customer, filters),
		"sort": filters["sort"],
		"date": last_date,
		"name": last_name,
		"snapshot": snapshot,
	}
	return urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _external_url(file):
	if not file.is_remote_file:
		return None
	parsed = urlparse(file.file_url or "")
	if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
		return None
	return file.file_url


def _context_matches(contexts, filters):
	for context in contexts:
		if filters["source"] != "all" and context["source"] != filters["source"]:
			continue
		if filters["direction"] != "all" and context["direction"] != filters["direction"]:
			continue
		if filters["package"] and context["package"] != filters["package"]:
			continue
		return True
	return False


def _matches_query(file, contexts, query):
	if not query:
		return True
	values = [file.file_name or ""]
	for context in contexts:
		values.extend((context["title"] or "", context["sender"] or "", context["recipients"] or ""))
	needle = query.casefold()
	return any(needle in value.casefold() for value in values)


@frappe.whitelist()
def get_documents(customer, filters=None, cursor=None, page_length=DOCUMENT_PAGE_SIZE):
	"""Return a permission-checked page of files associated with one Mandant."""
	if frappe.session.user == "Guest":
		frappe.throw(frappe._("Please log in to view client documents"), frappe.PermissionError)
	customer_doc = _require_customer(customer)
	filters = _normalize_filters(filters)
	try:
		page_length = int(page_length)
	except (TypeError, ValueError):
		frappe.throw(frappe._("Page length must be between 1 and {0}").format(DOCUMENT_MAX_PAGE_SIZE), frappe.ValidationError)
	if not 1 <= page_length <= DOCUMENT_MAX_PAGE_SIZE:
		frappe.throw(frappe._("Page length must be between 1 and {0}").format(DOCUMENT_MAX_PAGE_SIZE), frappe.ValidationError)
	if filters["package"] and not frappe.db.exists("FiBu Package", filters["package"]):
		frappe.throw(frappe._("The selected accounting package is not available for this client"), frappe.PermissionError)
	if filters["package"] and frappe.db.get_value("FiBu Package", filters["package"], "customer") != customer_doc.name:
		frappe.throw(frappe._("The selected accounting package is not available for this client"), frappe.PermissionError)

	state = _decode_cursor(cursor, customer_doc.name, filters)
	snapshot = frappe.utils.get_datetime(state["snapshot"]) if state else frappe.utils.now_datetime()
	permission_cache = {}
	file_contexts, communication_docs, package_docs = _collect_documents(customer_doc, filters, permission_cache)
	file_names = sorted(file_contexts)
	metadata = {}
	for chunk in _chunks(file_names):
		for file in frappe.get_all(
			"File",
			filters={"name": ["in", chunk], "is_folder": 0},
			fields=[
				"name", "file_name", "file_url", "file_size", "is_private", "attached_to_doctype",
				"attached_to_name", "attached_to_field", "creation",
			],
		):
			metadata[file.name] = file

	rows = []
	for name, file in metadata.items():
		if file.creation > snapshot or not file_contexts.get(name):
			continue
		if not _readable("File", name, permission_cache):
			continue
		if file.attached_to_doctype and file.attached_to_name and not _readable(
			file.attached_to_doctype, file.attached_to_name, permission_cache
		):
			continue
		contexts = list(file_contexts[name].values())
		if not _context_matches(contexts, filters):
			continue
		if _file_group(file.file_name) != filters["file_group"] and filters["file_group"] != "all":
			continue
		if not _matches_query(file, contexts, filters["query"]):
			continue
		communication = (
			communication_docs.get(file.attached_to_name)
			if file.attached_to_doctype == "Communication"
			else None
		)
		document_date = (
			communication.communication_date or communication.creation
			if communication
			else file.creation
		)
		document_date = str(document_date or file.creation)[:19].replace("T", " ")
		if filters["date_from"] and document_date[:10] < filters["date_from"]:
			continue
		if filters["date_to"] and document_date[:10] > filters["date_to"]:
			continue
		contexts.sort(key=lambda context: (context["source"], context["name"], context["doctype"]))
		remote_url = _external_url(file)
		if file.is_remote_file and not remote_url:
			continue
		rows.append(
			{
				"file_id": file.name,
				"file_name": file.file_name or "",
				"extension": PurePosixPath((file.file_name or "").replace("\\", "/")).suffix.lstrip("."),
				"file_size": int(file.file_size) if file.file_size is not None else None,
				"document_date": document_date,
				"date_kind": "email" if communication else "file",
				"storage_kind": "remote" if remote_url else "local",
				"external_url": remote_url,
				"can_download": not bool(remote_url),
				"can_select_for_zip": not bool(remote_url),
				"contexts": [dict(context) for context in contexts],
			}
		)
	rows.sort(key=lambda row: (row["document_date"], row["file_id"]), reverse=filters["sort"] == "newest")
	if state:
		last_key = (state["date"], state["name"])
		rows = [
			row
			for row in rows
			if ((row["document_date"], row["file_id"]) < last_key if filters["sort"] == "newest" else (row["document_date"], row["file_id"]) > last_key)
		]
	page = rows[:page_length]
	more = len(rows) > page_length
	next_cursor = (
		_encode_cursor(customer_doc.name, filters, page[-1]["document_date"], page[-1]["file_id"], str(snapshot))
		if more and page
		else None
	)
	return {"items": page, "next_cursor": next_cursor, "has_more": more}


def _authorized_files(customer, file_ids):
	"""Re-evaluate current Customer, relation graph, and File permissions before serving bytes."""
	customer_doc = _require_customer(customer)
	permission_cache = {}
	file_contexts, _, _ = _collect_documents(customer_doc, _normalize_filters(), permission_cache)
	authorized = {}
	for file_id in file_ids:
		if not frappe.db.exists("File", file_id) or file_id not in file_contexts:
			frappe.throw(frappe._("Document is no longer available"), frappe.PermissionError)
		if not _readable("File", file_id, permission_cache):
			frappe.throw(frappe._("Document is no longer available"), frappe.PermissionError)
		file = frappe.get_doc("File", file_id)
		if file.is_folder or file.is_remote_file or not file.file_url:
			frappe.throw(frappe._("This document cannot be downloaded"), frappe.PermissionError)
		if file.attached_to_doctype and file.attached_to_name and not _readable(
			file.attached_to_doctype, file.attached_to_name, permission_cache
		):
			frappe.throw(frappe._("Document is no longer available"), frappe.PermissionError)
		authorized[file_id] = file
	return customer_doc, authorized


def _safe_local_path(file):
	"""Return a real local path only after applying Frappe's File URL validation."""
	file.validate_file_path()
	path = Path(file.get_full_path()).resolve()
	from frappe.utils import get_files_path

	base_path = Path(get_files_path(is_private=file.is_private)).resolve()
	try:
		path.relative_to(base_path)
	except ValueError:
		frappe.throw(frappe._("The File URL you've entered is incorrect"), frappe.PermissionError)
	if not path.is_file():
		frappe.throw(frappe._("Document is no longer available"), frappe.PermissionError)
	return path


@frappe.whitelist()
def download_document(customer, file_id, disposition="attachment"):
	"""Serve an authorized local document without exposing its storage path."""
	if disposition not in {"inline", "attachment"}:
		frappe.throw(frappe._("Invalid download disposition"), frappe.ValidationError)
	_, files = _authorized_files(customer, [file_id])
	file = files[file_id]
	path = _safe_local_path(file)
	extension = PurePosixPath((file.file_name or "").replace("\\", "/")).suffix.lstrip(".").casefold()
	content_type = _INLINE_MIME_TYPES.get(extension) or mimetypes.guess_type(file.file_name or "")[0] or "application/octet-stream"
	if disposition == "inline" and extension not in _INLINE_MIME_TYPES:
		disposition = "attachment"
	frappe.local.response.filename = _safe_archive_names([file.file_name or "document"])[0]
	frappe.local.response.filecontent = path.read_bytes()
	frappe.local.response.content_type = content_type
	frappe.local.response.display_content_as = disposition
	frappe.local.response.type = "download"
	frappe.local.response_headers.set("Cache-Control", "private, no-store")
	frappe.local.response_headers.set("X-Content-Type-Options", "nosniff")


def _normalize_file_ids(file_ids):
	if isinstance(file_ids, str):
		try:
			file_ids = frappe.parse_json(file_ids)
		except (ValueError, TypeError):
			frappe.throw(frappe._("File IDs must be a valid JSON array"), frappe.ValidationError)
	if not isinstance(file_ids, list | tuple) or any(not isinstance(file_id, str) or not file_id for file_id in file_ids):
		frappe.throw(frappe._("File IDs must be a valid JSON array"), frappe.ValidationError)
	unique = list(dict.fromkeys(file_ids))
	if not unique or len(unique) > DOCUMENT_MAX_ZIP_FILES:
		frappe.throw(frappe._("Choose between 1 and {0} local files").format(DOCUMENT_MAX_ZIP_FILES), frappe.ValidationError)
	return unique


@frappe.whitelist(methods=["POST"])
def download_documents_zip(customer, file_ids):
	"""Create a complete, freshly authorized ZIP and reject partial archives."""
	if frappe.session.user == "Guest":
		frappe.throw(frappe._("Please log in to download client documents"), frappe.PermissionError)
	file_ids = _normalize_file_ids(file_ids)
	_, authorized_files = _authorized_files(customer, file_ids)
	files = []
	total_size = 0
	for file_id in file_ids:
		file = authorized_files[file_id]
		path = _safe_local_path(file)
		size = path.stat().st_size
		total_size += size
		if total_size > DOCUMENT_MAX_ZIP_BYTES:
			frappe.throw(frappe._("The selected files exceed the 100 MiB ZIP limit"), frappe.ValidationError)
		files.append((file, path, size))
	names = _safe_archive_names([file.file_name or "document" for file, _, _ in files])
	spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
	try:
		with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
			copied_bytes = 0
			for (_, path, _), archive_name in zip(files, names, strict=True):
				with path.open("rb") as source, archive.open(archive_name, "w") as target:
					while chunk := source.read(1024 * 1024):
						copied_bytes += len(chunk)
						if copied_bytes > DOCUMENT_MAX_ZIP_BYTES:
							frappe.throw(frappe._("The selected files exceed the 100 MiB ZIP limit"), frappe.ValidationError)
						target.write(chunk)
		spool.seek(0)
		content = spool.read()
	finally:
		spool.close()
	customer_name = _safe_archive_names([customer])[0]
	frappe.local.response.filename = f"Dokumente-{customer_name}-{frappe.utils.today()}.zip"
	frappe.local.response.filecontent = content
	frappe.local.response.content_type = "application/zip"
	frappe.local.response.display_content_as = "attachment"
	frappe.local.response.type = "download"
	frappe.local.response_headers.set("Cache-Control", "private, no-store")
	frappe.local.response_headers.set("X-Content-Type-Options", "nosniff")


@frappe.whitelist()
def get_package_options(customer):
	"""Return only readable FiBu packages for the current Mandant."""
	_require_customer(customer)
	return [
		{"name": package.name, "title": package.display_title}
		for package in frappe.get_list(
			"FiBu Package",
			filters={"customer": customer},
				fields=["name", "display_title"],
				order_by="period_start desc, name desc",
				limit=0,
			)
	]
