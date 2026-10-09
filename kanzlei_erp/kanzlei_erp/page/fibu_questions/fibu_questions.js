frappe.pages["fibu-questions"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Rückfragen"), single_column: true });
	const e = fibuQuestionEscape;
	let start = 0;
	let revision = 0;
	const controls = {};
	const body = $('<div class="p-3"></div>').appendTo(page.main);
	const definitions = [
		["customer", "Link", "Mandant", "Customer"], ["package", "Link", "FiBu Package", "FiBu Package"],
		["responsible", "Link", "Question Responsible", "User"],
		["state", "Select", "Question State", [{ value: "", label: __("All Question States") }, ...fibuQuestionStates.map((s) => ({ value: s, label: __(s) }))]],
		["overdue", "Check", "Overdue answers"], ["reminder_due", "Check", "Reminders due"], ["include_closed", "Check", "Include closed questions"]
	];
	for (const [fieldname, fieldtype, label, options] of definitions) controls[fieldname] = page.add_field({
		fieldname, fieldtype, label: __(label), options, change() { start = 0; load(); }
	});
	page.set_primary_action(__("New Question"), () => fibuQuestionCreate(null, null, {}, load));
	async function load() {
		if (Object.keys(controls).length !== definitions.length) return;
		const current = ++revision;
		const filters = Object.fromEntries(Object.entries(controls).map(([k, c]) => [k, c.get_value()]));
		const { message: result } = await frappe.call({ method: "kanzlei_erp.fibu_questions.get_questions", args: { filters, start, page_length: 20 } });
		if (current !== revision) return;
		const summary = Object.entries(result.summary).map(([s, n]) => `${e(__(s))}: <strong>${n}</strong>`).join(" · ");
		body.html(`<p>${summary}</p><p>${e(__("Total questions"))}: ${result.total}</p><div class="table-responsive"><table class="table table-bordered"><thead><tr>${["Question", "Mandant", "FiBu Package", "Question State", "Question Responsible", "Expected Response On", "Question Reminder On", "Remaining Question"].map((h) => `<th>${e(__(h))}</th>`).join("")}</tr></thead><tbody>${result.rows.map((r) => {
			const link = (doctype, name, label) => name ? `<a href="${e(frappe.utils.get_form_link(doctype, name))}">${e(label || name)}</a>` : "";
			return `<tr><td>${link("Task", r.name, r.subject)}</td><td>${link("Customer", r.kanzlei_customer)}</td><td>${link(r.kanzlei_fibu_supplement ? "FiBu Supplement" : "FiBu Package", r.kanzlei_fibu_supplement || r.kanzlei_fibu_package, r.context_title)}</td><td>${e(__(r.kanzlei_question_state || "Question management was not previously recorded."))}</td><td>${e(r.kanzlei_question_responsible)}${r.reminder_issue ? `<p class="text-danger">${e(__("Responsible employee is disabled or has no access."))}</p>` : ""}</td><td>${e(fibuQuestionDate(r.kanzlei_question_expected_response_on))}</td><td>${e(fibuQuestionDate(r.kanzlei_question_reminder_on))}</td><td>${e(r.kanzlei_question_remaining)}</td></tr>`;
		}).join("") || `<tr><td colspan="8">${e(__("No entries"))}</td></tr>`}</tbody></table></div><div class="d-flex justify-content-between"><button class="btn btn-default" data-prev ${start === 0 ? "disabled" : ""}>${e(__("Previous"))}</button><button class="btn btn-default" data-next ${start + 20 >= result.total ? "disabled" : ""}>${e(__("Next"))}</button></div>`);
		body.find("[data-prev]").on("click", () => { start = Math.max(0, start - 20); load(); });
		body.find("[data-next]").on("click", () => { start += 20; load(); });
	}
	wrapper.questions_load = load;
	load();
};
frappe.pages["fibu-questions"].on_page_show = function (wrapper) { if (wrapper.questions_load) wrapper.questions_load(); };
