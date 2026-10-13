// HTTP and actual Socket.IO traffic under four independent staff sessions.
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { io } = createRequire(path.resolve('apps/suite/package.json'))('socket.io-client');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main(state) {
	const clients = {};
	const sockets = [];
	const accepted = [];
	async function login(label, credentials) {
		const headers = { Host: state.site, 'X-Frappe-Site-Name': state.site };
		let response = await fetch(state.base + '/api/method/login', { method: 'POST', headers,
			body: new URLSearchParams({ usr: credentials.user, pwd: credentials.password }) });
		assert.equal(response.status, 200, 'Fixture login failed');
		headers.Cookie = response.headers.getSetCookie().map(cookie => cookie.split(';')[0]).join('; ');
		response = await fetch(state.base + '/drive', { headers });
		const html = await response.text();
		const csrf = html.match(/window.csrf_token\s*=\s*['"]([^'"]+)/);
		assert.ok(csrf, 'Drive did not expose CSRF token');
		headers['X-Frappe-CSRF-Token'] = csrf[1];
		const client = { headers, events: [] };
		client.request = (url, options = {}) => fetch(state.base + url, { ...options,
			headers: { ...headers, ...(url.startsWith('/dav') ? { Authorization: 'Basic ' +
				Buffer.from(credentials.user + ':' + credentials.password).toString('base64') } : {}), ...options.headers } });
		client.call = async (method, args = {}) => {
			const result = await client.request('/api/method/' + method, { method: 'POST', body: new URLSearchParams(args) });
			assert.equal(result.status, 200, 'API failed: ' + method);
			return (await result.json()).message;
		};
		const origin = state.site === 'development.localhost' ? 'http://development.localhost:8000' : state.base;
		client.socket = io(state.socket + '/' + state.site, { transports: ['websocket'], reconnection: false,
			extraHeaders: { Host: new URL(origin).host, Origin: origin, 'X-Frappe-Site-Name': state.site, Cookie: headers.Cookie } });
		sockets.push(client.socket);
		client.socket.onAny((event, payload) => client.events.push({ event, payload }));
		await new Promise((resolve, reject) => {
			const timer = setTimeout(() => reject(new Error('Socket connection timed out')), 10000);
			client.socket.once('connect', () => { clearTimeout(timer); resolve(); });
			client.socket.once('connect_error', error => { clearTimeout(timer); reject(error); });
		});
		clients[label] = client;
	}
	try {
		for (const [label, credentials] of Object.entries(state.users)) await login(label, credentials);
		const { writer, reader, stranger, revoked } = clients;
		const contentUrl = '/api/method/suite.drive.api.files.get_file_content?trigger_download=1&entity_name=';
		assert.equal((await stranger.request(contentUrl + state.alias.name)).status, 403);
		assert.equal((await stranger.request('/dav/Home/old-alias.xml')).status, 404);
		const propfind = await stranger.request('/dav/Home/', { method: 'PROPFIND', headers: { Depth: '1' } });
		assert.equal(propfind.status, 207);
		assert.ok(!(await propfind.text()).includes('old-alias.xml'));
		accepted.push('legacy alias: HTTP download, WebDAV GET and PROPFIND denied');
		const inserted = await stranger.request('/api/method/frappe.client.insert', { method: 'POST',
			body: new URLSearchParams({ doc: JSON.stringify({ doctype: 'File', file_name: 'forged.xml',
				file_url: state.original.url, is_private: 1, flags: { file_created: true, ignore_permissions: true } }) }) });
		assert.equal(inserted.status, 403);
		accepted.push('standard File API rejects protected URL before creation');
		const read = await reader.request(contentUrl + state.original.name);
		assert.equal(read.status, 200);
		assert.equal(await read.text(), '<invoice />');
		const token = await revoked.call('suite.drive.api.files.create_auth_token', { entity_name: state.original.name });
		revoked.socket.emit('doc_subscribe', 'File', state.original.name);
		revoked.socket.emit('doc_open', 'File', state.original.name);
		await delay(150);
		const revoke = spawnSync(state.python, [state.script, '--site', state.site, '--fixtures', '--mode', 'revoke'],
			{ input: JSON.stringify(state), encoding: 'utf8' });
		assert.equal(revoke.status, 0, 'Fixture permission revocation failed: ' + revoke.stderr.trim().split('\n').at(-1));
		await delay(150);
		for (const client of Object.values(clients)) client.events.length = 0;
		const upload = new FormData();
		upload.set('parent', state.root);
		upload.set('total_file_size', '11');
		upload.set('uuid', 'drive-security-' + state.run);
		upload.set('file', new Blob(['<invoice />'], { type: 'application/xml' }), 'after-revoke.xml');
		const uploaded = await writer.request('/api/method/suite.drive.api.files.upload_file', { method: 'POST', body: upload });
		assert.equal(uploaded.status, 200);
		const file = (await uploaded.json()).message;
		await writer.call('suite.drive.api.files.rename', { entity_name: state.original.name, new_title: 'renamed.xml' });
		writer.socket.emit('doc_open', 'File', state.original.name);
		await delay(700);
		for (const label of ['reader', 'writer']) {
			const signals = clients[label].events.filter(({ event, payload }) => event === 'drive-folder-changed' && payload.folder === state.root);
			assert.ok(signals.length, 'Authorized session did not receive folder invalidation');
			for (const { payload } of signals) assert.deepEqual(Object.keys(payload), ['folder']);
		}
		for (const label of ['stranger', 'revoked']) {
			const leaks = clients[label].events.filter(({ event, payload }) =>
				event === 'list-add' || event === 'drive-folder-changed' && payload.folder === state.root ||
				['doc_update', 'docinfo_update', 'file-docinfo-changed'].includes(event) &&
					[state.original.name, file.name].includes(payload.name ?? payload.doc?.reference_name) ||
				event === 'doc_viewers' && payload.docname === state.original.name);
			assert.equal(leaks.length, 0, 'Unauthorized session received File metadata');
		}
		assert.equal((await revoked.request(contentUrl + file.name)).status, 403);
		const guest = await fetch(state.base + '/api/method/suite.drive.api.files.get_file_content?entity_name=' +
			state.original.name + '&token=' + encodeURIComponent(token), { headers: { Host: state.site, 'X-Frappe-Site-Name': state.site } });
		assert.equal(guest.status, 403);
		accepted.push('actual realtime: current readers receive ID-only signals; stranger and revoked subscribers receive no File metadata');
		accepted.push('revoked download and cookieless preview token denied');
		const listed = await reader.call('suite.drive.api.list.files', { entity_name: state.root });
		assert.ok(listed.some(row => row.name === file.name));
		accepted.push('authorized reload returns uploaded file');
		console.log(JSON.stringify({ passed: accepted, staff_sessions: 4 }));
	} finally {
		for (const socket of sockets) socket.disconnect();
	}
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => main(JSON.parse(input)).catch(error => {
	console.error(error.message);
	process.exitCode = 1;
}));
