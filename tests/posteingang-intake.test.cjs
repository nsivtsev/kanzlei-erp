const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness(transport) {
 const values = new Map();
 const context = {window: {location: {host: 'test.local'}}, frappe: {session: {user: 'operator@example.invalid'}},
  localStorage: {getItem: k => values.get(k) ?? null, setItem: (k,v) => values.set(k,v), removeItem: k => values.delete(k)},
  crypto: {randomUUID: () => `key-${values.size}-${Math.random()}`}, console};
 vm.createContext(context);
 vm.runInContext(fs.readFileSync('kanzlei_erp/public/js/posteingang_intake.bundle.js', 'utf8'), context);
 return {client: new context.frappe.posteingang.IntakeClient(transport), context, values};
}

test('mixed batch continues after one failure and keeps failed key for retry', async () => {
 const sent=[];
 const h=harness(async (row) => {sent.push(row); if(row.file.name==='bad.pdf') throw Error('PDF_INVALID'); return {outcome:'created',letter:'one'};});
 const rows=[h.client.add({name:'bad.pdf'}),h.client.add({name:'good.pdf'})];
 assert.equal(rows[0].known,false);
 assert.equal(rows[0].date,null);
 await h.client.upload(rows);
 assert.equal(sent.length,2);
 assert.equal(rows[1].result.letter,'one');
 assert.equal(rows[0].error,'PDF_INVALID');
 const key=rows[0].key;
 await h.client.upload([rows[0]]);
 assert.equal(sent[2].key,key);
 assert.equal(rows[0].key,key);
});

test('lost response can be restored without retaining PDF bytes or result links', async () => {
 const h=harness(async () => {throw Error('NETWORK');});
 const row=h.client.add({name:'sensitive.pdf', bytes:'SECRET PDF'});
 row.known=true;row.date='2026-10-20';
 await h.client.upload([row]);
 const saved=[...h.values.values()].join('');
 assert.ok(!saved.includes('SECRET PDF'));
 assert.ok(!saved.includes('sensitive.pdf'));
 assert.ok(!saved.includes('letter'));
 const result=await h.client.recover(async key => ({outcome:'created',letter:'restored'}));
 assert.equal(result[0].result.letter,'restored');
 assert.equal(result[0].key,row.key);
});

test('retry freezes semantic parameters and unknown recovery requires file selection', async () => {
 const h=harness(async () => {throw Error('NETWORK');});
 const row=h.client.add({name:'a.pdf'});
 await h.client.upload([row]);
 row.known=true; row.date='2026-10-20';
 await h.client.upload([row]);
 assert.equal(row.sent.known,false);
 const recovered=await h.client.recover(async () => ({outcome:'unknown'}));
 assert.equal(recovered[0].file,null);
 assert.equal(recovered[0].result,null);
 assert.equal(recovered[0].key,row.key);
});
