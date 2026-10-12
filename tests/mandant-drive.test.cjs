const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");

function formHarness() {
	const buttons = new Map();
	const calls = [];
	const tabs = [];
	const errors = [];
	const context = {
		__: (text) => text,
		frappe: {
			ui: { form: { on() {} } },
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
		doc: { name: "Client A" },
		is_new: () => false,
		add_custom_button(label, callback) { buttons.set(label, callback); },
		remove_custom_button(label) { buttons.delete(label); },
	};
	return { context, frm, buttons, calls, tabs, errors };
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
