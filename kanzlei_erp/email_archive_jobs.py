"""Durable, account-scoped archive jobs for the Email Account form."""

from functools import partial

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime, validate_email_address
from frappe.utils.background_jobs import get_job_status
from rq import get_current_job
from rq.timeouts import JobTimeoutException

from kanzlei_erp.email_archive import _require_archive_account, import_archive

DOCTYPE = "Email Archive Import"
ACTIVE = ("Queued", "Running")
RUN_TIMEOUT = 6 * 60 * 60
STATE_FIELDS = ("name", "email_account", "status", "stage", "total", "processed", "imported",
	"existing", "failed", "unsaved_attachments", "new_contact_links", "queued_at", "started_at", "finished_at", "error_code")


def receiver_lock(account, *, timeout=RUN_TIMEOUT + 120):
	# Use a site-scoped key: Redis.lock itself does not call make_key.
	return frappe.cache.lock(frappe.cache.make_key(f"email-archive-receiver:{account}"), timeout=timeout)


class ArchiveReceiverGuard:
	"""Serialize the ordinary receiver with archive workers, including manual pulls."""

	def receive(self):
		job = get_current_job()
		# A killed ordinary short-queue receiver must not pause mail for six hours.
		timeout = job.timeout if job and job.timeout and job.timeout > 0 else 300
		lock = receiver_lock(self.name, timeout=timeout + 120)
		if not lock.acquire(blocking=False):
			return
		try:
			return super().receive()
		finally:
			if lock.owned():
				lock.release()


def _require_access(account, *, for_update=False):
	user = frappe.session.user
	if user == "Guest" or not ({"System Manager", "Super Email User"} & set(frappe.get_roles())):
		frappe.throw(_("Only email administrators may reimport an email archive."), frappe.PermissionError)
	if not frappe.db.get_value("User", user, "enabled"):
		frappe.throw(_("Please log in as a staff user"), frappe.PermissionError)
	doc = frappe.get_doc("Email Account", account, for_update=for_update)
	doc.check_permission("write")
	return doc


def _state(run):
	return {**{key: run.get(key) for key in STATE_FIELDS}, "folders": frappe.parse_json(run.folders_json or "[]")}


def _latest(account, *, active=False):
	if active:
		# A current read is essential after waiting for the account row lock:
		# an earlier permission query may already have opened an old DB snapshot.
		table = frappe.qb.DocType(DOCTYPE)
		rows = (frappe.qb.from_(table).select(table.star).where(table.email_account == account)
			.where(table.status.isin(ACTIVE)).orderby(table.creation, order=frappe.qb.desc)
			.limit(1).for_update().run(as_dict=True))
		return rows[0] if rows else None
	filters = {"email_account": account}
	names = frappe.get_all(DOCTYPE, filters=filters, order_by="creation desc, name desc", limit=1, pluck="name")
	return frappe.get_doc(DOCTYPE, names[0]) if names else None


@frappe.whitelist(methods=["POST"])
def get_import_state(email_account):
	_require_access(email_account)
	run = _latest(email_account)
	return _state(run) if run else None


@frappe.whitelist(methods=["POST"])
def start_import(email_account, sender_aliases=None):
	# This POST owns its transaction. Drop the authentication/boot snapshot before
	# taking the first lock, just as the other transaction-owning workflows do.
	if getattr(frappe.local, "request", None) is not None:
		frappe.db.rollback()
	for attempt in range(3):
		try:
			return _start_import(email_account, sender_aliases)
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			# MariaDB snapshot isolation can reject even a current read that
			# waited on an insert. Retry the entire transaction, including access.
			frappe.db.rollback()
			if attempt == 2:
				raise


def _start_import(email_account, sender_aliases):
	# Lock before any consistent permission reads. Under MariaDB snapshot
	# isolation, taking a current read only after waiting can otherwise raise 1020.
	frappe.db.get_value("Email Account", email_account, "name", for_update=True)
	# The account row serializes simultaneous starts from different tabs/users.
	_require_access(email_account, for_update=True)
	active = _latest(email_account, active=True)
	if active:
		return _state(active)
	_require_archive_account(email_account, require_safe_linking=True)
	aliases = frappe.parse_json(sender_aliases) if isinstance(sender_aliases, str) else sender_aliases or []
	if not isinstance(aliases, list) or len(aliases) > 50:
		frappe.throw(_("Enter at most 50 sender alias email addresses."))
	for alias in aliases:
		if not isinstance(alias, str) or not alias.strip() or validate_email_address(alias.strip(), throw=False) != alias.strip():
			frappe.throw(_("Enter valid sender alias email addresses."))
	run = frappe.get_doc({
		"doctype": DOCTYPE, "email_account": email_account, "status": "Queued", "stage": "queued",
		"queued_by": frappe.session.user, "queued_at": now_datetime(),
		"aliases_json": frappe.as_json([alias.strip().casefold() for alias in aliases]), "folders_json": "[]",
	}).insert(ignore_permissions=True)
	frappe.db.after_commit.add(partial(_enqueue, run.name))
	return _state(run)


def _finish(name, status, error_code=None):
	frappe.db.set_value(DOCTYPE, name, {"status": status, "stage": "finished",
		"finished_at": now_datetime(), "error_code": error_code})
	frappe.db.commit()


def _enqueue(name):
	try:
		frappe.enqueue("kanzlei_erp.email_archive_jobs.run_import", queue="long", timeout=RUN_TIMEOUT,
			job_id=f"email-archive-{name}", run_name=name)
	except Exception:
		frappe.db.rollback()
		_finish(name, "Failed", "QUEUE_UNAVAILABLE")
		frappe.log_error(title="Email archive queue failed")


def _release_run_lock(name, account):
	lock = receiver_lock(account)
	# Never release a regular receiver's lock or a newer import's lock.
	lock.local.token = name.encode()
	if lock.owned():
		lock.release()


def run_import(run_name):
	run = frappe.get_doc(DOCTYPE, run_name, for_update=True)
	if run.status != "Queued":
		return
	# Persist Running before I/O, preventing duplicate delivery of the same job.
	frappe.db.set_value(DOCTYPE, run.name, {"status": "Running", "stage": "waiting",
		"started_at": now_datetime()})
	frappe.db.commit()
	actor = frappe.session.user
	lock = receiver_lock(run.email_account)
	status, error_code = "Failed", None
	try:
		frappe.set_user(run.queued_by)
		_require_access(run.email_account)
		_require_archive_account(run.email_account, require_safe_linking=True)
		if not lock.acquire(blocking=True, blocking_timeout=60, token=run.name):
			_finish(run.name, "Failed", "RECEIVER_BUSY")
			return

		def progress(state):
			if not lock.owned():
				raise RuntimeError("Email archive receiver lock expired")
			values = {key: state[key] for key in ("stage", "total", "processed", "imported", "existing", "failed")}
			values["folders_json"] = frappe.as_json(state["folders"])
			frappe.db.set_value(DOCTYPE, run.name, values)
			frappe.db.commit()

		result = import_archive(run.email_account, sender_aliases=frappe.parse_json(run.aliases_json),
			progress_callback=progress, receiving_lock=lock)
		frappe.db.set_value(DOCTYPE, run.name, {key: result[key] for key in
			("imported", "existing", "unsaved_attachments", "new_contact_links")})
		frappe.db.set_value(DOCTYPE, run.name, "failed", len(result["failures"]))
		status = "Completed" if result["complete"] else "Completed with errors"
	except JobTimeoutException:
		frappe.db.rollback()
		status, error_code = "Interrupted", "WORKER_INTERRUPTED"
	except Exception:
		frappe.db.rollback()
		status, error_code = "Failed", "IMPORT_FAILED"
		frappe.log_error(title="Email archive background import failed")
	finally:
		try:
			if lock.owned():
				lock.release()
		finally:
			frappe.set_user(actor)
	# If Redis is unavailable during release, keep Running so recovery can still
	# find this run and release its token. Never strand a terminal run's lock.
	_finish(run.name, status, error_code)


def recover_imports():
	"""Recover hard-killed workers without stealing a live receiver's lock."""
	for row in frappe.get_all(DOCTYPE, filters={"status": ["in", ACTIVE]},
		fields=["name", "email_account", "queued_at", "started_at"]):
		# Avoid the commit/enqueue window; queue status errors must leave live jobs alone.
		if (now_datetime() - get_datetime(row.started_at or row.queued_at)).total_seconds() < 120:
			continue
		status = get_job_status(f"email-archive-{row.name}")
		if status in ("queued", "started", "deferred", "scheduled"):
			continue
		run = frappe.get_doc(DOCTYPE, row.name, for_update=True)
		if run.status not in ACTIVE:
			continue
		_release_run_lock(run.name, run.email_account)
		_finish(run.name, "Interrupted", "WORKER_INTERRUPTED")
