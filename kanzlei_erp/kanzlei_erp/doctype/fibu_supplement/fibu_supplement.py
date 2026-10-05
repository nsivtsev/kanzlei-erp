"""Late additions to a closed accounting period."""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from kanzlei_erp.fibu_materials import validate_materials
from kanzlei_erp.fibu_workflow import (
	change_preparation_stage,
	initialize_work_context,
	update_work_context,
	validate_workflow_document,
)


class FiBuSupplement(Document):
	def before_insert(self):
		frappe.db.sql("SELECT name FROM `tabFiBu Package` WHERE name=%s FOR UPDATE", self.package)
		parent = frappe.get_doc("FiBu Package", self.package)
		parent.check_permission("read")
		if parent.status != "Closed":
			frappe.throw(frappe._("Supplements require a closed FiBu package"))
		self.sequence = frappe.db.sql(
			"SELECT COALESCE(MAX(sequence), 0) + 1 FROM `tabFiBu Supplement` WHERE package=%s",
			self.package,
		)[0][0]
		if not self.responsible:
			self.responsible = parent.responsible
		if not self.deputy:
			self.deputy = parent.deputy

	def validate(self):
		initialize_work_context(self)
		previous = self.get_doc_before_save() if not self.is_new() else None
		if previous and previous.status == "Closed":
			frappe.throw(frappe._("A closed supplement cannot be changed"))
		if previous and (self.package != previous.package or self.sequence != previous.sequence):
			frappe.throw(frappe._("Supplement identity cannot be changed"))
		if self.is_new() and self.status != "Open":
			frappe.throw(frappe._("New supplements must be open"))
		if previous and self.status != previous.status and not self.flags.fibu_closing:
			frappe.throw(frappe._("Close the supplement using the Close action"))
		if previous and not self.flags.fibu_closing and any(
			self.get(field) != previous.get(field) for field in ("closure_note", "closed_by", "closed_at")
		):
			frappe.throw(frappe._("Close the supplement using the Close action"))
		if self.is_new() and (self.closure_note or self.closed_by or self.closed_at):
			frappe.throw(frappe._("Close the supplement using the Close action"))
		if self.external_due_date and not (self.external_due_source or "").strip():
			frappe.throw(frappe._("External deadline source is required"))
		if not (self.reason or "").strip():
			frappe.throw(frappe._("A reason is required for a supplement"))
		parent = frappe.get_doc("FiBu Package", self.package)
		parent.check_permission("read")
		self.display_title = f"{parent.display_title} — {self.sequence}"
		validate_materials(self)
		validate_workflow_document(self)

	def on_update(self):
		self.update_parent_indicator()

	def update_parent_indicator(self):
		is_open = bool(frappe.db.exists("FiBu Supplement", {"package": self.package, "status": "Open"}))
		frappe.db.set_value("FiBu Package", self.package, "has_open_supplements", int(is_open))

	@frappe.whitelist()
	def change_preparation_stage(self, target_stage: str, note: str = "", expected_modified: str = ""):
		return change_preparation_stage(self, target_stage, note, expected_modified)

	@frappe.whitelist()
	def update_work_context(
		self,
		waiting_for_mandant,
		waiting_reason: str = "",
		blocking_reason: str = "",
		next_action: str = "",
		next_action_assignee: str = "",
		next_action_task: str = "",
		expected_modified: str = "",
		note: str = "",
	):
		return update_work_context(
			self,
			waiting_for_mandant,
			waiting_reason,
			blocking_reason,
			next_action,
			next_action_assignee,
			next_action_task,
			expected_modified,
			note,
		)

	@frappe.whitelist()
	def close(self, note: str):
		self.check_permission("write")
		if self.status != "Open":
			frappe.throw(frappe._("Only open supplements can be closed"))
		note = (note or "").strip()
		if not note:
			frappe.throw(frappe._("Closure note is required"))
		frappe.db.sql("SELECT name FROM `tabFiBu Package` WHERE name=%s FOR UPDATE", self.package)
		frappe.db.sql("SELECT name FROM `tabFiBu Supplement` WHERE name=%s FOR UPDATE", self.name)
		unfinished = frappe.db.sql(
			"""SELECT name FROM `tabTask` WHERE kanzlei_fibu_supplement=%s
			AND status NOT IN ('Completed', 'Cancelled') LIMIT 1""",
			self.name,
		)
		if unfinished:
			frappe.throw(frappe._("The supplement has unfinished Tasks or Questions"))
		self.status = "Closed"
		self.closure_note = note
		self.closed_by = frappe.session.user
		self.closed_at = now_datetime()
		self.flags.fibu_closing = True
		try:
			self.save()
		finally:
			self.flags.fibu_closing = False
		return self.name


def on_doctype_update():
	frappe.db.add_unique("FiBu Supplement", ["package", "sequence"], constraint_name="unique_fibu_supplement_no")
