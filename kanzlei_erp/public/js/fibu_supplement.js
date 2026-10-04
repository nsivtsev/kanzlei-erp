frappe.ui.form.on("FiBu Supplement", {
	async onload(frm) {
		if (!frm.is_new() || !frm.doc.package || frm.doc.responsible) return;
		const { message: assignments } = await frappe.db.get_value("FiBu Package", frm.doc.package, ["responsible", "deputy"]);
		await frm.set_value({ responsible: assignments.responsible, deputy: assignments.deputy });
	},
	refresh(frm) {
		if (frm.is_new()) return;
		frappe.db.get_list("Task", {
			fields: ["name", "subject", "status", "kanzlei_work_kind"],
			filters: { kanzlei_fibu_supplement: frm.doc.name },
			limit: 0,
		}).then((tasks) => {
			const wrapper = frm.get_field("work_html").$wrapper;
			wrapper.html([["Task", __("Tasks")], ["Question", __("Questions")]].map(([kind, heading]) => {
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
		if (frm.doc.status === "Closed") {
			frm.disable_save();
			return;
		}
		for (const [label, kind] of [["New Task", "Task"], ["New Question", "Question"]]) {
			frm.add_custom_button(__(label), async () => {
				const { message: customer } = await frappe.db.get_value("FiBu Package", frm.doc.package, "customer");
				frappe.new_doc("Task", {
					kanzlei_customer: customer.customer,
					kanzlei_fibu_package: frm.doc.package,
					kanzlei_fibu_supplement: frm.doc.name,
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
			}, __("Close Supplement"), __("Close"));
		});
	},
});
