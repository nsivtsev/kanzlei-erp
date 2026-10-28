"""Server-only storage foundation; no intake, analysis or confirmation endpoint."""

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from uuid import uuid4

import frappe
from frappe.model.document import Document
from frappe.utils import cint, getdate, now_datetime

from kanzlei_erp.posteingang_setup import DEFAULTS

_scope = ContextVar("posteingang_operation", default=None)


def deny():
	frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)


@contextmanager
def _creation(letter, file):
	token = _scope.set((letter, file))
	try:
		yield
	finally:
		_scope.reset(token)


def creating(letter=None, file=None):
	state = _scope.get()
	return bool(state and (letter is None or state[0] == letter) and (file is None or state[1] == file))


class ProtectedRecord(Document):
	def before_insert(self):
		if not creating(
			self.name if self.doctype == "Incoming Letter" else self.get("letter") or self.parent
		):
			deny()

	def validate(self):
		letter = self.name if self.doctype == "Incoming Letter" else self.get("letter") or self.parent
		if not self.is_new() or not creating(letter):
			deny()
		if self.doctype == "Incoming Letter":
			validate_letter(self)
		elif self.doctype == "Incoming Letter Action":
			validate_action(self)
		elif self.doctype == "Incoming Letter Event":
			if self.revision_before != 0 or self.revision_after != 1 or self.event_type != "receive":
				deny()
		else:
			# Run lifecycle belongs to AP-05; no arbitrary insertion in AP-02.
			deny()

	def on_trash(self):
		deny()

	def before_rename(self, *args, **kwargs):
		deny()

	def db_set(self, *args, **kwargs):
		deny()

	def as_dict(self, *args, **kwargs):
		data = super().as_dict(*args, **kwargs)
		if not creating():
			# These are internal receipts/snapshots, not generic REST projections.
			# AP-06/07 add typed, permission-filtered projections when populated.
			for field in (
				"matching_result_json",
				"confirmation_receipt_json",
				"payload_json",
				"replaces_letter",
				"replacement_letter",
				"lease_token",
				"lease_expires_at",
				"source_snapshot_json",
				"configuration_json",
			):
				if field in data:
					data[field] = None
			if self.doctype == "Incoming Letter Event":
				data["result_json"] = None
		return data


def validate_letter(doc):
	if doc.status != "Received" or doc.revision != 1 or doc.analysis_generation != 0:
		deny()
	if any(
		doc.get(field)
		for field in (
			"current_run",
			"applied_run",
			"confirmed_customer",
			"confirmed_by",
			"confirmed_at",
			"replacement_letter",
			"replaces_letter",
			"correction_quarantined",
			"confirmation_receipt_json",
		)
	):
		deny()
	if bool(doc.received_date_known) != bool(doc.received_date):
		frappe.throw(frappe._("Receipt date must be explicit or unknown"), frappe.ValidationError)
	if doc.received_date:
		getdate(doc.received_date)
	if not 1 <= cint(doc.page_count) <= 50 or len(doc.actions) > 30:
		frappe.throw(frappe._("Posteingang limit exceeded"), frappe.ValidationError)
	file = frappe.get_doc("File", doc.original_file)
	if (
		not file.is_private
		or file.is_folder
		or file.attached_to_doctype != doc.doctype
		or file.attached_to_name != doc.name
	):
		deny()
	if len(doc.content_sha256 or "") != 64:
		frappe.throw(frappe._("Invalid content hash"), frappe.ValidationError)
	ids = set()
	for action in doc.actions:
		if not action.action_id:
			action.action_id = str(uuid4())
		if action.action_id in ids:
			frappe.throw(frappe._("Duplicate action identity"), frappe.ValidationError)
		ids.add(action.action_id)
		validate_action(action)


def validate_action(action):
	if (
		not (action.title or "").strip()
		or len(action.title) > 140
		or not (action.description or "").strip()
		or len(action.description) > 4000
	):
		frappe.throw(frappe._("Invalid action text"), frappe.ValidationError)
	if action.created_task or action.source_run or action.original_proposal_json:
		deny()


class SettingsValidationError(frappe.ValidationError, frappe.SecurityException):
	"""Expected validation without Frappe error snapshots containing serialized credentials."""


class SettingsRecord(Document):
	def as_dict(self, *args, **kwargs):
		data = super().as_dict(*args, **kwargs)
		if data.get("api_key"):
			data["api_key"] = "********"
		data["recovery_cursor"] = None
		return data

	def validate(self):
		from kanzlei_erp.posteingang_ai import concrete_model

		limits = {
			"max_file_bytes": (1, 20971520),
			"max_pages": (1, 50),
			"max_actions": (1, 30),
			"max_retries": (0, 2),
			"request_timeout_seconds": (1, 120),
			"retry_backoff_seconds": (30, 3600),
			"recovery_interval_seconds": (60, 3600),
			"recovery_batch_size": (1, 500),
			"worker_timeout_seconds": (300, 1800),
		}
		for field, (low, high) in limits.items():
			if not low <= cint(self.get(field)) <= high:
				frappe.throw(frappe._("Posteingang limit exceeded"), SettingsValidationError)
		for field in ("ocr_model", "extraction_model"):
			if self.get(field) and not concrete_model(self.get(field)):
				frappe.throw(frappe._("Use a concrete versioned Posteingang model"), SettingsValidationError)
		if cint(self.worker_timeout_seconds) < 2 * cint(self.request_timeout_seconds) + 60:
			frappe.throw(frappe._("Worker timeout is too short for both analysis steps"), SettingsValidationError)
		if self.provider == "mistral" and self.external_analysis_enabled:
			if not self.api_key or not self.ocr_model or not self.extraction_model:
				frappe.throw(frappe._("Mistral analysis requires an API key and both model versions"), SettingsValidationError)
		# Serialize concurrent Settings saves without decrypting the password.
		old = dict(frappe.db.sql("SELECT field, value FROM `tabSingles` WHERE doctype=%s FOR UPDATE", (self.doctype,)))
		fields = ("enabled", "external_analysis_enabled", "provider", "ocr_model", "extraction_model", "max_file_bytes", "max_pages", "max_actions", "request_timeout_seconds")
		def normalized(field, value):
			return cint(value) if self.meta.get_field(field).fieldtype in ("Int", "Check") else (value or "")

		changed = any(normalized(k, self.get(k)) != normalized(k, old.get(k)) for k in fields)
		password = self.get("api_key")
		changed |= bool(password and not self.is_dummy_password(password)) or bool(not password and old.get("api_key"))
		self.analysis_configuration_epoch = max(1, cint(old.get("analysis_configuration_epoch"))) + int(changed)
		self.recovery_cursor = old.get("recovery_cursor") or ""


def create_received_letter(*, actor, original, receipt, operation):
	from kanzlei_erp.object_storage import isolated_writes

	with isolated_writes():
		return _create_received_letter(actor=actor, original=original, receipt=receipt, operation=operation)


def create_correction_letter(*, old_letter, actor, original, receipt, operation):
	"""Only the administrative correction transaction can use this constructor."""
	from kanzlei_erp.object_storage import isolated_writes
	from kanzlei_erp.posteingang_confirmation import _authorized
	from kanzlei_erp.posteingang_jobs import _locked_letter

	old = _locked_letter(old_letter)
	_authorized(old, administrative=True)
	if old.status != "Completed" or old.replacement_letter or old.correction_quarantined:
		deny()
	with isolated_writes():
		return _create_received_letter(actor=actor, original=original, receipt=receipt, operation=operation, correction=True)


def _cleanup_correction_file(file):
	"""Core hash dedup cleanup cannot remove a distinct URL with equal bytes."""
	from pathlib import Path

	from kanzlei_erp.object_storage import stored

	if frappe.db.exists("File", {"file_url": file.file_url}):
		return
	if not stored(file) and str(file.file_url).startswith("/private/files/"):
		with _creation(file.attached_to_name, file.name):
			Path(file.get_full_path()).unlink(missing_ok=True)
	else:
		with _creation(file.attached_to_name, file.name):
			file.on_rollback()


def _create_received_letter(*, actor, original, receipt, operation, correction=False):
	"""Trusted server constructor. PDF content validation is provided by AP-03."""
	from kanzlei_erp.posteingang_permissions import staff

	if (
		actor != frappe.session.user
		or not staff(actor)
		or "Posteingang Operator" not in frappe.get_roles(actor)
	):
		deny()
	if not correction and not frappe.db.get_single_value("Posteingang Settings", "enabled"):
		deny()
	content = original["content"]
	settings = frappe.get_single("Posteingang Settings")
	if not isinstance(content, bytes) or not content or len(content) > min(settings.max_file_bytes, 20971520):
		frappe.throw(frappe._("Posteingang limit exceeded"), frappe.ValidationError)
	if not 1 <= original["page_count"] <= min(settings.max_pages, 50):
		frappe.throw(frappe._("Posteingang limit exceeded"), frappe.ValidationError)
	if bool(receipt["received_date_known"]) != bool(receipt.get("received_date")):
		frappe.throw(frappe._("Receipt date must be explicit or unknown"), frappe.ValidationError)
	letter_id, file_id = frappe.generate_hash(length=10), frappe.generate_hash(length=10)
	savepoint = "ap02_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	file = None
	try:
		with _creation(letter_id, file_id):
			file = frappe.get_doc(
				{
					"doctype": "File",
					"name": file_id,
					"file_name": original["file_name"],
					"content": content,
					"is_private": 1,
					"attached_to_doctype": "Incoming Letter",
					"attached_to_name": letter_id,
				}
			)
			if correction:
				# A distinct URL/binding is required: AP-02 intentionally intersects
				# all Letter permissions for aliases sharing the original URL.
				stem = original["file_name"].rsplit(".", 1)[0]
				file.file_name = file_id + "-" + stem[:120] + ".pdf"
				# ERPAttachmentGuard already forces a fresh write for _creation.
				# Let insert establish the storage creation capability first.
			file.insert(ignore_permissions=True, ignore_links=True, set_name=file_id)
			if correction:
				frappe.db.after_rollback.add(lambda: _cleanup_correction_file(file))
			doc = frappe.get_doc(
				{
					"doctype": "Incoming Letter",
					"name": letter_id,
					"original_file": file.name,
					"content_sha256": sha256(content).hexdigest(),
					"page_count": original["page_count"],
					"source": "erp_upload",
					"received_by": actor,
					"received_at": now_datetime(),
					"received_date_known": int(receipt["received_date_known"]),
					"received_date": receipt.get("received_date"),
					"status": "Received",
					"revision": 1,
					"analysis_generation": 0,
					"fact_evidence_json": "{}",
					"review_outcome": "pending",
					"intake_key": operation["key"],
					"intake_fingerprint": operation["fingerprint"],
				}
			)
			doc.insert(ignore_permissions=True, set_name=letter_id)
			frappe.get_doc(
				{
					"doctype": "Incoming Letter Event",
					"letter": doc.name,
					"event_type": "receive",
					"actor": actor,
					"occurred_at": now_datetime(),
					"revision_before": 0,
					"revision_after": 1,
					"generation": 0,
					"payload_json": "{}",
				}
			).insert(ignore_permissions=True)
			# Preserve rollback flags for a caller that writes an atomic receipt.
			doc._intake_file = file
			return doc
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		if file and file.flags.new_file:
			with _creation(letter_id, file_id):
				_cleanup_correction_file(file) if correction else file.on_rollback()
		raise
