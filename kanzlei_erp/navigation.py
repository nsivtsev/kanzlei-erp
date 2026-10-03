"""Focused Desk navigation; ERPNext document permissions still apply."""

import frappe


def configure_desk(bootinfo):
	allowed = {"kanzlei"}
	if "System Manager" in frappe.get_roles():
		allowed.add("einstellungen")

	bootinfo.workspace_sidebar_item = {
		name: sidebar
		for name, sidebar in bootinfo.workspace_sidebar_item.items()
		if name in allowed
	}
	if "kanzlei" in bootinfo.workspace_sidebar_item:
		bootinfo.workspace_sidebar_item["kanzlei"]["items"] = [
			item
			for item in bootinfo.workspace_sidebar_item["kanzlei"]["items"]
			if item["label"] != "Einstellungen"
		]
	if "einstellungen" in bootinfo.workspace_sidebar_item and "kanzlei" in bootinfo.workspace_sidebar_item:
		bootinfo.workspace_sidebar_item["kanzlei"]["items"].append(
			{
				"label": "Einstellungen",
				"type": "Link",
				"link_type": "URL",
				"link_to": None,
				"url": "/desk/company",
				"icon": "settings",
			}
		)
	bootinfo.desktop_icons = [
		icon for icon in bootinfo.desktop_icons if icon.label.lower() in allowed
	]
	bootinfo.app_data = [app for app in bootinfo.app_data if app["app_name"] == "kanzlei_erp"]
	bootinfo.workspaces["pages"] = []
	bootinfo.module_wise_workspaces = {}


def customer_dashboard(data):
	"""Show a Mandant's work and billing through existing ERPNext records."""
	return {
		"fieldname": "customer",
		"non_standard_fieldnames": {"Task": "kanzlei_customer", "Payment Entry": "party"},
		"dynamic_links": {"party": ["Customer", "party_type"]},
		"transactions": [
			{"label": "Arbeiten", "items": ["Task", "Work Schedule", "Timesheet"]},
			{"label": "Abrechnung", "items": ["Sales Invoice", "Payment Entry"]},
		],
	}
