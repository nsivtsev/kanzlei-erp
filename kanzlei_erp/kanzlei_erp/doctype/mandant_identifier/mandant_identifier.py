"""Rows are changed through Customer validation, never standalone writes."""
import frappe
from frappe.model.document import Document


class MandantIdentifier(Document):
	def validate(self):
		frappe.throw(frappe._("Posteingang operation is not permitted"), frappe.PermissionError)

	before_insert = validate
	on_trash = validate

	def db_set(self, *args, **kwargs):
		self.validate()

	def before_rename(self, *args, **kwargs):
		self.validate()
