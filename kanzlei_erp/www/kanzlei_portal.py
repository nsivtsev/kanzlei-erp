from urllib.parse import urlencode

import frappe
from frappe.sessions import get_csrf_token

from kanzlei_erp.portal import get_portal, require_staff

no_cache = 1
sitemap = 0

LABELS = (
	"Loading…", "No recently opened items yet.", "Open a client or a document to continue here next time.",
	"No pinned links yet.", "Pin a recent item with the star.", "No matching entries.",
	"Could not load your recent items and pinned links.", "Could not save your pinned links.",
	"This link could not be opened. Refresh the portal and try again.", "Your session has changed. Reload the portal.",
	"Retry", "Reload portal", "Pin link", "Unpin link", "Open", "Sign out", "Could not sign out. Please try again.",
	"Link pinned.", "Link unpinned.", "Remove pinned link", "Saving…",
)


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.redirect("/login?" + urlencode({"redirect-to": "/kanzlei-portal"}))
	require_staff()
	data = get_portal()
	full_name = data["full_name"]
	context.update({
		"no_cache": 1, "title": frappe._("Kanzlei Portal"), "portal": data,
		"brand": frappe.conf.get("kanzlei_portal_name") or "Golke",
		"initials": "".join(part[0] for part in full_name.split()[:2]).upper(),
		"greeting": frappe._("Welcome, {0}").format(full_name.split()[0]),
		"lang": frappe.local.lang,
		"theme": data["theme"].lower(),
		"portal_config": {"user": data["user"], "csrf_token": get_csrf_token(),
			"shortcuts": data["shortcuts"], "labels": {text: frappe._(text) for text in LABELS}},
		"asset_version": frappe.utils.get_build_version(),
	})
