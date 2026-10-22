"""Public PDF intake. No analysis, review or confirmation operations."""

import json
import re
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from hashlib import sha256

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from kanzlei_erp.posteingang_pdf import validate_pdf
from kanzlei_erp.posteingang_permissions import letter_access, staff

RECEIPT = "Incoming Letter Intake Receipt"
_receipt_write = ContextVar("posteingang_receipt_write", default=False)


def fail(code):
	frappe.throw(code, frappe.ValidationError)


def canonical(value):
	return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def operation_key(actor, key):
	return sha256(canonical([actor, "receive", key]).encode()).hexdigest()


def _actor():
	actor = frappe.session.user
	# Avoid role caches when the request has waited for another upload.
	roles = frappe.db.sql("SELECT role FROM `tabHas Role` WHERE parent=%s AND parenttype='User'", (actor,))
	if not staff(actor) or "Posteingang Operator" not in {r[0] for r in roles}:
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	return actor


def _key(key):
	if not isinstance(key, str) or not key.strip() or len(key) > 80:
		fail("INVALID_REQUEST")
	return key


def _receipt_date(known, value):
	if known is True or known == "true":
		known = True
	elif known is False or known == "false":
		known = False
	else:
		fail("INVALID_REQUEST")
	if not known:
		if value is not None:
			fail("INVALID_REQUEST")
		return {"received_date_known": False, "received_date": None}
	try:
		if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
			raise ValueError
		date.fromisoformat(value)
	except ValueError:
		fail("INVALID_REQUEST")
	return {"received_date_known": True, "received_date": value}


def _filename(value):
	name = str(value or "document.pdf").replace("\\", "/").rsplit("/", 1)[-1]
	name = "".join(c for c in name if not unicodedata.category(c).startswith("C")).strip(" .")
	return (name[:130] or "document") + ("" if name.lower().endswith(".pdf") else ".pdf")


def _limits():
	settings = frappe.get_single("Posteingang Settings")
	return settings, min(settings.max_file_bytes, 20971520), min(settings.max_pages, 50)


def _lock_names(key, digest):
	return [
		"ap03:" + sha256((frappe.local.site + "\0" + kind + value).encode()).hexdigest()[:58]
		for kind, value in (("request", key), ("content", digest))
	]


@contextmanager
def _locks(key, digest, *, fresh=False):
	acquired = []
	retained = False

	def release():
		while acquired:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (acquired.pop(),))

	def retain():
		nonlocal retained
		retained = True
		frappe.db.after_commit.add(release)
		frappe.db.after_rollback.add(release)
		# Fallback for request termination; after_request runs after transaction handling.
		frappe.local.ap03_release = release

	try:
		for name in _lock_names(key, digest):
			if frappe.db.sql("SELECT GET_LOCK(%s,5)", (name,))[0][0] != 1:
				fail("INTAKE_BUSY")
			acquired.append(name)
		if fresh:
			# This HTTP operation owns its transaction. Everything before here is
			# read-only. Reset repeatable-read snapshots after waiting for locks.
			frappe.db.rollback()
			frappe.db.value_cache.clear()
		# Clear request-local permission caches without changing login/session.
		frappe.local.cache = {}
		frappe.local.role_permissions = {}
		frappe.local.user_perms = None
		yield retain
	finally:
		if not retained:
			release()


def release_request_locks(response=None):
	if release := getattr(frappe.local, "ap03_release", None):
		release()
		frappe.local.ap03_release = None


def _lookup(key):
	rows = frappe.db.sql(f"SELECT * FROM `tab{RECEIPT}` WHERE name=%s", (key,), as_dict=True)
	return rows[0] if rows else None


def _visible(row):
	rows = frappe.db.sql("SELECT * FROM `tabIncoming Letter` WHERE name=%s", (row.letter,), as_dict=True)
	return rows and letter_access(rows[0])


def _existing(key, fingerprint, digest):
	if row := _lookup(key):
		if not _visible(row):
			fail("CANNOT_ACCEPT_FILE")
		if row.payload_fingerprint != fingerprint:
			fail("IDEMPOTENCY_CONFLICT")
		return {**json.loads(row.result_json), "replayed": True}, None
	settings, _, _ = _limits()
	if not settings.enabled:
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	rows = frappe.db.sql(
		"""SELECT * FROM `tabIncoming Letter` WHERE content_sha256=%s
		ORDER BY (status='Cancelled'), received_at, name""",
		(digest,),
		as_dict=True,
	)
	for row in rows:
		if letter_access(row):
			return None, row
	if rows:
		fail("CANNOT_ACCEPT_FILE")
	return None, None


def _save_receipt(key, actor, fingerprint, result):
	token = _receipt_write.set(True)
	try:
		frappe.get_doc(
			{
				"doctype": RECEIPT,
				"name": key,
				"actor": actor,
				"payload_fingerprint": fingerprint,
				"letter": result["letter"],
				"outcome": result["outcome"],
				"result_json": canonical(result),
				"created_at": now_datetime(),
			}
		).insert(ignore_permissions=True, set_name=key)
	finally:
		_receipt_write.reset(token)


class IntakeReceipt(Document):
	def before_insert(self):
		if not _receipt_write.get():
			frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)

	def validate(self):
		if not self.is_new():
			frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
		self.before_insert()

	def on_trash(self):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)

	before_rename = on_trash

	def db_set(self, *args, **kwargs):
		self.on_trash()


def _receive(stream, filename, request_key, received_date_known, received_date=None, *, fresh=False):
	from kanzlei_erp.object_storage import isolated_writes
	from kanzlei_erp.posteingang_model import create_received_letter

	actor = _actor()
	key = operation_key(actor, _key(request_key))
	receipt = _receipt_date(received_date_known, received_date)
	_, max_bytes, _ = _limits()
	# A prior successful request is replayable after limits are lowered.
	read_limit = 20971520 if frappe.db.exists(RECEIPT, key) else max_bytes
	content = stream.read(read_limit + 1)
	if not content:
		fail("INVALID_REQUEST")
	if len(content) > read_limit:
		fail("FILE_TOO_LARGE")
	digest = sha256(content).hexdigest()
	fingerprint = sha256(
		canonical(
			{"schema_version": "1.0", "source": "erp_upload", "content_sha256": digest, **receipt}
		).encode()
	).hexdigest()
	with _locks(key, digest, fresh=fresh) as retain:
		_actor()
		result, duplicate = _existing(key, fingerprint, digest)
		if result:
			return result
		if duplicate:
			result = {
				"schema_version": "1.0",
				"outcome": "duplicate",
				"letter": duplicate.name,
				"original_file": duplicate.original_file,
				"revision": duplicate.revision,
				"status": duplicate.status,
			}
			savepoint = "ap03_" + frappe.generate_hash(length=10)
			frappe.db.savepoint(savepoint)
			try:
				_save_receipt(key, actor, fingerprint, result)
				retain()
				return {**result, "replayed": False}
			except Exception:
				frappe.db.rollback(save_point=savepoint)
				raise
		_, max_bytes, max_pages = _limits()
		if len(content) > max_bytes:
			fail("FILE_TOO_LARGE")
	# No locks, database writes or persistent files during parsing.
	try:
		pages = validate_pdf(content, max_pages)
	except ValueError as exc:
		fail(str(exc))
	with _locks(key, digest, fresh=fresh) as retain:
		_actor()
		result, duplicate = _existing(key, fingerprint, digest)
		if result:
			return result
		_, max_bytes, max_pages = _limits()
		if len(content) > max_bytes:
			fail("FILE_TOO_LARGE")
		if pages > max_pages:
			fail("PAGE_LIMIT_EXCEEDED")
		savepoint = "ap03_" + frappe.generate_hash(length=10)
		frappe.db.savepoint(savepoint)
		created = None
		try:
			with isolated_writes():
				doc = duplicate
				if doc is None:
					doc = create_received_letter(
						actor=actor,
						original={"content": content, "file_name": _filename(filename), "page_count": pages},
						receipt=receipt,
						operation={"key": key, "fingerprint": fingerprint},
					)
					created = doc._intake_file
					frappe.local.ap03_created_file = created
				result = {
					"schema_version": "1.0",
					"outcome": "duplicate" if duplicate else "created",
					"letter": doc.name,
					"original_file": doc.original_file,
					"revision": doc.revision,
					"status": doc.status,
				}
				_save_receipt(key, actor, fingerprint, result)
				if duplicate is None:
					from kanzlei_erp.posteingang_jobs import queue_received

					queue_received(doc.name)
				retain()
				return {**result, "replayed": False}
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			if created:
				created.on_rollback()
			raise


@frappe.whitelist(methods=["POST"])
def receive(request_key=None, received_date_known=None, received_date=None, **extra):
	from kanzlei_erp.object_storage import isolated_writes

	_actor()
	files = list(frappe.request.files.items(multi=True))
	cmd = extra.pop("cmd", None)
	if (
		cmd not in (None, "kanzlei_erp.posteingang.receive")
		or extra
		or len(files) != 1
		or files[0][0] != "file"
	):
		fail("INVALID_REQUEST")
	file = files[0][1]
	frappe.local.ap03_created_file = None
	result = None
	# Preserve intents if commit outcome cannot be established. Never delete
	# an original merely because the database connection became unavailable.
	with isolated_writes(cleanup_on_error=False) as cleanups:
		try:
			result = _receive(
				file.stream, file.filename, request_key, received_date_known, received_date, fresh=True
			)
			# Commit belongs to this one-file HTTP operation. Catch failures before
			# Frappe's later sync_database(), which resets rollback callbacks.
			frappe.db.commit()
			return result
		except Exception:
			frappe.db.rollback()
			# An after_commit callback can fail after SQL COMMIT succeeded. Never
			# remove durable bytes in that case; a retry recovers the receipt.
			if result and frappe.db.exists(RECEIPT, operation_key(frappe.session.user, request_key)):
				return result
			if created := frappe.local.ap03_created_file:
				created.on_rollback()
			for cleanup in cleanups:
				cleanup()
			raise
		finally:
			release_request_locks()


@frappe.whitelist(methods=["POST"])
def get_intake_result(request_key):
	actor = _actor()
	key = operation_key(actor, _key(request_key))
	# Read endpoint does not lock or reveal another user's request namespace.
	row = frappe.db.get_value(RECEIPT, key, ["letter", "result_json"], as_dict=True)
	if not row or not letter_access(frappe.get_doc("Incoming Letter", row.letter)):
		return {"outcome": "unknown"}
	return {**json.loads(row.result_json), "replayed": True}


@frappe.whitelist()
def get_intake_options():
	_actor()
	settings, max_bytes, max_pages = _limits()
	return {"enabled": bool(settings.enabled), "max_file_bytes": max_bytes, "max_pages": max_pages}
