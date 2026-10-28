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

test("received date is rendered without datetime conversion and has an explicit label", () => {
	const context={__:(value)=>value,document:{createElement(tag){return{tag,children:[],textContent:'',appendChild(child){this.children.push(child);},setAttribute(){},addEventListener(){}};}},frappe:{provide(){context.kanzlei_erp={};},datetime:{str_to_user(){throw Error('calendar receipt date must not be converted');}}}};
	vm.runInNewContext(fs.readFileSync('kanzlei_erp/public/js/mandant_documents.bundle.js','utf8'),context);
	const browser=Object.create(context.kanzlei_erp.MandantDocuments.prototype);
	browser.selected=new Map();browser.expanded=new Set();
	const row=browser.renderRow({file_id:'file',file_name:'original.pdf',extension:'pdf',document_date:'2026-09-15 00:00:00',date_kind:'received',contexts:[],file_size:1});
	assert.equal(row.children[2].children[0].textContent,'2026-09-15');
	assert.equal(row.children[2].children[1].textContent,'Received on');
});
