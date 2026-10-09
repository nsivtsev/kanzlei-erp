// Staff actions on existing Task records; Communication links never send mail.
const fibuQuestionStates = ["Draft", "Waiting", "Answer Received", "Resolved", "Cancelled"];
const fibuQuestionEscape = (value) => frappe.utils.escape_html(String(value || ""));
const fibuQuestionDate = (value) => value ? frappe.datetime.str_to_user(value) : "";

function fibuQuestionCreate(contextDoctype, contextName, defaults = {}, onCreated = null) {
	const fields = [];
	if (!contextName) fields.push(
		{ fieldname: "context_doctype", fieldtype: "Select", label: __("Work Context"), options: "FiBu Package\nFiBu Supplement", default: "FiBu Package", reqd: 1 },
		{ fieldname: "context_name", fieldtype: "Dynamic Link", label: __("FiBu Period or Supplement"), options: "context_doctype", reqd: 1 }
	);
	fields.push(
		{ fieldname: "subject", fieldtype: "Data", label: __("Question subject"), default: defaults.subject || "", reqd: 1 },
		{ fieldname: "description", fieldtype: "Text Editor", label: __("Question content"), default: defaults.description || "" }
	);
	const creationKey = crypto.randomUUID();
	let pending = false;
	const dialog = new frappe.ui.Dialog({ title: __("New Question"), fields, primary_action_label: __("Create"),
		async primary_action(values) {
			if (pending) return;
			pending = true;
			dialog.get_primary_btn().prop("disabled", true);
			try {
				const { message: name } = await frappe.call({ method: "kanzlei_erp.fibu_questions.create_question", args: {
					context_doctype: contextDoctype || values.context_doctype, context_name: contextName || values.context_name,
					values: { ...defaults, subject: values.subject, description: values.description, creation_key: creationKey }
				} });
				dialog.hide();
				if (onCreated) await onCreated(name);
				frappe.set_route("Form", "Task", name);
			} finally { pending = false; dialog.get_primary_btn().prop("disabled", false); }
		}
	});
	dialog.show();
}

function fibuQuestionActions(frm) {
	if (frm.doc.kanzlei_work_kind !== "Question" || frm.is_new()) return;
	const state = frm.doc.kanzlei_question_state;
	const writable = frm.perm.some((p) => p.write);
	const wrapper = frm.get_field("kanzlei_question_html").$wrapper;
	const events = frm.doc.kanzlei_question_events || [];
	const e = fibuQuestionEscape;
	const detailLabels = {
		state: "Question State",
		answer: "Answer content", is_partial: "Partial answer", remaining: "Remaining Question", note: "Request description",
		reason: "Reason", decision: "Question Decision", requested_on: "Requested On", answered_on: "Answered On",
		subject: "Question subject", description: "Question content", responsible: "Question Responsible", recipient: "Question Recipient",
		entry_key: "Checklist Entry ID", file: "Question Document", operation: "Operation Context",
		expected_response_on: "Expected Response On", reminder_on: "Question Reminder On"
	};
	const displayValue = (key, value) => {
		if (value === undefined || value === null || value === "") return "";
		if (key === "is_partial") return __(Number(value) ? "Yes" : "No");
		if (key === "state") return __(value);
		if (["requested_on", "answered_on", "expected_response_on", "reminder_on"].includes(key)) return fibuQuestionDate(value);
		return value;
	};
	wrapper.html(!state ? `<p class="text-muted">${e(__("Question management was not previously recorded."))}</p>` :
		`<p><strong>${e(__(state))}</strong></p>${events.map((event) => {
			const details = JSON.parse(event.details_json || "{}");
			const lines = Object.entries(details).map(([key, value]) =>
				`<p><strong>${e(__(detailLabels[key] || key))}:</strong> ${e(displayValue(key, value))}</p>`).join("");
			const snapshots = [JSON.parse(event.before_json || "{}"), JSON.parse(event.after_json || "{}")];
			const changes = Object.keys(snapshots[1]).filter((key) => detailLabels[key] && snapshots[0][key] !== snapshots[1][key]).map((key) =>
				`<p>${e(__(detailLabels[key]))}: ${e(displayValue(key, snapshots[0][key]))} → ${e(displayValue(key, snapshots[1][key]))}</p>`).join("");
			const link = event.communication ? `<a href="${e(frappe.utils.get_form_link("Communication", event.communication))}">${e(__("Communication"))}</a>` : "";
			return `<details class="mb-2"><summary>${e(__(event.action))} · ${e(event.recorded_by)} · ${e(frappe.datetime.str_to_user(event.recorded_at))}</summary>${lines}${changes}${link}</details>`;
		}).join("")}`
	);
	if (state) {
		for (const field of ["status", "subject", "description", "kanzlei_work_kind", "kanzlei_customer", "kanzlei_fibu_package", "kanzlei_fibu_supplement", "completed_by", "completed_on"]) frm.set_df_property(field, "read_only", 1);
	}
	if (!writable) return;
	const terminal = ["Resolved", "Cancelled"].includes(state);
	const editor = () => [
		{ fieldname: "subject", fieldtype: "Data", label: __("Question subject"), default: frm.doc.subject, reqd: 1 },
		{ fieldname: "description", fieldtype: "Text Editor", label: __("Question content"), default: frm.doc.description },
		{ fieldname: "responsible", fieldtype: "Link", label: __("Question Responsible"), options: "User", reqd: 1, default: frm.doc.kanzlei_question_responsible,
			get_query: () => ({ filters: { enabled: 1, user_type: "System User" } }) },
		{ fieldname: "recipient", fieldtype: "Link", label: __("Question Recipient"), options: "Contact", default: frm.doc.kanzlei_question_recipient },
		{ fieldname: "entry_key", fieldtype: "Select", label: __("Checklist Source"), options: "", default: frm.doc.kanzlei_question_entry_key },
		{ fieldname: "file", fieldtype: "Link", label: __("Question Document"), options: "File", default: frm.doc.kanzlei_question_file, get_query: () => ({ filters: { is_private: 1, is_folder: 0 } }) },
		{ fieldname: "operation", fieldtype: "Small Text", label: __("Operation Context"), default: frm.doc.kanzlei_question_operation },
		{ fieldname: "expected_response_on", fieldtype: "Date", label: __("Expected Response On"), default: frm.doc.kanzlei_question_expected_response_on },
		{ fieldname: "reminder_on", fieldtype: "Date", label: __("Question Reminder On"), default: frm.doc.kanzlei_question_reminder_on }
	];
	const communicationField = { fieldname: "communication", fieldtype: "Link", label: __("Communication"), options: "Communication" };
	const actions = !state ? [["initialize", "Initialize Question"]] : terminal ? [["reopen", "Reopen Question"]] : [
		["edit", "Edit Question"], ["request", "Record Request"],
		...(["Waiting", "Answer Received"].includes(state) ? [["response", "Record Answer"]] : []),
		["resolve", "Resolve Question"], ["cancel", "Cancel Question"]
	];
	for (const [action, label] of actions) frm.add_custom_button(__(label), async () => {
		if (frm.is_dirty()) { frappe.msgprint(__("Save or reload the record before applying a question action.")); return; }
		const expectedModified = frm.doc.modified;
		let fields;
		if (["edit", "initialize"].includes(action)) {
			fields = editor();
			const context = await frappe.db.get_doc(frm.doc.kanzlei_fibu_supplement ? "FiBu Supplement" : "FiBu Package", frm.doc.kanzlei_fibu_supplement || frm.doc.kanzlei_fibu_package);
			fields.find((f) => f.fieldname === "responsible").default ||= context.responsible;
			fields.find((f) => f.fieldname === "entry_key").options = [{ value: "", label: "" }, ...(context.checklist || []).map((r) => ({ value: r.entry_key, label: r.source_title }))];
		} else if (action === "request") fields = [
			{ fieldname: "recipient", fieldtype: "Link", label: __("Question Recipient"), options: "Contact", default: frm.doc.kanzlei_question_recipient, reqd: 1 },
			{ fieldname: "requested_on", fieldtype: "Date", label: __("Requested On"), default: frappe.datetime.get_today(), reqd: 1 },
			{ fieldname: "expected_response_on", fieldtype: "Date", label: __("Expected Response On"), default: frm.doc.kanzlei_question_expected_response_on, reqd: 1 },
			{ fieldname: "reminder_on", fieldtype: "Date", label: __("Question Reminder On"), default: frm.doc.kanzlei_question_reminder_on },
			communicationField, { fieldname: "note", fieldtype: "Small Text", label: __("Request description"), mandatory_depends_on: "eval:!doc.communication" }
		];
		else if (action === "response") fields = [
			{ fieldname: "answered_on", fieldtype: "Date", label: __("Answered On"), default: frappe.datetime.get_today(), reqd: 1 },
			{ fieldname: "answer", fieldtype: "Small Text", label: __("Answer content"), reqd: 1 },
			{ fieldname: "is_partial", fieldtype: "Check", label: __("Partial answer") },
			{ fieldname: "remaining", fieldtype: "Small Text", label: __("Remaining Question"), default: frm.doc.kanzlei_question_remaining, mandatory_depends_on: "eval:doc.is_partial" }, communicationField
		];
		else fields = [{ fieldname: action === "resolve" ? "decision" : "reason", fieldtype: "Small Text", label: __(action === "resolve" ? "Question Decision" : "Reason"), reqd: 1 }];
		let pending = false;
		const dialog = new frappe.ui.Dialog({ title: __(label), fields, primary_action_label: __("Save"), async primary_action(values) {
			if (pending) return;
			if (frm.is_dirty() || frm.doc.modified !== expectedModified) { frappe.msgprint(__("This record changed. Reload it before applying the workflow action")); return; }
			pending = true; dialog.get_primary_btn().prop("disabled", true);
			try {
				await frappe.call({ method: "kanzlei_erp.fibu_questions.update_question", args: { name: frm.doc.name, action, values, expected_modified: expectedModified } });
				dialog.hide(); await frm.reload_doc();
			} finally { pending = false; dialog.get_primary_btn().prop("disabled", false); }
		} });
		dialog.show();
	}, __("Question"));
}

function fibuQuestionRow(task) {
	const e = fibuQuestionEscape;
	const state = task.kanzlei_question_state;
	return `<span>${e(__(state || "Question management was not previously recorded."))}</span>${task.kanzlei_question_responsible ? ` · ${e(task.kanzlei_question_responsible)}` : ""}${task.kanzlei_question_expected_response_on ? ` · ${e(__("Expected Response On"))}: ${e(fibuQuestionDate(task.kanzlei_question_expected_response_on))}` : ""}${task.kanzlei_question_reminder_on ? ` · ${e(__("Question Reminder On"))}: ${e(fibuQuestionDate(task.kanzlei_question_reminder_on))}` : ""}${task.kanzlei_question_remaining ? `<br>${e(task.kanzlei_question_remaining)}` : ""}`;
}

Object.assign(window, { fibuQuestionStates, fibuQuestionEscape, fibuQuestionDate,
	fibuQuestionCreate, fibuQuestionActions, fibuQuestionRow });
