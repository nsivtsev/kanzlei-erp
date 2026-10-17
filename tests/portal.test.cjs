const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const file = 'kanzlei_erp/public/js/portal.js';
const api = fs.existsSync(file) ? require('../' + file) : {};
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; };

test('portal search matches labels, application and context without treating markup as HTML', () => {
	assert.equal(typeof api.matches, 'function', 'Portal search is missing');
	assert.equal(api.matches({label:'Müller GmbH',app:'Drive',context:'September'}, '  DRIVE  '), true);
	assert.equal(api.matches({label:'Müller GmbH',app:'Drive',context:'September'}, 'september'), true);
	assert.equal(api.matches({label:'<img src=x>'}, '<img'), true);
	assert.equal(api.matches({label:'Müller'}, 'unrelated'), false);
});

test('ERP visit tracker accepts only saved supported forms', () => {
	assert.equal(typeof api.historyReference, 'function', 'Portal history tracker is missing');
	assert.deepEqual(api.historyReference(['Form','Customer','Client / 1']), {doctype:'Customer',name:'Client / 1'});
	for (const route of [[],['List','Customer'],['Form','User','Administrator'],['Form','Task','new-task-1'],['Form','Task',''],null])
		assert.equal(api.historyReference(route), null);
});

test('an old load cannot overwrite a newer portal response', async () => {
	assert.equal(typeof api.PortalState, 'function', 'Portal state is missing');
	const requests = [deferred(),deferred()]; let n=0;
	const state = new api.PortalState('reader', () => requests[n++].promise);
	const first=state.load(), second=state.load();
	requests[1].resolve({user:'reader',recent:[{name:'new'}],pins:[]}); await second;
	requests[0].resolve({user:'reader',recent:[{name:'old'}],pins:[]}); await first;
	assert.equal(state.data.recent[0].name, 'new');
});

test('session mismatch clears protected records and prevents further mutations', async () => {
	assert.equal(typeof api.PortalState, 'function'); let writes=0;
	const state=new api.PortalState('reader', async method => { if(method==='set_pin') writes++; return {user:'other',recent:[{name:'secret'}],pins:[]}; });
	state.data={recent:[{name:'old'}],pins:[{name:'old'}]};
	await state.load(); await state.toggle({doctype:'Customer',name:'x'});
	assert.deepEqual(state.data, {recent:[],pins:[]});
	assert.equal(state.status,'session_lost'); assert.equal(writes,0);
});

test('pin mutation is serialized, sends expected user and reloads server state', async () => {
	assert.equal(typeof api.PortalState,'function'); const pending=deferred(), calls=[];
	const state=new api.PortalState('reader', (method,args) => {calls.push([method,args]); return method==='set_pin' ? pending.promise : Promise.resolve({user:'reader',recent:[],pins:[{doctype:'Customer',name:'x'}]});});
	const first=state.toggle({doctype:'Customer',name:'x'}); await state.toggle({doctype:'Customer',name:'y'});
	assert.equal(calls.length,1); assert.equal(state.busy,true);
	assert.deepEqual(calls[0][1],{doctype:'Customer',name:'x',pinned:1,expected_user:'reader'});
	pending.resolve({user:'reader'}); await first;
	assert.equal(state.data.pins[0].name,'x'); assert.equal(state.busy,false);
});

test('failed pin saves preserve confirmed state and remain retryable', async () => {
	assert.equal(typeof api.PortalState,'function');
	const state=new api.PortalState('reader', async () => {throw new Error('unavailable');});
	state.data={recent:[],pins:[{doctype:'Customer',name:'x'}]};
	await state.toggle({doctype:'Customer',name:'x'});
	assert.equal(state.data.pins.length,1); assert.equal(state.status,'save_error'); assert.equal(state.busy,false);
});

test('leaving a page clears data and discards late responses', async () => {
	assert.equal(typeof api.PortalState,'function'); const pending=deferred();
	const state=new api.PortalState('reader',()=>pending.promise);
	const loading=state.load(); state.suspend();
	pending.resolve({user:'reader',recent:[{name:'secret'}],pins:[]}); await loading;
	assert.deepEqual(state.data,{recent:[],pins:[]});
});

test('a rejected pin request clears protected state after session loss', async () => {
	const state=new api.PortalState('reader', async () => {const error=new Error('Forbidden');error.status=403;throw error;});
	state.data={recent:[{doctype:'Customer',name:'x',label:'Private'}],pins:[]};
	await state.toggle(state.data.recent[0]);
	assert.deepEqual(state.data,{recent:[],pins:[]}); assert.equal(state.status,'session_lost');
});

test('opening a record cannot navigate after the page was suspended', async () => {
	const pending=deferred(), navigated=[];
	const state=new api.PortalState('reader',()=>pending.promise);
	assert.equal(typeof state.open,'function','Record opening must use lifecycle-aware state');
	const opening=state.open({doctype:'Customer',name:'x'},url=>navigated.push(url));
	state.suspend(); pending.resolve({user:'reader',url:'/desk/customer/x'}); await opening;
	assert.deepEqual(navigated,[]);
});

test('record opening uses the validated server destination and rejects another session', async () => {
	let result={user:'reader',url:'/desk/customer/x'}; const navigated=[];
	const state=new api.PortalState('reader', async ()=>result);
	assert.equal(typeof state.open,'function');
	await state.open({doctype:'Customer',name:'x'},url=>navigated.push(url));
	assert.deepEqual(navigated,['/desk/customer/x']);
	result={user:'other',url:'/desk/customer/secret'};
	await state.open({doctype:'Customer',name:'x'},url=>navigated.push(url));
	assert.equal(navigated.length,1); assert.equal(state.status,'session_lost');
});

test('starting logout invalidates an earlier open before logout completes', async () => {
	const open=deferred(), logout=deferred(), navigated=[];
	const state=new api.PortalState('reader',method => method==='logout' ? logout.promise : open.promise);
	assert.equal(typeof state.logout,'function');
	const opening=state.open({doctype:'Customer',name:'x'},url=>navigated.push(url));
	const signingOut=state.logout(url=>navigated.push(url));
	open.resolve({user:'reader',url:'/desk/customer/x'}); await opening;
	assert.deepEqual(navigated,[]);
	logout.resolve({}); await signingOut; assert.deepEqual(navigated,['/login']);
});

test('the native empty Desk home route returns to the portal', () => {
	let change;
	const destinations=[];
	const context={
		KanzleiPortal:api,
		window:{location:{assign:url=>destinations.push(url)}},
		frappe:{router:{on:(_event,callback)=>{change=callback;}},get_route:()=>[''],session:{user:'reader'},call:()=>Promise.resolve()},
	};
	const tracker=fs.readFileSync('kanzlei_erp/public/js/portal_history.bundle.js','utf8').replace('import "./portal.js";','');
	vm.runInNewContext(tracker,context); change();
	assert.deepEqual(destinations,['/kanzlei-portal']);
});

test('a pin finishing during logout cannot restart visibility refresh or cancel sign-out', async () => {
	const pin=deferred(), logout=deferred(), navigated=[];
	const state=new api.PortalState('reader',method=>method==='logout' ? logout.promise : pin.promise);
	assert.equal(typeof state.refresh,'function');
	const saving=state.toggle({doctype:'Customer',name:'x'});
	const signingOut=state.logout(url=>navigated.push(url));
	pin.resolve({user:'reader'}); await saving;
	await state.refresh(); logout.resolve({}); await signingOut;
	assert.deepEqual(navigated,['/login']);
});
