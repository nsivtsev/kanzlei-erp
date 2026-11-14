const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function load(clearTimer = clearTimeout) {
	const hooks = {};
	const frappe = {
		ui: { form: { on(name, events) { hooks[name] = events; } } },
		user_roles: ["System Manager"],
		utils: { escape_html: (s) => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;") },
	};
	vm.runInNewContext(fs.readFileSync("kanzlei_erp/public/js/email_account.js", "utf8"), {
		frappe, __: (text, values = []) => text.replace(/\{(\d+)\}/g, (_, i) => values[i]), setTimeout, clearTimeout: clearTimer,
	});
	return { ...frappe.email_archive, frappe, hooks };
}

test("native browser timer functions are called without a client receiver", async () => {
	const { ImportClient } = load(function () { "use strict"; assert.equal(this, undefined); });
	let loaded = false;
	const client = new ImportClient("account-1", async () => null, { onstate() { loaded = true; } });
	await client.open();
	assert.equal(loaded, true);
	client.close();
});

test("progress is scoped to its account, resumes an active run and stops polling at completion", async () => {
	const { ImportClient } = load();
	const calls = [], timers = [], states = [];
	const answers = [{ name: "run-1", status: "Running" }, { name: "run-1", status: "Completed", processed: 5 }];
	const client = new ImportClient("account-1", async (method, args) => {
		calls.push({ method, args }); return answers.shift();
	}, { onstate: (state) => states.push(state), schedule: (fn) => { timers.push(fn); return timers.length; }, cancel() {} });
	await client.open();
	assert.equal(calls[0].args.email_account, "account-1");
	assert.equal(timers.length, 1);
	await timers.shift()();
	assert.equal(states.at(-1).status, "Completed");
	assert.equal(timers.length, 0);
});

test("closing the dialog ignores in-flight progress and cancels pending timers", async () => {
	const { ImportClient } = load();
	let resolve;
	const states = [];
	const client = new ImportClient("account-1", () => new Promise((done) => { resolve = done; }), { onstate: (state) => states.push(state) });
	const request = client.open();
	client.close();
	resolve({ status: "Running" });
	await request;
	assert.equal(states.length, 0);
});

test("start prevents duplicate clicks and invalidates older state requests", async () => {
	const { ImportClient } = load();
	let resolveOld, resolveStart;
	const states = [], calls = [];
	const client = new ImportClient("account-1", (method, args) => {
		calls.push({ method, args });
		return new Promise((done) => { if (method === "start_import") resolveStart = done; else resolveOld = done; });
	}, { onstate: (state) => states.push(state), schedule: () => 1, cancel() {} });
	const opening = client.open();
	const starting = client.start(["old@example.invalid"]);
	await client.start([]);
	resolveStart({ name: "new-run", status: "Queued" });
	await starting;
	resolveOld({ name: "old-run", status: "Completed" });
	await opening;
	assert.equal(calls.length, 2);
	assert.equal(states.at(-1).name, "new-run");
	assert.equal(JSON.stringify(calls[1].args.sender_aliases), '["old@example.invalid"]');
	client.close();
});

test("progress escapes mailbox names and displays real processed counts even with failures", () => {
	const { progressHTML } = load();
	const html = progressHTML({ status: "Running", stage: "importing", total: 10, processed: 6,
		imported: 2, existing: 3, failed: 1, folders: [{ name: '<img src=x onerror="alert(1)">', total: 10, processed: 6, imported: 2, existing: 3, failed: 1 }] });
	assert.match(html, /60%/);
	assert.match(html, /value="6"/);
	assert.match(html, /&lt;img/);
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /role="status"/);
});

test("an empty completed mailbox has a full progress bar", () => {
	const { progressHTML } = load();
	const html = progressHTML({ status: "Completed", stage: "finished", total: 0, processed: 0, folders: [] });
	assert.match(html, /100%/);
	assert.match(html, /max="1" value="1"/);
});

test("only saved writable IMAP accounts expose the action", () => {
	const { hooks, frappe } = load();
	const buttons = [];
	const frm = { doc: { name: "account-1", use_imap: 1 }, perm: [{ write: 1 }],
		is_new: () => false, add_custom_button(label) { buttons.push(label); }, remove_custom_button() {} };
	hooks["Email Account"].refresh(frm);
	assert.equal(buttons.length, 1);
	frm.doc.use_imap = 0;
	hooks["Email Account"].refresh(frm);
	assert.equal(buttons.length, 1);
	frm.doc.use_imap = 1;
	frappe.user_roles = [];
	hooks["Email Account"].refresh(frm);
	assert.equal(buttons.length, 1);
});

test("first-time account replaces loading text when Frappe omits a null message", async () => {
	const { showImport, frappe } = load();
	let html = "Loading";
	frappe.call = async () => ({});
	frappe.ui.Dialog = class {
		constructor() { this.fields_dict = { progress: { $wrapper: { html(value) { html = value; } } } }; }
		get_primary_btn() { return { prop() {} }; }
		set_df_property() {}
		show() {}
	};
	const dialog = showImport({ is_dirty: () => false, doc: { name: "account-1" } });
	await new Promise(setImmediate);
	assert.match(html, /No archive import has been started/);
	dialog.onhide();
});
