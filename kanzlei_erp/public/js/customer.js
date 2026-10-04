// Keep the Mandant form focused on Kanzlei work; document permissions are unchanged.
frappe.ui.form.on("Customer", {
	refresh(frm) {
		frm.toggle_display(
			"accounting_tab",
			frappe.session.user === "Administrator" || frappe.user.has_role("System Manager")
		);

		for (const doctype of ["Quotation", "Sales Order", "Opportunity", "Pricing Rule", "Payment Entry"]) {
			frm.remove_custom_button(__(doctype), __("Create"));
		}
		for (const report of ["Accounts Receivable", "Accounting Ledger"]) {
			frm.remove_custom_button(__(report), __("View"));
		}
		for (const action of ["Get Customer Group Details", "Link with Supplier"]) {
			frm.remove_custom_button(__(action), __("Actions"));
		}
		frm.dashboard.stats_area_row.empty();
		frm.dashboard.stats_area.hide();

		if (frm.is_new()) return;
		if (frappe.model.can_read("FiBu Package")) {
			frm.add_custom_button(__("FiBu Periods"), () => {
				frappe.route_options = { customer: frm.doc.name };
				frappe.set_route("List", "FiBu Package");
			});
		}
		if (frappe.model.can_create("FiBu Package")) {
			frm.add_custom_button(__("New FiBu Package"), () => {
				frappe.new_doc("FiBu Package", { customer: frm.doc.name });
			}, __("Create"));
		}
		if (frappe.model.can_read("Work Schedule")) {
			frm.add_custom_button(__("Work Schedules"), () => {
				frappe.route_options = { customer: frm.doc.name };
				frappe.set_route("List", "Work Schedule");
			});
		}
		if (frappe.model.can_create("Work Schedule")) {
			frm.add_custom_button(__("New Work Schedule"), () => {
				frappe.new_doc("Work Schedule", { customer: frm.doc.name });
			}, __("Create"));
		}
	},
});
