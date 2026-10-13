const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");

test("File docinfo refresh preserves unsaved form values and ignores navigation", async () => {
	let signal, finish;
	const doc = { doctype: "File", name: "a", file_name: "unsaved edit" };
	const frm = { doc, timeline: { refresh() { this.refreshed = true; } },
		footer: { refresh_comments_count() {} } };
	const context = { window: { cur_frm: frm }, frappe: {
		realtime: { on(event, handler) { assert.equal(event, "file-docinfo-changed"); signal = handler; } },
		model: { docinfo: { File: {} } },
		call: () => new Promise(resolve => { finish = resolve; }),
	} };
	vm.runInNewContext(fs.readFileSync("kanzlei_erp/public/js/drive_realtime.bundle.js", "utf8"), context);
	const request = signal({ name: "a" });
	finish({ docinfo: { comments: ["new"] } });
	await request;
	assert.equal(frm.doc.file_name, "unsaved edit");
	assert.equal(frm.timeline.refreshed, true);
	const late = signal({ name: "a" });
	context.window.cur_frm = { doc: { doctype: "File", name: "b" } };
	finish({ docinfo: { comments: ["late"] } });
	await late;
	assert.equal(context.frappe.model.docinfo.File.a.comments[0], "new");
});
