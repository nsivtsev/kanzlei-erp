const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

test("Drive context opens its parent folder without routing to the File form", () => {
	const opened = [];
	const routes = [];
	const context = {
		__: (value) => value,
		document: {
			createElement(tag) {
				return {
					tag, children: [], listeners: {},
					appendChild(child) { this.children.push(child); },
					addEventListener(event, callback) { this.listeners[event] = callback; },
				};
			},
		},
		frappe: {
			provide() { context.kanzlei_erp = {}; },
			set_route(...route) { routes.push(route); },
		},
		window: { open(...args) { opened.push(args); } },
	};
	vm.runInNewContext(fs.readFileSync("kanzlei_erp/public/js/mandant_documents.bundle.js", "utf8"), context);
	const browser = Object.create(context.kanzlei_erp.MandantDocuments.prototype);
	browser.expanded = new Set();
	const cell = browser.renderContexts({ file_id: "file-1", contexts: [
		{ source: "drive", name: "folder-1", title: "Belege", url: "/drive/d/folder-1" },
	] });
	const link = cell.children[0].children[0];
	link.listeners.click({ preventDefault() {} });
	assert.deepEqual(opened, [["/drive/d/folder-1", "_blank", "noopener,noreferrer"]]);
	assert.deepEqual(routes, []);
});
