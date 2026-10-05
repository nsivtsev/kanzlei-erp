const fibuPreparationStages = [
	"Collection",
	"Review",
	"Clarification",
	"Ready",
	"Transfer",
	"Receipt Confirmed",
];

function fibuWorkflowSaveThenCall(frm, method, args) {
	return (async () => {
		if (frm.is_dirty()) await frm.save();
		args.expected_modified = frm.doc.modified;
		await frm.call(method, args);
		await frm.reload_doc();
	})();
}

function fibuWorkflowStageDialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Change Preparation Stage"),
		fields: [
			{
				fieldname: "target_stage",
				fieldtype: "Select",
				label: __("Preparation Stage"),
				options: fibuPreparationStages.join("\n"),
				reqd: 1,
			},
			{ fieldname: "note", fieldtype: "Small Text", label: __("Note") },
		],
		primary_action_label: __("Change Stage"),
		primary_action(values) {
			fibuWorkflowSaveThenCall(frm, "change_preparation_stage", values)
				.then(() => dialog.hide());
		},
	});
	dialog.set_value("target_stage", frm.doc.preparation_stage);
	dialog.show();
}

function fibuWorkflowContextDialog(frm) {
	const completed = frm.doc.preparation_stage === "Receipt Confirmed";
	const dialog = new frappe.ui.Dialog({
		title: __("Update Workflow Context"),
		fields: [
			{
				fieldname: "waiting_for_mandant",
				fieldtype: "Check",
				label: __("Waiting for Mandant"),
			},
			{
				fieldname: "waiting_reason",
				fieldtype: "Small Text",
				label: __("Waiting Reason"),
				depends_on: "eval:doc.waiting_for_mandant",
			},
			{ fieldname: "blocking_reason", fieldtype: "Small Text", label: __("Blocking Reason") },
			{
				fieldname: "next_action",
				fieldtype: "Small Text",
				label: __("Next Action"),
				reqd: !completed,
			},
			{
				fieldname: "next_action_assignee",
				fieldtype: "Link",
				label: __("Next Action Assignee"),
				options: "User",
				reqd: !completed,
			},
			{
				fieldname: "next_action_task",
				fieldtype: "Link",
				label: __("Next Action Task"),
				options: "Task",
			},
			{ fieldname: "note", fieldtype: "Small Text", label: __("Note") },
		],
		primary_action_label: __("Update"),
		primary_action(values) {
			fibuWorkflowSaveThenCall(frm, "update_work_context", values)
				.then(() => dialog.hide());
		},
	});
	dialog.set_values({
		waiting_for_mandant: frm.doc.waiting_for_mandant,
		waiting_reason: frm.doc.waiting_reason,
		blocking_reason: frm.doc.blocking_reason,
		next_action: frm.doc.next_action,
		next_action_assignee: frm.doc.next_action_assignee,
		next_action_task: frm.doc.next_action_task,
	});
	dialog.fields_dict.next_action_task.get_query = () => ({
		filters: frm.doctype === "FiBu Supplement"
			? { kanzlei_fibu_package: frm.doc.package, kanzlei_fibu_supplement: frm.doc.name }
			: { kanzlei_fibu_package: frm.doc.name, kanzlei_fibu_supplement: ["is", "not set"] },
	});
	dialog.show();
}

for (const doctype of ["FiBu Package", "FiBu Supplement"]) {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			frm.toggle_display("waiting_reason", !!frm.doc.waiting_for_mandant);
			frm.toggle_display("blocking_reason", !!frm.doc.blocking_reason);
			const untrackedStage = frm.doc.status === "Closed" && !frm.doc.preparation_stage;
			frm.get_field("workflow_untracked_html").$wrapper
				.toggle(untrackedStage)
				.html(untrackedStage
					? `<p class="text-muted">${frappe.utils.escape_html(__("Preparation stage was not recorded before workflow rollout."))}</p>`
					: "");
			frm.set_query("next_action_task", () => ({
				filters: frm.doctype === "FiBu Supplement"
					? { kanzlei_fibu_package: frm.doc.package, kanzlei_fibu_supplement: frm.doc.name }
					: { kanzlei_fibu_package: frm.doc.name, kanzlei_fibu_supplement: ["is", "not set"] },
			}));
			if (frm.is_new() || frm.doc.status !== "Open") return;
			if (frm.doc.preparation_stage !== "Receipt Confirmed") {
				frm.add_custom_button(__("Change Preparation Stage"), () => fibuWorkflowStageDialog(frm), __("Workflow"));
			}
			frm.add_custom_button(__("Update Workflow Context"), () => fibuWorkflowContextDialog(frm), __("Workflow"));
		},
	});
}
