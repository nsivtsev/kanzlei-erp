const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");

function formHarness() {
	const buttons = new Map();
	const calls = [];
	const tabs = [];
	const errors = [];
	const handlers = {};
	const values = [];
	const route = ["Form", "Customer", "Client A"];
	const context = {
		__: (text) => text,
		frappe: {
			get_route: () => route,
			ui: { form: { on(doctype, events) { Object.assign(handlers, events); } } },
			call: (args) => new Promise((resolve, reject) => calls.push({ args, resolve, reject })),
			msgprint: (message) => errors.push(message),
		},
		window: { open: (url) => {
			const tab = { location: { href: url }, opener: {}, closed: false, close() { this.closed = true; } };
			tabs.push(tab);
			return tab;
		} },
	};
	vm.runInNewContext(fs.readFileSync("kanzlei_erp/public/js/customer.js", "utf8"), context);
	const frm = {
		doc: { name: "Client A", kanzlei_drive_folder: null, customer_name: "Original", modified: "original-time" },
		is_new: () => false,
		async set_value(field, value, if_missing, skip_dirty_trigger) {
			values.push({ field, value, skip_dirty_trigger });
			this.doc[field] = value;
			if (!skip_dirty_trigger) this.doc.__unsaved = 1;
		},
		add_custom_button(label, callback) { buttons.set(label, callback); },
		remove_custom_button(label) { buttons.delete(label); },
	};
	return { context, frm, buttons, calls, tabs, errors, handlers, values, route };
}

test("saved Mandant opens its folder in Drive", async () => {
	const h = formHarness();
	const ready = h.context.ensureMandantDrive(h.frm);
	h.calls[0].resolve({ message: { enabled: true, folder: "abc", url: "/drive/d/abc", can_create: false } });
	await ready;
	const click = h.buttons.get("Drive Folder")();
	assert.equal(h.calls[1].args.args.customer, "Client A");
	h.calls[1].resolve({ message: { enabled: true, folder: "abc", url: "/drive/d/abc" } });
	await click;
	assert.equal(h.tabs[0].location.href, "/drive/d/abc");
	assert.equal(h.tabs[0].opener, null);
});

async function offerCreate(h) {
	const ready = h.context.ensureMandantDrive(h.frm);
	h.calls.at(-1).resolve({ message: { enabled: true, folder: null, can_create: true } });
	await ready;
}

const tick = () => new Promise(resolve => setImmediate(resolve));

test("initial folder lookup synchronizes only the protected field without dirtying a clean form", async () => {
	const h = formHarness();
	const ready = h.context.ensureMandantDrive(h.frm);
	h.calls[0].resolve({ message: { enabled: true, folder: "abc", url: "/drive/d/abc" } });
	await ready;
	assert.equal(h.frm.doc.kanzlei_drive_folder, "abc");
	assert.equal(h.frm.doc.__unsaved, undefined);
	assert.equal(h.frm.doc.modified, "original-time");
	assert.deepEqual(h.values, [{ field: "kanzlei_drive_folder", value: "abc", skip_dirty_trigger: true }]);
});

test("creation preserves unsaved business fields and immediately offers the existing folder", async () => {
	const h = formHarness();
	h.frm.doc.customer_name = "Edited";
	h.frm.doc.__unsaved = 1;
	await offerCreate(h);
	const click = h.buttons.get("Create Drive Folder")();
	h.calls.at(-1).resolve({ message: { enabled: true, folder: "new", url: "/drive/d/new" } });
	await click;
	assert.equal(h.frm.doc.kanzlei_drive_folder, "new");
	assert.equal(h.frm.doc.customer_name, "Edited");
	assert.equal(h.frm.doc.__unsaved, 1);
	assert.equal(h.frm.doc.modified, "original-time");
	assert.ok(h.buttons.has("Drive Folder"));
	assert.ok(!h.buttons.has("Create Drive Folder"));
	assert.equal(h.calls.length, 2);
});

test("opening an existing folder repairs a stale form mapping", async () => {
	const h = formHarness();
	const ready = h.context.ensureMandantDrive(h.frm);
	h.calls[0].resolve({ message: { enabled: true, folder: "abc", url: "/drive/d/abc" } });
	await ready;
	h.frm.doc.kanzlei_drive_folder = null;
	const click = h.buttons.get("Drive Folder")();
	h.calls[1].resolve({ message: { enabled: true, folder: "abc", url: "/drive/d/abc" } });
	await click;
	assert.equal(h.frm.doc.kanzlei_drive_folder, "abc");
});

test("before_save synchronizes a folder created in another tab before allowing the save", async () => {
	const h = formHarness();
	h.frm.doc.customer_name = "Edited";
	h.frm.doc.__unsaved = 1;
	assert.equal(typeof h.handlers.before_save, "function");
	let done = false;
	const save = h.handlers.before_save(h.frm).then(() => { done = true; });
	await tick();
	assert.equal(done, false);
	assert.match(h.calls[0].args.method, /\.get_folder$/);
	h.calls[0].resolve({ message: { enabled: true, folder: "other-tab", url: "/drive/d/other-tab" } });
	await save;
	assert.equal(h.frm.doc.kanzlei_drive_folder, "other-tab");
	assert.equal(h.frm.doc.customer_name, "Edited");
	assert.equal(h.frm.doc.__unsaved, 1);
});

test("before_save waits for an in-flight creation then checks the authoritative mapping", async () => {
	const h = formHarness();
	await offerCreate(h);
	const click = h.buttons.get("Create Drive Folder")();
	assert.equal(typeof h.handlers.before_save, "function");
	const save = h.handlers.before_save(h.frm);
	await tick();
	assert.equal(h.calls.length, 2);
	h.calls[1].resolve({ message: { enabled: true, folder: "new", url: "/drive/d/new" } });
	await click;
	await tick();
	assert.equal(h.calls.length, 3);
	assert.match(h.calls[2].args.method, /\.get_folder$/);
	h.calls[2].resolve({ message: { enabled: true, folder: "new", url: "/drive/d/new" } });
	await save;
	assert.equal(h.frm.doc.kanzlei_drive_folder, "new");
});

test("double click issues one creation request and reserves one tab", async () => {
	const h = formHarness();
	await offerCreate(h);
	const button = h.buttons.get("Create Drive Folder");
	const first = button();
	await button();
	assert.equal(h.calls.length, 2);
	assert.equal(h.tabs.length, 1);
	h.calls[1].resolve({ message: { enabled: true, folder: "new", url: "/drive/d/new" } });
	await first;
});

test("blocked popup still synchronizes a successfully created folder", async () => {
	const h = formHarness();
	h.context.window.open = () => null;
	await offerCreate(h);
	const click = h.buttons.get("Create Drive Folder")();
	h.calls[1].resolve({ message: { enabled: true, folder: "new", url: "/drive/d/new" } });
	await click;
	assert.equal(h.frm.doc.kanzlei_drive_folder, "new");
	assert.ok(h.buttons.has("Drive Folder"));
	assert.equal(h.errors.length, 1);
});

test("replacing the form document with the same name discards a late lookup", async () => {
	const h = formHarness();
	const ready = h.context.ensureMandantDrive(h.frm);
	h.frm.doc = { name: "Client A", customer_name: "Replacement" };
	h.calls[0].resolve({ message: { enabled: true, folder: "abc", url: "/drive/d/abc" } });
	await ready;
	assert.equal(h.buttons.size, 0);
	assert.equal(h.frm.doc.kanzlei_drive_folder, undefined);
});

test("late creation after navigation closes its reserved tab without touching the new form", async () => {
	const h = formHarness();
	await offerCreate(h);
	const click = h.buttons.get("Create Drive Folder")();
	h.frm.doc = { name: "Client B" };
	h.calls[1].resolve({ message: { enabled: true, folder: "new", url: "/drive/d/new" } });
	await click;
	assert.equal(h.frm.doc.kanzlei_drive_folder, undefined);
	assert.equal(h.tabs[0].closed, true);
	assert.equal(h.tabs[0].location.href, "about:blank");
});

test("out-of-order empty lookup cannot erase a successfully created folder", async () => {
	const h = formHarness();
	await offerCreate(h);
	const click = h.buttons.get("Create Drive Folder")();
	const refresh = h.context.ensureMandantDrive(h.frm);
	h.calls[1].resolve({ message: { enabled: true, folder: "new", url: "/drive/d/new" } });
	await click;
	h.calls[2].resolve({ message: { enabled: true, folder: null, can_create: true } });
	await refresh;
	assert.equal(h.frm.doc.kanzlei_drive_folder, "new");
	assert.ok(h.buttons.has("Drive Folder"));
	assert.ok(!h.buttons.has("Create Drive Folder"));
});

test("failed mandatory lookup rejects saving and preserves unsaved changes", async () => {
	const h = formHarness();
	h.frm.doc.__unsaved = 1;
	h.frm.doc.customer_name = "Edited";
	assert.equal(typeof h.handlers.before_save, "function");
	const save = h.handlers.before_save(h.frm);
	const rejected = assert.rejects(save);
	await tick();
	h.calls[0].reject(new Error("network unavailable"));
	await rejected;
	assert.equal(h.errors.length, 1);
	assert.equal(h.frm.doc.customer_name, "Edited");
	assert.equal(h.frm.doc.__unsaved, 1);
});

test("navigation during mandatory lookup rejects the old save without editing the new document", async () => {
	const h = formHarness();
	assert.equal(typeof h.handlers.before_save, "function");
	const save = h.handlers.before_save(h.frm);
	const rejected = assert.rejects(save);
	await tick();
	h.frm.doc = { name: "Client B" };
	h.calls[0].resolve({ message: { enabled: true, folder: "old", url: "/drive/d/old" } });
	await rejected;
	assert.equal(h.frm.doc.kanzlei_drive_folder, undefined);
});

test("new forms and already synchronized forms need no pre-save request", async () => {
	const h = formHarness();
	assert.equal(typeof h.handlers.before_save, "function");
	h.frm.is_new = () => true;
	await h.handlers.before_save(h.frm);
	h.frm.is_new = () => false;
	h.frm.doc.kanzlei_drive_folder = "known";
	await h.handlers.before_save(h.frm);
	assert.equal(h.calls.length, 0);
});

for (const state of [{ enabled: false }, { enabled: true, folder: null, can_create: true }]) {
	test(`ordinary save continues when Drive state is ${JSON.stringify(state)}`, async () => {
		const h = formHarness();
		assert.equal(typeof h.handlers.before_save, "function");
		const save = h.handlers.before_save(h.frm);
		await tick();
		h.calls[0].resolve({ message: state });
		await save;
		assert.equal(h.frm.doc.kanzlei_drive_folder, null);
	});
}

test("navigation before Frappe replaces frm.doc discards a late creation", async () => {
	const h = formHarness();
	await offerCreate(h);
	const click = h.buttons.get("Create Drive Folder")();
	h.route[2] = "Client B";
	h.calls[1].resolve({ message: { enabled: true, folder: "new", url: "/drive/d/new" } });
	await click;
	assert.equal(h.frm.doc.kanzlei_drive_folder, null);
	assert.equal(h.tabs[0].closed, true);
});

test("leaving the Customer page rejects a mandatory lookup before Frappe replaces frm.doc", async () => {
	const h = formHarness();
	const save = h.handlers.before_save(h.frm);
	const rejected = assert.rejects(save);
	await tick();
	h.route.splice(0, h.route.length, "List", "Customer", "List");
	h.calls[0].resolve({ message: { enabled: true, folder: "new", url: "/drive/d/new" } });
	await rejected;
	assert.equal(h.frm.doc.kanzlei_drive_folder, null);
});

test("an old failed lookup cannot invalidate the current form's save", async () => {
	const h = formHarness();
	const oldSave = h.handlers.before_save(h.frm);
	const oldRejected = assert.rejects(oldSave);
	await tick();
	h.frm.doc = { name: "Client B", kanzlei_drive_folder: null };
	h.route[2] = "Client B";
	h.context.frappe.validated = true;
	const newSave = h.handlers.before_save(h.frm);
	await tick();
	h.calls[0].reject(new Error("old request failed"));
	await oldRejected;
	h.calls[1].resolve({ message: { enabled: true, folder: "current", url: "/drive/d/current" } });
	await newSave;
	assert.equal(h.context.frappe.validated, true);
	assert.equal(h.frm.doc.kanzlei_drive_folder, "current");
});

test("creation is an explicit action, read-only users get no create button", async () => {
	const h = formHarness();
	let ready = h.context.ensureMandantDrive(h.frm);
	h.calls[0].resolve({ message: { enabled: true, folder: null, can_create: false } });
	await ready;
	assert.equal(h.buttons.size, 0);
	ready = h.context.ensureMandantDrive(h.frm);
	h.calls[1].resolve({ message: { enabled: true, folder: null, can_create: true } });
	await ready;
	const click = h.buttons.get("Create Drive Folder")();
	assert.match(h.calls[2].args.method, /\.create_folder$/);
	h.calls[2].resolve({ message: { enabled: true, folder: "new", url: "/drive/d/new" } });
	await click;
	assert.equal(h.tabs[0].location.href, "/drive/d/new");
});

test("unsaved clients and stale form responses add no action", async () => {
	const h = formHarness();
	h.frm.is_new = () => true;
	await h.context.ensureMandantDrive(h.frm);
	assert.equal(h.calls.length, 0);
	h.frm.is_new = () => false;
	const ready = h.context.ensureMandantDrive(h.frm);
	h.frm.doc.name = "Client B";
	h.calls[0].resolve({ message: { enabled: true, folder: "abc", url: "/drive/d/abc" } });
	await ready;
	assert.equal(h.buttons.size, 0);
});

test("failed access closes the temporary tab without opening stale folder URLs", async () => {
	const h = formHarness();
	const ready = h.context.ensureMandantDrive(h.frm);
	h.calls[0].resolve({ message: { enabled: true, folder: "abc", url: "/drive/d/abc" } });
	await ready;
	const click = h.buttons.get("Drive Folder")();
	h.calls[1].reject(new Error("permission revoked"));
	await click;
	assert.equal(h.tabs[0].closed, true);
	assert.equal(h.errors.length, 1);
});
