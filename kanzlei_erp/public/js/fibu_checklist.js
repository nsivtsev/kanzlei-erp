// Staff-confirmed completeness uses server actions and the period's source snapshot.
(() => {
	const statuses = ["Expected", "Partially Received", "Received", "Checked", "Not Applicable"];
	const categories = ["Incoming Invoices", "Outgoing Invoices", "Bank Account", "Card", "Cash", "Payment Service", "Marketplace", "Contracts", "Assets", "Other"];
	const statusLabel = (value) => value === "Received" ? __("Received in full") : __(value);
	const escape = (value) => frappe.utils.escape_html(String(value || ""));
	const evidenceFields = [
		{ fieldname: "file", fieldtype: "Link", label: __("File"), options: "File", in_list_view: 1, get_query: () => ({ filters: { is_private: 1, is_folder: 0 } }) },
		{ fieldname: "communication", fieldtype: "Link", label: __("Communication"), options: "Communication" },
		{ fieldname: "external_url", fieldtype: "Data", label: __("External Evidence URL"), in_list_view: 1 },
		{ fieldname: "covered_from", fieldtype: "Date", label: __("Covered From"), reqd: 1, in_list_view: 1 },
		{ fieldname: "covered_to", fieldtype: "Date", label: __("Covered To"), reqd: 1, in_list_view: 1 },
		{ fieldname: "note", fieldtype: "Small Text", label: __("Note") },
	];

	function historyDetails(snapshot, entryKey) {
		const items = (snapshot.items || []).filter((item) => !entryKey || item.entry_key === entryKey);
		return items.map((item) => {
			const evidence = (snapshot.evidence || []).filter((row) => row.entry_key === item.entry_key).map((row) =>
				`<li>${escape(row.covered_from)} — ${escape(row.covered_to)} · ${escape(row.file || row.communication || row.external_url)}${row.note ? ` · ${escape(row.note)}` : ""}</li>`).join("");
			return `<p><strong>${escape(item.source_title)}</strong> · ${escape(statusLabel(item.status))}<br>${escape(item.expected_from)} — ${escape(item.expected_to)}${item.reason ? `<br>${escape(item.reason)}` : ""}${item.note ? `<br>${escape(item.note)}` : ""}</p>${evidence ? `<ul>${evidence}</ul>` : ""}`;
		}).join("") || `<p>${escape(__("No entries"))}</p>`;
	}

	function history(frm) {
		const events = frm.doc.checklist_events || [];
		const dialog = new frappe.ui.Dialog({ title: __("Checklist History"), size: "large", fields: [{ fieldname: "history", fieldtype: "HTML" }] });
		dialog.fields_dict.history.$wrapper.html(events.length ? events.map((event) => {
			const before = JSON.parse(event.before_json || "{}");
			const after = JSON.parse(event.after_json || "{}");
			const item = (after.items || []).find((row) => row.entry_key === event.entry_key);
			const old = (before.items || []).find((row) => row.entry_key === event.entry_key);
			return `<div class="mb-3"><strong>${escape(__(event.action))}</strong> · ${escape(event.recorded_by)} · ${escape(frappe.datetime.str_to_user(event.recorded_at))}${item ? `<p>${escape(item.source_title)}: ${old ? `${escape(statusLabel(old.status))} → ` : ""}${escape(statusLabel(item.status))}<br>${escape(item.expected_from)} — ${escape(item.expected_to)}${item.reason ? `<br>${escape(item.reason)}` : ""}${item.note ? `<br>${escape(item.note)}` : ""}</p>` : ""}<details><summary>${escape(__("Change Details"))}</summary><p><strong>${escape(__("Before Change"))}</strong></p>${historyDetails(before, event.entry_key)}<p><strong>${escape(__("After Change"))}</strong></p>${historyDetails(after, event.entry_key)}</details></div>`;
		}).join("") : `<p>${escape(__("No entries"))}</p>`);
		dialog.show();
	}

	function editEntry(frm, item) {
		const expectedModified = frm.doc.modified;
		const existing = (frm.doc.checklist_evidence || []).filter((row) => row.entry_key === item.entry_key).map((row) => Object.fromEntries(evidenceFields.map((field) => [field.fieldname, row[field.fieldname] || ""])));
		const dialog = new frappe.ui.Dialog({
			title: __("Update Source Completeness"), size: "extra-large",
			fields: [
				{ fieldname: "source", fieldtype: "Data", label: __("Source Name"), read_only: 1, default: item.source_title },
				{ fieldname: "status", fieldtype: "Select", label: __("Completeness Status"), options: statuses.map((value) => ({ value, label: statusLabel(value) })), reqd: 1, default: item.status },
				{ fieldname: "expected_from", fieldtype: "Date", label: __("Expected From"), reqd: 1, default: item.expected_from },
				{ fieldname: "expected_to", fieldtype: "Date", label: __("Expected To"), reqd: 1, default: item.expected_to },
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), default: item.reason, mandatory_depends_on: "eval:doc.status=='Not Applicable'" },
				{ fieldname: "note", fieldtype: "Small Text", label: __("Note"), default: item.note },
				{ fieldname: "evidence_help", fieldtype: "HTML", options: `<p class="text-muted">${escape(__("Choose one file, communication, or external link per evidence row. Enter the dates actually covered."))}</p>` },
				{ fieldname: "evidence", fieldtype: "Table", label: __("Checklist Evidence"), fields: evidenceFields, data: existing, in_place_edit: true },
				{ fieldname: "upload", fieldtype: "Button", label: __("Upload evidence file"), click() {
					new frappe.ui.FileUploader({ doctype: frm.doctype, docname: frm.doc.name, make_attachments_public: 0, allow_toggle_private: false, on_success(file) {
						const table = dialog.fields_dict.evidence;
						table.df.data.push({ file: file.name, covered_from: dialog.get_value("expected_from"), covered_to: dialog.get_value("expected_to") });
						table.refresh();
					} });
				} },
			],
			primary_action_label: __("Save"),
			primary_action(values) {
				const evidence = (values.evidence || []).map((row) => Object.fromEntries(evidenceFields.map((field) => [field.fieldname, row[field.fieldname] || ""])));
				const { source, ...args } = values;
				delete args.upload;
				delete args.evidence_help;
				if (frm.is_dirty() || frm.doc.modified !== expectedModified) {
					frappe.msgprint(__("This record changed. Reload it before applying the workflow action"));
					return;
				}
				frm.call("update_checklist_entry", { ...args, entry_key: item.entry_key, evidence: JSON.stringify(evidence), expected_modified: expectedModified }).then(async () => { dialog.hide(); await frm.reload_doc(); });
			},
		});
		dialog.show();
	}

	async function addSource(frm) {
		if (frm.is_dirty()) await frm.save();
		const parent = frm.doctype === "FiBu Package" ? frm.doc : await frappe.db.get_doc("FiBu Package", frm.doc.package);
		const customer = await frappe.db.get_doc("Customer", parent.customer);
		const profile = customer.kanzlei_data_sources || [];
		const original = parent.checklist || [];
		const origins = frm.doctype === "FiBu Supplement" ? ["Original Package", "Client Profile", "Manual"] : ["Client Profile", "Manual"];
		const dialog = new frappe.ui.Dialog({
			title: __("Add Checklist Source"),
			fields: [
				{ fieldname: "origin", fieldtype: "Select", label: __("Source Origin"), options: origins.join("\n"), default: origins[0], reqd: 1, onchange() {
					const origin = dialog.get_value("origin");
					const rows = origin === "Original Package" ? original : profile;
					dialog.set_df_property("selection", "options", rows.map((row) => ({ value: row.entry_key || row.source_key, label: row.source_title })));
					dialog.set_value("selection", "");
				} },
				{ fieldname: "selection", fieldtype: "Select", label: __("Source Name"), options: (origins[0] === "Original Package" ? original : profile).map((row) => ({ value: row.entry_key || row.source_key, label: row.source_title })), depends_on: "eval:doc.origin!='Manual'", mandatory_depends_on: "eval:doc.origin!='Manual'" },
				{ fieldname: "category", fieldtype: "Select", label: __("Source Category"), options: categories.join("\n"), depends_on: "eval:doc.origin=='Manual'", mandatory_depends_on: "eval:doc.origin=='Manual'" },
				{ fieldname: "source_title", fieldtype: "Data", label: __("Source Name"), depends_on: "eval:doc.origin=='Manual'", mandatory_depends_on: "eval:doc.origin=='Manual'" },
				{ fieldname: "expected_from", fieldtype: "Date", label: __("Expected From"), depends_on: "eval:doc.origin=='Manual'", mandatory_depends_on: "eval:doc.origin=='Manual'", default: parent.period_start },
				{ fieldname: "expected_to", fieldtype: "Date", label: __("Expected To"), depends_on: "eval:doc.origin=='Manual'", mandatory_depends_on: "eval:doc.origin=='Manual'", default: parent.period_end },
			],
			primary_action_label: __("Add"), primary_action(values) {
				let args;
				if (values.origin === "Manual") args = { category: values.category, source_title: values.source_title, expected_from: values.expected_from, expected_to: values.expected_to };
				else args = values.origin === "Original Package" ? { parent_entry_key: values.selection } : { source_key: values.selection };
				fibuWorkflowSaveThenCall(frm, "add_checklist_source", args).then(() => dialog.hide());
			},
		});
		dialog.show();
	}

	async function render(frm) {
		const wrapper = frm.get_field("checklist_html").$wrapper;
		if (frm.is_new()) {
			wrapper.html(`<p class="text-muted">${escape(__("The source checklist is created when the period is saved."))}</p>`);
			return;
		}
		const { message: summary } = await frappe.call({ method: "kanzlei_erp.fibu_checklist.get_checklist_summary", args: { doctype: frm.doctype, name: frm.doc.name } });
		const writable = frm.doc.status === "Open" && frm.perm.some((permission) => permission.write);
		const items = frm.doc.checklist || [];
		let html = "";
		if (!summary.initialized) html += `<p class="text-muted">${escape(__("Completeness was not recorded before checklist rollout."))}</p>`;
		else if (!items.length) html += `<p class="text-warning">${escape(__("Sources are not configured"))}</p>`;
		if (items.length) {
			html += `<p>${statuses.map((status) => `${escape(statusLabel(status))}: <strong>${summary.counts[status]}</strong>`).join(" · ")}</p>`;
			if (!summary.complete) html += `<p class="text-warning">${escape(__("The source checklist is incomplete. Missing or unchecked sources remain visible."))}</p>`;
			html += `<div class="table-responsive"><table class="table table-bordered"><thead><tr>${["Source Name", "Expected Interval", "Completeness Status", "Missing Dates / Review", "Checklist Evidence"].map((label) => `<th>${escape(__(label))}</th>`).join("")}${writable ? "<th></th>" : ""}</tr></thead><tbody>`;
			for (const item of items) {
				const pending = summary.pending.find((row) => row.entry_key === item.entry_key);
				const details = pending ? (pending.missing.length ? pending.missing.map(([a, b]) => `${escape(a)} — ${escape(b)}`).join("<br>") : escape(__("Awaiting review"))) : escape(item.reason || (item.checked_by ? `${item.checked_by} · ${frappe.datetime.str_to_user(item.checked_at)}` : ""));
				const evidence = (frm.doc.checklist_evidence || []).filter((row) => row.entry_key === item.entry_key).map((row) => {
					const label = `${escape(row.covered_from)} — ${escape(row.covered_to)}`;
					if (row.external_url) return `<a href="${escape(row.external_url)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
					const doctype = row.file ? "File" : "Communication";
					return `<a href="${escape(frappe.utils.get_form_link(doctype, row.file || row.communication))}">${label}</a>`;
				}).join("<br>");
				html += `<tr><td>${escape(item.source_title)}<br><small class="text-muted">${escape(__(item.category))}</small></td><td>${escape(item.expected_from)} — ${escape(item.expected_to)}</td><td>${escape(statusLabel(item.status))}</td><td>${details}</td><td>${evidence}</td>${writable ? `<td><button type="button" class="btn btn-xs btn-default" data-entry="${escape(item.entry_key)}">${escape(__("Update"))}</button></td>` : ""}</tr>`;
			}
			html += "</tbody></table></div>";
		}
		wrapper.html(html);
		wrapper.find("[data-entry]").on("click", (event) => editEntry(frm, items.find((row) => row.entry_key === event.currentTarget.dataset.entry)));
		frm.add_custom_button(__("Checklist History"), () => history(frm), __("Completeness"));
		if (writable) {
			if (!summary.initialized) frm.add_custom_button(__("Initialize Checklist"), () => fibuWorkflowSaveThenCall(frm, "initialize_checklist", {}), __("Completeness"));
			else frm.add_custom_button(__("Add Checklist Source"), () => addSource(frm), __("Completeness"));
		}
	}
	for (const doctype of ["FiBu Package", "FiBu Supplement"]) frappe.ui.form.on(doctype, { refresh: render });
})();
