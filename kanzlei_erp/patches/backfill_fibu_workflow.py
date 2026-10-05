"""Initialize workflow defaults for existing open FiBu work."""

import frappe


def execute():
	for doctype in ("FiBu Package", "FiBu Supplement"):
		frappe.db.sql(
			f"""UPDATE `tab{doctype}`
			SET preparation_stage=NULL, next_action=NULL, next_action_assignee=NULL
			WHERE status='Closed'"""
		)
		frappe.db.sql(
			f"""UPDATE `tab{doctype}`
			SET preparation_stage=COALESCE(NULLIF(preparation_stage, ''), 'Collection'),
				next_action=COALESCE(NULLIF(next_action, ''), 'Review the current period status'),
				next_action_assignee=COALESCE(NULLIF(next_action_assignee, ''), responsible)
			WHERE status='Open'"""
		)
