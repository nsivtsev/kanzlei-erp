"""Explicit language setup for a German-speaking Kanzlei."""

import frappe


def configure_german_language(user=None):
	"""Set the site's default language and optionally one user's preference."""
	settings = frappe.get_doc("System Settings")
	if settings.language != "de":
		settings.language = "de"
		settings.save(ignore_permissions=True)

	if user:
		profile = frappe.get_doc("User", user)
		if profile.language != "de":
			profile.language = "de"
			profile.save(ignore_permissions=True)

	frappe.clear_cache()
