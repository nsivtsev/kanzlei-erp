const checklistStatuses = ["Expected", "Partially Received", "Received in Full", "Reviewed", "Not Applicable"];
const sourceCategories = [
	"Incoming Invoices",
	"Outgoing Invoices",
	"Bank Account",
	"Card",
	"Cash Register",
	"Payment Service",
	"Marketplace",
	"Contract",
	"Assets",
];

function checklistEntries(frm) {
	return frm.doc.checklist_entries || [];
}

function renderChecklist(frm) {
	const wrapper = frm.fields_dict.checklist_summary?.$wrapper;
	if (!wrapper) return;
	if (!frm.doc.checklist_initialized) {
		wrapper.html(`<p class="text-muted">${__("Checklist not initialized")}</p>`);
		return;
	}
	const entries = checklistEntries(frm);
	if (!entries.length) {
		wrapper.html(`<p class="text-muted">${__("No configured sources apply to this period")}</p>`);
		return;
	}
	const count = (statuses) => entries.filter((entry) => statuses.includes(entry.status)).length;
	const evidence = frm.doc.checklist_evidence || [];
	const counts = [
		`${__("Missing or partial")}: ${count(["Expected", "Partially Received"])}`,
		`${__("Received, awaiting review")}: ${count(["Received in Full"])}`,
		`${__("Reviewed")}: ${count(["Reviewed"])}`,
		`${__("Not Applicable")}: ${count(["Not Applicable"])}`,
	];
	const rows = entries.map(
		(entry) => {
			const sourceEvidence = evidence.filter((proof) => proof.source_id === entry.source_id);
			const proofLinks = sourceEvidence.map((proof) => {
				let label = proof.evidence_type;
				let href = "";
				if (proof.evidence_type === "File" && proof.file) {
					label = proof.file;
					href = `/app/file/${encodeURIComponent(proof.file)}`;
				} else if (proof.evidence_type === "Communication" && proof.communication) {
					label = proof.communication;
					href = `/app/communication/${encodeURIComponent(proof.communication)}`;
				} else if (proof.evidence_type === "External URL" && proof.external_url) {
					try {
						const url = new URL(proof.external_url);
						if (["http:", "https:"].includes(url.protocol) && !url.username && !url.password) {
							href = url.href;
							label = proof.external_url;
						}
					} catch (_error) {
						// Invalid stored links remain visible as text without becoming clickable.
					}
				}
				const text = frappe.utils.escape_html(label);
				const link = href
					? `<a href="${frappe.utils.escape_html(href)}" target="_blank" rel="noopener noreferrer">${text}</a>`
					: text;
				return `<li>${link} · ${frappe.utils.escape_html(proof.covered_from)} – ${frappe.utils.escape_html(proof.covered_through)}${proof.note ? ` · ${frappe.utils.escape_html(proof.note)}` : ""}</li>`;
			});
			const noEvidence = sourceEvidence.length ? "" : `<li class="text-muted">${__("No evidence")}</li>`;
			const reason = entry.status === "Not Applicable" && entry.inapplicable_reason
				? `<p><strong>${__("Reason Not Applicable")}:</strong> ${frappe.utils.escape_html(entry.inapplicable_reason)}</p>`
				: "";
			const sourceNote = entry.source_note
				? `<p class="text-muted">${frappe.utils.escape_html(entry.source_note)}</p>`
				: "";
			return `<article class="checklist-entry"><p><strong>${frappe.utils.escape_html(__(entry.category))} — ${frappe.utils.escape_html(entry.source_name)}</strong> · ${frappe.utils.escape_html(__(entry.status))} · ${frappe.utils.escape_html(entry.expected_from)} – ${frappe.utils.escape_html(entry.expected_through)} <button class="btn btn-xs btn-default" data-checklist-source="${frappe.utils.escape_html(entry.source_id)}">${__("Update")}</button></p>${sourceNote}${reason}<ul>${proofLinks.join("")}${noEvidence}</ul></article>`;
		}
	).join("");
	wrapper.html(`<p>${counts.join(" · ")}</p>${rows || `<p class="text-muted">${__("No configured sources")}</p>`}`);
	wrapper.find("button[data-checklist-source]").on("click", (event) => {
		const entry = entries.find((row) => row.source_id === event.currentTarget.dataset.checklistSource);
		if (entry) showUpdateDialog(frm, entry);
	});
}

function checklistAction(frm, method, args = {}) {
	return frm.call(method, { ...args, expected_modified: frm.doc.modified }).then(() => frm.reload_doc());
}

function showAddEntryDialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Add Expected Source"),
		fields: [
			{ fieldname: "category", fieldtype: "Select", label: __("Category"), options: sourceCategories.join("\n"), reqd: 1 },
			{ fieldname: "source_name", fieldtype: "Data", label: __("Source Name"), reqd: 1 },
			{ fieldname: "source_note", fieldtype: "Small Text", label: __("Source Note") },
			{ fieldname: "service", fieldtype: "Link", label: __("Service"), options: "Item" },
			{ fieldname: "expected_from", fieldtype: "Date", label: __("Expected From"), default: frm.doc.period_start || "" , reqd: 1 },
			{ fieldname: "expected_through", fieldtype: "Date", label: __("Expected Through"), default: frm.doc.period_end || "", reqd: 1 },
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
		],
		primary_action_label: __("Add Source"),
		primary_action: (values) => {
			const { reason, ...source } = values;
			checklistAction(frm, "add_checklist_entry", { source_json: JSON.stringify(source), reason }).then(() => dialog.hide());
		},
	});
	dialog.show();
}

function showUpdateDialog(frm, entry) {
	const dialog = new frappe.ui.Dialog({
		title: __("Update Checklist Source"),
		fields: [
			{ fieldname: "category", fieldtype: "Select", label: __("Category"), options: sourceCategories.join("\n"), default: entry.category, read_only: 1, reqd: 1 },
			{ fieldname: "source_name", fieldtype: "Data", label: __("Source Name"), default: entry.source_name, reqd: 1 },
			{ fieldname: "source_note", fieldtype: "Small Text", label: __("Source Note"), default: entry.source_note, read_only: 1 },
			{ fieldname: "service", fieldtype: "Link", label: __("Service"), options: "Item", default: entry.service, read_only: 1 },
			{ fieldname: "expected_from", fieldtype: "Date", label: __("Expected From"), default: entry.expected_from, reqd: 1 },
			{ fieldname: "expected_through", fieldtype: "Date", label: __("Expected Through"), default: entry.expected_through, reqd: 1 },
			{ fieldname: "status", fieldtype: "Select", label: __("Status"), options: checklistStatuses.join("\n"), default: entry.status, reqd: 1 },
			{ fieldname: "inapplicable_reason", fieldtype: "Small Text", label: __("Reason Not Applicable"), default: entry.inapplicable_reason },
			{ fieldname: "evidence_type", fieldtype: "Select", label: __("Add Evidence"), options: "\nFile\nCommunication\nExternal URL" },
			{ fieldname: "file", fieldtype: "Link", label: __("File"), options: "File", depends_on: "eval:doc.evidence_type == 'File'" },
			{ fieldname: "communication", fieldtype: "Link", label: __("Communication"), options: "Communication", depends_on: "eval:doc.evidence_type == 'Communication'" },
			{ fieldname: "external_url", fieldtype: "Small Text", label: __("External URL"), depends_on: "eval:doc.evidence_type == 'External URL'" },
			{ fieldname: "covered_from", fieldtype: "Date", label: __("Covered From"), depends_on: "eval:doc.evidence_type" },
			{ fieldname: "covered_through", fieldtype: "Date", label: __("Covered Through"), depends_on: "eval:doc.evidence_type" },
			{ fieldname: "evidence_note", fieldtype: "Small Text", label: __("Evidence Note"), depends_on: "eval:doc.evidence_type" },
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason for Change"), reqd: 1 },
		],
		primary_action_label: __("Save Checklist Entry"),
		primary_action: (values) => {
			const evidence = values.evidence_type ? {
				evidence_type: values.evidence_type,
				file: values.file,
				communication: values.communication,
				external_url: values.external_url,
				covered_from: values.covered_from,
				covered_through: values.covered_through,
				note: values.evidence_note,
			} : null;
			const values_json = { ...entry, ...values };
			delete values_json.evidence_type;
			delete values_json.file;
			delete values_json.communication;
			delete values_json.external_url;
			delete values_json.covered_from;
			delete values_json.covered_through;
			delete values_json.evidence_note;
			const reason = values_json.reason;
			delete values_json.reason;
			checklistAction(frm, "update_checklist_entry", {
				source_id: entry.source_id,
				values_json: JSON.stringify(values_json),
				evidence_json: evidence ? JSON.stringify(evidence) : "{}",
				reason,
			}).then(() => dialog.hide());
		},
	});
	dialog.show();
}

function showSupplementChecklistDialog(frm, sources) {
	const options = sources.map((source) => ({
		label: `${__(source.category)} — ${source.source_name} (${source.expected_from} – ${source.expected_through})`,
		value: source.source_id,
		checked: false,
	}));
	const dialog = new frappe.ui.Dialog({
		title: __("Select Sources Affected by this Supplement"),
		fields: [
			{ fieldname: "source_ids", fieldtype: "MultiCheck", label: __("Affected Sources"), options },
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), default: frm.doc.reason, reqd: 1 },
		],
		primary_action_label: __("Initialize Checklist"),
		primary_action: (values) => {
			checklistAction(frm, "initialize_checklist", {
				source_ids: JSON.stringify(values.source_ids || []),
				reason: values.reason,
			}).then(() => dialog.hide());
		},
	});
	dialog.show();
}

function showLegacyChecklistDialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Initialize Checklist"),
		fields: [
			{
				fieldname: "warning",
				fieldtype: "HTML",
				options: `<p class="text-warning">${__("This action captures the Mandant's currently confirmed source list. It cannot reconstruct which sources applied when the period began.")}</p>`,
			},
			{ fieldname: "confirmed", fieldtype: "Check", label: __("I understand this uses the current source list"), reqd: 1 },
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
		],
		primary_action_label: __("Initialize Checklist"),
		primary_action: (values) => {
			if (!values.confirmed) return;
			checklistAction(frm, "initialize_checklist", { reason: values.reason }).then(() => dialog.hide());
		},
	});
	dialog.show();
}

function setupChecklist(frm, doctype) {
	renderChecklist(frm);
	if (frm.is_new() || frm.doc.status !== "Open") return;
	if (!frm.doc.checklist_initialized) {
		frm.add_custom_button(__("Initialize Checklist"), () => {
			if (doctype === "FiBu Package") return showLegacyChecklistDialog(frm);
			frm.call("get_checklist_sources").then((result) => showSupplementChecklistDialog(frm, result.message || []));
		}, __("Completeness Checklist"));
		return;
	}
	frm.add_custom_button(__("Add Expected Source"), () => showAddEntryDialog(frm), __("Completeness Checklist"));
}

frappe.ui.form.on("FiBu Package", {
	refresh(frm) {
		setupChecklist(frm, "FiBu Package");
	},
});

frappe.ui.form.on("FiBu Supplement", {
	refresh(frm) {
		setupChecklist(frm, "FiBu Supplement");
	},
});
