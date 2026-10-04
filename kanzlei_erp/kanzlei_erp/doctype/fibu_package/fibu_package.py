"""A single Mandant service and accounting period."""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from kanzlei_erp.fibu_materials import validate_materials
from kanzlei_erp.fibu_period import period_bounds, period_label


class FiBuPackage(Document):
	def on_trash(self):
		if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
			frappe.throw(frappe._("Only a System Manager can delete an empty FiBu package"))
		if (
			self.status != "Open"
			or self.get("files")
			or self.get("messages")
			or self.get("transfers")
			or frappe.db.exists("Task", {"kanzlei_fibu_package": self.name})
			or frappe.db.exists("FiBu Supplement", {"package": self.name})
			or frappe.db.exists("File", {"attached_to_doctype": "FiBu Package", "attached_to_name": self.name})
			or frappe.db.exists("Communication", {"reference_doctype": "FiBu Package", "reference_name": self.name})
			or frappe.db.exists("Comment", {"reference_doctype": "FiBu Package", "reference_name": self.name})
			or frappe.db.exists("Version", {"ref_doctype": "FiBu Package", "docname": self.name})
		):
			frappe.throw(frappe._("A FiBu package with work or history cannot be deleted"))

	def before_validate(self):
		if self.is_new() and not self.responsible:
			self.responsible = frappe.session.user

	def validate(self):
		previous = self.get_doc_before_save() if not self.is_new() else None
		if previous and previous.status == "Closed":
			frappe.throw(frappe._("A closed FiBu package cannot be changed"))
		if previous and any(
			self.get(field) != previous.get(field)
			for field in ("customer", "service", "period_type", "period_year", "period_number")
		):
			frappe.throw(frappe._("Mandant, service, and accounting period cannot be changed"))
		if self.is_new():
			service = frappe.db.get_value("Item", self.service, ["disabled", "is_stock_item"], as_dict=True)
			if not service or service.disabled or service.is_stock_item:
				frappe.throw(frappe._("Choose an active non-stock service"))
		if self.external_due_date and not (self.external_due_source or "").strip():
			frappe.throw(frappe._("External deadline source is required"))
		if self.is_new() and self.status != "Open":
			frappe.throw(frappe._("New FiBu packages must be open"))
		if previous and self.status != previous.status and not self.flags.fibu_closing:
			frappe.throw(frappe._("Close the package using the Close action"))
		if previous and not self.flags.fibu_closing and any(
			self.get(field) != previous.get(field) for field in ("closure_note", "closed_by", "closed_at")
		):
			frappe.throw(frappe._("Close the package using the Close action"))
		if previous and (self.has_open_supplements or 0) != (previous.has_open_supplements or 0):
			frappe.throw(frappe._("The open supplement indicator is maintained by the system"))
		if self.is_new() and (self.closure_note or self.closed_by or self.closed_at or self.has_open_supplements):
			frappe.throw(frappe._("Close the package using the Close action"))
		try:
			start, end = period_bounds(self.period_type, self.period_year, self.period_number)
		except ValueError:
			frappe.throw(frappe._("Invalid accounting period"))
		self.period_start = start.isoformat()
		self.period_end = end.isoformat()
		self.display_title = f"{self.customer} — {self.service} — {period_label(self.period_type, self.period_year, self.period_number)}"
		if frappe.db.exists(
			"FiBu Package",
			{
				"customer": self.customer,
				"service": self.service,
				"period_start": self.period_start,
				"period_end": self.period_end,
				"name": ["!=", self.name or ""],
			},
		):
			frappe.throw(frappe._("A FiBu package already exists for this Mandant, service, and period"))
		validate_materials(self)

	@frappe.whitelist()
	def close(self, note: str):
		self.check_permission("write")
		if self.status != "Open":
			frappe.throw(frappe._("Only open FiBu packages can be closed"))
		note = (note or "").strip()
		if not note:
			frappe.throw(frappe._("Closure note is required"))
		frappe.db.sql("SELECT name FROM `tabFiBu Package` WHERE name=%s FOR UPDATE", self.name)
		unfinished = frappe.db.sql(
			"""SELECT name FROM `tabTask`
			WHERE kanzlei_fibu_package=%s
			AND (kanzlei_fibu_supplement IS NULL OR kanzlei_fibu_supplement='')
			AND status NOT IN ('Completed', 'Cancelled') LIMIT 1""",
			self.name,
		)
		if unfinished:
			frappe.throw(frappe._("The package has unfinished Tasks or Questions"))
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
	frappe.db.add_unique(
		"FiBu Package",
		["customer", "service", "period_start", "period_end"],
		constraint_name="unique_fibu_work_period",
	)
