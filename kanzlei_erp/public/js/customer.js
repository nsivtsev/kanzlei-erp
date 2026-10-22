// Keep the Mandant form focused on Kanzlei work; document permissions are unchanged.
function ensureMandantDocuments(frm) {
	const wrapper = frm.fields_dict.kanzlei_documents_html?.$wrapper?.get(0);
	if (!wrapper || !window.kanzlei_erp?.MandantDocuments) return;
	frm.__mandantDocuments ||= new window.kanzlei_erp.MandantDocuments(wrapper);
	frm.__mandantDocuments.setCustomer(frm.doc.name, frm.is_new());
}

function mandantDriveContext(frm) {
	const previous = frm.__mandantDriveContext;
	if (previous?.doc === frm.doc && previous.customer === frm.doc.name) return previous;
	const context = { doc: frm.doc, customer: frm.doc.name, generation: 0, opening: null };
	frm.__mandantDriveContext = context;
	return context;
}

function currentMandantDrive(frm, context) {
	const route = frappe.get_route();
	return frm.doc === context.doc && frm.doc.name === context.customer && !frm.is_new()
		&& route[0] === "Form" && route[1] === "Customer" && route[2] === context.customer;
}

async function syncMandantDrive(frm, context, state) {
	if (!currentMandantDrive(frm, context)) return false;
	// The server owns this immutable binding. An older empty reply must not
	// clear a folder obtained from a completed creation or a newer lookup.
	if (state?.enabled && state.folder && context.doc.kanzlei_drive_folder !== state.folder) {
		await frm.set_value("kanzlei_drive_folder", state.folder, false, true);
	}
	return currentMandantDrive(frm, context);
}

function renderMandantDrive(frm, context, state) {
	if (!currentMandantDrive(frm, context)) return;
	frm.remove_custom_button(__("Drive Folder"));
	frm.remove_custom_button(__("Create Drive Folder"));
	const generation = ++context.generation;
	if (!state?.enabled) return;
	const folder = context.doc.kanzlei_drive_folder || state.folder;
	if (!folder && !state.can_create) return;
	const label = folder ? __("Drive Folder") : __("Create Drive Folder");
	frm.add_custom_button(label, () => {
		if (!currentMandantDrive(frm, context) || context.generation !== generation || context.opening) return;
		const operation = { promise: null };
		context.opening = operation;
		// Reserve a tab during the click so popup blockers do not reject the async result.
		const tab = window.open("about:blank", "_blank");
		if (tab) tab.opener = null;
		operation.promise = (async () => {
			try {
				const result = await frappe.call({
					method: `kanzlei_erp.mandant_drive.${folder ? "get_folder" : "create_folder"}`,
					args: { customer: context.customer },
				});
				if (!currentMandantDrive(frm, context)) { tab?.close(); return; }
				if (!result.message?.url) throw new Error("Folder unavailable");
				if (!await syncMandantDrive(frm, context, result.message)) { tab?.close(); return; }
				renderMandantDrive(frm, context, result.message);
				if (tab) tab.location.href = result.message.url;
				else frappe.msgprint(__("Allow popups to open the Drive folder."));
			} catch {
				tab?.close();
				if (currentMandantDrive(frm, context)) {
					frappe.msgprint(__("Could not open the Mandant folder. Check your access and try again."));
				}
			} finally {
				if (context.opening === operation) context.opening = null;
			}
		})();
		return operation.promise;
	});
}

async function ensureMandantDrive(frm) {
	frm.remove_custom_button(__("Drive Folder"));
	frm.remove_custom_button(__("Create Drive Folder"));
	if (frm.is_new()) return;
	const context = mandantDriveContext(frm);
	const generation = ++context.generation;
	try {
		const response = await frappe.call({
			method: "kanzlei_erp.mandant_drive.get_folder", args: { customer: context.customer },
		});
		if (context.generation !== generation || !await syncMandantDrive(frm, context, response.message)) return;
		renderMandantDrive(frm, context, response.message);
	} catch {
		// Frappe displays the server's permission or availability error.
	}
}

async function syncMandantDriveBeforeSave(frm) {
	if (frm.is_new() || frm.doc.kanzlei_drive_folder) return;
	const context = mandantDriveContext(frm);
	try {
		await context.opening?.promise;
		if (!currentMandantDrive(frm, context)) throw new Error("Form changed");
		const response = await frappe.call({
			method: "kanzlei_erp.mandant_drive.get_folder", args: { customer: context.customer },
		});
		if (typeof response.message?.enabled !== "boolean") throw new Error("Folder state unavailable");
		if (!await syncMandantDrive(frm, context, response.message)) throw new Error("Form changed");
	} catch {
		const message = currentMandantDrive(frm, context)
			? __("Could not synchronize the Mandant folder. Your changes are preserved. Please try saving again.")
			: __("Saving was cancelled because the Mandant form changed. Please save the current form again.");
		frappe.msgprint(message);
		throw new Error(message);
	}
}

frappe.ui.form.on("Customer", {
	before_save: syncMandantDriveBeforeSave,
	refresh(frm) {
		frappe.mandant_identifiers?.attach(frm);
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
