const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = [path.resolve(__dirname, "../development/frappe-bench/apps/frappe/realtime/handlers.js"),
	path.resolve(__dirname, "../../frappe/realtime/handlers.js")].find(fs.existsSync);
const handlers = require(source);

test("File presence excludes revoked users even in an existing room", async () => {
	const emitted = [];
	const broadcasts = [];
	const listeners = {};
	const sockets = [true, false].map((allowed, i) => ({
		id: String(i), user: `user${i}`, leave() {},
		frappe_request: async () => ({ json: async () => ({ message: allowed }) }),
		emit: (...args) => emitted.push({ user: i, args }),
	}));
	const initiator = {
		user: "user0", user_type: "System User", join() {}, leave() {},
		on: (event, handler) => { listeners[event] = handler; },
		nsp: { adapter: { rooms: new Map([["open_doc:File/test-file", new Set(["0", "1"])]]) },
			sockets: new Map(sockets.map(s => [s.id, s])),
			to: () => ({ emit: (...args) => broadcasts.push(args) }) },
	};
	handlers(initiator);
	listeners.doc_close("File", "test-file");
	await new Promise(resolve => setTimeout(resolve, 10));
	assert.equal(broadcasts.length, 0);
	assert.equal(emitted.length, 1);
	assert.equal(emitted[0].user, 0);
	assert.deepEqual(emitted[0].args[1].users, ["user0"]);
});
