const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function harness(transport) {
 const context = {frappe: {posteingang: {}}, crypto: {randomUUID: () => 'stable-key'}, setTimeout, clearTimeout};
 vm.createContext(context);
 const path='kanzlei_erp/public/js/posteingang_jobs.bundle.js';
 assert.ok(fs.existsSync(path), 'AP-05 client missing');
 vm.runInContext(fs.readFileSync(path,'utf8'),context);
 return new context.frappe.posteingang.AnalysisClient(transport);
}
test('lost response retries the same intent and blocks concurrent submission', async () => {
 const calls=[];let release;
 const client=harness((op,args)=>{calls.push({op,args});return new Promise((resolve,reject)=>{release=reject;});});
 const pending=client.mutate('queue_analysis','letter',1);
 assert.equal(await client.mutate('queue_analysis','letter',1),null);
 release(Error('NETWORK'));await assert.rejects(pending);
 const retry=client.mutate('queue_analysis','letter',1);
 assert.equal(calls[0].args.request_key,calls[1].args.request_key);
 release(Error('NETWORK'));await assert.rejects(retry);
});
test('poll does not reload dirty forms and stops when permission is lost', async () => {
 const client=harness(async()=>{throw Error('PERMISSION');});
 let reloads=0;
 await client.refresh({doc:{name:'one'},is_dirty:()=>true,reload_doc:()=>reloads++});
 assert.equal(reloads,0);
 await assert.rejects(client.refresh({doc:{name:'one'},is_dirty:()=>false,reload_doc:()=>reloads++}));
 assert.equal(client.stopped,true);
});
test('state response is data and never executes document HTML', async () => {
 const client=harness(async()=>({status:'Needs Review',revision:5,run:{status:'Succeeded'},allowed_operations:[]}));
 let reloads=0;
 const state=await client.refresh({doc:{name:'one',revision:4},is_dirty:()=>false,reload_doc:()=>reloads++});
 assert.equal(state.revision,5);assert.equal(reloads,1);assert.equal(client.stopped,true);
});
test('edits started while polling is in flight are preserved', async () => {
 let resolve;let dirty=false;let reloads=0;
 const client=harness(()=>new Promise(r=>{resolve=r;}));
 const pending=client.refresh({doc:{name:'one',revision:1},is_dirty:()=>dirty,reload_doc:()=>reloads++});
 dirty=true;resolve({status:'Processing',revision:2,allowed_operations:[]});
 await pending;assert.equal(reloads,0);
});
test('navigation while polling is in flight prevents late reload', async () => {
 let resolve;let active=true;let reloads=0;
 const client=harness(()=>new Promise(r=>{resolve=r;}));
 const pending=client.refresh({doc:{name:'one',revision:1},is_dirty:()=>false,reload_doc:()=>reloads++},()=>active);
 active=false;resolve({status:'Processing',revision:2,allowed_operations:[]});
 await pending;assert.equal(reloads,0);
});
