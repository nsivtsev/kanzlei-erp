// File comments reload through the permission-checked API, without touching edits.
frappe.realtime.on("file-docinfo-changed", async ({ name } = {}) => {
	const frm = window.cur_frm;
	if (!name || frm?.doc?.doctype !== "File" || frm.doc.name !== name) return;
	try {
		const response = await frappe.call({
			method: "frappe.desk.form.load.get_docinfo",
			args: { doctype: "File", name },
		});
		if (window.cur_frm !== frm || frm.doc.name !== name || !response.docinfo) return;
		frappe.model.docinfo.File ||= {};
		frappe.model.docinfo.File[name] = response.docinfo;
		frm.timeline?.refresh();
		frm.footer?.refresh_comments_count();
	} catch {
		// The normal Frappe API displays permission/network errors.
	}
});
