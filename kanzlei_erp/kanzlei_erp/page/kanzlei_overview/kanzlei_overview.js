frappe.pages["kanzlei-overview"].on_page_load = function (wrapper) {
	wrapper.overview = new KanzleiOverview(wrapper);
};
frappe.pages["kanzlei-overview"].on_page_show = function (wrapper) {
	wrapper.overview?.refresh();
};

class KanzleiOverview {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Work Overview"),
			single_column: true,
		});
		this.bucket = "urgent";
		this.sections = Object.fromEntries(
			["tasks", "agenda", "mail"].map((key) => [key, { revision: 0, rows: [] }])
		);
		this.root = $('<div class="kanzlei-overview"></div>').appendTo(this.page.main);
		this.page.set_secondary_action(__("Refresh"), () => this.refresh(), "refresh");
		if (frappe.model.can_create("Task"))
			this.page.set_primary_action(
				__("New Task"),
				() =>
					frappe.model.with_doctype("Task", () => {
						const task = frappe.model.get_new_doc("Task");
						frappe.set_route("Form", "Task", task.name);
					}),
				"add"
			);
		const e = (s) => this.escape(__(s));
		this.root.html(`
			<div class="overview-intro"><div><span class="overview-eyebrow">KANZLEI</span><h2>${e(
				"Your team at a glance"
			)}</h2><p>${e(
			"Emails, tasks and upcoming team deadlines"
		)}</p></div><span class="overview-date"></span></div>
			<div class="overview-summary" aria-label="${e("Task summary")}">${[
			["overdue", "Overdue tasks"],
			["today", "Due today"],
			["upcoming", "Next seven days"],
		]
			.map(
				([key, label]) =>
					`<button class="overview-stat ${key}" data-bucket="${key}" disabled><span>${e(
						label
					)}</span><strong data-count="${key}">—</strong><span class="overview-stat-hint">${e(
						key === "overdue"
							? "Needs attention"
							: key === "today"
							? "Keep today on track"
							: "Plan ahead"
					)}</span></button>`
			)
			.join("")}</div>
			<div class="overview-grid">
				<section class="overview-panel overview-tasks"><header><div><h3>${e("Team tasks")}</h3><p>${e(
			"Focus on the work that needs attention"
		)}</p></div><button class="overview-link" data-all-tasks>${e(
			"All tasks"
		)} →</button></header>
					<div class="overview-tabs" role="group" aria-label="${e("Task category")}">${[
			["urgent", "Urgent"],
			["overdue", "Overdue"],
			["today", "Today"],
		]
			.map(
				([key, label]) =>
					`<button data-bucket="${key}" aria-pressed="${key === this.bucket}">${e(
						label
					)}</button>`
			)
			.join("")}</div><div data-section="tasks" aria-live="polite"></div></section>
				<section class="overview-panel overview-agenda"><header><div><h3>${e(
					"Calendar"
				)}</h3><p data-agenda-range>${e(
			"Upcoming task deadlines"
		)}</p></div><button class="overview-link" data-calendar>${e(
			"Open calendar"
		)} →</button></header><div data-section="agenda" aria-live="polite"></div></section>
				<section class="overview-panel overview-mail"><header><div><h3>${e(
					"Latest incoming mail"
				)}</h3><p data-mail-accounts>${e(
			"Assigned mailboxes"
		)}</p></div><button class="overview-link" data-inbox disabled>${e(
			"Open mail"
		)} →</button></header><div data-section="mail" aria-live="polite"></div></section>
			</div>`);
		this.root.on("click", "[data-bucket]", (event) => {
			this.bucket = event.currentTarget.dataset.bucket;
			this.updateTabs();
			this.load("tasks");
		});
		this.root.on("click", "[data-more]", (event) =>
			this.load(event.currentTarget.dataset.more, true)
		);
		this.root.on("click", "[data-retry]", (event) =>
			this.load(event.currentTarget.dataset.retry)
		);
		this.root.on("click", "[data-calendar]", () =>
			frappe.set_route("List", "Task", "Calendar", "default")
		);
		this.root.on("click", "[data-inbox]", () => {
			const accounts = this.sections.mail.result?.accounts || [];
			if (accounts.length)
				frappe.set_route(
					"List",
					"Communication",
					"Inbox",
					accounts.length === 1 ? accounts[0].name : "All Accounts"
				);
		});
		this.root.on("click", "[data-all-tasks]", () => {
			frappe.route_options = {
				kanzlei_work_kind: ["!=", "Question"],
				is_group: 0,
				is_template: 0,
			};
			frappe.set_route("List", "Task");
		});
	}

	escape(value) {
		return frappe.utils.escape_html(String(value ?? ""));
	}
	date(value) {
		return frappe.datetime.str_to_user(value);
	}
	link(doctype, name, label) {
		return `<a href="${this.escape(frappe.utils.get_form_link(doctype, name))}">${this.escape(
			label
		)}</a>`;
	}
	updateTabs() {
		this.root
			.find("[data-bucket]")
			.each((_, node) =>
				node.setAttribute("aria-pressed", node.dataset.bucket === this.bucket)
			);
	}
	refresh() {
		["tasks", "agenda", "mail"].forEach((key) => this.load(key));
	}

	async load(key, append = false) {
		const section = this.sections[key];
		if (append && section.loading) return;
		const revision = ++section.revision;
		section.loading = true;
		const body = this.root.find(`[data-section="${key}"]`);
		if (!append) {
			section.rows = [];
			body.html(
				`<div class="overview-state" role="status">${this.escape(__("Loading…"))}</div>`
			);
		} else body.find("[data-more]").prop("disabled", true);
		if (key === "tasks") this.setCounts(null);
		if (key === "mail") this.root.find("[data-inbox]").prop("disabled", true);
		const args =
			key === "mail"
				? { page_length: 5 }
				: { start: append ? section.rows.length : 0, page_length: 8 };
		if (key === "tasks") args.bucket = this.bucket;
		try {
			const methods = {
				tasks: "get_task_overview",
				agenda: "get_deadline_agenda",
				mail: "get_incoming_mail",
			};
			const response = await frappe.call({
				method: `kanzlei_erp.overview.${methods[key]}`,
				args,
				silent: true,
			});
			if (revision !== section.revision) return;
			const result = response.message;
			section.result = result;
			section.rows = append ? section.rows.concat(result.rows || []) : result.rows || [];
			if (result.today) this.root.find(".overview-date").text(this.date(result.today));
			if (key === "tasks") this.setCounts(result.summary);
			if (key === "agenda" && result.range_start)
				this.root
					.find("[data-agenda-range]")
					.text(`${this.date(result.range_start)} – ${this.date(result.range_end)}`);
			if (key === "mail") {
				const accounts = result.accounts || [];
				this.root
					.find("[data-mail-accounts]")
					.text(accounts.length === 1 ? accounts[0].name : __("Assigned mailboxes"));
				this.root.find("[data-inbox]").prop("disabled", result.state !== "ready");
			}
			this.render(key);
		} catch (error) {
			if (revision !== section.revision) return;
			body.html(
				`<div class="overview-state overview-error"><p>${this.escape(
					__("Could not load this section.")
				)}</p><button class="btn btn-default btn-sm" data-retry="${key}">${this.escape(
					__("Retry")
				)}</button></div>`
			);
		} finally {
			if (revision === section.revision) section.loading = false;
		}
	}
	setCounts(summary) {
		this.root.find("[data-count]").each((_, node) => {
			node.textContent = summary ? summary[node.dataset.count] : "—";
		});
		this.root.find(".overview-stat").prop("disabled", !summary);
		this.updateTabs();
	}
	metadata(row) {
		const customer = row.customer
			? this.link("Customer", row.customer, row.customer_name)
			: this.escape(__(row.customer_restricted ? "Customer unavailable" : "No customer"));
		const assignees = row.assignees.length
			? row.assignees.map((user) => this.escape(user.full_name)).join("<br>")
			: this.escape(__("Unassigned"));
		return `<div class="overview-task-meta"><span>${customer}</span><span>${assignees}</span></div>`;
	}
	taskRow(row, agenda = false) {
		const overdue = row.exp_end_date < this.sections.tasks.result?.today;
		return `<article class="overview-task-row"><div class="overview-task-content">${this.link(
			"Task",
			row.name,
			row.subject || __("Untitled task")
		)}${this.metadata(
			row
		)}</div><div class="overview-task-status"><span class="overview-status">${this.escape(
			__(row.status, null, "Task status")
		)}</span>${
			!agenda
				? `<span class="${overdue ? "overview-overdue" : ""}">${this.escape(
						overdue ? __("Overdue") + " · " : ""
				  )}${this.escape(this.date(row.exp_end_date))}</span>`
				: ""
		}</div></article>`;
	}
	render(key) {
		const { rows, result } = this.sections[key];
		let html = "";
		if (result.state !== "ready")
			html = `<div class="overview-state">${this.escape(
				__(
					result.state === "no_account"
						? "No mailbox is assigned to you. Contact your administrator."
						: "You do not have access to this section."
				)
			)}</div>`;
		else if (!rows.length)
			html = `<div class="overview-state"><span class="overview-empty-icon">${frappe.utils.icon(
				key === "mail" ? "mail" : "check",
				"lg"
			)}</span><p>${this.escape(
				__(
					key === "mail"
						? "No open incoming mail."
						: key === "agenda"
						? "No deadlines in the next seven days."
						: "No tasks in this category."
				)
			)}</p></div>`;
		else if (key === "tasks") html = rows.map((row) => this.taskRow(row)).join("");
		else if (key === "agenda") {
			let date = null;
			for (const row of rows) {
				if (row.exp_end_date !== date) {
					date = row.exp_end_date;
					html += `<h4 class="overview-day">${this.escape(this.date(date))}</h4>`;
				}
				html += this.taskRow(row, true);
			}
		} else
			html = rows
				.map(
					(row) =>
						`<article class="overview-mail-row"><div class="overview-mail-heading"><span>${this.escape(
							row.sender_full_name || row.sender
						)}</span><time>${this.escape(
							this.date(row.communication_date)
						)}</time></div><div class="overview-mail-subject">${this.link(
							"Communication",
							row.name,
							row.subject || __("No subject")
						)}${
							row.has_attachment
								? `<span title="${this.escape(
										__("Has attachments")
								  )}" aria-label="${this.escape(
										__("Has attachments")
								  )}">${frappe.utils.icon("attachment", "sm")}</span>`
								: ""
						}</div><p>${this.escape(row.preview)}</p>${
							result.accounts.length > 1
								? `<small>${this.escape(row.email_account)}</small>`
								: ""
						}</article>`
				)
				.join("");
		if (result.has_more)
			html += `<div class="overview-more"><button class="btn btn-default btn-sm" data-more="${key}">${this.escape(
				__("Show more")
			)}</button><span>${rows.length} / ${result.total}</span></div>`;
		this.root.find(`[data-section="${key}"]`).html(html);
	}
}
