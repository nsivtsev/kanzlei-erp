"""Prevent Drive from moving ERP attachment bytes before document validation."""

import frappe


def require_drive_material(doc):
	if doc.get("attached_to_doctype") and doc.get("attached_to_name"):
		frappe.throw(frappe._("Manage ERP attachments from their original document"), frappe.PermissionError)
	# Moving/trashing a parent folder must not bypass the attachment guard.
	parents = [doc.name] if doc.is_folder else []
	seen = set()
	while parents:
		seen.update(parents)
		children = frappe.get_all("File", filters={"folder": ["in", parents]}, fields=["name", "is_folder", "attached_to_doctype", "attached_to_name"])
		parents = []
		for child in children:
			if child.attached_to_doctype and child.attached_to_name:
				frappe.throw(frappe._("Manage ERP attachments from their original document"), frappe.PermissionError)
			if child.is_folder and child.name not in seen:
				parents.append(child.name)


class ERPAttachmentGuard:
	@frappe.whitelist()
	def rename(self, new_file_name):
		require_drive_material(self)
		return super().rename(new_file_name)

	@frappe.whitelist()
	def move(self, new_parent=None):
		require_drive_material(self)
		return super().move(new_parent)

	def permanent_delete(self):
		require_drive_material(self)
		return super().permanent_delete()
