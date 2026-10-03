"""Focused Desk navigation; ERPNext document permissions still apply."""

import frappe


EMAIL_INBOX_URL = "/desk/communication/view/inbox"


def _has_assigned_email_account(user):
	return bool(
		frappe.get_all(
			"User Email",
			filters={"parent": user, "parenttype": "User"},
			fields=["email_account"],
			limit=1,
		)
	)


def communication_has_permission(doc, ptype, user=None, debug=False):
	"""Allow Mandant readers to open linked mail and its private attachments."""
	if ptype != "read" or doc.communication_medium != "Email":
		return True
	user = user or frappe.session.user
	if user == "Administrator" or {"System Manager", "Super Email User"} & set(frappe.get_roles(user)):
		return True
	if doc.email_account and frappe.db.exists(
		"User Email", {"parent": user, "email_account": doc.email_account}
	):
		return True
	if doc.reference_doctype and doc.reference_doctype != "Customer":
		return True

	customer_names = set(
		frappe.get_all(
			"Communication Link",
			filters={"parent": doc.name, "link_doctype": "Customer"},
			pluck="link_name",
		)
	)
	if doc.reference_doctype == "Customer" and doc.reference_name:
		customer_names.add(doc.reference_name)
	return any(
		frappe.has_permission("Customer", ptype="read", doc=customer_name, user=user)
		for customer_name in customer_names
	)


def configure_desk(bootinfo):
	allowed = {"kanzlei"}
	if "System Manager" in frappe.get_roles():
		allowed.add("einstellungen")
	show_mail = _has_assigned_email_account(frappe.session.user)

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
			and (item["label"] != "E-Mail" or show_mail)
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
