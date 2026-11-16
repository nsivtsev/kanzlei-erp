"""Transactional, serialized invalidations; no content or network access here."""

import frappe


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


def record_ids(names, extra_client=None):
	if not names:
		return
	if not frappe.db.has_column("File", "kanzlei_drive_customer"):
		if "suite" not in frappe.get_installed_apps():
			return
		if frappe.flags.in_migrate or frappe.flags.in_install:
			return
		raise RuntimeError("Drive customer field is unavailable; migrate the site")
	if not (frappe.db.table_exists("AI Source State") and frappe.db.table_exists("AI Source Event")):
		if frappe.flags.in_migrate or frappe.flags.in_install:
			return
		raise RuntimeError("AI source capture schema is unavailable; migrate the site")

	for name in sorted(set(names)):
		rows = frappe.db.sql(
			"SELECT is_folder, kanzlei_drive_customer FROM `tabFile` WHERE name=%s FOR UPDATE",
			name,
			as_dict=True,
		)
		row = rows[0] if rows else None
		if row and row.is_folder:
			continue
		clients = {row.kanzlei_drive_customer if row else None, extra_client} - {None, ""}
		for client in sorted(clients):
			# Held until commit/rollback, so published order is commit-safe.
			frappe.db.sql("INSERT IGNORE INTO `tabAI Source State` (name, `sequence`) VALUES ('drive', 0)")
			_record(name, client)


def _record(name, client):
	frappe.db.sql("UPDATE `tabAI Source State` SET `sequence` = `sequence` + 1 WHERE name = 'drive'")
	sequence = frappe.db.sql("SELECT `sequence` FROM `tabAI Source State` WHERE name = 'drive'")[0][0]
	frappe.db.sql(
		"INSERT INTO `tabAI Source Event` (name, `sequence`, source_id, client_id) VALUES (%s, %s, %s, %s)",
		(str(sequence).zfill(20), sequence, "file:" + name, client),
	)


def file_changed(doc, method=None):
	old = doc.get_doc_before_save()
	record_ids(
		descendants(doc.name) if doc.get("is_folder") else [doc.name], doc.get("kanzlei_drive_customer")
	)
	if old and old.get("kanzlei_drive_customer") != doc.get("kanzlei_drive_customer"):
		record_ids(
			descendants(doc.name) if doc.get("is_folder") else [doc.name], old.get("kanzlei_drive_customer")
		)


def customer_changed(doc, method=None):
	old = doc.get_doc_before_save()
	roots = {doc.get("kanzlei_drive_folder"), old.get("kanzlei_drive_folder") if old else None}
	if old and len(roots) > 1:
		from kanzlei_erp.object_storage import mark_mutation

		mark_mutation()
		record_ids([name for root in roots for name in descendants(root)])


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
