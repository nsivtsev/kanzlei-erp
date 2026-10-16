const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

test("Contact retries only saved correspondence using the server session identity", async () => {
	let handler, action, dirty = true;
	const calls = [], messages = [];
	const frappe = {
		ui: { form: { on(doctype, events) { assert.equal(doctype, "Contact"); handler = events.refresh; } } },
		msgprint(message) { messages.push(message); },
		async call(args) { calls.push(args); },
	};
	vm.runInNewContext(fs.readFileSync("kanzlei_erp/public/js/contact.js", "utf8"), { frappe, __: (s) => s });
	const frm = { is_new: () => false, is_dirty: () => dirty, perm: [{ write: 1 }],
		doc: { name: "contact-1" }, custom_buttons: {}, add_custom_button(label, callback) { action = callback; } };
	handler(frm);
	await action();
	assert.equal(calls.length, 0);
	assert.equal(messages.length, 1);
	dirty = false;
	await action();
	assert.equal(calls[0].method, "kanzlei_erp.email_history.enqueue_contact_history");
	assert.equal(JSON.stringify(calls[0].args), JSON.stringify({ contact: "contact-1" }));
});
