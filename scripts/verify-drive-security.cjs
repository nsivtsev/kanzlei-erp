// HTTP and actual Socket.IO traffic under four independent staff sessions.
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
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
			const body = await result.json();
			assert.equal(result.status, 200, 'API failed: ' + method + ' [' + result.status + ' ' + (body.exc_type || '') + ']');
			return body.message;
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
		for (const [label, client] of Object.entries(clients)) {
			const profile = await client.call('suite.api.account.get_logged_in_user');
			assert.equal(profile.user_type, label === 'portal' ? 'Website User' : 'System User');
			for (let repeat = 0; repeat < 2; repeat++) {
				const desk = await client.request('/desk/kanzlei-overview');
				if (label === 'portal') { assert.equal(desk.status, 403); continue; }
				assert.equal(desk.status, 200);
				const match = (await desk.text()).match(/frappe\.boot\s*=\s*(\{[^\n]+\});/);
				assert.ok(match, 'Desk boot missing');
				const boot = JSON.parse(match[1]);
				const items = boot.workspace_sidebar_item.kanzlei.items;
				const driveLinks = items.filter(item => item.url === '/drive');
				assert.equal(driveLinks.length, 1, 'Staff Drive menu must be unique');
				const index = items.indexOf(driveLinks[0]);
				const timesheetIndex = items.findIndex(item => item.link_to === 'Timesheet' || item.url === '/desk/timesheet');
				// Frappe omits Timesheet when this role cannot read it; Files
				// remains the last daily-work item before optional settings.
				if (timesheetIndex >= 0) assert.equal(timesheetIndex, index - 1);
				assert.equal(index, items.length - (label === 'manager' ? 2 : 1));
			}
		}
		const guestDesk = await fetch(state.base + '/desk/kanzlei-overview', {
			redirect: 'manual', headers: { Host: state.site, 'X-Frappe-Site-Name': state.site },
		});
		assert.ok([301, 302, 403].includes(guestDesk.status));
		if (guestDesk.status !== 403) assert.match(guestDesk.headers.get('location'), /^\/login\?/);
		const ordinaryFolder = await stranger.call('suite.drive.api.files.create_folder', {
			file_name: 'ordinary-navigation-' + state.run, parent: state.homes[2],
		});
		assert.equal(ordinaryFolder.is_folder, 1);
		assert.equal(ordinaryFolder.folder, state.homes[2]);
		assert.ok(!ordinaryFolder.kanzlei_drive_customer);
		accepted.push('ERP Files menu is unique for staff and manager; portal/Guest Desk denied; staff without Customer access creates an ordinary Home folder');
		const listMethod = 'kanzlei_erp.mandant_drive.list_mandanten';
		const search = '_Drive';
		const clientPage = await writer.call(listMethod, { search, page_length: 1 });
		assert.equal(clientPage.rows.length, 1); assert.equal(clientPage.has_next, true);
		const next = await writer.call(listMethod, { search, start: clientPage.next_start, page_length: 1 });
		assert.equal(next.rows.length, 1); assert.notEqual(clientPage.rows[0].customer, next.rows[0].customer);
		assert.equal(next.has_next, false);
		const writerRows = [...clientPage.rows, ...next.rows];
		assert.equal(writerRows.find(row => row.customer === state.form_customer).state, 'missing');
		assert.equal(writerRows.find(row => row.customer === state.form_customer).can_create, true);
		assert.equal(writerRows.find(row => row.customer === state.customer).state, 'ready');
		const readerRows = (await reader.call(listMethod, { search })).rows;
		assert.equal(readerRows.length, 1); assert.equal(readerRows[0].customer, state.customer);
		assert.equal(readerRows[0].folder, state.root); assert.equal(readerRows[0].can_create, false);
		assert.deepEqual((await stranger.call(listMethod, { search: state.customer })).rows, []);
		const portalList = await clients.portal.request('/api/method/' + listMethod);
		assert.equal(portalList.status, 403);
		const navigation = await reader.call('suite.drive.api.permissions.get_entity_with_permissions', { entity_name: state.original.name });
		assert.equal(navigation.mandant.customer, state.customer); assert.equal(navigation.mandant.folder, state.root);
		assert.equal(navigation.breadcrumbs[0].name, state.root);
		assert.ok(!navigation.breadcrumbs.some(crumb => crumb.file_name === 'Administrator' || crumb.name === 'Administrator'));
		const grant = spawnSync(state.python, [state.script, '--site', state.site, '--fixtures', '--mode', 'grant'], { input: JSON.stringify(state), encoding: 'utf8' });
		assert.equal(grant.status, 0, 'Fixture permission grant failed');
		const newlyReadable = (await reader.call(listMethod, { search })).rows.find(row => row.customer === state.form_customer);
		assert.equal(newlyReadable.state, 'missing'); assert.equal(newlyReadable.can_create, false);
		accepted.push('Mandanten lists/search/pages follow current Customer rights; portal denied; grant appears; direct entity context excludes Administrator');
		const formSnapshot = await writer.call('frappe.client.get', { doctype: 'Customer', name: state.form_customer });
		assert.ok(!formSnapshot.kanzlei_drive_folder);
		const folderResults = await Promise.all([1, 2].map(() =>
			writer.call('kanzlei_erp.mandant_drive.create_folder', { customer: state.form_customer })));
		assert.equal(folderResults[0].folder, folderResults[1].folder);
		state.form_root = folderResults[0].folder;
		const saveCustomer = doc => writer.request('/api/method/frappe.client.save', {
			method: 'POST', body: new URLSearchParams({ doc: JSON.stringify(doc) }),
		});
		formSnapshot.customer_name = 'Saved form ' + state.run;
		assert.equal((await saveCustomer(formSnapshot)).status, 403, 'A stale blank mapping must remain protected');
		formSnapshot.kanzlei_drive_folder = (await writer.call('kanzlei_erp.mandant_drive.get_folder', {
			customer: state.form_customer })).folder;
		let savedResponse = await saveCustomer(formSnapshot);
		assert.equal(savedResponse.status, 200, 'Synchronized Customer save failed');
		const savedForm = (await savedResponse.json()).message;
		assert.equal(savedForm.customer_name, formSnapshot.customer_name);
		assert.equal(savedForm.kanzlei_drive_folder, state.form_root);
		for (const mapping of [null, state.root]) {
			assert.equal((await saveCustomer({ ...savedForm, kanzlei_drive_folder: mapping })).status, 403,
				'Protected Customer mapping was changed');
		}
		savedResponse = await saveCustomer({ ...savedForm, customer_name: 'Other tab ' + state.run });
		assert.equal(savedResponse.status, 200);
		const staleSave = await saveCustomer({ ...savedForm, customer_name: 'Stale edit ' + state.run });
		assert.notEqual(staleSave.status, 200);
		assert.equal((await staleSave.json()).exc_type, 'TimestampMismatchError');
		const formCheck = spawnSync(state.python, [state.script, '--site', state.site, '--fixtures', '--mode', 'verify-form'],
			{ input: JSON.stringify(state), encoding: 'utf8' });
		assert.equal(formCheck.status, 0, 'Concurrent folder or persisted Customer checks failed');
		accepted.push('parallel staff folder creation returns one persisted root; synchronized Customer saves while mapping forgery and stale business edits are denied');
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
		async function readyArchive(client, entities) {
			const archive = await client.call('suite.drive.api.files.download_folder', { entities: JSON.stringify(entities) });
			for (let attempt = 0; attempt < 100; attempt++) {
				const status = await client.call('suite.drive.api.files.download_status', { token: archive.token });
				assert.notEqual(status.status, 'failed', 'Archive build failed');
				if (status.status === 'ready') return { ...archive, ...status };
				await delay(200);
			}
			throw new Error('Archive did not become ready');
		}
		const uploadedSamples = {};
		for (const [label, parent] of [['mandant', state.root], ['ordinary', state.homes[0]]]) {
			uploadedSamples[label] = [];
			for (const sample of state.download_samples) {
				const bytes = Buffer.from(sample.data, 'base64');
				const form = new FormData();
				form.set('parent', parent); form.set('total_file_size', String(bytes.length));
				form.set('uuid', 'downloads-' + state.run + '-' + label + '-' + sample.name.replace('.', '-'));
				form.set('file', new Blob([bytes], { type: sample.mime }), sample.name);
				const upload = await writer.request('/api/method/suite.drive.api.files.upload_file', { method: 'POST', body: form });
				assert.equal(upload.status, 200, 'Office upload failed');
				const file = (await upload.json()).message;
				uploadedSamples[label].push(file.name);
				const client = label === 'mandant' ? reader : writer;
				const card = await client.call('suite.drive.api.permissions.get_entity_with_permissions', { entity_name: file.name });
				assert.equal(card.can_download, true); assert.equal(card.file_url, null);
				assert.ok((sample.name.endsWith('.xml') ? ['application/xml', 'text/xml'] : [sample.mime]).includes(card.mime_type));
				const downloaded = await client.request(contentUrl + file.name);
				assert.equal(downloaded.status, 200, 'Office download failed');
				assert.equal(createHash('sha256').update(Buffer.from(await downloaded.arrayBuffer())).digest('hex'), sample.sha256);
				if (label === 'mandant') assert.equal((await stranger.request(contentUrl + file.name)).status, 403);
			}
			const client = label === 'mandant' ? reader : writer;
			const archive = await readyArchive(client, [parent, ...uploadedSamples[label]]);
			assert.deepEqual(archive.skipped_counts, { hosted_content: label === 'ordinary' ? 1 : 0, links: label === 'ordinary' ? 1 : 0 });
			const result = await client.request('/api/method/suite.drive.api.files.download_archive?token=' + encodeURIComponent(archive.token));
			assert.equal(result.status, 200);
			const check = spawnSync(state.python, ['-c', `import base64,hashlib,io,json,sys,zipfile
payload=json.load(sys.stdin)
with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload['zip']))) as archive:
 names=archive.namelist()
 assert len(names)==len(set(names)), 'Duplicate ZIP members'
 for sample in payload['samples']:
  matched=[name for name in names if name.rsplit('/',1)[-1]==sample['name']]
  assert len(matched)==1, 'Missing or repeated Office member'
  assert hashlib.sha256(archive.read(matched[0])).hexdigest()==sample['sha256'], 'ZIP bytes changed'
print('ZIP byte checks passed')`], { encoding: 'utf8', input: JSON.stringify({ zip: Buffer.from(await result.arrayBuffer()).toString('base64'), samples: state.download_samples }) });
			assert.equal(check.status, 0, 'ZIP source hash verification failed');
		}
		accepted.push('real Office uploads: source/direct/ZIP SHA-256 agree in Mandant and ordinary folders; overlapping selection has no duplicates');
		accepted.push('mixed ordinary ZIP reports hosted/link omissions without external Office preview');
		const revokedArchive = await readyArchive(revoked, [state.root]);
		const token = await revoked.call('suite.drive.api.files.create_auth_token', { entity_name: state.original.name });
		revoked.socket.emit('doc_subscribe', 'File', state.original.name);
		revoked.socket.emit('doc_open', 'File', state.original.name);
		await delay(150);
		const revoke = spawnSync(state.python, [state.script, '--site', state.site, '--fixtures', '--mode', 'revoke'],
			{ input: JSON.stringify(state), encoding: 'utf8' });
		assert.equal(revoke.status, 0, 'Fixture permission revocation failed: ' + revoke.stderr.trim().split('\n').at(-1));
		assert.deepEqual((await revoked.call(listMethod, { search: state.customer })).rows, []);
		assert.equal((await revoked.request('/api/method/suite.drive.api.permissions.get_entity_with_permissions?entity_name=' + encodeURIComponent(state.root))).status, 403);
		accepted.push('Mandanten removes revoked client and denies its old direct folder URL');
		assert.equal((await revoked.request('/api/method/suite.drive.api.files.download_archive?token=' + encodeURIComponent(revokedArchive.token))).status, 403);
		const rejectedZip = await stranger.request('/api/method/suite.drive.api.files.download_folder', { method: 'POST', body: new URLSearchParams({ entities: JSON.stringify([state.root]) }) });
		assert.equal(rejectedZip.status, 403);
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
		console.log(JSON.stringify({ passed: accepted, staff_sessions: 4, navigation_sessions: 2 }));
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
