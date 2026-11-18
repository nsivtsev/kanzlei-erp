"""Bounded Drive inventory and current-state invalidation projection."""

import base64
import hashlib
import hmac
import json
import re
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy

import frappe

from kanzlei_erp.ai_connector import auth, policy
from kanzlei_erp.ai_connector.errors import ConnectorError, fail, text
from kanzlei_erp.posteingang_permissions import staff


def settings():
	config = auth.config()
	value = config.get("ingestion")
	if not isinstance(value, dict) or value.get("enabled") is not True:
		fail("CONNECTOR_UNAVAILABLE", 503)
	user = text(value.get("user"), 140)
	generation = text(value.get("generation"), 100)
	clients = value.get("client_ids")
	digest = value.get("key_sha256")
	if (
		user == "Administrator"
		or not staff(user)
		or not isinstance(clients, list)
		or not clients
		or len(clients) > 1000
		or not all(isinstance(x, str) and x and len(x) <= 140 for x in clients)
		or not isinstance(digest, str)
		or not re.fullmatch(r"[0-9a-f]{64}", digest)
		or digest == config["service_key_sha256"]
	):
		fail("CONNECTOR_UNAVAILABLE", 503)
	key = frappe.get_request_header("X-Kanzlei-AI-Service-Key") or ""
	if not 32 <= len(key) <= 256 or not hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), digest):
		fail("UNAUTHENTICATED", 401)
	binding = hashlib.sha256(
		json.dumps(
			[config["connector_id"], config["audience"], user, sorted(clients), generation, digest],
			separators=(",", ":"),
		).encode()
	).hexdigest()
	return user, set(clients), binding


@contextmanager
def principal(user):
	original = frappe.local.session
	previous = deepcopy(original)
	form = frappe.local.form_dict
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous.user)
		original.clear()
		original.update(previous)
		frappe.local.session = original
		frappe.local.form_dict = form


@contextmanager
def current_permission_context():
	"""Use a private READ COMMITTED connection for a background byte read.

	It never commits or rolls back the RPC request's database connection. The
	post-stream projection is the consistency point for fresh SQL-backed
	DocShare, Customer-root, File and Drive-ancestry reads at READ COMMITTED.
	Frappe role and User Permission freshness remains cache-dependent; this
	background ingestion boundary is not a user-facing retrieval authorization
	contract and must be tightened before the pilot.
	"""
	from frappe.database import get_db

	conf = frappe.local.conf
	fresh = get_db(
		socket=conf.db_socket,
		host=conf.db_host,
		port=conf.db_port,
		user=conf.db_user or conf.db_name,
		password=conf.db_password,
		cur_db_name=conf.db_name,
	)
	original_db = frappe.local.db
	original_cache = frappe.local.cache
	original_request_cache = frappe.local.request_cache
	original_role_permissions = frappe.local.role_permissions
	original_user_perms = frappe.local.user_perms
	try:
		# MariaDB's default repeatable-read snapshot could retain permissions
		# from before streaming. READ COMMITTED makes the final projection see a
		# revocation committed while object storage was being read.
		fresh.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		fresh.begin(read_only=True)
		frappe.local.db = fresh
		frappe.local.cache = {}
		frappe.local.request_cache = defaultdict(dict)
		yield
	finally:
		try:
			try:
				fresh.rollback()
			finally:
				fresh.close()
		finally:
			frappe.local.db = original_db
			frappe.local.cache = original_cache
			frappe.local.request_cache = original_request_cache
			frappe.local.role_permissions = original_role_permissions
			frappe.local.user_perms = original_user_perms


def refresh_principal_permissions(user):
	"""Discard permission state before a current SQL-backed projection."""
	frappe.cache.hdel("roles", user)
	frappe.cache.hdel("user_permissions", user)
	# has_permission also consults state attached to this request. The first
	# projection may have populated these before the stream, so they must not
	# survive to the final SQL-backed decision. Redis can be repopulated by an
	# older transaction, therefore this does not promise fresh role/User Permission
	# revocations.
	frappe.local.cache = {}
	frappe.local.request_cache = defaultdict(dict)
	frappe.local.role_permissions = {}
	frappe.local.user_perms = None


def encode(data, binding):
	raw = base64.urlsafe_b64encode(
		json.dumps({"binding": binding, **data}, separators=(",", ":")).encode()
	).decode()
	signature = hmac.new(frappe.conf.encryption_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
	return raw + "." + signature


def decode(value, binding, kind):
	text(value, 4096)
	try:
		raw, signature = value.split(".")
		expected = hmac.new(frappe.conf.encryption_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
		if not hmac.compare_digest(signature, expected):
			fail()
		data = json.loads(base64.urlsafe_b64decode(raw))
		if data["binding"] != binding or data["kind"] != kind:
			fail()
		return data
	except ValueError, KeyError, TypeError, UnicodeError:
		fail()


def require_clients(clients):
	for client in clients:
		if not frappe.has_permission("Customer", "read", doc=client):
			fail("FORBIDDEN", 403)


def project(name, client, clients, locked=False):
	if client not in clients or not frappe.has_permission("Customer", "read", doc=client):
		return None
	try:
		file = frappe.get_doc("File", name, for_update=locked)
		if file.get("kanzlei_drive_customer") != client or file.is_folder:
			return None
		from suite.drive.utils.downloads import content_kind

		if content_kind(file) != "stored" or not frappe.has_permission("File", "read", doc=file):
			return None
		policy._eligible(file)
		if (
			file.attached_to_doctype
			and file.attached_to_name
			and not frappe.has_permission(file.attached_to_doctype, "read", doc=file.attached_to_name)
		):
			return None
		root = frappe.db.get_value("Customer", client, "kanzlei_drive_folder")
		parent = file.folder
		seen = set()
		while parent and parent not in seen and len(seen) < 256:
			seen.add(parent)
			if locked:
				rows = frappe.db.sql(
					"SELECT name,folder,is_folder,status,kanzlei_drive_customer FROM `tabFile` WHERE name=%s FOR UPDATE",
					(parent,),
					as_dict=True,
				)
				row = rows[0] if rows else None
			else:
				row = frappe.db.get_value(
					"File",
					parent,
					["name", "folder", "is_folder", "status", "kanzlei_drive_customer"],
					as_dict=True,
				)
			if not row or not row.is_folder or row.status != "Active" or row.kanzlei_drive_customer != client:
				return None
			if parent == root:
				from kanzlei_erp.object_storage import get_binding

				if not get_binding(file.file_url):
					fail("UNSUPPORTED_STORAGE", 409)
				return policy.manifest(file, client)
			parent = row.folder
	except ConnectorError as error:
		if error.code == "UNSUPPORTED_STORAGE":
			raise
		return None
	except frappe.PermissionError, frappe.DoesNotExistError, FileNotFoundError:
		return None
	return None


def read_current(source, clients, *, before_final_projection=None):
	"""Current background projection; never infer access from S3 alone."""
	value = policy.reference(source)
	if value["client_id"] not in clients:
		fail("FORBIDDEN", 403)
	require_clients(clients)
	manifest = project(value["source_id"][5:], value["client_id"], clients)
	if not manifest or manifest["revision"] != value["revision"]:
		fail("STALE_SOURCE", 409)
	content = policy.read_source(
		{"kind": "client", "client_id": value["client_id"]}, value, lock_current=False
	)
	# This is the SQL-backed authorization consistency point. Refresh cache state
	# immediately before it so current DocShare/root/Drive reads use the isolated
	# READ COMMITTED connection; role/User Permission freshness remains Frappe
	# cache-dependent and is deliberately not promised by this stage.
	if before_final_projection:
		before_final_projection()
	manifest = project(value["source_id"][5:], value["client_id"], clients)
	if not manifest or manifest["revision"] != value["revision"]:
		fail("STALE_SOURCE", 409)
	return content


def inventory(cursor=None, limit=50):
	limit = policy.limit(limit)
	user, clients, binding = settings()
	with principal(user):
		require_clients(clients)
		if cursor:
			data = decode(cursor, binding, "inventory")
			watermark, after = data["watermark"], data["after"]
		else:
			# Ordinary consistent read: W and the candidate rows share this snapshot.
			rows = frappe.db.sql("SELECT `sequence` FROM `tabAI Source State` WHERE name='drive'")
			watermark, after = int(rows[0][0]) if rows else 0, ""
		items = []
		rows = frappe.get_all(
			"File",
			filters={"name": [">", after], "kanzlei_drive_customer": ["in", list(clients)]},
			fields=["name", "kanzlei_drive_customer"],
			order_by="name asc",
			limit=limit,
		)
		for row in rows:
			item = project(row.name, row.kanzlei_drive_customer, clients)
			if item:
				items.append(item)
		next_cursor = (
			encode({"kind": "inventory", "watermark": watermark, "after": rows[-1].name}, binding)
			if len(rows) == limit
			else None
		)
		return {
			"version": 1,
			"items": items,
			"next_cursor": next_cursor,
			"changes_cursor": encode({"kind": "changes", "after": watermark}, binding),
		}


def changes(cursor, limit=50):
	limit = policy.limit(limit)
	user, clients, binding = settings()
	data = decode(cursor, binding, "changes")
	if type(data.get("after")) is not int or data["after"] < 0:
		fail()
	with principal(user):
		require_clients(clients)
		rows = frappe.get_all(
			"AI Source Event",
			filters={"sequence": [">", data["after"]], "client_id": ["in", list(clients)]},
			fields=["sequence", "source_id", "client_id"],
			order_by="sequence asc",
			limit=limit,
		)
		items = []
		for row in rows:
			manifest = project(row.source_id[5:], row.client_id, clients)
			items.append(
				{
					"source_id": row.source_id,
					"client_id": row.client_id,
					"operation": "upsert" if manifest else "delete",
					"manifest": manifest,
				}
			)
		after = rows[-1].sequence if rows else data["after"]
		return {
			"version": 1,
			"items": items,
			"next_cursor": encode({"kind": "changes", "after": after}, binding),
		}
