function fibuPackageWorkList(frm) {
	frappe.db.get_list("Task", {
		fields: ["name", "subject", "status", "kanzlei_work_kind"],
		filters: { kanzlei_fibu_package: frm.doc.name, kanzlei_fibu_supplement: ["is", "not set"] },
		limit: 0,
	}).then((tasks) => {
		const wrapper = frm.get_field("work_html").$wrapper;
		const groups = [
			["Task", __("Tasks")],
			["Question", __("Questions")],
		];
		wrapper.html(groups.map(([kind, heading]) => {
			const rows = (tasks || []).filter((task) => (task.kanzlei_work_kind || "Task") === kind);
			return `<p><strong>${heading}</strong></p>${rows.length ? rows.map((task) =>
				`<p><a href="#" data-task="${frappe.utils.escape_html(task.name)}">${frappe.utils.escape_html(task.subject)}</a> — ${frappe.utils.escape_html(__(task.status))}</p>`
			).join("") : `<p class="text-muted">${__("No entries")}</p>`}`;
		}).join(""));
		wrapper.find("a[data-task]").on("click", (event) => {
			event.preventDefault();
			frappe.set_route("Form", "Task", event.currentTarget.dataset.task);
		});
	});
}

function fibuPackageSupplementList(frm) {
	frappe.db.get_list("FiBu Supplement", {
		fields: ["name", "display_title", "status", "reason"],
		filters: { package: frm.doc.name },
		limit: 0,
	}).then((supplements) => {
		const wrapper = frm.get_field("supplements_html").$wrapper;
		wrapper.html((supplements || []).length ? supplements.map((entry) =>
			`<p><a href="#" data-supplement="${frappe.utils.escape_html(entry.name)}">${frappe.utils.escape_html(entry.display_title)}</a> — ${frappe.utils.escape_html(__(entry.status))}: ${frappe.utils.escape_html(entry.reason)}</p>`
		).join("") : `<p class="text-muted">${__("No supplements")}</p>`);
		wrapper.find("a[data-supplement]").on("click", (event) => {
			event.preventDefault();
			frappe.set_route("Form", "FiBu Supplement", event.currentTarget.dataset.supplement);
		});
	});
}

frappe.ui.form.on("FiBu Package", {
	onload(frm) {
		if (frm.is_new() && !frm.doc.responsible) frm.set_value("responsible", frappe.session.user);
	},
	refresh(frm) {
		frm.set_query("service", () => ({ filters: { disabled: 0, is_stock_item: 0 } }));
		frm.toggle_display("period_number", frm.doc.period_type !== "Yearly");
		if (frm.is_new()) return;
		fibuPackageWorkList(frm);
		fibuPackageSupplementList(frm);
		if (frm.doc.status === "Closed") {
			frm.disable_save();
			if (frappe.model.can_create("FiBu Supplement")) {
				frm.add_custom_button(__("New Supplement"), () => {
					frappe.new_doc("FiBu Supplement", {
						package: frm.doc.name,
						responsible: frm.doc.responsible,
						deputy: frm.doc.deputy,
					});
				}, __("Create"));
			}
			return;
		}
		for (const [label, kind] of [["New Task", "Task"], ["New Question", "Question"]]) {
			frm.add_custom_button(__(label), () => {
				frappe.new_doc("Task", {
					kanzlei_customer: frm.doc.customer,
					kanzlei_fibu_package: frm.doc.name,
					kanzlei_work_kind: kind,
				});
			}, __("Create"));
		}
		frm.add_custom_button(__("Upload document"), () => {
			new frappe.ui.FileUploader({
				doctype: frm.doctype,
				docname: frm.doc.name,
				frm,
				make_attachments_public: 0,
				allow_toggle_private: false,
				on_success: async (file) => {
					frm.add_child("files", { file: file.name });
					await frm.save();
					frm.reload_doc();
				},
			});
		});
		frm.add_custom_button(__("Close"), () => {
			frappe.prompt([
				{ fieldname: "note", fieldtype: "Small Text", label: __("Closure Note"), reqd: 1 },
			], async (values) => {
				await frm.call("close", { note: values.note });
				frm.reload_doc();
			}, __("Close FiBu Package"), __("Close"));
		});
	},
	period_type(frm) {
		frm.toggle_display("period_number", frm.doc.period_type !== "Yearly");
		if (frm.doc.period_type === "Yearly") frm.set_value("period_number", null);
	},
});
