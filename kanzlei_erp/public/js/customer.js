// Keep the Mandant form focused on Kanzlei work; document permissions are unchanged.
function ensureMandantDocuments(frm) {
	const wrapper = frm.fields_dict.kanzlei_documents_html?.$wrapper?.get(0);
	if (!wrapper || !window.kanzlei_erp?.MandantDocuments) return;
	frm.__mandantDocuments ||= new window.kanzlei_erp.MandantDocuments(wrapper);
	frm.__mandantDocuments.setCustomer(frm.doc.name, frm.is_new());
}

frappe.ui.form.on("Customer", {
	setup(frm) {
		frm.set_query("service", "kanzlei_data_sources", () => ({ filters: { disabled: 0, is_stock_item: 0 } }));
	},
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
		ensureMandantDocuments(frm);
		if (!frm.is_new() && !frm.doc.kanzlei_sources_reviewed && frappe.model.can_write("Customer")) {
			frm.add_custom_button(__("Confirm source list"), () => {
				if (frm.is_dirty()) {
					frappe.msgprint(__("Save source changes before confirming the list"));
					return;
				}
				frappe.prompt(
					[{ fieldname: "confirmed", fieldtype: "Check", label: __("I confirm this list is complete"), reqd: 1 }],
					(values) => {
						if (!values.confirmed) return;
						frappe.call("kanzlei_erp.api.confirm_customer_sources", {
							customer_name: frm.doc.name,
							expected_modified: frm.doc.modified,
						}).then(() => frm.reload_doc());
					},
					__("Confirm source list"),
					__("Confirm")
				);
			}, __("FiBu"));
		}

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
	on_tab_change(frm) {
		if (frm.get_active_tab()?.df?.fieldname === "kanzlei_documents_tab") {
			ensureMandantDocuments(frm);
			frm.__mandantDocuments?.activate(true);
		}
	},
});
