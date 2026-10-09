"""Immutable question events validated on their parent Task."""

import frappe
from frappe.model.document import Document


class FiBuQuestionEvent(Document):
	def validate(self):
		# Parent saves insert/update children directly; standalone REST/client saves
		# must never mutate the append-only log outside the guarded Task action.
		frappe.throw(frappe._("Question history can only be recorded through question actions"))

	def on_trash(self):
		frappe.throw(frappe._("Question history cannot be deleted"))
