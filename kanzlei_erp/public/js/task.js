frappe.ui.form.on("Task", {
	refresh(frm) {
		if(!frm.is_new())frappe.require("posteingang_source.bundle.js").then(()=>{
			if(frappe.get_route_str()===`Form/Task/${frm.doc.name}`)frappe.posteingang.attach_task_source(frm);
		});
		frm.set_df_property("kanzlei_customer", "read_only", !!frm.doc.kanzlei_incoming_letter);
		frm.set_query("kanzlei_fibu_supplement", () => ({
			filters: { package: frm.doc.kanzlei_fibu_package, status: "Open" },
		}));
		frm.toggle_display("kanzlei_fibu_supplement", !!frm.doc.kanzlei_fibu_package);
		fibuQuestionActions(frm);
	},
});
