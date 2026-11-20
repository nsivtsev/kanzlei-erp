"""Transactional, serialized invalidations; no content or network access here."""

import frappe

# Internal ledger marker so a Drive-only cursor cannot observe mail attachment
# events. The feed always exposes these as canonical file:<name> IDs.
ATTACHMENT_EVENT_PREFIX = "attachment:"
MAIL_CONTENT_FIELDS = frozenset(
	{
		"communication_type",
		"communication_medium",
		"sent_or_received",
		"subject",
		"sender",
		"recipients",
		"cc",
		"bcc",
		"communication_date",
		"in_reply_to",
		"content",
		"text_content",
	}
)
MAIL_CHANGE_FIELDS = MAIL_CONTENT_FIELDS | {"reference_doctype", "reference_name"}


def _mail_enabled():
	from kanzlei_erp.ai_connector import mail

	return mail.enabled()


def descendants(root):
	if not root:
		return []
	seen = {root}
	pending = [root]
	while pending:
		batch, pending = pending[:200], pending[200:]
		# Locking reads see committed membership beyond an older RR snapshot.
		placeholders = ", ".join(["%s"] * len(batch))
		rows = frappe.db.sql(
			f"SELECT name, is_folder FROM `tabFile` WHERE folder IN ({placeholders}) FOR UPDATE",
			tuple(batch),
			as_dict=True,
		)
		for row in rows:
			if row.name not in seen:
				seen.add(row.name)
				if row.is_folder:
					pending.append(row.name)
	return sorted(seen)


def _capture_ready():
	if frappe.db.table_exists("AI Source State") and frappe.db.table_exists("AI Source Event"):
		return True
	if frappe.flags.in_migrate or frappe.flags.in_install:
		return False
	raise RuntimeError("AI source capture schema is unavailable; migrate the site")


def record_ids(names, extra_client=None, *, old_parent=None):
	if not names:
		return
	email_enabled = _mail_enabled()
	has_drive = frappe.db.has_column("File", "kanzlei_drive_customer")
	if (
		not has_drive
		and "suite" in frappe.get_installed_apps()
		and not (frappe.flags.in_migrate or frappe.flags.in_install)
	):
		raise RuntimeError("Drive customer field is unavailable; migrate the site")
	if not (has_drive or email_enabled) or not _capture_ready():
		return

	fields = ["is_folder"]
	if has_drive:
		fields.append("kanzlei_drive_customer")
	if email_enabled:
		fields.extend(("attached_to_doctype", "attached_to_name"))
	invalidations = set()
	for name in sorted(set(names)):
		row = frappe.db.get_value("File", name, fields, as_dict=True, for_update=True)
		if row and row.is_folder:
			continue
		clients = {row.get("kanzlei_drive_customer") if row else None, extra_client} - {None, ""}
		invalidations.update(("file:" + name, client) for client in clients)
		if email_enabled:
			current = _parent_name(row)
			for parent in sorted({old_parent, current} - {None, ""}):
				invalidations.update(
					(ATTACHMENT_EVENT_PREFIX + name, client) for client in _parent_clients(parent)
				)
	_record_many(invalidations)


def _record_many(invalidations):
	if not invalidations:
		return
	# Held until commit/rollback, so published order is commit-safe.
	frappe.db.sql("INSERT IGNORE INTO `tabAI Source State` (name, `sequence`) VALUES ('drive', 0)")
	for source_id, client in sorted(invalidations):
		_record(source_id, client)


def _record(source_id, client):
	frappe.db.sql("UPDATE `tabAI Source State` SET `sequence` = `sequence` + 1 WHERE name = 'drive'")
	sequence = frappe.db.sql("SELECT `sequence` FROM `tabAI Source State` WHERE name = 'drive'")[0][0]
	frappe.db.sql(
		"INSERT INTO `tabAI Source Event` (name, `sequence`, source_id, client_id) VALUES (%s, %s, %s, %s)",
		(str(sequence).zfill(20), sequence, source_id, client),
	)


def _parent_name(file):
	if file and file.get("attached_to_doctype") == "Communication":
		return file.get("attached_to_name") or None
	return None


def _is_email(doc):
	return (
		doc
		and doc.communication_type == "Communication"
		and doc.communication_medium == "Email"
		and doc.sent_or_received in ("Received", "Sent")
	)


def _mail_clients(doc):
	if not _is_email(doc):
		return set()
	clients = {
		link.link_name for link in doc.timeline_links if link.link_doctype == "Customer" and link.link_name
	}
	if doc.reference_doctype == "Customer" and doc.reference_name:
		clients.add(doc.reference_name)
	return clients


def _parent_clients(name):
	parent = frappe.db.get_value(
		"Communication",
		name,
		[
			"communication_type",
			"communication_medium",
			"sent_or_received",
			"reference_doctype",
			"reference_name",
		],
		as_dict=True,
		for_update=True,
	)
	if not _is_email(parent):
		return set()
	parent.timeline_links = frappe.db.sql(
		"""SELECT link_doctype, link_name FROM `tabCommunication Link`
		WHERE parent=%s AND parenttype='Communication' AND parentfield='timeline_links'
			AND link_doctype='Customer' FOR UPDATE""",
		name,
		as_dict=True,
	)
	return _mail_clients(parent)


def _record_communication(name, clients):
	if not clients or not _capture_ready():
		return
	files = frappe.db.sql(
		"""SELECT name FROM `tabFile`
		WHERE attached_to_doctype='Communication' AND attached_to_name=%s AND is_folder=0
		ORDER BY name FOR UPDATE""",
		name,
		pluck=True,
	)
	_record_many(
		{
			(source_id, client)
			for source_id in ["email:" + name, *(ATTACHMENT_EVENT_PREFIX + file for file in files)]
			for client in clients
		}
	)


def _communication_changed(doc, old):
	current, previous = _mail_clients(doc), _mail_clients(old)
	if current != previous or (
		old and any(doc.get(field) != old.get(field) for field in MAIL_CONTENT_FIELDS)
	):
		_record_communication(doc.name, current | previous)


def communication_changed(doc, method=None):
	if _mail_enabled():
		_communication_changed(doc, doc.get_doc_before_save())


def communication_deleting(doc, method=None):
	if _mail_enabled():
		# Use locking reads before Frappe removes links and attachments. Its
		# deletion document may have been loaded from an older RR snapshot.
		_record_communication(doc.name, _parent_clients(doc.name))


def communication_linked(doc):
	if _mail_enabled():
		# email_history holds the parent lock and writes child rows without a parent save.
		_record_communication(doc.name, _mail_clients(doc))


def file_changed(doc, method=None):
	old = doc.get_doc_before_save()
	folder = doc.get("is_folder")
	record_ids(
		descendants(doc.name) if folder else [doc.name],
		doc.get("kanzlei_drive_customer"),
		old_parent=(_parent_name(old) if method == "on_update" else doc.flags.get("ai_feed_old_parent"))
		if not folder
		else None,
	)
	if old and old.get("kanzlei_drive_customer") != doc.get("kanzlei_drive_customer"):
		record_ids(descendants(doc.name) if folder else [doc.name], old.get("kanzlei_drive_customer"))


def attachment_deleting(doc, method=None):
	if not _mail_enabled():
		return
	file = frappe.db.get_value(
		"File",
		doc.name,
		["is_folder", "attached_to_doctype", "attached_to_name"],
		as_dict=True,
		for_update=True,
	)
	parent = _parent_name(file)
	if parent and not file.is_folder and _capture_ready():
		_record_many({(ATTACHMENT_EVENT_PREFIX + doc.name, client) for client in _parent_clients(parent)})


def customer_changed(doc, method=None):
	old = doc.get_doc_before_save()
	roots = {doc.get("kanzlei_drive_folder"), old.get("kanzlei_drive_folder") if old else None}
	if old and len(roots) > 1:
		from kanzlei_erp.object_storage import mark_mutation

		mark_mutation()
		record_ids([name for root in roots for name in descendants(root)])


class FileMailCapture:
	def db_set(self, fieldname, value=None, update_modified=True, notify=False, commit=False):
		fields = fieldname if isinstance(fieldname, dict) else {fieldname: value}
		if (
			not self.name
			or not fields.keys() & {"attached_to_doctype", "attached_to_name"}
			or not _mail_enabled()
		):
			return super().db_set(fieldname, value, update_modified, notify, commit)
		old = frappe.db.get_value(
			"File", self.name, ["attached_to_doctype", "attached_to_name"], as_dict=True, for_update=True
		)
		# ERPAttachmentGuard records the new and previous bindings before its commit.
		previous = self.flags.get("ai_feed_old_parent")
		self.flags.ai_feed_old_parent = _parent_name(old)
		try:
			return super().db_set(fieldname, value, update_modified, notify, commit)
		finally:
			self.flags.ai_feed_old_parent = previous


class CommunicationCapture:
	def db_set(self, fieldname, value=None, update_modified=True, notify=False, commit=False):
		fields = fieldname if isinstance(fieldname, dict) else {fieldname: value}
		if not self.name or not fields.keys() & MAIL_CHANGE_FIELDS or not _mail_enabled():
			return super().db_set(fieldname, value, update_modified, notify, commit)
		try:
			old = frappe.get_doc("Communication", self.name, for_update=True)
			result = super().db_set(fieldname, value, update_modified, notify, False)
			current = frappe.get_doc("Communication", self.name, for_update=True)
			_communication_changed(current, old)
			if commit:
				frappe.db.commit()
			return result
		except Exception:
			if commit:
				frappe.db.rollback()
			raise


class CustomerCapture:
	def db_set(self, fieldname, value=None, update_modified=True, notify=False, commit=False):
		fields = fieldname if isinstance(fieldname, dict) else {fieldname: value}
		if "kanzlei_drive_folder" not in fields:
			return super().db_set(fieldname, value, update_modified, notify, commit)
		try:
			old = (
				frappe.db.get_value("Customer", self.name, "kanzlei_drive_folder", for_update=True)
				if self.name
				else None
			)
			result = super().db_set(fieldname, value, update_modified, notify, False)
			if self.name:
				from kanzlei_erp.object_storage import mark_mutation

				mark_mutation()
				record_ids(
					[name for root in {old, fields["kanzlei_drive_folder"]} for name in descendants(root)]
				)
				if commit:
					frappe.db.commit()
			return result
		except Exception:
			if commit:
				frappe.db.rollback()
			raise
