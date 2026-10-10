const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const navigation = fs.readFileSync("kanzlei_erp/public/js/kanzlei_navigation.bundle.js", "utf8");
function navigate(path, stored, hash = "") {
	let saved = stored,
		removed = false,
		route = null;
	const context = {
		frappe: {
			boot: { workspace_sidebar_item: { kanzlei: {} }, desktop_icons: [] },
			session: { user: "reader" },
			ui: { sidebar_item: { TypeLink: function () {} }, Sidebar: function () {} },
		},
		window: {
			location: { pathname: path, search: "?x=1", hash },
			history: {
				replaceState(_, __, value) {
					route = value;
				},
			},
		},
		localStorage: {
			getItem() {
				return saved;
			},
			setItem(_, value) {
				saved = value;
			},
			removeItem() {
				saved = null;
				removed = true;
			},
		},
	};
	context.frappe.ui.sidebar_item.TypeLink.prototype.make = function () {};
	vm.runInNewContext(navigation, context);
	return { saved, route, removed };
}
test("blank Desk/App paths redirect before rendering, direct routes and hashes remain", () => {
	for (const path of ["/desk", "/desk/", "/app", "/app/"])
		assert.equal(navigate(path, null).route, "/desk/kanzlei-overview?x=1");
	assert.equal(navigate("/desk/task/TEST", null).route, null);
	assert.equal(navigate("/desk", null, "#task/TEST").route, null);
});
test("layout repair changes only the obsolete relationship and handles corrupt storage", () => {
	const layout = [
		{ label: "Kanzlei", idx: 3 },
		{ label: "Einstellungen", parent_icon: "Kanzlei", idx: 5 },
		{ label: "Other", parent_icon: "Personal" },
	];
	const fixed = JSON.parse(navigate("/desk", JSON.stringify(layout)).saved);
	assert.deepEqual(fixed[0], layout[0]);
	assert.deepEqual(fixed[2], layout[2]);
	assert.equal(fixed[1].parent_icon, null);
	assert.equal(fixed[1].idx, 5);
	assert.equal(navigate("/desk", "{bad").removed, true);
	assert.equal(navigate("/desk", "{}").removed, true);
});
