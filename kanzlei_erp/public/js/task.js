frappe.ui.form.on("Task", {
	refresh(frm) {
		frm.set_df_property("kanzlei_customer", "read_only", !!frm.doc.kanzlei_incoming_letter);
		frm.set_query("kanzlei_fibu_supplement", () => ({
			filters: { package: frm.doc.kanzlei_fibu_package, status: "Open" },
		}));
		frm.toggle_display("kanzlei_fibu_supplement", !!frm.doc.kanzlei_fibu_package);
		fibuQuestionActions(frm);
	},
});
