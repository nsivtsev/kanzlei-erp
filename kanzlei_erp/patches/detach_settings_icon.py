"""Detach only the historical Einstellungen -> Kanzlei Desktop relationship."""

import json

import frappe


def execute():
	if frappe.db.exists("Desktop Icon", {"name": "Einstellungen", "parent_icon": "Kanzlei"}):
		frappe.db.set_value("Desktop Icon", "Einstellungen", "parent_icon", None)
	for row in frappe.get_all("Desktop Layout", fields=["name", "layout"]):
		try:
			layout = json.loads(row.layout or "null")
		except ValueError, TypeError:
			continue
		if not isinstance(layout, list):
			continue
		changed = False
		for icon in layout:
			if (
				isinstance(icon, dict)
				and icon.get("label") == "Einstellungen"
				and icon.get("parent_icon") == "Kanzlei"
			):
				icon["parent_icon"] = None
				changed = True
		if changed:
			frappe.db.set_value("Desktop Layout", row.name, "layout", json.dumps(layout, ensure_ascii=False))
