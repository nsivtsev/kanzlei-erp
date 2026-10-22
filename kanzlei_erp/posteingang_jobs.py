"""AP-05 durable workflow. Only IDs enter RQ; no provider I/O under row locks.

HTTP endpoints and process_run/recover_runs own transaction boundaries. Internal
queue_received participates in intake's transaction and never commits it.
"""

import json
from functools import partial
from hashlib import sha256
from uuid import uuid4

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime
from rq.timeouts import JobTimeoutException

from kanzlei_erp import posteingang_ai as ai
from kanzlei_erp.posteingang import canonical
from kanzlei_erp.posteingang_ai_validation import InvalidResult, parse_json, validate_pages
from kanzlei_erp.posteingang_permissions import letter_access, staff

QUEUE = "long"
TERMINAL = {"Succeeded", "Failed", "Cancelled", "Superseded"}
QUEUE_FROM = {"Received", "Needs Review", "Needs Clarification", "Error"}
TRANSITIONS = {
	"Received": {"Queued", "Needs Review", "Needs Clarification", "Cancelled"},
	"Queued": {"Processing", "Error", "Needs Review", "Needs Clarification", "Cancelled"},
	"Processing": {"Needs Review", "Error", "Needs Clarification", "Cancelled"},
	"Needs Review": {"Queued", "Needs Clarification", "Cancelled"},
	"Needs Clarification": {"Needs Review", "Queued", "Cancelled"},
	"Error": {"Queued", "Needs Review", "Needs Clarification", "Cancelled"},
	"Completed": set(), "Cancelled": set(),
}
LETTER_FIELDS = {"status", "analysis_generation", "current_run", "cancellation_reason", "selected_customer", "customer_reason"}
RUN_FIELDS = {"status", "stage", "started_at", "finished_at", "attempt_count", "error_code", "error_stage",
	"next_attempt_at", "lease_token", "lease_expires_at", "ocr_pages_json", "result_json", "reused_ocr_from"}


class WorkflowError(frappe.ValidationError, frappe.SecurityException):
	"""Expected failure, without snapshots of request/document content."""


def fail(code):
	frappe.throw(code, WorkflowError)


def _text(value, limit):
	if not isinstance(value, str) or not value.strip() or len(value) > limit:
		return False
	try:
		value.encode("utf-8")
	except UnicodeError:
		return False
	return True


def _rollback_safely():
	try:
		frappe.db.rollback()
	except Exception:
		pass


def _log(code):
	try:
		frappe.logger("posteingang").warning(code)
	except Exception:
		pass


def _clear():
	frappe.db.value_cache.clear()
	frappe.local.cache = {}
	frappe.local.role_permissions = {}
	frappe.local.user_perms = None


def _fresh():
	frappe.db.rollback()
	_clear()


def _http_boundary():
	if getattr(frappe.local, "request", None) is not None:
		_fresh()
	else:
		_clear()


def _operator(actor):
	return staff(actor) and bool(frappe.db.exists("Has Role", {"parent": actor, "parenttype": "User", "role": "Posteingang Operator"}))


def _authorized(letter, actor):
	return bool(_operator(actor) and letter_access(letter, actor) and not letter.correction_quarantined
		and (not letter.selected_customer or frappe.has_permission("Customer", "write", doc=letter.selected_customer, user=actor)))


def _locked_letter(name):
	if not _text(name, 140):
		fail("INVALID_REQUEST")
	rows = frappe.db.sql("SELECT * FROM `tabIncoming Letter` WHERE name=%s FOR UPDATE", (name,), as_dict=True)
	if not rows:
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	return rows[0]


def _locked_run(name):
	rows = frappe.db.sql("SELECT * FROM `tabIncoming Letter Run` WHERE name=%s FOR UPDATE", (name,), as_dict=True)
	return rows[0] if rows else None


def _snapshot(letter):
	return {"original_file": letter.original_file, "content_sha256": letter.content_sha256,
		"page_count": letter.page_count, "received_date_known": bool(letter.received_date_known),
		"received_date": str(letter.received_date) if letter.received_date else None}


def _configuration(settings):
	return {key: settings.get(key) for key in ("provider", "ocr_model", "extraction_model", "max_file_bytes", "max_pages", "max_actions", "request_timeout_seconds", "max_retries", "retry_backoff_seconds", "worker_timeout_seconds")}


def _analysis_ready(settings):
	return bool(settings.enabled and (settings.provider == "fake" or settings.external_analysis_enabled)
		and ai._valid_configuration(settings) and (settings.provider != "mistral" or settings.api_key))


def _insert(doctype, values):
	# Narrow server implementation, not a permission/context bypass available to
	# document.save(). Inputs are built exclusively by this module's workflow.
	doc = frappe.get_doc({"doctype": doctype, "name": frappe.generate_hash(length=10), **values})
	doc.db_insert()
	return doc


def _change(letter, actor, event_type, changes=None, *, run=None, run_changes=None, payload=None, receipt=None):
	changes, run_changes = changes or {}, run_changes or {}
	if set(changes) - LETTER_FIELDS or set(run_changes) - RUN_FIELDS:
		fail("INVALID_REQUEST")
	new_status = changes.get("status", letter.status)
	if new_status != letter.status and new_status not in TRANSITIONS[letter.status]:
		fail("INVALID_TRANSITION")
	if run_changes and (not run or run.letter != letter.name or run.status in TERMINAL):
		fail("STALE_RUN")
	before = letter.revision
	if run_changes:
		frappe.db.set_value("Incoming Letter Run", run.name, run_changes)
		run.update(run_changes)
	frappe.db.set_value("Incoming Letter", letter.name, {**changes, "revision": before + 1})
	letter.update({**changes, "revision": before + 1})
	result = {"schema_version": "1.0", "letter": letter.name, "run": letter.current_run,
		"generation": letter.analysis_generation, "revision": letter.revision, "status": letter.status}
	_insert("Incoming Letter Event", {"letter": letter.name, "actor": actor, "event_type": event_type,
		"occurred_at": now_datetime(), "revision_before": before, "revision_after": letter.revision,
		"generation": letter.analysis_generation, "payload_json": canonical(payload or {}),
		**({"operation_key": receipt[0], "payload_fingerprint": receipt[1], "result_json": canonical(result)} if receipt else {})})
	frappe.clear_document_cache("Incoming Letter", letter.name)
	if run:
		frappe.clear_document_cache("Incoming Letter Run", run.name)
	return {**result, "replayed": False}


def _schedule(run_name):
	frappe.db.after_commit.add(partial(_dispatch, run_name))


def _dispatch(run_name):
	"""A failed queue write cannot turn a durable intake commit into failure."""
	try:
		run = frappe.db.get_value("Incoming Letter Run", run_name,
			["status", "attempt_count", "configuration_json"], as_dict=True)
		if not run or run.status != "Queued":
			return
		config = parse_json(run.configuration_json)
		frappe.enqueue("kanzlei_erp.posteingang_jobs.process_run", queue=QUEUE,
			timeout=config["worker_timeout_seconds"], run_name=run_name,
			job_id=f"ap05-{run_name}-{run.attempt_count + 1}", deduplicate=True)
	except Exception:
		# No traceback, exception repr, provider context or content in diagnostics.
		_log("QUEUE_UNAVAILABLE")


def _new_run(letter, actor, settings, receipt=None):
	generation = letter.analysis_generation + 1
	run = _insert("Incoming Letter Run", {"letter": letter.name, "generation": generation,
		"queued_by": actor, "schema_version": "1.0", "prompt_version": ai.PROMPT_VERSION if settings.provider == "mistral" else "fake-v1",
		"provider": settings.provider, "ocr_model": settings.ocr_model if settings.provider == "mistral" else None,
		"extraction_model": settings.extraction_model if settings.provider == "mistral" else None,
		"source_snapshot_json": canonical(_snapshot(letter)), "configuration_json": canonical(_configuration(settings)),
		"configuration_epoch": settings.analysis_configuration_epoch, "status": "Queued", "stage": "pending",
		"queued_at": now_datetime(), "next_attempt_at": now_datetime(), "attempt_count": 0})
	result = _change(letter, actor, "queue_analysis", {"status": "Queued", "analysis_generation": generation, "current_run": run.name}, receipt=receipt)
	_schedule(run.name)
	return result


def queue_received(letter_name):
	"""Intake-only integration, same transaction; never walks old Received rows."""
	settings = frappe.get_single("Posteingang Settings")
	if settings.auto_analysis_enabled and _analysis_ready(settings):
		letter = _locked_letter(letter_name)
		if letter.status == "Received" and not letter.current_run and _authorized(letter, frappe.session.user):
			_new_run(letter, frappe.session.user, settings)


def _operation(operation, letter_name, expected_revision, request_key, reason=None, extra=None):
	_http_boundary()
	extra = dict(extra or {})
	cmd = extra.pop("cmd", None)
	if cmd not in (None, "kanzlei_erp.posteingang_jobs." + operation) or extra or not _text(request_key, 80):
		fail("INVALID_REQUEST")
	if type(expected_revision) is str and expected_revision.isascii() and expected_revision.isdecimal() and len(expected_revision) <= 10:
		expected_revision = int(expected_revision)
	if type(expected_revision) is not int or not 1 <= expected_revision <= 2147483647:
		fail("INVALID_REQUEST")
	if operation == "cancel" and not _text(reason, 1000):
		fail("INVALID_REQUEST")
	letter = _locked_letter(letter_name)
	actor = frappe.session.user
	if not _authorized(letter, actor):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	key = sha256(canonical([actor, letter.name, operation, request_key]).encode()).hexdigest()
	fingerprint = sha256(canonical({"schema_version": "1.0", "operation": operation, "letter": letter.name, "reason": reason}).encode()).hexdigest()
	old = frappe.db.get_value("Incoming Letter Event", {"operation_key": key}, ["payload_fingerprint", "result_json"], as_dict=True, for_update=True)
	if old:
		if old.payload_fingerprint != fingerprint:
			fail("IDEMPOTENCY_CONFLICT")
		return {**parse_json(old.result_json), "replayed": True}
	if expected_revision != letter.revision:
		fail("REVISION_CONFLICT")
	settings = frappe.get_single("Posteingang Settings")
	if operation == "queue_analysis":
		if letter.status in ("Queued", "Processing"):
			fail("ANALYSIS_IN_PROGRESS")
		if letter.status not in QUEUE_FROM:
			fail("INVALID_TRANSITION")
		if not _analysis_ready(settings):
			fail("ANALYSIS_STOPPED")
		return _new_run(letter, actor, settings, (key, fingerprint))
	status = "Cancelled" if operation == "cancel" else "Needs Review"
	if status not in TRANSITIONS[letter.status] or (operation == "manual_review" and not settings.enabled):
		fail("INVALID_TRANSITION")
	run = _locked_run(letter.current_run) if letter.current_run else None
	changes = {"status": status}
	if operation == "cancel" or letter.status in ("Queued", "Processing"):
		changes["analysis_generation"] = letter.analysis_generation + 1
	if operation == "cancel":
		changes["cancellation_reason"] = reason
	run_changes = {}
	if run and run.status not in TERMINAL:
		run_changes = {"status": "Cancelled" if operation == "cancel" else "Superseded", "finished_at": now_datetime(), "stage": "finished", "lease_token": None, "lease_expires_at": None}
	return _change(letter, actor, operation, changes, run=run, run_changes=run_changes,
		payload={"reason": reason} if reason else None, receipt=(key, fingerprint))


@frappe.whitelist(methods=["POST"])
def queue_analysis(letter=None, expected_revision=None, request_key=None, **extra):
	return _operation("queue_analysis", letter, expected_revision, request_key, extra=extra)


@frappe.whitelist(methods=["POST"])
def manual_review(letter=None, expected_revision=None, request_key=None, **extra):
	return _operation("manual_review", letter, expected_revision, request_key, extra=extra)


@frappe.whitelist(methods=["POST"])
def cancel(letter=None, expected_revision=None, request_key=None, reason=None, **extra):
	return _operation("cancel", letter, expected_revision, request_key, reason, extra)


@frappe.whitelist(methods=["GET", "POST"])
def get_analysis_state(letter=None, **extra):
	cmd = extra.pop("cmd", None)
	if cmd not in (None, "kanzlei_erp.posteingang_jobs.get_analysis_state") or extra or not _text(letter, 140):
		fail("INVALID_REQUEST")
	_http_boundary()
	doc = frappe.get_doc("Incoming Letter", letter)
	if not letter_access(doc) or (not doc.confirmed_customer and not _operator(frappe.session.user)):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)
	settings = frappe.get_single("Posteingang Settings")
	allowed = []
	if _authorized(doc, frappe.session.user):
		if doc.status in QUEUE_FROM and _analysis_ready(settings):
			allowed.append("queue_analysis")
		if "Needs Review" in TRANSITIONS[doc.status] and settings.enabled:
			allowed.append("manual_review")
		if "Cancelled" in TRANSITIONS[doc.status]:
			allowed.append("cancel")
	run = None
	if doc.current_run:
		run = frappe.db.get_value("Incoming Letter Run", {"name": doc.current_run, "letter": doc.name},
			["name", "status", "stage", "attempt_count", "next_attempt_at", "error_code", "error_stage"], as_dict=True)
	return {"schema_version": "1.0", "letter": doc.name, "revision": doc.revision,
		"generation": doc.analysis_generation, "status": doc.status, "run": run, "allowed_operations": allowed}


def _pair(run_name):
	name = frappe.db.get_value("Incoming Letter Run", run_name, "letter")
	if not name:
		return None, None
	return _locked_letter(name), _locked_run(run_name)


def _current(letter, run):
	return bool(letter and run and letter.current_run == run.name and letter.analysis_generation == run.generation
		and letter.status in ("Queued", "Processing") and run.status not in TERMINAL)


def _permitted(letter, run):
	settings = frappe.get_single("Posteingang Settings")
	if not run.queued_by or not run.configuration_json or not run.source_snapshot_json:
		return False
	try:
		config, snapshot = parse_json(run.configuration_json), parse_json(run.source_snapshot_json)
	except InvalidResult:
		return False
	return bool(_authorized(letter, run.queued_by) and _analysis_ready(settings)
		and settings.analysis_configuration_epoch == run.configuration_epoch
		and snapshot == _snapshot(letter)
		and run.prompt_version == (ai.PROMPT_VERSION if settings.provider == "mistral" else "fake-v1")
		and all(config.get(k) == settings.get(k) for k in
			("provider", "ocr_model", "extraction_model", "max_file_bytes", "max_pages", "max_actions", "request_timeout_seconds")))


def _stop(letter, run, code="ANALYSIS_STOPPED", stage="worker"):
	_change(letter, run.queued_by or letter.received_by, "analysis_stopped" if code == "ANALYSIS_STOPPED" else "analysis_failed",
		{"status": "Error"}, run=run, run_changes={"status": "Cancelled" if code == "ANALYSIS_STOPPED" else "Failed",
		"finished_at": now_datetime(), "stage": "finished", "lease_token": None, "lease_expires_at": None,
		"error_code": code, "error_stage": stage})


def _claim(run_name):
	_fresh()
	letter, run = _pair(run_name)
	if not _current(letter, run) or run.status != "Queued" or get_datetime(run.next_attempt_at) > now_datetime():
		frappe.db.rollback()
		return None
	if not _permitted(letter, run):
		_stop(letter, run)
		frappe.db.commit()
		return None
	config = parse_json(run.configuration_json)
	if run.attempt_count >= config["max_retries"] + 1:
		_stop(letter, run, run.error_code or "INTERNAL_ERROR", run.error_stage or "worker")
		frappe.db.commit()
		return None
	token = str(uuid4())
	_change(letter, run.queued_by, "analysis_started", {"status": "Processing"}, run=run,
		run_changes={"status": "Running", "stage": "prepare", "started_at": run.started_at or now_datetime(),
		"attempt_count": run.attempt_count + 1, "lease_token": token,
		"lease_expires_at": add_to_date(now_datetime(), seconds=config["worker_timeout_seconds"] + 60),
		"error_code": None, "error_stage": None})
	frappe.db.commit()
	return run.queued_by, token


def _fenced(run_name, token):
	_fresh()
	letter, run = _pair(run_name)
	if not _current(letter, run) or run.status != "Running" or run.lease_token != token or get_datetime(run.lease_expires_at) <= now_datetime():
		frappe.db.rollback()
		return None, None
	if not _permitted(letter, run):
		_stop(letter, run)
		frappe.db.commit()
		return None, None
	return letter, run


def _ocr(run, source):
	if not run.ocr_pages_json:
		return None
	try:
		pages = validate_pages(parse_json(run.ocr_pages_json)["pages"], source.page_count)
	except (InvalidResult, KeyError, TypeError) as error:
		raise InvalidResult("LIMIT_EXCEEDED" if isinstance(error, InvalidResult) and error.code == "LIMIT_EXCEEDED" else "OCR_FAILED") from None
	return ai.OcrResult(source.content_sha256, run.ocr_model or None, run.provider, pages)


def _reusable(run, source):
	if run.ocr_pages_json:
		return _ocr(run, source), run.reused_ocr_from
	rows = frappe.db.sql("""SELECT * FROM `tabIncoming Letter Run` WHERE letter=%s AND generation<%s
		AND ocr_pages_json IS NOT NULL ORDER BY generation DESC""", (run.letter, run.generation), as_dict=True)
	for old in rows:
		if old.schema_version == run.schema_version and old.provider == run.provider and old.ocr_model == run.ocr_model and parse_json(old.source_snapshot_json) == parse_json(run.source_snapshot_json):
			return _ocr(old, source), old.name
	return None, None


def _failure(run_name, token, failure):
	letter, run = _fenced(run_name, token)
	if not run:
		return
	config = parse_json(run.configuration_json)
	if failure.code == "ANALYSIS_STOPPED":
		_stop(letter, run)
	elif failure.retryable and run.attempt_count < config["max_retries"] + 1:
		delay = min(3600, max(config["retry_backoff_seconds"] * 2 ** (run.attempt_count - 1), failure.retry_after_seconds or 0))
		_change(letter, run.queued_by, "analysis_retry_scheduled", run=run,
			run_changes={"status": "Queued", "next_attempt_at": add_to_date(now_datetime(), seconds=delay),
			"lease_token": None, "lease_expires_at": None, "error_code": failure.code, "error_stage": failure.stage})
	else:
		_stop(letter, run, failure.code, failure.stage)
	frappe.db.commit()


def process_run(run_name):
	"""RQ boundary. Always return a small, content-free result, never traceback."""
	previous_user, token = frappe.session.user, None
	try:
		claim = _claim(run_name)
		if not claim:
			return {"code": "STALE_RUN"}
		actor, token = claim
		frappe.set_user(actor)
		_fresh()
		source = ai.prepare_source(frappe.db.get_value("Incoming Letter Run", run_name, "letter"), actor=actor)
		if isinstance(source, ai.Failure):
			_failure(run_name, token, source)
			return {"code": source.code}
		letter, run = _fenced(run_name, token)
		if not run:
			return {"code": "STALE_RUN"}
		try:
			ocr, reused = _reusable(run, source)
		except InvalidResult as error:
			_failure(run_name, token, ai.Failure(error.code, "ocr"))
			return {"code": error.code}
		_change(letter, actor, "analysis_stage", run=run, run_changes={"stage": "extraction" if ocr else "ocr"})
		frappe.db.commit()
		if not ocr:
			_fresh()
			ocr = ai.recognize(source)
			if isinstance(ocr, ai.Failure):
				_failure(run_name, token, ocr)
				return {"code": ocr.code}
		letter, run = _fenced(run_name, token)
		if not run:
			return {"code": "STALE_RUN"}
		_change(letter, actor, "ocr_checkpoint", run=run, run_changes={"ocr_pages_json": canonical({"pages": ocr.pages}), "reused_ocr_from": reused, "stage": "extraction"})
		frappe.db.commit()
		# This fence precedes the second paid operation, after durable OCR.
		letter, run = _fenced(run_name, token)
		if not run:
			return {"code": "STALE_RUN"}
		frappe.db.rollback()  # release all row locks before HTTP
		result = ai.extract(source, ocr)
		if isinstance(result, ai.Failure):
			_failure(run_name, token, result)
			return {"code": result.code}
		letter, run = _fenced(run_name, token)
		if not run:
			return {"code": "STALE_RUN"}
		_change(letter, actor, "stage_result", {"status": "Needs Review"}, run=run,
			run_changes={"status": "Succeeded", "stage": "finished", "finished_at": now_datetime(),
			"result_json": canonical(result.extraction), "lease_token": None, "lease_expires_at": None})
		frappe.db.commit()
		return {"code": "SUCCEEDED"}
	except Exception as error:
		# Includes RQ's catchable JobTimeoutException. SIGKILL is handled by lease recovery.
		code = "WORKER_INTERRUPTED" if isinstance(error, JobTimeoutException) else error.code if isinstance(error, InvalidResult) else "INTERNAL_ERROR"
		try:
			_fresh()
			if token:
				_failure(run_name, token, ai.Failure(code, "worker"))
		except Exception:
			# Unknown DB outcome: leave the durable lease for recovery; never log frames.
			pass
		return {"code": code}
	finally:
		_rollback_safely()
		frappe.set_user(previous_user)


def recover_runs():
	"""Bounded Frappe scheduler sweep; never makes provider calls."""
	previous_user = frappe.session.user
	try:
		_fresh()
		settings = frappe.get_single("Posteingang Settings")
		# Redis throttles healthy scheduling, but its outage must not stop DB cleanup.
		try:
			if not frappe.cache.set(frappe.cache.make_key("ap05-recovery-tick"), "1", ex=settings.recovery_interval_seconds, nx=True):
				return
		except Exception:
			pass
		batch = settings.recovery_batch_size
		active = """`tabIncoming Letter Run` r JOIN `tabIncoming Letter` l
			ON l.name=r.letter AND l.current_run=r.name AND l.analysis_generation=r.generation
			AND l.status IN ('Queued','Processing')"""
		expired = frappe.db.sql(f"""SELECT r.name FROM {active} WHERE r.status='Running'
			AND r.lease_expires_at<=%s ORDER BY r.lease_expires_at, r.name LIMIT %s""", (now_datetime(), batch), pluck=True)
		remaining = batch - len(expired)
		rotating = []
		if remaining:
			for comparison in (">", "<="):
				rotating += frappe.db.sql(f"""SELECT r.name FROM {active}
					WHERE r.status IN ('Queued','Running') AND r.name {comparison} %s
					AND (r.status!='Running' OR r.lease_expires_at IS NULL OR r.lease_expires_at>%s)
					ORDER BY r.name LIMIT %s""", (settings.recovery_cursor or "", now_datetime(), remaining - len(rotating)), pluck=True)
				if len(rotating) == remaining:
					break
		names = expired + rotating
		for name in names:
			_fresh()
			letter, run = _pair(name)
			if not _current(letter, run):
				if name in rotating:
					frappe.db.set_single_value("Posteingang Settings", "recovery_cursor", name, update_modified=False)
					frappe.db.commit()
				else:
					frappe.db.rollback()
				continue
			frappe.set_user(run.queued_by or previous_user)
			if not _permitted(letter, run):
				_stop(letter, run)
			elif run.status == "Running" and get_datetime(run.lease_expires_at) <= now_datetime():
				_stop(letter, run, "WORKER_INTERRUPTED", "worker")
			elif run.status == "Queued" and get_datetime(run.next_attempt_at) <= now_datetime():
				_schedule(run.name)
			if name in rotating:
				# Operational cursor, not business state or configuration epoch.
				frappe.db.set_single_value("Posteingang Settings", "recovery_cursor", name, update_modified=False)
			frappe.db.commit()
	except Exception:
		_rollback_safely()
		_log("RECOVERY_UNAVAILABLE")
	finally:
		_rollback_safely()
		frappe.set_user(previous_user)
