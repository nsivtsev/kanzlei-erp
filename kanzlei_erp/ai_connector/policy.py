"""Source operations reuse the current Mandant document/storage policy."""

import base64
import hashlib
import hmac
import json
import mimetypes
import time
from dataclasses import dataclass
from urllib.parse import quote

import frappe

from kanzlei_erp.ai_connector import auth, mail, native_drive
from kanzlei_erp.ai_connector.errors import ConnectorError, fail, text
from kanzlei_erp.ai_connector.native_drive import MAX_BYTES
from kanzlei_erp.mandant_documents import _require_customer, get_authorized_document_files, get_documents

MAX_BATCH = 50
DELEGATED_DOCUMENT_PAGE_SIZE = 4


def limit(value):
	if type(value) is not int or not 1 <= value <= 100:
		fail()
	return value


def scope(value):
	if (
		not isinstance(value, dict)
		or set(value) - {"version", "kind", "client_id"}
		or type(value.get("version", 1)) is not int
		or value.get("version", 1) != 1
	):
		fail()
	if value.get("kind") == "client":
		client = text(value.get("client_id"))
		if not frappe.has_permission("Customer", "read", doc=client):
			fail("FORBIDDEN", 403)
	elif value.get("kind") == "general" and not value.get("client_id"):
		if not auth.general_access(frappe.session.user):
			fail("FORBIDDEN", 403)
	else:
		fail()
	return value


def check_scope(value):
	checked = scope(value)
	if checked["kind"] == "client":
		_require_customer(checked["client_id"])
	return {"version": 1, "allowed": True}


def reference(value):
	if not isinstance(value, dict) or set(value) != {"source_id", "revision", "client_id"}:
		fail()
	text(value["client_id"])
	text(value["source_id"])
	kind, _, name = value["source_id"].partition(":")
	if kind not in {"file", "email"} or not name:
		fail()
	import re

	if not isinstance(value["revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["revision"]):
		fail()
	return value


def _cursor(value, binding):
	if value is None:
		return None
	text(value, 4096)
	try:
		raw, signature = value.split(".")
		expected = hmac.new(frappe.conf.encryption_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
		if not hmac.compare_digest(signature, expected):
			fail()
		payload = json.loads(base64.urlsafe_b64decode(raw))
		if (
			payload["binding"] != binding
			or payload["user"] != frappe.session.user
			or payload["exp"] <= time.time()
		):
			fail()
		return payload["after"]
	except ValueError, KeyError, TypeError:
		fail()


def _encode_cursor(after, binding):
	if after is None:
		return None
	raw = base64.urlsafe_b64encode(
		json.dumps(
			{"after": after, "binding": binding, "user": frappe.session.user, "exp": int(time.time()) + 300},
			separators=(",", ":"),
		).encode()
	).decode()
	signature = hmac.new(frappe.conf.encryption_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
	return raw + "." + signature


def clients(page_limit=50, cursor=None):
	page_limit = limit(page_limit)
	after = _cursor(cursor, "clients")
	page, continuation = [], None
	# Refill after record-level denials, with a bounded scan. A continuation
	# after an empty page is valid; it never exposes a denied record's metadata.
	for _batch in range(5):
		rows = frappe.get_list(
			"Customer",
			filters={"name": [">", after]} if after else {},
			fields=["name", "customer_name"],
			order_by="name asc",
			limit=100,
		)
		for row in rows:
			if frappe.has_permission("Customer", "read", doc=row.name):
				page.append(row)
			if len(page) > page_limit:
				continuation = page[page_limit - 1].name
				break
		if len(page) > page_limit:
			break
		if len(rows) < 100:
			continuation = None
			break
		after = rows[-1].name
		continuation = after
	page = page[:page_limit]
	return {
		"version": 1,
		# Frappe may mask customer_name for a share-only reader. Preserve that
		# field policy; the already-readable record ID remains a valid label.
		"items": [{"id": r.name, "label": r.customer_name or r.name} for r in page],
		"next_cursor": _encode_cursor(continuation, "clients"),
	}


def _file(client_id, file_id):
	_, files = get_authorized_document_files(client_id, [file_id])
	return _ordinary_file(files[file_id])


def _ordinary_file(file):
	if mail.is_attachment(file):
		fail("FORBIDDEN", 403)
	return _eligible(file)


def _eligible(file):
	if (
		file.get("status") not in (None, "", "Active")
		or file.get("content_doctype")
		or file.get("content_docname")
	):
		fail("FORBIDDEN", 403)
	if int(file.file_size or 0) > MAX_BYTES:
		fail("LIMIT_EXCEEDED", 413)
	return file


@dataclass(frozen=True)
class SourceState:
	revision: str
	byte_size: int
	native: native_drive.NativeObject | None


def _source_state(file, client_id, *, managed_only=False, locked=False):
	from kanzlei_erp.object_storage import get_binding

	fields = (
		"name",
		"file_url",
		"modified",
		"file_name",
		"content_hash",
		"file_size",
		"folder",
		"status",
		"is_folder",
		"file_type",
		"mime_type",
		"content_doctype",
		"content_docname",
		"is_remote_file",
		"attached_to_doctype",
		"attached_to_name",
		"attached_to_field",
		"kanzlei_drive_customer",
		"is_private",
	)
	state = {key: str(file.get(key) or "") for key in fields}
	size = int(file.file_size or 0)
	native = None
	binding = get_binding(file.file_url)
	if binding:
		state["storage"] = [binding.object_key, binding.sha256, binding.file_size]
	elif native_drive.is_native(file):
		native = native_drive.inspect(file, client_id, locked=locked)
		state["storage"] = ["native", native.bucket, native.key, *native.fingerprint]
		size = native.fingerprint[1]
	elif managed_only:
		fail("UNSUPPORTED_STORAGE", 409)
	else:
		from pathlib import Path

		stat = Path(file.get_full_path()).stat()
		state["storage"] = [stat.st_size, stat.st_mtime_ns]
	revision = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
	return SourceState(revision, size, native)


@native_drive.bounded
def manifest(file, client_id, *, managed_only=False, locked=False):
	state = _source_state(file, client_id, managed_only=managed_only, locked=locked)
	return {
		"version": 1,
		"source_id": "file:" + file.name,
		"source_type": "file",
		"revision": state.revision,
		"client_ids": [client_id],
		"title": file.file_name,
		"mime_type": file.get("mime_type")
		or mimetypes.guess_type(file.file_name)[0]
		or "application/octet-stream",
		"source_modified_at": str(file.modified),
		"locator": _file_locator(client_id, file.name),
		"byte_size": state.byte_size,
		"active": True,
	}


def _file_locator(client_id, file_id):
	return (
		"/api/method/kanzlei_erp.mandant_documents.download_document?customer="
		+ quote(client_id, safe="")
		+ "&file_id="
		+ quote(file_id, safe="")
		+ "&disposition=inline"
	)


@native_drive.bounded
def documents(client_id, page_limit=50, cursor=None, *, query=None):
	text(client_id)
	limit(page_limit)
	scope({"kind": "client", "client_id": client_id})
	if query is not None:
		text(query)
	binding = "documents:" + client_id
	underlying = _cursor(cursor, binding)
	email_enabled = mail.enabled()
	email_after = None
	if isinstance(underlying, dict):
		if underlying == {"files": True}:
			pass
		elif set(underlying) == {"email_after", "query"} and query == underlying["query"] and email_enabled:
			email_after = text(underlying["email_after"])
		else:
			fail()
		underlying = None
	if email_enabled and (cursor is None or email_after is not None):
		names = mail.message_names(client_id, after=email_after, query=query, page_limit=page_limit + 1)
		if names:
			continuation = {"files": True}
			if len(names) > page_limit:
				continuation = {"email_after": names[page_limit - 1], "query": query}
			return {
				"version": 1,
				"items": [
					item for name in names[:page_limit] if (item := mail.project("email:" + name, client_id))
				],
				"next_cursor": _encode_cursor(continuation, binding),
			}
	page = get_documents(
		client_id,
		filters={"query": query} if query is not None else None,
		cursor=underlying,
		page_length=min(page_limit, DELEGATED_DOCUMENT_PAGE_SIZE),
	)
	items = []
	ids = [row["file_id"] for row in page["items"] if row["can_download"]]
	files = get_authorized_document_files(client_id, ids, allow_partial=True)[1] if ids else {}
	for row in page["items"]:
		if row["file_id"] not in files:
			continue
		try:
			file = files[row["file_id"]]
			if mail.is_attachment(file):
				if email_enabled and (item := mail.project("file:" + file.name, client_id)):
					items.append(item)
			else:
				items.append(manifest(_eligible(file), client_id))
		except ConnectorError as error:
			if error.status == 503:
				raise
		except frappe.PermissionError, frappe.DoesNotExistError, FileNotFoundError:
			continue
	return {
		"version": 1,
		"items": items,
		"next_cursor": _encode_cursor(page["next_cursor"], binding),
	}


def _authorized(value, requested_scope, *, managed_only=False):
	if requested_scope["kind"] == "client" and value["client_id"] != requested_scope["client_id"]:
		fail("FORBIDDEN", 403)
	file = _file(value["client_id"], value["source_id"][5:])
	state = _source_state(file, value["client_id"], managed_only=managed_only)
	if state.revision != value["revision"]:
		fail("STALE_SOURCE", 409)
	return file, state


def _mail_ids(values):
	attachments = mail.attachment_ids(
		{value["source_id"][5:] for value in values if value["source_id"].startswith("file:")}
	)
	return {
		value["source_id"]
		for value in values
		if value["source_id"].startswith("email:") or value["source_id"][5:] in attachments
	}


@native_drive.bounded
def resolve_source_locator(requested_scope, source):
	requested_scope = scope(requested_scope)
	value = reference(source)
	client_id, source_id = value["client_id"], value["source_id"]
	if requested_scope["kind"] == "client" and client_id != requested_scope["client_id"]:
		fail("FORBIDDEN", 403)
	if source_id in _mail_ids([value]):
		item = mail.project(source_id, client_id)
		if item is None:
			fail("FORBIDDEN", 403)
		if item["revision"] != value["revision"]:
			fail("STALE_SOURCE", 409)
		locator = item["locator"]
	else:
		file, _ = _authorized(value, requested_scope)
		locator = _file_locator(client_id, file.name)
	return {"version": 1, "locator": locator}


@native_drive.bounded
def authorize_sources(requested_scope, sources, *, expected_mail_ids=None):
	requested_scope = scope(requested_scope)
	if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_BATCH:
		fail()
	values = [reference(value) for value in sources]

	def in_scope(value):
		return requested_scope["kind"] == "general" or value["client_id"] == requested_scope["client_id"]

	mail_ids = _mail_ids([value for value in values if in_scope(value)])
	if expected_mail_ids is not None and mail_ids != expected_mail_ids:
		fail("FORBIDDEN", 403)
	groups = {}
	for value in values:
		if in_scope(value) and value["source_id"] not in mail_ids:
			groups.setdefault(value["client_id"], set()).add(value["source_id"][5:])
	files = {}
	for client_id, ids in groups.items():
		try:
			_, allowed_files = get_authorized_document_files(client_id, sorted(ids), allow_partial=True)
			files.update({(client_id, name): file for name, file in allowed_files.items()})
		except frappe.PermissionError, frappe.DoesNotExistError:
			continue
	decisions = []
	revisions = {}
	for value in values:
		source_id, client_id = value["source_id"], value["client_id"]
		key = client_id, source_id
		try:
			if key not in revisions:
				if source_id in mail_ids and in_scope(value):
					item = mail.project(source_id, client_id)
					revisions[key] = item["revision"] if item else None
				else:
					file = files.get((client_id, source_id[5:]))
					revisions[key] = _source_state(_ordinary_file(file), client_id).revision if file else None
			allowed = in_scope(value) and revisions[key] == value["revision"]
		except ConnectorError as error:
			if error.status == 503:
				raise
			revisions[key] = None
			allowed = False
		except frappe.PermissionError, frappe.DoesNotExistError, FileNotFoundError:
			revisions[key] = None
			allowed = False
		decisions.append({**value, "allowed": allowed})
	return {"version": 1, "decisions": decisions}


@native_drive.bounded
def read_source(requested_scope, source, *, lock_current=True, managed_only=False, authenticate_current=None):
	from kanzlei_erp.drive_storage import invalidate
	from kanzlei_erp.object_storage import open_file

	requested_scope = scope(requested_scope)
	value = reference(source)
	if value["source_id"] in _mail_ids([value]):
		if requested_scope["kind"] == "client" and value["client_id"] != requested_scope["client_id"]:
			fail("FORBIDDEN", 403)
		if managed_only:
			fail("FORBIDDEN", 403)
		if authenticate_current is None:
			return mail.read_current(value)
		from kanzlei_erp.ai_connector.inventory import (
			current_permission_context,
			refresh_principal_permissions,
		)

		user = frappe.session.user

		def refresh():
			if authenticate_current()["subject"] != user:
				fail("UNAUTHENTICATED", 401)
			refresh_principal_permissions(user)
			scope(requested_scope)

		with current_permission_context():
			refresh()
			return mail.read_current(value, before_final_projection=refresh)
	file, state = _authorized(value, requested_scope, managed_only=managed_only)
	if state.native:
		content = state.native.read()
		invalidate()
		if authenticate_current is not None:
			from kanzlei_erp.ai_connector.inventory import (
				current_permission_context,
				refresh_principal_permissions,
			)

			user = frappe.session.user
			with current_permission_context():
				if authenticate_current()["subject"] != user:
					fail("UNAUTHENTICATED", 401)
				refresh_principal_permissions(user)
				scope(requested_scope)
				_authorized(value, requested_scope, managed_only=managed_only)
			return content
		_file(value["client_id"], file.name)
	else:
		with open_file(file) as stream:
			content = stream.read(MAX_BYTES + 1)
		if len(content) > MAX_BYTES:
			fail("LIMIT_EXCEEDED", 413)
		_authorized(value, requested_scope, managed_only=managed_only)
	# The ordinary delegated path takes a current lock. The fixed-ingestion path
	# runs inside inventory.current_permission_context instead: it is read-only
	# and makes its post-read authorization projection at READ COMMITTED.
	from kanzlei_erp.object_storage import lock_binding

	try:
		current = frappe.get_doc("File", file.name, for_update=lock_current)
		if native_drive.is_native(current) != bool(state.native):
			fail("STALE_SOURCE", 409)
		if lock_current:
			lock_binding(current.file_url)
		current_revision = _source_state(
			current,
			value["client_id"],
			managed_only=managed_only,
			locked=lock_current,
		).revision
	except frappe.QueryDeadlockError:
		# innodb_snapshot_isolation rejects a current read of a row changed
		# since this request's snapshot. Never return the already-read bytes.
		fail("STALE_SOURCE", 409)
	except frappe.QueryTimeoutError:
		if state.native:
			fail("STALE_SOURCE", 409)
		raise
	if current_revision != value["revision"]:
		fail("STALE_SOURCE", 409)
	return content
