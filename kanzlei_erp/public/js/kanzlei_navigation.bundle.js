// ERPNext entities span Selling, Projects, and Accounts. Keep their navigation
// in Kanzlei even when this browser remembers an older ERPNext sidebar.
if (frappe.boot.workspace_sidebar_item.kanzlei) {
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
		window.history.replaceState(null, "", `/desk/customer${window.location.search}`);
	}
}
