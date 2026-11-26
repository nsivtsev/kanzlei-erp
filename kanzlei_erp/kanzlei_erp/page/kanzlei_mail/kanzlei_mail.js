frappe.pages["kanzlei-mail"].on_page_load = function (wrapper) {
	wrapper.mail = new KanzleiMail(wrapper);
};
frappe.pages["kanzlei-mail"].on_page_show = function (wrapper) {
	wrapper.mail?.show();
};

const KM_METHOD = "kanzlei_erp.mail_client.";
// Mail labels such as "To" or "Forward" carry a context so they do not change other Desk screens.
const _m = (text, args) => __(text, args, "Email client");
const KM_FOLDERS = [
	["inbox", "Inbox", "inbox"],
	["unassigned", "Without Mandant", "unassigned"],
	["sent", "Sent", "send"],
	["spam", "Spam", "spam"],
	["trash", "Trash", "trash"],
];
const KM_ICONS = {
	inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
	unassigned: '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
	send: '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
	spam: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
	trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
	reply: '<path d="M9 17 4 12l5-5"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>',
	replyall: '<path d="m7 17-5-5 5-5"/><path d="m12 17-5-5 5-5"/><path d="M22 18v-2a4 4 0 0 0-4-4H7"/>',
	forward: '<path d="m15 17 5-5-5-5"/><path d="M4 18v-2a4 4 0 0 1 4-4h12"/>',
	unread: '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
	restore: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
	open: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
	clip: '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
	search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
	back: '<path d="m15 18-6-6 6-6"/>',
	file: '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><path d="M14 2v6h6"/>',
	image: '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.09-3.09a2 2 0 0 0-2.82 0L6 21"/>',
	link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
};

class KanzleiMail {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({ parent: wrapper, title: _m("E-Mail"), single_column: true });
		$(wrapper).addClass("kanzlei-mail-page");
		this.state = { folder: "inbox", account: "", search: "", unread: false, attachments: false };
		this.rows = [];
		this.selected = null;
		this.message = null;
		this.accounts = [];
		this.counts = {};
		this.revision = { list: 0, message: 0, boxes: 0 };
		this.remoteImages = new Set();
		this.page.set_primary_action(_m("New Email"), () => this.compose(), "add");
		this.page.set_secondary_action(_m("Refresh"), () => this.refresh(true), "refresh");
		this.build();
		this.bind();
		if (frappe.realtime?.doctype_subscribe) {
			frappe.realtime.doctype_subscribe("Communication");
			frappe.realtime.on("list_update", (data) => {
				if (data?.doctype === "Communication") this.scheduleRefresh();
			});
		}
	}

	e(value) {
		return frappe.utils.escape_html(String(value ?? ""));
	}
	t(value, args) {
		return this.e(_m(value, args));
	}
	icon(name, cls = "") {
		return `<svg class="km-icon ${cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${KM_ICONS[name]}</svg>`;
	}

	build() {
		const folders = KM_FOLDERS.map(
			([key, label, icon]) => `<li><button type="button" class="km-folder" data-folder="${key}">
				${this.icon(icon)}<span class="km-folder-label">${this.t(label)}</span><span class="km-count" data-count="${key}"></span></button></li>`
		).join("");
		this.root = $(`<div class="kanzlei-mail" data-view="list">
			<nav class="km-rail" aria-label="${this.t("Mail folders")}">
				<ul class="km-folders">${folders}</ul>
				<div class="km-accounts" hidden></div>
			</nav>
			<section class="km-list" aria-label="${this.t("Messages")}">
				<header class="km-list-head">
					<div class="km-list-title"><h2 data-folder-title></h2><span data-folder-meta></span></div>
					<label class="km-search">${this.icon("search")}<span class="sr-only">${this.t("Search mail")}</span>
						<input type="search" data-search autocomplete="off" placeholder="${this.t("Search sender, subject or recipient")}"></label>
					<div class="km-filters" role="group" aria-label="${this.t("Filter messages")}">
						<button type="button" data-filter="unread" aria-pressed="false">${this.t("Unread")}</button>
						<button type="button" data-filter="attachments" aria-pressed="false">${this.icon("clip")}${this.t("With attachments")}</button>
					</div>
				</header>
				<div class="km-rows" data-rows role="listbox" tabindex="0" aria-label="${this.t("Messages")}"></div>
			</section>
			<article class="km-reader" data-reader aria-live="polite"></article>
		</div>`).appendTo(this.page.main);
		this.renderEmptyReader();
		this.updateFolderUI();
	}

	bind() {
		const root = this.root;
		root.on("click", "[data-folder]", (ev) => this.setFolder(ev.currentTarget.dataset.folder));
		root.on("click", "[data-account]", (ev) => {
			this.state.account = ev.currentTarget.dataset.account;
			this.renderAccounts();
			this.loadList();
		});
		root.on("click", "[data-filter]", (ev) => {
			const key = ev.currentTarget.dataset.filter;
			this.state[key] = !this.state[key];
			ev.currentTarget.setAttribute("aria-pressed", this.state[key]);
			this.loadList();
		});
		let timer;
		root.on("input", "[data-search]", (ev) => {
			clearTimeout(timer);
			timer = setTimeout(() => {
				this.state.search = ev.target.value.trim();
				this.loadList();
			}, 250);
		});
		root.on("click", ".km-row", (ev) => this.open(ev.currentTarget.dataset.name));
		root.on("click", "[data-more]", () => this.loadList(true));
		root.on("click", "[data-retry-list]", () => this.loadList());
		root.on("click", "[data-retry-message]", () => this.open(this.selected, true));
		root.on("click", "[data-action]", (ev) => this.action(ev.currentTarget.dataset.action, ev.currentTarget));
		root.on("click", "[data-thread]", (ev) => this.open(ev.currentTarget.dataset.thread));
		$(window).on("resize.kanzlei-mail", () => this.fit());
		$(document).on("keydown.kanzlei-mail", (ev) => this.keydown(ev));
	}

	show() {
		this.fit();
		const name = frappe.get_route()[1];
		if (!this.loaded) {
			this.loaded = true;
			this.refresh().then(() => name && this.open(decodeURIComponent(name)));
		} else if (name && name !== this.selected) this.open(decodeURIComponent(name));
		else this.refresh();
	}

	fit() {
		if (!this.root?.[0]?.isConnected) return;
		const top = Math.max(0, this.root[0].getBoundingClientRect().top + window.scrollY);
		this.root[0].style.setProperty("--km-top", `${Math.round(top)}px`);
	}

	scheduleRefresh() {
		clearTimeout(this.refreshTimer);
		this.refreshTimer = setTimeout(() => this.refresh(), 1200);
	}

	refresh(notify = false) {
		return Promise.all([this.loadBoxes(), this.loadList()]).then(() => {
			if (notify) frappe.show_alert({ message: _m("Mail updated"), indicator: "green" }, 2);
		});
	}

	async call(method, args = {}, type) {
		const response = await frappe.call({ method: KM_METHOD + method, args, type });
		return response.message;
	}

	async loadBoxes() {
		const revision = ++this.revision.boxes;
		try {
			const result = await this.call("get_mailboxes");
			if (revision !== this.revision.boxes) return;
			this.boxState = result.state;
			this.accounts = result.accounts || [];
			this.counts = result.counts || {};
			if (this.state.account && !this.accounts.some((a) => a.name === this.state.account))
				this.state.account = "";
			this.renderAccounts();
			this.updateFolderUI();
		} catch (error) {
			if (revision === this.revision.boxes) this.counts = {};
		}
	}

	setFolder(folder) {
		if (!KM_FOLDERS.some(([key]) => key === folder)) return;
		this.state.folder = folder;
		this.updateFolderUI();
		this.loadList();
		this.root.attr("data-view", "list");
	}

	updateFolderUI() {
		const current = KM_FOLDERS.find(([key]) => key === this.state.folder);
		this.root.find("[data-folder]").each((_, node) => {
			const active = node.dataset.folder === this.state.folder;
			node.classList.toggle("is-active", active);
			if (active) node.setAttribute("aria-current", "page");
			else node.removeAttribute("aria-current");
		});
		for (const [key] of KM_FOLDERS) {
			const value = this.counts[key];
			this.root.find(`[data-count="${key}"]`).text(value ? (value > 999 ? "999+" : value) : "");
		}
		this.root.find("[data-folder-title]").text(_m(current[1]));
		const meta = {
			inbox: this.counts.inbox ? _m("{0} unread", [this.counts.inbox]) : _m("Everything read"),
			unassigned: _m("Incoming mail not yet linked to a Mandant"),
			sent: _m("Sent from your mailboxes"),
			spam: _m("Marked as spam"),
			trash: _m("Moved to trash"),
		}[this.state.folder];
		this.root.find("[data-folder-meta]").text(meta);
	}

	renderAccounts() {
		const box = this.root.find(".km-accounts");
		if (this.accounts.length < 2) return box.prop("hidden", true).empty();
		const item = (name, label, sub) => `<button type="button" class="km-account" data-account="${this.e(name)}"
			aria-pressed="${this.state.account === name}"><span>${this.e(label)}</span>${sub ? `<small>${this.e(sub)}</small>` : ""}</button>`;
		box.prop("hidden", false).html(
			`<h3>${this.t("Mailboxes")}</h3>` +
				item("", _m("All mailboxes")) +
				this.accounts.map((a) => item(a.name, a.name, a.email_id !== a.name ? a.email_id : "")).join("")
		);
	}

	async loadList(append = false) {
		if (append && this.listLoading) return;
		const revision = ++this.revision.list;
		const body = this.root.find("[data-rows]");
		this.listLoading = true;
		if (!append) {
			this.next = 0;
			if (!this.rows.length) body.html(this.skeleton());
			body.attr("aria-busy", "true");
		}
		try {
			const result = await this.call("get_messages", {
				folder: this.state.folder,
				account: this.state.account || undefined,
				search: this.state.search || undefined,
				unread: this.state.unread ? 1 : 0,
				attachments: this.state.attachments ? 1 : 0,
				start: append ? this.next : 0,
			});
			if (revision !== this.revision.list) return;
			this.listState = result.state;
			this.rows = append ? this.rows.concat(result.rows) : result.rows;
			this.hasMore = result.has_more;
			this.next = result.next_start;
			this.renderList();
		} catch (error) {
			if (revision !== this.revision.list) return;
			body.html(
				`<div class="km-empty is-error"><p>${this.t("Messages could not be loaded.")}</p>
				<button type="button" class="btn btn-default btn-sm" data-retry-list>${this.t("Try again")}</button></div>`
			);
		} finally {
			if (revision === this.revision.list) {
				this.listLoading = false;
				body.attr("aria-busy", "false");
			}
		}
	}

	skeleton() {
		return `<div class="km-skeleton" aria-hidden="true">${'<div class="km-skeleton-row"><i></i><b></b><b></b></div>'.repeat(7)}</div>`;
	}

	renderList() {
		const body = this.root.find("[data-rows]");
		if (this.listState === "no_account" || this.listState === "no_access") {
			body.html(`<div class="km-empty"><h3>${this.t(
				this.listState === "no_account" ? "No mailbox is assigned to you" : "You cannot read mail"
			)}</h3><p>${this.t("Ask your administrator to assign the shared mailbox in User Email.")}</p></div>`);
			return;
		}
		if (!this.rows.length) {
			const filtered = this.state.search || this.state.unread || this.state.attachments;
			const text = filtered
				? "No messages match these filters."
				: {
						inbox: "Your inbox is empty.",
						unassigned: "Every incoming message is linked to a Mandant.",
						sent: "No sent messages yet.",
						spam: "No spam.",
						trash: "Trash is empty.",
				  }[this.state.folder];
			body.html(`<div class="km-empty"><p>${this.t(text)}</p></div>`);
			return;
		}
		let group = null;
		const parts = [];
		for (const row of this.rows) {
			const label = this.groupLabel(row.communication_date);
			if (label !== group) {
				group = label;
				parts.push(`<div class="km-group" role="presentation">${this.e(label)}</div>`);
			}
			parts.push(this.rowHtml(row));
		}
		if (this.hasMore)
			parts.push(`<div class="km-more"><button type="button" class="btn btn-default btn-sm" data-more>${this.t("Load older messages")}</button></div>`);
		body.html(parts.join(""));
	}

	rowHtml(row) {
		const sent = row.sent_or_received === "Sent";
		const who = sent
			? _m("To: {0}", [this.firstAddress(row.recipients)])
			: row.sender_full_name || row.sender || _m("Unknown sender");
		const unread = !sent && !row.seen;
		const selected = row.name === this.selected;
		const mandant = row.mandants?.[0];
		const extra = (row.mandants?.length || 0) - 1;
		return `<div class="km-row${unread ? " is-unread" : ""}${selected ? " is-selected" : ""}" role="option"
			aria-selected="${selected}" data-name="${this.e(row.name)}" tabindex="-1">
			${this.avatar(sent ? this.firstAddress(row.recipients) : row.sender_full_name || row.sender, sent ? "" : row.sender)}
			<div class="km-row-main">
				<div class="km-row-top"><span class="km-from">${this.e(who)}</span>
					${row.has_attachment ? `<span class="km-clip" title="${this.t("Has attachments")}">${this.icon("clip")}</span>` : ""}<time datetime="${this.e(row.communication_date)}">${this.e(this.shortDate(row.communication_date))}</time></div>
				<div class="km-row-subject">${unread ? `<span class="km-dot" title="${this.t("Unread")}"></span>` : ""}${this.e(row.subject || _m("(No subject)"))}</div>
				<div class="km-row-preview">${this.e(row.preview)}</div>
				${mandant ? `<div class="km-row-tags"><span class="km-akte" title="${this.t("Mandant")}">${this.e(mandant.customer_name)}</span>${extra > 0 ? `<span class="km-akte-more">+${extra}</span>` : ""}</div>` : ""}
			</div>
		</div>`;
	}

	avatar(label, address = "") {
		const text = String(label || address || "?").replace(/["<>]/g, "").trim();
		const words = text.split(/[\s@._-]+/).filter(Boolean);
		const initials = ((words[0]?.[0] || "?") + (words.length > 1 ? words[1][0] : words[0]?.[1] || "")).toUpperCase();
		let hash = 0;
		for (const ch of (address || text).toLowerCase()) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
		return `<span class="km-avatar" style="--km-hue:${hash % 360}" aria-hidden="true">${this.e(initials)}</span>`;
	}

	firstAddress(value) {
		const first = String(value || "").split(",")[0].trim();
		const match = first.match(/^\s*"?([^"<]*)"?\s*<([^>]+)>/);
		return (match ? match[1].trim() || match[2] : first) || _m("Unknown recipient");
	}

	date(value) {
		if (!value) return null;
		const date = new Date(String(value).replace(" ", "T"));
		return isNaN(date) ? null : date;
	}
	locale() {
		return frappe.boot?.lang || "de";
	}
	shortDate(value) {
		const date = this.date(value);
		if (!date) return "";
		const now = new Date();
		const sameDay = date.toDateString() === now.toDateString();
		const opts = sameDay
			? { hour: "2-digit", minute: "2-digit" }
			: date.getFullYear() === now.getFullYear()
			? { day: "numeric", month: "short" }
			: { day: "2-digit", month: "2-digit", year: "numeric" };
		return new Intl.DateTimeFormat(this.locale(), opts).format(date);
	}
	longDate(value) {
		const date = this.date(value);
		return date
			? new Intl.DateTimeFormat(this.locale(), { weekday: "short", day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" }).format(date)
			: "";
	}
	groupLabel(value) {
		const date = this.date(value);
		if (!date) return _m("Undated");
		const start = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
		const days = Math.round((start(new Date()) - start(date)) / 86400000);
		if (days <= 0) return _m("Today");
		if (days === 1) return _m("Yesterday");
		if (days < 7) return _m("Earlier this week");
		const now = new Date();
		if (date.getMonth() === now.getMonth() && date.getFullYear() === now.getFullYear())
			return _m("Earlier this month");
		const label = new Intl.DateTimeFormat(this.locale(), { month: "long", year: "numeric" }).format(date);
		return label.charAt(0).toUpperCase() + label.slice(1);
	}

	selectRow(name) {
		this.selected = name;
		this.root.find(".km-row").each((_, node) => {
			const active = node.dataset.name === name;
			node.classList.toggle("is-selected", active);
			node.setAttribute("aria-selected", active);
			if (active) node.scrollIntoView({ block: "nearest" });
		});
	}

	async open(name, force = false) {
		if (!name) return;
		if (name === this.selected && this.message?.name === name && !force) {
			this.root.attr("data-view", "reader");
			return;
		}
		this.selectRow(name);
		this.root.attr("data-view", "reader");
		const revision = ++this.revision.message;
		const reader = this.root.find("[data-reader]");
		reader.attr("aria-busy", "true").html(`<div class="km-reader-loading">${this.t("Loading message…")}</div>`);
		try {
			const message = await this.call("get_message", { name });
			if (revision !== this.revision.message) return;
			this.message = message;
			this.renderMessage();
			try {
				window.history.replaceState(window.history.state, "", `/desk/kanzlei-mail/${encodeURIComponent(name)}`);
			} catch (e) {
				/* the address bar is only a convenience */
			}
			if (message.sent_or_received === "Received" && !message.seen) this.markSeen(true, true);
		} catch (error) {
			if (revision !== this.revision.message) return;
			this.message = null;
			reader.html(`<div class="km-empty is-error"><p>${this.t("This message could not be opened.")}</p>
				<button type="button" class="btn btn-default btn-sm" data-retry-message>${this.t("Try again")}</button></div>`);
		} finally {
			if (revision === this.revision.message) reader.attr("aria-busy", "false");
		}
	}

	renderEmptyReader() {
		this.root.find("[data-reader]").html(`<div class="km-reader-empty">
			<div class="km-reader-empty-sheet" aria-hidden="true">${this.icon("unread")}</div>
			<p>${this.t("Select a message to read it here.")}</p>
			<dl class="km-keys"><div><dt><kbd>J</kbd> <kbd>K</kbd></dt><dd>${this.t("Next or previous message")}</dd></div>
			<div><dt><kbd>R</kbd></dt><dd>${this.t("Reply")}</dd></div><div><dt><kbd>/</kbd></dt><dd>${this.t("Search")}</dd></div></dl>
		</div>`);
	}

	button(action, icon, label, opts = {}) {
		return `<button type="button" class="km-tool${opts.primary ? " is-primary" : ""}" data-action="${action}"
			title="${this.t(label)}${opts.key ? ` (${opts.key})` : ""}" aria-label="${this.t(label)}">${this.icon(icon)}${opts.text ? `<span>${this.t(label)}</span>` : ""}</button>`;
	}

	renderMessage() {
		const m = this.message;
		const sent = m.sent_or_received === "Sent";
		const status = m.email_status;
		const tools = [
			`<button type="button" class="km-tool km-back" data-action="back" aria-label="${this.t("Back to messages")}">${this.icon("back")}<span>${this.t("Messages")}</span></button>`,
			this.button("reply", "reply", "Reply", { text: true, primary: true, key: "R" }),
			this.button("reply_all", "replyall", "Reply all", { text: true, key: "A" }),
			this.button("forward", "forward", "Forward", { text: true, key: "F" }),
			'<span class="km-tool-gap"></span>',
		];
		if (!sent && status === "Open") tools.push(this.button("unread", "unread", "Mark as unread", { key: "U" }));
		if (m.can_write) {
			if (status === "Open") {
				if (!sent) tools.push(this.button("spam", "spam", "Mark as spam"));
				tools.push(this.button("trash", "trash", "Move to trash", { key: "#" }));
			} else tools.push(this.button("restore", "restore", status === "Spam" ? "Not spam" : "Restore", { text: true }));
		}
		tools.push(this.button("form", "open", "Open record"));

		const people = [
			["To", m.recipients],
			["Cc", m.cc],
			["Bcc", m.bcc],
		].filter(([, value]) => value);
		const attachments = m.attachments || [];
		const thread = m.thread || [];
		const remote = this.hasRemoteImages(m.content);
		const showImages = this.remoteImages.has(m.name);

		this.root.find("[data-reader]").html(`
			<div class="km-toolbar" role="toolbar" aria-label="${this.t("Message actions")}">${tools.join("")}</div>
			<div class="km-reader-scroll">
				${this.akteHtml(m)}
				<header class="km-message-head">
					<h1 class="km-subject">${this.e(m.subject || _m("(No subject)"))}</h1>
					${status !== "Open" ? `<p class="km-status-note">${this.t(status === "Spam" ? "This message is in spam." : "This message is in the trash.")}</p>` : ""}
					<div class="km-sender">
						${this.avatar(sent ? this.firstAddress(m.recipients) : m.sender_full_name || m.sender, sent ? "" : m.sender)}
						<div class="km-people">
							<p class="km-from-line"><strong>${this.e(m.sender_full_name || m.sender)}</strong>${m.sender_full_name ? ` <span>${this.e(m.sender)}</span>` : ""}</p>
							<dl>${people
								.map(([label, value]) => `<div><dt>${this.t(label)}</dt><dd>${this.e(value)}</dd></div>`)
								.join("")}</dl>
						</div>
						<time class="km-when" datetime="${this.e(m.communication_date)}">${this.e(this.longDate(m.communication_date))}</time>
					</div>
				</header>
				${remote && !showImages ? `<div class="km-images-note"><span>${this.t("External images are blocked to protect your privacy.")}</span>
					<button type="button" class="km-link-button" data-action="images">${this.t("Show images")}</button></div>` : ""}
				<div class="km-sheet"><iframe class="km-body" title="${this.t("Message content")}"
					sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe></div>
				${attachments.length ? `<section class="km-attachments" aria-label="${this.t("Attachments")}">
					<h2>${this.e(attachments.length === 1 ? _m("1 attachment") : _m("{0} attachments", [attachments.length]))}</h2>
					<ul>${attachments.map((file) => this.attachmentHtml(file)).join("")}</ul></section>` : ""}
				${thread.length > 1 ? `<section class="km-thread" aria-label="${this.t("Conversation")}">
					<h2>${this.e(_m("Conversation, {0} messages", [thread.length]))}</h2>
					<ol>${thread.map((item) => this.threadHtml(item)).join("")}</ol></section>` : ""}
				<button type="button" class="km-quick-reply" data-action="reply">${this.icon("reply")}<span>${this.e(
					_m("Reply to {0}", [sent ? this.firstAddress(m.recipients) : m.sender_full_name || m.sender])
				)}</span></button>
			</div>`);
		this.writeBody(m, showImages);
		this.root.find(".km-reader-scroll").scrollTop(0);
	}

	akteHtml(m) {
		const mandants = m.mandants || [];
		const links = (m.links || [])
			.map((link) => `<a class="km-akte-link" href="${this.e(frappe.utils.get_form_link(link.doctype, link.name))}">${this.icon("link")}${this.e(link.label)} ${this.e(link.name)}</a>`)
			.join("");
		if (!mandants.length)
			return `<div class="km-akte-strip is-open">
				<span class="km-akte-label">${this.t("Not linked to a Mandant yet")}</span>${links}
				${m.can_write ? `<button type="button" class="km-link-button" data-action="assign">${this.t("Link to Mandant")}</button>` : ""}
			</div>`;
		return `<div class="km-akte-strip">
			${mandants
				.map((row) => `<a class="km-akte-tab" href="${this.e(frappe.utils.get_form_link("Customer", row.name))}" title="${this.t("Open Mandant")}">${this.e(row.customer_name)}</a>`)
				.join("")}
			${links}
			${m.can_write ? `<button type="button" class="km-link-button" data-action="assign">${this.t("Add Mandant")}</button>` : ""}
		</div>`;
	}

	attachmentHtml(file) {
		const name = file.file_name || file.file_url?.split("/").pop() || _m("File");
		const ext = (name.split(".").pop() || "").toLowerCase();
		const kind = ["png", "jpg", "jpeg", "gif", "webp", "heic"].includes(ext) ? "image" : "file";
		return `<li><a class="km-file" href="${this.e(file.file_url)}" target="_blank" rel="noopener">
			<span class="km-file-type" data-ext="${this.e(ext.slice(0, 4))}">${this.icon(kind)}<em>${this.e(ext.slice(0, 4))}</em></span>
			<span class="km-file-name">${this.e(name)}</span>
			<small>${this.e(file.file_size ? this.size(file.file_size) : "")}</small></a></li>`;
	}

	threadHtml(item) {
		return `<li><button type="button" class="km-thread-item${item.current ? " is-current" : ""}" data-thread="${this.e(item.name)}"
			${item.current ? 'aria-current="true"' : ""}>
			<span class="km-thread-who">${this.e(item.sent_or_received === "Sent" ? _m("You") : item.sender_full_name || item.sender)}</span>
			<time>${this.e(this.shortDate(item.communication_date))}</time>
			<span class="km-thread-preview">${this.e(item.preview)}</span></button></li>`;
	}

	size(bytes) {
		const units = ["B", "KB", "MB", "GB"];
		let value = Number(bytes),
			unit = 0;
		while (value >= 1024 && unit < units.length - 1) {
			value /= 1024;
			unit++;
		}
		return `${new Intl.NumberFormat(this.locale(), { maximumFractionDigits: unit ? 1 : 0 }).format(value)} ${units[unit]}`;
	}

	hasRemoteImages(html) {
		return /<img[^>]+src\s*=\s*["']?\s*(https?:)?\/\//i.test(html || "") || /url\(\s*["']?\s*(https?:)?\/\//i.test(html || "");
	}

	bodyDocument(content, showImages) {
		const images = showImages ? "'self' data: https: http:" : "'self' data:";
		const looksHtml = /<\/?(p|div|br|table|span|a|img|html|body|h\d|ul|ol|li|blockquote)\b/i.test(content);
		const body = looksHtml ? content : `<pre class="km-plain">${this.e(content)}</pre>`;
		return `<!doctype html><html><head><meta charset="utf-8">
			<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src ${images}">
			<meta name="referrer" content="no-referrer"><base target="_blank">
			<style>
				html{color-scheme:light}
				body{margin:0;padding:28px 32px 32px;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;color:#1d2624;background:#fff;overflow-wrap:anywhere}
				img{max-width:100%;height:auto}table{max-width:100%}body>:first-child{margin-top:0}body>:last-child{margin-bottom:0}
				a{color:#1d6b56}
				blockquote{margin:12px 0;padding:0 0 0 14px;border-left:3px solid #d5dbd8;color:#55605c}
				pre.km-plain{white-space:pre-wrap;font:inherit;margin:0}
				@media (max-width:600px){body{padding:20px 18px}}
			</style></head><body>${body}</body></html>`;
	}

	writeBody(message, showImages) {
		const frame = this.root.find(".km-body")[0];
		if (!frame) return;
		const resize = () => {
			try {
				const doc = frame.contentDocument;
				if (doc?.documentElement) frame.style.height = `${Math.max(120, doc.documentElement.scrollHeight)}px`;
			} catch (e) {
				frame.style.height = "60vh";
			}
		};
		frame.addEventListener("load", () => {
			resize();
			try {
				const doc = frame.contentDocument;
				doc.querySelectorAll("img").forEach((img) => img.addEventListener("load", resize));
				if (window.ResizeObserver) new ResizeObserver(resize).observe(doc.body);
			} catch (e) {
				/* cross-origin fallback keeps a fixed height */
			}
		});
		frame.srcdoc = this.bodyDocument(message.content || "", showImages);
	}

	async action(name, node) {
		const m = this.message;
		if (name === "back") {
			this.root.attr("data-view", "list");
			return;
		}
		if (!m) return;
		if (name === "reply" || name === "reply_all" || name === "forward") return this.compose(name);
		if (name === "images") {
			this.remoteImages.add(m.name);
			return this.renderMessage();
		}
		if (name === "form") return frappe.set_route("Form", "Communication", m.name);
		if (name === "assign") return this.assignMandant();
		if (name === "unread") {
			await this.markSeen(false);
			this.message = null;
			this.selected = null;
			this.root.attr("data-view", "list");
			return this.renderEmptyReader();
		}
		if (["spam", "trash", "restore"].includes(name)) return this.move(name, node);
	}

	async markSeen(seen, silent = false) {
		const name = this.message?.name;
		if (!name) return;
		try {
			await this.call("set_seen", { names: JSON.stringify([name]), seen: seen ? 1 : 0 }, "POST");
			if (this.message?.name === name) this.message.seen = seen;
			const row = this.rows.find((r) => r.name === name);
			if (row) {
				row.seen = seen;
				const node = this.root.find(`.km-row[data-name="${CSS.escape(name)}"]`);
				node.toggleClass("is-unread", !seen);
				node.find(".km-dot").remove();
				if (!seen) node.find(".km-row-subject").prepend(`<span class="km-dot" title="${this.t("Unread")}"></span>`);
			}
			if (this.message?.email_status !== "Spam" || !seen) this.loadBoxes();
		} catch (error) {
			if (!silent) frappe.show_alert({ message: _m("The read status could not be changed."), indicator: "red" });
		}
	}

	async move(target, node) {
		const m = this.message;
		node.disabled = true;
		try {
			await this.call("move_to", { name: m.name, folder: target }, "POST");
			const index = this.rows.findIndex((r) => r.name === m.name);
			const next = this.rows[index + 1] || this.rows[index - 1];
			this.rows = this.rows.filter((r) => r.name !== m.name);
			this.renderList();
			this.loadBoxes();
			frappe.show_alert({
				message: { spam: _m("Moved to spam"), trash: _m("Moved to trash"), restore: _m("Message restored") }[target],
				indicator: "green",
			}, 3);
			this.message = null;
			this.selected = null;
			if (next && window.matchMedia("(min-width: 768px)").matches) this.open(next.name);
			else {
				this.renderEmptyReader();
				this.root.attr("data-view", "list");
			}
		} catch (error) {
			node.disabled = false;
		}
	}

	assignMandant() {
		const m = this.message;
		const dialog = new frappe.ui.Dialog({
			title: _m("Link to Mandant"),
			fields: [
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: _m("Mandant"), reqd: 1 },
				{ fieldtype: "HTML", options: `<p class="text-muted small">${this.t("The message then appears in the Mandant's documents and timeline.")}</p>` },
			],
			primary_action_label: _m("Link"),
			primary_action: async ({ customer }) => {
				dialog.get_primary_btn().prop("disabled", true);
				try {
					const result = await this.call("assign_mandant", { name: m.name, customer }, "POST");
					dialog.hide();
					if (this.message?.name !== m.name) return;
					this.message.mandants = result.mandants;
					const row = this.rows.find((r) => r.name === m.name);
					if (row) row.mandants = result.mandants;
					if (this.state.folder === "unassigned") this.rows = this.rows.filter((r) => r.name !== m.name);
					this.renderList();
					this.selectRow(m.name);
					this.root.find(".km-akte-strip").replaceWith(this.akteHtml(this.message));
					this.loadBoxes();
					frappe.show_alert({ message: _m("Linked to {0}", [result.mandants.at(-1)?.customer_name || customer]), indicator: "green" }, 3);
				} catch (error) {
					dialog.get_primary_btn().prop("disabled", false);
				}
			},
		});
		dialog.show();
	}

	defaultSender() {
		const account = this.accounts.find((a) => a.name === this.state.account && a.can_send) || this.accounts.find((a) => a.can_send) || this.accounts[0];
		return account?.email_id;
	}

	compose(mode) {
		const m = this.message;
		let composer;
		if (!mode || !m) {
			composer = new frappe.views.CommunicationComposer({ doc: {}, sender: this.defaultSender(), title: _m("New Email") });
		} else {
			const doc = {
				doctype: "Communication",
				name: m.name,
				subject: m.subject,
				sender: m.sender,
				recipients: m.recipients,
				cc: m.cc,
				bcc: m.bcc,
				content: m.content,
				communication_date: m.communication_date,
				sent_or_received: m.sent_or_received,
				email_account: m.email_account,
			};
			const subject = m.subject || "";
			const prefixed = (prefix, pattern) => (pattern.test(subject) ? subject : `${prefix}: ${subject}`);
			const args = { doc, last_email: doc, sender: m.account_email_id, is_a_reply: true };
			if (mode === "forward")
				Object.assign(args, { forward: true, subject: prefixed("Fw", /^\s*(fw|fwd|wg)\s*:/i), attachments: m.attachments });
			else
				Object.assign(args, {
					subject: prefixed("Re", /^\s*(re|aw|antw)\s*:/i),
					recipients: m.sent_or_received === "Sent" ? m.recipients : m.sender,
					cc: mode === "reply_all" ? m.cc : undefined,
				});
			composer = new frappe.views.CommunicationComposer(args);
		}
		composer?.dialog?.$wrapper?.one("hidden.bs.modal", () => this.scheduleRefresh());
	}

	visibleRows() {
		return this.root.find(".km-row").toArray();
	}

	keydown(ev) {
		if (!this.root[0].isConnected || !$(this.wrapper).is(":visible")) return;
		if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
		if ($(ev.target).is("input, textarea, select, [contenteditable], .ql-editor") || $(".modal.show").length) {
			if (ev.key === "Escape" && $(ev.target).is("[data-search]")) ev.target.blur();
			return;
		}
		const key = ev.key;
		const step = key === "j" || key === "ArrowDown" ? 1 : key === "k" || key === "ArrowUp" ? -1 : 0;
		if (step) {
			const rows = this.visibleRows();
			if (!rows.length) return;
			const index = rows.findIndex((row) => row.dataset.name === this.selected);
			const next = rows[Math.min(rows.length - 1, Math.max(0, index + step))] || rows[0];
			ev.preventDefault();
			this.open(next.dataset.name);
		} else if (key === "/") {
			ev.preventDefault();
			this.root.find("[data-search]").trigger("focus");
		} else if (key === "Escape" && this.root.attr("data-view") === "reader") {
			this.root.attr("data-view", "list");
		} else if (this.message) {
			const action = { r: "reply", a: "reply_all", f: "forward", "#": "trash", Delete: "trash" }[key];
			if (key === "u") {
				ev.preventDefault();
				this.markSeen(!this.message.seen);
			} else if (action) {
				const node = this.root.find(`[data-action="${action}"]`)[0];
				if (node) {
					ev.preventDefault();
					node.click();
				}
			}
		}
	}
}
