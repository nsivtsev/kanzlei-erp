frappe.ui.form.on("Contact", {
	refresh(frm) {
		if (frm.is_new() || !frm.perm?.some((permission) => permission.write)) return;
		frm.add_custom_button(__("Link old correspondence"), async () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the contact before linking old correspondence."));
				return;
			}
			await frappe.call({
				method: "kanzlei_erp.email_history.enqueue_contact_history",
				args: { contact: frm.doc.name },
				btn: frm.custom_buttons[__("Link old correspondence")],
			});
		});
	},
});
