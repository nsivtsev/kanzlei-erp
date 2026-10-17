/* Personal portal state is shared with the Desk history tracker; no browser storage. */
(function (root) {
	"use strict";
	function matches(entry, query) {
		const text = [entry.label, entry.app, entry.context, entry.kind, entry.description].filter(Boolean).join(" ").toLocaleLowerCase();
		return text.includes(String(query || "").trim().toLocaleLowerCase());
	}
	function historyReference(route) {
		if (!Array.isArray(route) || route.length !== 3 || route[0] !== "Form" ||
			!["Customer", "Task", "FiBu Package", "FiBu Supplement"].includes(route[1]) ||
			typeof route[2] !== "string" || !route[2] || route[2].startsWith("new-")) return null;
		return {doctype: route[1], name: route[2]};
	}
	class PortalState {
		constructor(user, request, changed = () => {}) {
			this.user = user;
			this.request = request;
			this.changed = changed;
			this.data = {recent: [], pins: []};
			this.status = "loading";
			this.busy = false;
			this.opening = false;
			this.revision = 0;
		}
		sessionLost() {
			this.revision++;
			this.data = {recent: [], pins: []};
			this.status = "session_lost";
			this.busy = false;
			this.changed();
		}
		async load() {
			if (["session_lost", "signing_out"].includes(this.status)) return;
			const revision = ++this.revision;
			this.status = "loading";
			this.changed();
			try {
				const result = await this.request("get_personal");
				if (revision !== this.revision) return;
				if (result.user !== this.user) return this.sessionLost();
				this.data = {recent: result.recent, pins: result.pins};
				this.status = "ready";
			} catch (error) {
				if (revision !== this.revision) return;
				if ([401, 403].includes(error.status)) return this.sessionLost();
				this.status = "load_error";
			} finally {
				if (revision === this.revision) this.changed();
			}
		}
		async toggle(entry) {
			if (this.busy || this.opening || ["session_lost", "signing_out"].includes(this.status)) return;
			const revision = ++this.revision;
			this.busy = true;
			this.changed();
			const pinned = this.data.pins.some(row => row.doctype === entry.doctype && row.name === entry.name);
			try {
				const result = await this.request("set_pin", {doctype: entry.doctype, name: entry.name, pinned: pinned ? 0 : 1, expected_user: this.user});
				if (revision !== this.revision) return;
				if (result.user !== this.user) return this.sessionLost();
				await this.load();
			} catch (error) {
				if (revision !== this.revision) return;
				if ([401, 403].includes(error.status)) return this.sessionLost();
				this.status = "save_error";
			} finally {
				if (this.status !== "signing_out") this.busy = false;
				this.changed();
			}
		}
		async open(entry, navigate) {
			if (this.opening || this.busy || ["session_lost", "signing_out"].includes(this.status)) return;
			const revision = ++this.revision;
			this.opening = true;
			try {
				const result = await this.request("open_record", {doctype: entry.doctype, name: entry.name, expected_user: this.user});
				if (revision !== this.revision) return;
				if (result.user !== this.user) return this.sessionLost();
				if (!/^\/(desk|drive)\//.test(result.url)) throw new Error("Invalid destination");
				navigate(result.url);
			} catch (error) {
				if (revision !== this.revision) return;
				if ([401, 403].includes(error.status)) return this.sessionLost();
				this.status = "open_error";
			} finally {
				this.opening = false;
				this.changed();
			}
		}
		async logout(navigate) {
			if (this.status === "signing_out") return;
			this.status = "signing_out";
			this.busy = true;
			this.suspend();
			const revision = this.revision;
			try {
				await this.request("logout");
				if (revision === this.revision) navigate("/login");
			} catch (_) {
				if (revision !== this.revision) return;
				this.busy = false;
				this.status = "ready";
				await this.load();
				if (this.status !== "session_lost") this.status = "logout_error";
			} finally {
				this.changed();
			}
		}
		refresh() {
			if (this.busy || this.status === "signing_out") return;
			this.suspend();
			return this.load();
		}
		suspend() {
			this.revision++;
			this.data = {recent: [], pins: []};
			this.changed();
		}
	}
	const api = {matches, historyReference, PortalState};
	root.KanzleiPortal = api;
	if (typeof module !== "undefined" && module.exports) module.exports = api;
	if (typeof document === "undefined" || !document.getElementById("portal-config")) return;

	const config = JSON.parse(document.getElementById("portal-config").textContent);
	const t = text => config.labels[text] || text;
	const get = id => document.getElementById(id);
	const search = get("portal-search");
	const feedback = get("portal-status");
	const retry = get("portal-retry");
	const personal = get("portal-personal");
	const recent = get("portal-recent-list");
	const pins = get("portal-pins");
	const profiles = document.querySelector(".portal-profile");
	let pinFocus = null;

	async function request(method, args = {}) {
		const path = method === "logout" ? "/api/method/logout" : `/api/method/kanzlei_erp.portal.${method}`;
		const response = await fetch(path, {
			method: "POST", credentials: "same-origin", cache: "no-store",
			headers: {"Content-Type": "application/json", "X-Frappe-CSRF-Token": config.csrf_token},
			body: JSON.stringify({...args, _lang: document.documentElement.lang}),
		});
		if (!response.ok) {const error = new Error("Portal request failed"); error.status = response.status; throw error;}
		const data = await response.json();
		return data.message;
	}
	function element(tag, className, text) {
		const node = document.createElement(tag);
		if (className) node.className = className;
		if (text !== undefined) node.textContent = text;
		return node;
	}
	function icon(name) {
		const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
		svg.setAttribute("class", "portal-icon"); svg.setAttribute("aria-hidden", "true");
		const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
		use.setAttribute("href", `/assets/kanzlei_erp/images/portal-icons.svg#${name}`); svg.append(use);
		return svg;
	}
	function recordButton(entry, className) {
		const button = element("button", className, entry.label);
		button.type = "button";
		button.dataset.doctype = entry.doctype; button.dataset.name = entry.name;
		button.dataset.openRecord = "true";
		return button;
	}
	function pinButton(entry, remove = false) {
		const button = element("button", "portal-pin-button");
		const pressed = state.data.pins.some(row => row.doctype === entry.doctype && row.name === entry.name);
		button.type = "button"; button.disabled = state.busy;
		button.dataset.doctype = entry.doctype; button.dataset.name = entry.name;
		button.dataset.pin = "true";
		button.setAttribute("aria-label", `${t(pressed ? "Unpin link" : "Pin link")}: ${entry.label}`);
		button.setAttribute("aria-pressed", String(pressed));
		button.append(icon(remove ? "close" : "star"));
		return button;
	}
	function metadata(entry) {return [entry.app, entry.context || entry.kind].filter(Boolean).join(" · ");}
	function emptyMessage(container, title, detail) {
		const text = element("p", "portal-empty", t(title));
		if (detail) text.append(element("span", "", t(detail)));
		container.append(text);
	}
	function displayDate(value) {
		// Render the date recorded in the site's time zone, without shifting calendar days.
		const date = new Date(value.slice(0, 10) + "T12:00:00");
		if (!Number.isFinite(date.getTime())) return "";
		return new Intl.DateTimeFormat(document.documentElement.lang, {day:"numeric",month:"short"}).format(date);
	}
	function filter() {
		const query = search.value;
		const nodes = [...document.querySelectorAll("[data-search]")];
		nodes.forEach(node => {node.hidden = !matches({label: node.dataset.search}, query);});
		get("portal-no-results").hidden = !query.trim() || nodes.some(node => !node.hidden);
	}
	function render() {
		// Preserve keyboard focus when state changes rebuild the personal blocks.
		const focused = document.activeElement;
		if (focused?.dataset.pin) pinFocus = [focused.dataset.doctype, focused.dataset.name];
		recent.replaceChildren(); pins.replaceChildren();
		for (const entry of state.data.recent) {
			const row = element("div", "portal-recent-row"); row.dataset.search = [entry.label, metadata(entry)].join(" ");
			const mark = element("span", "portal-file-icon"); mark.append(icon(entry.doctype === "Customer" ? "building" : entry.kind === "Folder" || entry.url.startsWith("/drive/d/") ? "folder" : "file"));
			const content = element("div", "portal-row-content"); content.append(recordButton(entry, "portal-row-title"), element("p", "portal-row-meta", metadata(entry)));
			const time = element("time", "portal-time", displayDate(entry.visited)); time.dateTime = entry.visited.replace(" ", "T");
			row.append(mark, content, time, pinButton(entry)); recent.append(row);
		}
		for (const entry of state.data.pins) {
			const row = element("div", "portal-pinned-item"); row.dataset.search = [entry.label, metadata(entry)].join(" ");
			const button = recordButton(entry, "portal-shortcut"); button.replaceChildren(icon(entry.url.startsWith("/drive/d/") ? "folder" : "file"));
			const content = element("span", "portal-row-content"); content.append(element("span", "portal-shortcut-label", entry.label), element("span", "portal-row-meta", metadata(entry)));
			button.append(content); row.append(button, pinButton(entry, true)); pins.append(row);
		}
		if (!state.data.recent.length && state.status === "ready") emptyMessage(recent, "No recently opened items yet.", "Open a client or a document to continue here next time.");
		if (!state.data.pins.length && state.status === "ready") emptyMessage(pins, "No pinned links yet.", "Pin a recent item with the star.");
		if (state.status === "loading" && !state.data.recent.length) emptyMessage(recent, "Loading…");
		const messages = {load_error:"Could not load your recent items and pinned links.", save_error:"Could not save your pinned links.",open_error:"This link could not be opened. Refresh the portal and try again.",logout_error:"Could not sign out. Please try again.",session_lost:"Your session has changed. Reload the portal."};
		feedback.textContent = state.busy ? t("Saving…") : t(messages[state.status] || "");
		feedback.parentElement.classList.toggle("is-error", ["load_error", "save_error", "open_error", "session_lost"].includes(state.status));
		retry.hidden = !["load_error", "save_error", "open_error"].includes(state.status);
		get("portal-reload").hidden = state.status !== "session_lost";
		personal.setAttribute("aria-busy", String(state.status === "loading" || state.busy));
		get("portal-main").inert = state.status === "signing_out";
		document.querySelector(".portal-header").inert = state.status === "signing_out";
		filter();
		if (pinFocus && !state.busy) {
			[...document.querySelectorAll("[data-pin]")].find(node => node.dataset.doctype === pinFocus[0] && node.dataset.name === pinFocus[1] && !node.closest("[hidden]"))?.focus();
			pinFocus = null;
		}
	}
	const state = new PortalState(config.user, request, render);
	search.addEventListener("input", filter);
	retry.addEventListener("click", () => state.load());
	personal.addEventListener("click", event => {
		const pin = event.target.closest("[data-pin]");
		if (pin) return state.toggle({doctype: pin.dataset.doctype, name: pin.dataset.name});
		const open = event.target.closest("[data-open-record]");
		if (open) state.open({doctype: open.dataset.doctype, name: open.dataset.name}, url => window.location.assign(url));
	});
	document.querySelector("[data-logout]").addEventListener("click", () => state.logout(url => window.location.assign(url)));
	document.addEventListener("click", event => {if (!profiles.contains(event.target)) profiles.open = false;});
	document.addEventListener("keydown", event => {if (event.key === "Escape") profiles.open = false;});
	window.addEventListener("pagehide", () => state.suspend());
	window.addEventListener("pageshow", event => {if (event.persisted) window.location.reload();});
	document.addEventListener("visibilitychange", () => {if (document.visibilityState === "visible") state.refresh();});
	state.load();
})(globalThis);
