"""Prevent Drive from moving ERP attachment bytes before document validation."""

import frappe


def require_drive_material(doc):
	from kanzlei_erp.mandant_drive import protect_root

	protect_root(doc)
	if doc.get("attached_to_doctype") and doc.get("attached_to_name"):
		frappe.throw(frappe._("Manage ERP attachments from their original document"), frappe.PermissionError)
	# Moving/trashing a parent folder must not bypass the attachment guard.
	parents = [doc.name] if doc.is_folder else []
	seen = set()
	while parents:
		seen.update(parents)
		fields = ["name", "is_folder", "attached_to_doctype", "attached_to_name"]
		if frappe.get_meta("File").has_field("kanzlei_drive_customer"):
			fields.append("kanzlei_drive_customer")
		children = frappe.get_all("File", filters={"folder": ["in", parents]}, fields=fields)
		parents = []
		for child in children:
			protect_root(child)
			if child.attached_to_doctype and child.attached_to_name:
				frappe.throw(frappe._("Manage ERP attachments from their original document"), frappe.PermissionError)
			if child.is_folder and child.name not in seen:
				parents.append(child.name)


class ERPAttachmentGuard:
	@frappe.whitelist()
	def rename(self, new_file_name):
		from kanzlei_erp.mandant_drive import storage_mutation

		with storage_mutation(self):
			return super().rename(new_file_name)

	@frappe.whitelist()
	def move(self, new_parent=None):
		from kanzlei_erp.mandant_drive import storage_mutation, validate_move

		require_drive_material(self)
		validate_move(self, new_parent)
		with storage_mutation(self):
			return super().move(new_parent)

	def permanent_delete(self):
		from kanzlei_erp.mandant_drive import storage_mutation

		with storage_mutation(self):
			return super().permanent_delete()
