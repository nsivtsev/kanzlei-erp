// Keep the Mandant form focused on Kanzlei work; document permissions are unchanged.
function ensureMandantDocuments(frm) {
	const wrapper = frm.fields_dict.kanzlei_documents_html?.$wrapper?.get(0);
	if (!wrapper || !window.kanzlei_erp?.MandantDocuments) return;
	frm.__mandantDocuments ||= new window.kanzlei_erp.MandantDocuments(wrapper);
	frm.__mandantDocuments.setCustomer(frm.doc.name, frm.is_new());
}

async function ensureMandantDrive(frm) {
	frm.remove_custom_button(__("Drive Folder"));
	frm.remove_custom_button(__("Create Drive Folder"));
	const generation = (frm.__driveGeneration || 0) + 1;
	frm.__driveGeneration = generation;
	if (frm.is_new()) return;
	const customer = frm.doc.name;
	const current = () => frm.doc.name === customer && frm.__driveGeneration === generation && !frm.is_new();
	const api = "kanzlei_erp.mandant_drive";
	let response;
	try {
		response = await frappe.call({ method: `${api}.get_folder`, args: { customer } });
	} catch {
		return; // Frappe displays the server's permission or availability error.
	}
	const state = response.message;
	if (!current() || !state?.enabled || (!state.folder && !state.can_create)) return;
	const label = state.folder ? __("Drive Folder") : __("Create Drive Folder");
	frm.add_custom_button(label, async () => {
		if (!current() || frm.__driveOpening) return;
		frm.__driveOpening = true;
		// Reserve a tab during the click so popup blockers do not reject the async result.
		const tab = window.open("about:blank", "_blank");
		if (tab) tab.opener = null;
		try {
			const result = await frappe.call({
				method: `${api}.${state.folder ? "get_folder" : "create_folder"}`,
				args: { customer },
			});
			if (!result.message?.url) throw new Error("Folder unavailable");
			if (tab) tab.location.href = result.message.url;
			else frappe.msgprint(__("Allow popups to open the Drive folder."));
			if (!state.folder && current()) {
				state.folder = result.message.folder;
				frm.remove_custom_button(label);
				void ensureMandantDrive(frm);
			}
		} catch {
			tab?.close();
			frappe.msgprint(__("Could not open the Mandant folder. Check your access and try again."));
		} finally {
			frm.__driveOpening = false;
		}
	});
}

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
		ensureMandantDocuments(frm);
		void ensureMandantDrive(frm);

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
