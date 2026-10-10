// ERPNext entities span Selling, Projects, and Accounts. Keep their navigation
// in Kanzlei even when this browser remembers an older ERPNext sidebar.
if (frappe.boot.workspace_sidebar_item.kanzlei) {
	// Repair only the old settings-folder relationship; preserve all other layout preferences.
	for (const icon of frappe.boot.desktop_icons || []) {
		if (icon.label === "Einstellungen" && icon.parent_icon === "Kanzlei")
			icon.parent_icon = null;
	}
	try {
		const key = `${frappe.session.user}:desktop`;
		const stored = localStorage.getItem(key);
		let layout = null;
		if (stored) {
			try {
				layout = JSON.parse(stored);
			} catch (_) {
				localStorage.removeItem(key);
			}
			if (layout !== null && !Array.isArray(layout)) localStorage.removeItem(key);
		}
		let changed = false;
		if (Array.isArray(layout))
			for (const icon of layout) {
				if (icon?.label === "Einstellungen" && icon.parent_icon === "Kanzlei") {
					icon.parent_icon = null;
					changed = true;
				}
			}
		if (changed) localStorage.setItem(key, JSON.stringify(layout));
	} catch (_) {
		// A corrupt or unavailable localStorage must not prevent Desk from starting.
	}
	const make_link = frappe.ui.sidebar_item.TypeLink.prototype.make;
	frappe.ui.sidebar_item.TypeLink.prototype.make = function () {
		make_link.call(this);
		if (this.item.link_type === "URL" && this.path?.startsWith("/desk/")) {
			this.wrapper?.find("a.item-anchor").removeAttr("target");
		}
	};

	frappe.ui.Sidebar.prototype.resolve_sidebar = function (entity) {
		const candidates = this.get_workspace_sidebars(entity);
		this.preferred_sidebars = candidates;
		return candidates.includes(this.sidebar_title)
			? this.sidebar_title
			: candidates[0] || "Kanzlei";
	};

	// Choose the first route before Desk starts; app_ready fires while the
	// initial asynchronous Desktop route is still rendering.
	if (
		["/desk", "/desk/", "/app", "/app/"].includes(window.location.pathname) &&
		!window.location.hash
	) {
		window.history.replaceState(null, "", `/desk/kanzlei-overview${window.location.search}`);
	}
}
