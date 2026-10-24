const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

test("global copy guard blocks protected Task before fields are stripped", () => {
 const frappe = {model: {}, throw(message) {throw new Error(message);}};
 assert.ok(fs.existsSync("kanzlei_erp/public/js/posteingang_task_guard.js"), "global Task copy guard is missing");
 vm.runInNewContext(fs.readFileSync("kanzlei_erp/public/js/posteingang_task_guard.js", "utf8"), {frappe, __: x => x});
 assert.throws(() => frappe.model.document_copy_guard({doctype:"Task",kanzlei_incoming_letter:"protected"}), /cannot be duplicated/);
 assert.doesNotThrow(() => frappe.model.document_copy_guard({doctype:"Task", subject:"ordinary"}));
 assert.doesNotThrow(() => frappe.model.document_copy_guard({doctype:"Customer"}));
});
