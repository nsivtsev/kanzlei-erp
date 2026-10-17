import "./portal.js";

// Only saved Kanzlei forms are recorded. The server rechecks current read access.
frappe.router.on("change", () => {
	if (!frappe.get_route()[0] || frappe.get_route()[0] === "desktop") {
		window.location.assign("/kanzlei-portal");
		return;
	}
	const reference = globalThis.KanzleiPortal.historyReference(frappe.get_route());
	if (!reference || frappe.session.user === "Guest") return;
	frappe.call({method: "kanzlei_erp.portal.record_visit", args: {...reference, expected_user: frappe.session.user}, silent: true}).catch(() => {
		// Optional history must not interrupt opening the actual form.
	});
});
