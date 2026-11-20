"""Current Customer-scoped projections of ERP email and its attachments."""

import hashlib
import json
from dataclasses import dataclass
from email.utils import getaddresses
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode
from zipfile import is_zipfile

import frappe

from kanzlei_erp import drive_storage, object_storage
from kanzlei_erp.ai_connector import auth, native_drive
from kanzlei_erp.ai_connector.errors import ConnectorError, fail, text
from kanzlei_erp.ai_connector.native_drive import MAX_BYTES
from kanzlei_erp.mandant_documents import _safe_local_path

MIME_TYPE = "application/vnd.kanzlei.email+json"
PDF_MIME_TYPE = "application/pdf"
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DIRECTIONS = {"Received": "incoming", "Sent": "outgoing"}


def _flag(key):
	settings = auth.get_site_config(cached=False).get("kanzlei_ai_connector")
	return isinstance(settings, dict) and settings.get(key) is True


def enabled():
	return _flag("email_enabled")


def is_attachment(file):
	if file.attached_to_doctype != "Communication" or not file.attached_to_name:
		return False
	return frappe.db.get_value("Communication", file.attached_to_name, "communication_medium") == "Email"


def attachment_ids(ids):
	if not ids:
		return set()
	files = frappe.qb.DocType("File")
	messages = frappe.qb.DocType("Communication")
	return set(
		frappe.qb.from_(files)
		.join(messages)
		.on(files.attached_to_name == messages.name)
		.select(files.name)
		.where(files.name.isin(sorted(ids)))
		.where(files.attached_to_doctype == "Communication")
		.where(messages.communication_medium == "Email")
		.run(pluck=True)
	)


def message_names(client_id, *, after, query, page_limit):
	messages = frappe.qb.DocType("Communication")
	links = frappe.qb.DocType("Communication Link")
	linked = (
		frappe.qb.from_(links)
		.select(links.parent)
		.where(links.parenttype == "Communication")
		.where(links.parentfield == "timeline_links")
		.where(links.link_doctype == "Customer")
		.where(links.link_name == client_id)
	)
	page = (
		frappe.qb.from_(messages)
		.select(messages.name)
		.where(messages.communication_medium == "Email")
		.where(messages.communication_type == "Communication")
		.where(messages.sent_or_received.isin(list(DIRECTIONS)))
		.where(
			((messages.reference_doctype == "Customer") & (messages.reference_name == client_id))
			| messages.name.isin(linked)
		)
		.orderby(messages.name)
		.limit(page_limit)
	)
	if after:
		page = page.where(messages.name > after)
	if query:
		page = page.where(messages.subject.like("%" + query + "%"))
	return page.run(pluck=True)


def _direct_clients(message):
	clients = {
		row.link_name for row in message.timeline_links if row.link_doctype == "Customer" and row.link_name
	}
	if message.reference_doctype == "Customer" and message.reference_name:
		clients.add(message.reference_name)
	return clients


def attachment_clients(file):
	"""Current direct ties of a previously authorized legacy Drive attachment."""
	if file.attached_to_doctype != "Communication" or not file.attached_to_name:
		fail("FORBIDDEN", 403)
	message = frappe.get_doc("Communication", file.attached_to_name)
	if message.communication_medium != "Email" or not frappe.has_permission(
		"Communication", "read", doc=message
	):
		fail("FORBIDDEN", 403)
	return _direct_clients(message)


def _json(value):
	return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _message_bytes(message):
	from frappe.utils import get_datetime

	body = message.content or message.text_content or ""
	if len(body) > MAX_BYTES:
		fail("LIMIT_EXCEEDED", 413)
	recipients = [message.get(field) for field in ("recipients", "cc", "bcc") if message.get(field)]
	content = _json(
		{
			"version": 1,
			"subject": message.subject or "",
			"sender": message.sender or "",
			"recipients": list(dict.fromkeys(address for _, address in getaddresses(recipients) if address)),
			"direction": DIRECTIONS[message.sent_or_received],
			"sent_at": get_datetime(message.communication_date).isoformat()
			if message.communication_date
			else None,
			"reply_to": "email:" + message.in_reply_to if message.in_reply_to else None,
			"body": body,
			"body_format": "html" if message.content else "text",
		}
	)
	if len(content) > MAX_BYTES:
		fail("LIMIT_EXCEEDED", 413)
	return content


def is_docx(filename):
	return PurePosixPath(filename or "").suffix.casefold() == ".docx"


def require_docx(content):
	# Inspect the local-file signature and ZIP end record without reading or
	# expanding package entries. The AI parser validates the OOXML package.
	if content.startswith(b"PK\x03\x04"):
		try:
			if is_zipfile(BytesIO(content)):
				return
		except (ValueError, OverflowError):
			# Malformed directory offsets can fail while seeking.
			pass
	fail("FORBIDDEN", 403)


def _check_file(file):
	from kanzlei_erp.ai_connector import policy

	policy._eligible(file)
	if file.is_folder or not frappe.has_permission("File", "read", doc=file):
		fail("FORBIDDEN", 403)
	suffix = PurePosixPath(file.file_name or "").suffix.casefold()
	if suffix == ".pdf":
		mime_type = PDF_MIME_TYPE
	elif suffix == ".docx" and _flag("docx_enabled"):
		mime_type = DOCX_MIME_TYPE
	else:
		fail("FORBIDDEN", 403)
	if not native_drive.is_native(file):
		url = file.file_url or ""
		# Only local Frappe URLs (including their managed S3 bindings) are eligible.
		if file.is_remote_file or not url.startswith(("/files/", "/private/files/")):
			fail("FORBIDDEN", 403)
		if not object_storage.canonical_url(url):
			fail("FORBIDDEN", 403)
		if not object_storage.get_binding(url):
			_safe_local_path(file)
	drive_storage.require_storage_access(file)
	return mime_type


@dataclass(frozen=True)
class _Source:
	manifest: dict
	clients: set[str]
	content: bytes | None = None
	file: object | None = None
	native: native_drive.NativeObject | None = None


def _source(source_id, client_id=None):
	if not enabled():
		fail("FORBIDDEN", 403)
	from kanzlei_erp.ai_connector import policy

	kind, _, name = text(source_id).partition(":")
	if kind not in {"email", "file"} or not name:
		fail()
	file = frappe.get_doc("File", name) if kind == "file" else None
	if file is not None and (
		file.name != name or file.attached_to_doctype != "Communication" or not file.attached_to_name
	):
		fail("FORBIDDEN", 403)
	message_name = file.attached_to_name if file is not None else name
	message = frappe.get_doc("Communication", message_name)
	if (
		message.name != message_name
		or message.communication_medium != "Email"
		or message.communication_type != "Communication"
		or message.sent_or_received not in DIRECTIONS
		or message.name in {".", ".."}
		or "/" in message.name
		or "\\" in message.name
		or any(ord(char) < 32 or ord(char) == 127 for char in message.name)
	):
		fail("FORBIDDEN", 403)
	clients = _direct_clients(message)
	owner = file.get(drive_storage.MARKER) if file is not None else None
	if client_id is None:
		# A managed File retains its owning Drive even when its message
		# has several direct ties.
		client_id = owner or next(
			(client for client in sorted(clients) if frappe.has_permission("Customer", "read", doc=client)),
			None,
		)
	if (
		client_id not in clients
		or (owner and client_id != owner)
		or not frappe.has_permission("Customer", "read", doc=client_id)
		or not frappe.has_permission("Communication", "read", doc=message)
	):
		fail("FORBIDDEN", 403)

	parent_id = "email:" + message.name
	if file is None:
		content = _message_bytes(message)
		revision = hashlib.sha256(_json([source_id, sorted(clients)]) + content).hexdigest()
		manifest = {
			"version": 1,
			"source_id": source_id,
			"source_type": "email",
			"revision": revision,
			"client_ids": [client_id],
			"title": (message.subject or message.name)[:500],
			"mime_type": MIME_TYPE,
			"source_modified_at": str(message.modified),
			"locator": "/desk/communication/" + quote(message.name, safe=""),
			"byte_size": len(content),
			"active": True,
		}
		return _Source(manifest, clients, content=content)

	mime_type = _check_file(file)
	# Native inspection checks Drive ancestry itself. Apply that same ownership
	# boundary to classic bindings and stored local files with a Drive marker.
	if owner and not native_drive.is_native(file) and not native_drive.in_drive(file, owner):
		fail("FORBIDDEN", 403)
	state = policy._source_state(file, client_id)
	if state.byte_size > MAX_BYTES:
		fail("LIMIT_EXCEEDED", 413)
	revision = hashlib.sha256(_json([state.revision, parent_id, sorted(clients)])).hexdigest()
	manifest = {
		"version": 1,
		"source_id": source_id,
		"source_type": "file",
		"revision": revision,
		"client_ids": [client_id],
		"title": file.file_name,
		"mime_type": mime_type,
		"source_modified_at": str(file.modified),
		"locator": "/api/method/kanzlei_erp.ai_connector.api.download_mail_attachment?"
		+ urlencode({"customer": client_id, "file_id": name, "revision": revision}),
		"byte_size": state.byte_size,
		"active": True,
		"parent_message_id": parent_id,
	}
	return _Source(manifest, clients, file=file, native=state.native)


@native_drive.bounded
def project(source_id, client_id):
	try:
		return _source(source_id, client_id).manifest
	except ConnectorError as error:
		if error.status == 503:
			raise
	except frappe.PermissionError, frappe.DoesNotExistError, FileNotFoundError:
		pass
	return None


@native_drive.bounded
def external_clients(source_id, *, client_id=None, revision=None):
	"""Include every explicit Customer tie, even if the principal cannot read it."""
	current = _source(source_id, client_id)
	if revision is not None and current.manifest["revision"] != revision:
		fail("FORBIDDEN", 403)
	return current.clients


def read_current(ref, *, before_final_projection=None):
	return read_current_with_mime(ref, before_final_projection=before_final_projection)[0]


@native_drive.bounded
def read_current_with_mime(ref, *, before_final_projection=None):
	"""Check the expected revision both before reading and before releasing bytes.

	The caller supplies the permission refresh for its current transaction.
	"""
	from kanzlei_erp.ai_connector import policy

	value = policy.reference(ref)
	source_id, client_id, revision = value["source_id"], value["client_id"], value["revision"]
	initial = _source(source_id, client_id)
	if initial.manifest["revision"] != revision:
		fail("STALE_SOURCE", 409)
	if initial.file is None:
		content = initial.content
	elif initial.native:
		content = initial.native.read()
	else:
		with object_storage.open_file(initial.file) as stream:
			content = stream.read(MAX_BYTES + 1)
	if len(content) > MAX_BYTES:
		fail("LIMIT_EXCEEDED", 413)
	mime_type = initial.manifest["mime_type"]
	if mime_type == DOCX_MIME_TYPE:
		require_docx(content)
	elif mime_type == PDF_MIME_TYPE and not content.startswith(b"%PDF-"):
		fail("FORBIDDEN", 403)
	if before_final_projection is not None:
		before_final_projection()
	if initial.file is not None:
		drive_storage.invalidate()
	final = _source(source_id, client_id)
	if final.manifest["revision"] != revision or final.manifest["byte_size"] != len(content):
		fail("STALE_SOURCE", 409)
	return content, mime_type
