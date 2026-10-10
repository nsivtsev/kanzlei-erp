const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const source = fs.readFileSync(
	"kanzlei_erp/kanzlei_erp/page/kanzlei_overview/kanzlei_overview.js",
	"utf8"
);

function overview() {
	const pending = [];
	const context = {
		frappe: {
			pages: { "kanzlei-overview": {} },
			call: (request) =>
				new Promise((resolve, reject) => pending.push({ request, resolve, reject })),
		},
		__: (s) => s,
	};
	vm.createContext(context);
	vm.runInContext(source + "\nthis.Overview = KanzleiOverview;", context);
	const page = Object.create(context.Overview.prototype);
	page.bucket = "urgent";
	page.sections = Object.fromEntries(
		["tasks", "agenda", "mail"].map((key) => [key, { revision: 0, rows: [] }])
	);
	const output = {};
	page.root = {
		find(selector) {
			return {
				html(value) {
					output[selector] = value;
					return this;
				},
				prop() {
					return this;
				},
				find() {
					return this;
				},
				text() {
					return this;
				},
			};
		},
	};
	page.setCounts = (summary) => {
		output.counts = summary;
	};
	page.escape = String;
	page.date = String;
	page.render = (key) => {
		output[key] = page.sections[key].rows;
	};
	return { page, pending, output };
}
const result = (name) => ({
	message: {
		state: "ready",
		rows: [{ name }],
		summary: { today: 1 },
		accounts: [],
		has_more: false,
	},
});
test("a late filter response never replaces the latest result or counts", async () => {
	const { page, pending, output } = overview();
	const old = page.load("tasks");
	page.bucket = "today";
	const latest = page.load("tasks");
	pending[1].resolve(result("new"));
	await latest;
	pending[0].resolve(result("old"));
	await old;
	assert.equal(output.tasks[0].name, "new");
	assert.equal(page.sections.tasks.result.summary.today, 1);
	assert.equal(pending[1].request.args.start, 0);
	assert.equal(pending[1].request.args.bucket, "today");
});
test("independent failed mail leaves successful task and agenda results", async () => {
	const { page, pending, output } = overview();
	const tasks = page.load("tasks"),
		agenda = page.load("agenda"),
		mail = page.load("mail");
	pending[0].resolve(result("task"));
	pending[1].resolve(result("deadline"));
	pending[2].reject(new Error("mail unavailable"));
	await Promise.all([tasks, agenda, mail]);
	assert.equal(output.tasks[0].name, "task");
	assert.equal(output.agenda[0].name, "deadline");
	assert.match(output['[data-section="mail"]'], /data-retry="mail"/);
	assert.equal(page.sections.mail.loading, false);
});
test("pagination appends once, blocks repeated clicks and stale pagination after refresh", async () => {
	const { page, pending } = overview();
	const initial = page.load("tasks");
	pending[0].resolve(result("one"));
	await initial;
	const more = page.load("tasks", true);
	await page.load("tasks", true);
	assert.equal(pending.length, 2);
	assert.equal(pending[1].request.args.start, 1);
	const refresh = page.load("tasks");
	pending[2].resolve(result("refreshed"));
	await refresh;
	pending[1].resolve(result("late"));
	await more;
	assert.equal(page.sections.tasks.rows.length, 1);
	assert.equal(page.sections.tasks.rows[0].name, "refreshed");
});
test("an error does not turn summary into zero counts and retry starts from zero", async () => {
	const { page, pending, output } = overview();
	const first = page.load("tasks");
	pending[0].reject(new Error("fail"));
	await first;
	assert.equal(output.counts, null);
	assert.match(output['[data-section="tasks"]'], /data-retry/);
	const retry = page.load("tasks");
	assert.equal(pending[1].request.args.start, 0);
	pending[1].resolve(result("retried"));
	await retry;
	assert.equal(output.tasks[0].name, "retried");
});
