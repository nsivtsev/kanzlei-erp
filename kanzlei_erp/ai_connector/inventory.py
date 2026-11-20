"""Bounded source inventory and current-state invalidation projection."""

import base64
import hashlib
import hmac
import json
import re
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy

import frappe

from kanzlei_erp.ai_connector import auth, native_drive, policy
from kanzlei_erp.ai_connector.errors import ConnectorError, fail, text
from kanzlei_erp.ai_connector.events import ATTACHMENT_EVENT_PREFIX
from kanzlei_erp.posteingang_permissions import staff

INVENTORY_PHASES = ("drive", "email", "attachment")


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
	"""Use a private READ COMMITTED connection for a current permission check.

	It never commits or rolls back the RPC request's database connection. The
	post-stream projection is the consistency point for fresh SQL-backed
	DocShare, Customer-root, File and Drive-ancestry reads at READ COMMITTED.
	Frappe role and User Permission freshness remains cache-dependent and
	must be tightened before the pilot.
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


def _feed_binding(binding, email_enabled):
	# Leave all Drive-only cursors valid when mail is off.
	return hashlib.sha256(f"{binding}:mail:v1".encode()).hexdigest() if email_enabled else binding


def _has_mail_parent(name):
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM `tabFile` f JOIN `tabCommunication` c ON c.name=f.attached_to_name
		WHERE f.name=%s AND f.attached_to_doctype='Communication' AND c.communication_medium='Email'""",
			name,
		)
	)


def _project_event(source_id, client, clients, email_enabled):
	from kanzlei_erp.ai_connector import mail

	if source_id.startswith("email:"):
		return mail.project(source_id, client) if email_enabled else None
	if not source_id.startswith("file:"):
		return None
	name = source_id[5:]
	if email_enabled and _has_mail_parent(name):
		return mail.project(source_id, client)
	return project(name, client, clients)


def _drive_file(name, client, clients, *, locked=False):
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
		if native_drive.in_drive(file, client, locked=locked):
			return file
	except ConnectorError, frappe.PermissionError, frappe.DoesNotExistError, FileNotFoundError:
		return None
	return None


@native_drive.bounded
def project(name, client, clients, locked=False):
	file = _drive_file(name, client, clients, locked=locked)
	if not file:
		return None
	try:
		return policy.manifest(file, client, managed_only=True, locked=locked)
	except ConnectorError as error:
		if error.code == "UNSUPPORTED_STORAGE" or error.status == 503:
			raise
		return None
	except frappe.PermissionError, frappe.DoesNotExistError, FileNotFoundError:
		return None
	return None


@native_drive.bounded
def read_current(source, clients, *, before_final_projection=None):
	"""Read current bytes under the background identity."""
	from kanzlei_erp.ai_connector import mail

	value = policy.reference(source)
	if value["client_id"] not in clients:
		fail("FORBIDDEN", 403)
	require_clients(clients)
	source_id = value["source_id"]
	email_enabled = mail.enabled()
	if source_id.startswith("email:"):
		if not email_enabled:
			fail("STALE_SOURCE", 409)
		return mail.read_current(value, before_final_projection=before_final_projection)
	name = source_id[5:]
	if email_enabled and _has_mail_parent(name):
		return mail.read_current(value, before_final_projection=before_final_projection)
	file = _drive_file(name, value["client_id"], clients)
	if not file:
		fail("STALE_SOURCE", 409)
	content = policy.read_source(
		{"kind": "client", "client_id": value["client_id"]}, value, lock_current=False, managed_only=True
	)
	# This is the SQL-backed authorization consistency point. Refresh cache state
	# immediately before it so current DocShare/root/Drive reads use the isolated
	# READ COMMITTED connection; role/User Permission freshness remains Frappe
	# cache-dependent and is deliberately not promised by this stage.
	if before_final_projection:
		before_final_projection()
	if native_drive.is_native(file):
		from kanzlei_erp.drive_storage import invalidate

		invalidate()
	if email_enabled and _has_mail_parent(name):
		fail("STALE_SOURCE", 409)
	manifest = project(name, value["client_id"], clients)
	if not manifest or manifest["revision"] != value["revision"]:
		fail("STALE_SOURCE", 409)
	return content


def _mail_candidates(phase, clients, after, after_client, limit):
	# Seek on the source/client pair because one message can belong to multiple
	# clients. Deduplicate direct and explicit relationships before paging.
	if phase == "attachment":
		source = "f.name"
		attachment_join = """
			JOIN `tabFile` f ON f.attached_to_doctype='Communication'
				AND f.attached_to_name=c.name AND f.is_folder=0
		"""
	else:
		source = "c.name"
		attachment_join = ""
	mail_filter = """
		c.communication_type='Communication' AND c.communication_medium='Email'
		AND c.sent_or_received IN ('Received', 'Sent')
	"""
	return frappe.db.sql(
		f"""
		(SELECT {source} AS name, c.reference_name AS client_id
		FROM `tabCommunication` c {attachment_join}
		WHERE {mail_filter} AND c.reference_doctype='Customer' AND c.reference_name IN %(clients)s
			AND ({source} > %(after)s OR ({source} = %(after)s AND c.reference_name > %(after_client)s))
		ORDER BY name, client_id LIMIT %(limit)s)
		UNION DISTINCT
		(SELECT DISTINCT {source} AS name, l.link_name AS client_id
		FROM `tabCommunication Link` l
		JOIN `tabCommunication` c ON c.name=l.parent {attachment_join}
		WHERE {mail_filter} AND l.parenttype='Communication' AND l.parentfield='timeline_links'
			AND l.link_doctype='Customer' AND l.link_name IN %(clients)s
			AND ({source} > %(after)s OR ({source} = %(after)s AND l.link_name > %(after_client)s))
		ORDER BY name, client_id LIMIT %(limit)s)
		ORDER BY name, client_id LIMIT %(limit)s
		""",
		{"clients": tuple(sorted(clients)), "after": after, "after_client": after_client, "limit": limit},
		as_dict=True,
	)


def _drive_candidates(clients, after, limit, email_enabled):
	fields = ["name", "kanzlei_drive_customer AS client_id"]
	if email_enabled:
		fields.append("attached_to_doctype")
	return frappe.get_all(
		"File",
		filters={"name": [">", after], "kanzlei_drive_customer": ["in", list(clients)]},
		fields=fields,
		order_by="name asc",
		limit=limit,
	)


@native_drive.bounded
def inventory(cursor=None, limit=50):
	from kanzlei_erp.ai_connector import mail

	limit = policy.limit(limit)
	user, clients, binding = settings()
	email_enabled = mail.enabled()
	binding = _feed_binding(binding, email_enabled)
	phases = INVENTORY_PHASES if email_enabled else INVENTORY_PHASES[:1]
	with principal(user):
		require_clients(clients)
		phase, after, after_client = "drive", "", ""
		if cursor:
			data = decode(cursor, binding, "inventory")
			watermark, after = data["watermark"], data["after"]
			if email_enabled:
				phase, after_client = data["phase"], data["after_client"]
		else:
			# Ordinary consistent read: W and the candidate rows share this snapshot.
			rows = frappe.db.sql("SELECT `sequence` FROM `tabAI Source State` WHERE name='drive'")
			watermark = int(rows[0][0]) if rows else 0
		if phase not in phases:
			fail()
		items = []
		rows = (
			_drive_candidates(clients, after, limit, email_enabled)
			if phase == "drive"
			else _mail_candidates(phase, clients, after, after_client, limit)
		)
		scanned = 0
		for row in rows:
			if scanned and native_drive.page_should_yield():
				break
			if phase == "drive":
				# Current Communication attachments are handled in their own phase.
				attached = row.get("attached_to_doctype") == "Communication" and _has_mail_parent(row.name)
				item = None if attached else project(row.name, row.client_id, clients)
			else:
				prefix = "email:" if phase == "email" else "file:"
				item = mail.project(prefix + row.name, row.client_id)
			if item:
				items.append(item)
			scanned += 1
		next_position = None
		if scanned and (len(rows) == limit or scanned < len(rows)):
			last = rows[scanned - 1]
			next_position = phase, last.name, last.client_id
		elif phase != phases[-1]:
			next_position = phases[phases.index(phase) + 1], "", ""
		next_cursor = None
		if next_position:
			phase, after, after_client = next_position
			payload = {"kind": "inventory", "watermark": watermark, "after": after}
			if email_enabled:
				payload.update(phase=phase, after_client=after_client)
			next_cursor = encode(payload, binding)
		return {
			"version": 1,
			"items": items,
			"next_cursor": next_cursor,
			"changes_cursor": encode({"kind": "changes", "after": watermark}, binding),
		}


@native_drive.bounded
def changes(cursor, limit=50):
	from kanzlei_erp.ai_connector import mail

	limit = policy.limit(limit)
	user, clients, binding = settings()
	email_enabled = mail.enabled()
	binding = _feed_binding(binding, email_enabled)
	data = decode(cursor, binding, "changes")
	if type(data.get("after")) is not int or data["after"] < 0:
		fail()
	with principal(user):
		require_clients(clients)
		filters = {"sequence": [">", data["after"]], "client_id": ["in", list(clients)]}
		if not email_enabled:
			filters["source_id"] = ["like", "file:%"]
		rows = frappe.get_all(
			"AI Source Event",
			filters=filters,
			fields=["sequence", "source_id", "client_id"],
			order_by="sequence asc",
			limit=limit,
		)
		items = []
		after = data["after"]
		for row in rows:
			if items and native_drive.page_should_yield():
				break
			source_id = row.source_id
			if source_id.startswith(ATTACHMENT_EVENT_PREFIX):
				source_id = "file:" + source_id.removeprefix(ATTACHMENT_EVENT_PREFIX)
			manifest = _project_event(source_id, row.client_id, clients, email_enabled)
			items.append(
				{
					"source_id": source_id,
					"client_id": row.client_id,
					"operation": "upsert" if manifest else "delete",
					"manifest": manifest,
				}
			)
			after = row.sequence
		return {
			"version": 1,
			"items": items,
			"next_cursor": encode({"kind": "changes", "after": after}, binding),
		}
