"""Commit-time, permission-aware invalidations. Never broadcast File payloads."""

import frappe


def _pending():
	queue = getattr(frappe.local, "kanzlei_drive_events", None)
	if queue is None:
		queue = {"changes": {}, "docinfo": set(), "valid": True}
		frappe.local.kanzlei_drive_events = queue
		def clear():
			queue["valid"] = False
			if getattr(frappe.local, "kanzlei_drive_events", None) is queue:
				del frappe.local.kanzlei_drive_events
		def flush():
			if not queue["valid"]:
				return
			clear()
			try:
				_deliver(queue["changes"].values(), queue["docinfo"])
			except Exception:
				frappe.log_error("Drive notifications failed", frappe.get_traceback())
		frappe.db.after_commit.add(flush)
		frappe.db.after_rollback.add(clear)
	return queue


def changed(doc, method=None):
	from kanzlei_erp.drive_storage import invalidate
	# All File writes invalidate only storage metadata, never cache Customer rights.
	invalidate()
	if "suite" not in frappe.get_installed_apps() or not doc.name:
		return
	if any(frappe.flags.get(flag) for flag in ("in_import", "in_patch", "in_migrate", "in_install")):
		return
	old = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	contexts = [(doc.get("folder"), doc.get("kanzlei_drive_customer"))]
	if old and old.folder != doc.folder:
		contexts.append((old.folder, old.get("kanzlei_drive_customer")))
	queue = _pending()
	# File ID is private server state; only folder ID is sent to authorized users.
	for folder, customer in contexts:
		if folder:
			queue["changes"][(doc.name, folder)] = (doc.name, folder, customer)


def _deliver(changes, docinfo=()):
	from suite.drive.api.permissions import user_has_permission
	from kanzlei_erp.mandant_drive import _customer_access

	users = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, pluck="name")
	sent = set()
	for name, folder, customer in changes:
		row = frappe.db.get_value("File", name, ["name", "folder", "kanzlei_drive_customer", "modified"], as_dict=True)
		for user in users:
			if not frappe.db.exists("File", folder) or not user_has_permission(folder, "read", user):
				continue
			readable = bool(row and user_has_permission(name, "read", user))
			if row and row.folder == folder:
				if not readable:
					continue
			elif customer:
				if not _customer_access(customer, user)["read"]:
					continue
			# For deleted ordinary files the surviving parent permission is authoritative.
			if (folder, user) not in sent:
				frappe.publish_realtime("drive-folder-changed", {"folder": folder}, user=user)
				sent.add((folder, user))
			if readable:
				frappe.publish_realtime("doc_update", {"doctype": "File", "name": name,
					"modified": row.modified}, user=user)
				if name in docinfo:
					frappe.publish_realtime("file-docinfo-changed", {"name": name}, user=user)


def intercept_file_event(event, message, doctype=None, docname=None, **kwargs):
	"""Frappe extension intercepts document/comment notifications before queueing."""
	message = message or {}
	if doctype != "File" and message.get("doctype") != "File":
		return False
	name = docname or message.get("name")
	if name and frappe.db.exists("File", name):
		changed(frappe.get_doc("File", name))
		if event == "docinfo_update":
			_pending()["docinfo"].add(name)
	# All File events go through the same sanitized, fresh permission check.
	return True
