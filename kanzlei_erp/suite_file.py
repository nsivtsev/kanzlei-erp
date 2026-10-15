"""Prevent Drive from moving ERP attachment bytes before document validation."""

from types import SimpleNamespace

import frappe


def require_drive_material(doc):
	from kanzlei_erp.drive_storage import require_storage_access
	from kanzlei_erp.mandant_drive import protect_root

	require_storage_access(doc, "write")
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
	def insert(self, *args, **kwargs):
		from kanzlei_erp.drive_storage import creation_flow

		with creation_flow():
			return super().insert(*args, **kwargs)

	def db_set(self, *args, **kwargs):
		from kanzlei_erp.drive_storage import invalidate

		invalidate()
		try:
			result = super().db_set(*args, **kwargs)
			from kanzlei_erp.object_storage import mark_mutation
			mark_mutation()
			return result
		finally:
			invalidate()

	def notify_update(self):
		from kanzlei_erp.drive_realtime import changed

		if "suite" not in frappe.get_installed_apps():
			return super().notify_update()
		changed(self)

	def before_insert(self):
		from kanzlei_erp.drive_storage import grant_creation, invalidate, validate_file_binding
		from kanzlei_erp.object_storage import lock_binding

		if "suite" not in frappe.get_installed_apps():
			return super().before_insert()
		lock_binding(self.file_url)
		invalidate()
		validate_file_binding(self)
		result = super().before_insert()
		grant_creation(self, self.file_url)
		validate_file_binding(self)
		return result

	def _delete_file_on_disk(self):
		from kanzlei_erp.drive_storage import exact_io, require_storage_access
		from kanzlei_erp.object_storage import get_binding, schedule_delete

		require_storage_access(self, "write")
		if get_binding(self.file_url):
			schedule_delete(self)
			return
		with exact_io(self):
			return super()._delete_file_on_disk()

	def validate(self):
		from kanzlei_erp.drive_storage import invalidate, validate_file_binding

		invalidate()
		validate_file_binding(self)
		return super().validate()

	def get_content(self, *args, **kwargs):
		from kanzlei_erp.drive_storage import require_storage_access
		from kanzlei_erp.object_storage import open_file, stored

		require_storage_access(self)
		if not self.get("content") and stored(self):
			with open_file(self) as source:
				data = source.read()
			encodings = kwargs.get("encodings", args[0] if args else None)
			from frappe.core.doctype.file.file import FILE_ENCODING_OPTIONS, OLE_FILE_SIGNATURE
			if not data.startswith(OLE_FILE_SIGNATURE):
				for encoding in FILE_ENCODING_OPTIONS if encodings is None else encodings:
					try:
						self._content = data.decode(encoding)
						return self._content
					except UnicodeDecodeError:
						continue
			self._content = data
			return self._content
		return super().get_content(*args, **kwargs)

	def get_full_path(self):
		from kanzlei_erp.drive_storage import require_storage_access
		from kanzlei_erp.object_storage import materialize, stored

		require_storage_access(self)
		if stored(self):
			return materialize(self)
		return super().get_full_path()

	def exists_on_disk(self):
		from kanzlei_erp.object_storage import client, file_key, settings, stored

		if stored(self):
			from kanzlei_erp.drive_storage import require_storage_access
			require_storage_access(self)
			client().head_object(Bucket=settings().bucket, Key=file_key(self))
			return True
		return super().exists_on_disk()

	def make_thumbnail(self, set_as_thumbnail=True, width=300, height=300, suffix="small", crop=False):
		from kanzlei_erp.object_storage import make_thumbnail, stored

		if stored(self):
			return make_thumbnail(self, width=width, height=height, suffix=suffix, set_as_thumbnail=set_as_thumbnail, crop=crop)
		return super().make_thumbnail(set_as_thumbnail, width, height, suffix, crop)

	def validate_file_path(self):
		from kanzlei_erp.object_storage import stored

		if stored(self):
			from kanzlei_erp.drive_storage import require_storage_access
			require_storage_access(self)
			return
		return super().validate_file_path()

	def handle_is_private_changed(self):
		from kanzlei_erp.object_storage import bind, get_binding

		if get_binding(self.file_url):
			before = self.get_doc_before_save()
			if before and before.is_private != self.is_private:
				frappe.throw("Changing the privacy of an S3 attachment requires creating a new attachment.", frappe.ValidationError)
			return
		return super().handle_is_private_changed()

	def on_rollback(self):
		from kanzlei_erp.drive_storage import is_alias

		# A rejected pre-existing alias never owned the bytes it points at.
		if is_alias(self):
			return
		return super().on_rollback()

	def _delete_blob_after_commit(self, blob_is_trashed):
		from kanzlei_erp.drive_storage import deleted_storage, require_storage_access

		captured = frappe._dict(self.as_dict())
		with deleted_storage(captured):
			require_storage_access(self, "write")
			if self.manager.s3_enabled:
				from kanzlei_erp.object_storage import file_key, schedule_derivatives, schedule_key_deletion
				schedule_derivatives(self)
				schedule_key_deletion(file_key(self))
				schedule_key_deletion(str(self.manager.get_thumbnail_path(self.name)))
				return
		def cleanup():
			with deleted_storage(captured):
				if blob_is_trashed:
					self.manager.delete_from_trash(captured)
				else:
					self.manager.delete_file(captured)
		frappe.db.after_commit.add(cleanup)

	def on_trash(self):
		from kanzlei_erp.drive_storage import require_storage_access

		require_storage_access(self, "write")
		return super().on_trash()

	def create_attachment_copy(self, attached_to_doctype, attached_to_name, attached_to_field=None, ignore_permissions=False):
		from kanzlei_erp.drive_storage import require_storage_access

		require_storage_access(self)
		source = frappe.get_doc("File", self.name)
		frappe.has_permission("File", "read", doc=source, throw=True)
		frappe.has_permission(attached_to_doctype, "write", doc=attached_to_name, throw=True)
		return super().create_attachment_copy(attached_to_doctype, attached_to_name, attached_to_field, ignore_permissions)

	@frappe.whitelist()
	def rename(self, new_file_name):
		from suite.drive.utils.files import FileManager

		from kanzlei_erp.mandant_drive import storage_mutation

		proposed = SimpleNamespace(**self.as_dict())
		proposed.file_name = new_file_name
		with storage_mutation(self, target=FileManager().get_disk_path(proposed)):
			return super().rename(new_file_name)

	@frappe.whitelist()
	def move(self, new_parent=None):
		from kanzlei_erp.mandant_drive import storage_mutation, validate_move

		require_drive_material(self)
		validate_move(self, new_parent)
		from suite.drive.utils import get_new_file_name, get_user_folder
		from suite.drive.utils.files import FileManager
		new_parent = new_parent or get_user_folder().name
		proposed = SimpleNamespace(**self.as_dict())
		proposed.folder = new_parent
		proposed.file_name = get_new_file_name(self.file_name, new_parent, self.is_folder, self.name)
		with storage_mutation(self, target=FileManager().get_disk_path(proposed)):
			return super().move(new_parent)

	def permanent_delete(self):
		from kanzlei_erp.mandant_drive import storage_mutation

		with storage_mutation(self):
			return super().permanent_delete()
