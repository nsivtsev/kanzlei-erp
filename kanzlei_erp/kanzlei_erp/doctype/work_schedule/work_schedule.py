from datetime import timedelta

import frappe
from frappe.model.document import Document
from frappe.utils import getdate, nowdate


class WorkSchedule(Document):
	def validate(self):
		if self.last_due_date and getdate(self.last_due_date) < getdate(self.first_due_date):
			frappe.throw(frappe._("Last Due Date cannot be before First Due Date"))

	def on_update(self):
		from kanzlei_erp.work_schedule import generate_for_schedule

		today = getdate(nowdate())
		generate_for_schedule(self, today - timedelta(days=30), today + timedelta(days=365))
