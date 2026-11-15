"""Source operations reuse the current Mandant document/storage policy."""

import base64
import hashlib
import hmac
import json
import mimetypes
import time
from urllib.parse import quote

import frappe

from kanzlei_erp.ai_connector import auth
from kanzlei_erp.ai_connector.errors import ConnectorError, fail, text
from kanzlei_erp.mandant_documents import get_authorized_document_files, get_documents

MAX_BYTES = 20 * 1024 * 1024
MAX_BATCH = 50


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


def reference(value):
	if not isinstance(value, dict) or set(value) != {"source_id", "revision", "client_id"}:
		fail()
	text(value["client_id"])
	text(value["source_id"])
	if not value["source_id"].startswith("file:") or len(value["source_id"]) <= 5:
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
	return _eligible(files[file_id])


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


def _revision(file):
	from kanzlei_erp.object_storage import S3_URL_PREFIX, get_binding

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
	binding = get_binding(file.file_url)
	if binding:
		state["storage"] = [binding.object_key, binding.sha256, binding.file_size]
	elif not file.file_url.startswith(S3_URL_PREFIX):
		from pathlib import Path

		stat = Path(file.get_full_path()).stat()
		state["storage"] = [stat.st_size, stat.st_mtime_ns]
	return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def manifest(file, client_id):
	return {
		"version": 1,
		"source_id": "file:" + file.name,
		"source_type": "file",
		"revision": _revision(file),
		"client_ids": [client_id],
		"title": file.file_name,
		"mime_type": file.get("mime_type")
		or mimetypes.guess_type(file.file_name)[0]
		or "application/octet-stream",
		"source_modified_at": str(file.modified),
		"locator": "/api/method/kanzlei_erp.mandant_documents.download_document?customer="
		+ quote(client_id, safe="")
		+ "&file_id="
		+ quote(file.name, safe="")
		+ "&disposition=inline",
		"byte_size": int(file.file_size or 0),
		"active": True,
	}


def documents(client_id, page_limit=50, cursor=None):
	text(client_id)
	limit(page_limit)
	scope({"kind": "client", "client_id": client_id})
	underlying = _cursor(cursor, "documents:" + client_id)
	page = get_documents(client_id, cursor=underlying, page_length=page_limit)
	items = []
	ids = [row["file_id"] for row in page["items"] if row["can_download"]]
	files = get_authorized_document_files(client_id, ids, allow_partial=True)[1] if ids else {}
	for row in page["items"]:
		if row["file_id"] not in files:
			continue
		try:
			items.append(manifest(_eligible(files[row["file_id"]]), client_id))
		except frappe.PermissionError, frappe.DoesNotExistError, FileNotFoundError, ConnectorError:
			continue
	return {
		"version": 1,
		"items": items,
		"next_cursor": _encode_cursor(page["next_cursor"], "documents:" + client_id),
	}


def authorized(value, requested_scope):
	if requested_scope["kind"] == "client" and value["client_id"] != requested_scope["client_id"]:
		fail("FORBIDDEN", 403)
	file = _file(value["client_id"], value["source_id"][5:])
	if _revision(file) != value["revision"]:
		fail("STALE_SOURCE", 409)
	return file


def authorize_sources(requested_scope, sources):
	requested_scope = scope(requested_scope)
	if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_BATCH:
		fail()
	values = [reference(value) for value in sources]
	groups = {}
	for value in values:
		if requested_scope["kind"] == "general" or value["client_id"] == requested_scope["client_id"]:
			groups.setdefault(value["client_id"], set()).add(value["source_id"][5:])
	files = {}
	for client_id, ids in groups.items():
		try:
			_, allowed_files = get_authorized_document_files(client_id, sorted(ids), allow_partial=True)
			files.update({(client_id, name): file for name, file in allowed_files.items()})
		except frappe.PermissionError, frappe.DoesNotExistError:
			continue
	decisions = []
	for value in values:
		try:
			file = files.get((value["client_id"], value["source_id"][5:]))
			allowed = bool(file and _revision(_eligible(file)) == value["revision"])
		except frappe.PermissionError, frappe.DoesNotExistError, FileNotFoundError, ConnectorError:
			allowed = False
		decisions.append({**value, "allowed": allowed})
	return {"version": 1, "decisions": decisions}


def read_source(requested_scope, source):
	from kanzlei_erp.object_storage import open_file

	requested_scope = scope(requested_scope)
	value = reference(source)
	file = authorized(value, requested_scope)
	with open_file(file) as stream:
		content = stream.read(MAX_BYTES + 1)
	if len(content) > MAX_BYTES:
		fail("LIMIT_EXCEEDED", 413)
	authorized(value, requested_scope)
	# FOR UPDATE is a current read even under MariaDB repeatable-read. Lock the
	# managed storage binding as well: its revision can change without File.modified.
	from kanzlei_erp.object_storage import lock_binding

	try:
		current = frappe.get_doc("File", file.name, for_update=True)
		lock_binding(current.file_url)
		current_revision = _revision(current)
	except frappe.QueryDeadlockError:
		# innodb_snapshot_isolation rejects a current read of a row changed
		# since this request's snapshot. Never return the already-read bytes.
		fail("STALE_SOURCE", 409)
	if current_revision != value["revision"]:
		fail("STALE_SOURCE", 409)
	return content
