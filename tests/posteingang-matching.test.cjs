const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function harness(transport) {
 const path='kanzlei_erp/public/js/posteingang_matching.bundle.js';
 assert.ok(fs.existsSync(path),'AP-06 client missing');
 let keys=0;
 const context={frappe:{posteingang:{},ui:{form:{on(){}}}},crypto:{randomUUID:()=> 'intent-uuid-'+(++keys)}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(path,'utf8'),context);
 return new context.frappe.posteingang.MatchingClient(transport);
}
test('selection network retry keeps key and original revision, blocks double click',async()=>{
 const calls=[];let reject;
 const client=harness((op,args)=>{calls.push(args);return new Promise((r,j)=>{reject=j;});});
 const pending=client.select('letter','customer','reason',1);
 assert.equal(await client.select('letter','customer','reason',1),null);
 reject(Error('NETWORK'));await assert.rejects(pending);
 const retry=client.select('letter','customer','reason',9);
 assert.equal(calls[1].request_key,calls[0].request_key);
 assert.equal(calls[1].expected_revision,1);
 reject(Error('NETWORK'));await assert.rejects(retry);
});
test('new load clears previous state and ignores dirty or navigated late replies',async()=>{
 let resolve,clears=0,active=true,dirty=false;
 const client=harness(()=>new Promise(r=>{resolve=r;}));
 const frm={doc:{name:'letter'},is_dirty:()=>dirty};
 const pending=client.load(frm,()=>active,()=>clears++);
 assert.equal(clears,1);dirty=true;resolve({matching:{candidates:[{customer:'private'}]}});
 assert.equal(await pending,null);
 dirty=false;const late=client.load(frm,()=>active,()=>clears++);
 active=false;resolve({selected_customer:{customer:'private'}});
 assert.equal(await late,null);
});
test('a superseded response cannot replace a newer permission filtered reply',async()=>{
 const pending=[];const client=harness(()=>new Promise(r=>pending.push(r)));
 const frm={doc:{name:'letter'},is_dirty:()=>false};
 const old=client.load(frm,()=>true,()=>{}), fresh=client.load(frm,()=>true,()=>{});
 pending[1]({matching:null,selected_customer:null});assert.equal((await fresh).matching,null);
 pending[0]({selected_customer:{customer:'revoked'}});assert.equal(await old,null);
});
test('replacing the form document rejects an old response before another attach',async()=>{
 let resolve;const client=harness(()=>new Promise(r=>{resolve=r;}));
 const frm={doc:{name:'old'},is_dirty:()=>false};
 const pending=client.load(frm,()=>true,()=>{});
 frm.doc={name:'new'};resolve({selected_customer:{customer:'old-private'}});
 assert.equal(await pending,null);
});
test('a definitive revision conflict starts a new intent after reread',async()=>{
 const calls=[];
 const client=harness(async(op,args)=>{calls.push(args);if(calls.length===1) throw Error('REVISION_CONFLICT');return {revision:10};});
 await assert.rejects(client.select('letter','customer','reason',1));
 await client.select('letter','customer','reason',9);
 assert.equal(calls[1].expected_revision,9);
 assert.notEqual(calls[1].request_key,calls[0].request_key);
});
test('manual search waits for the visible dialog before accepting a fast reply',async()=>{
 class Element {
  constructor(tag){this.tag=tag;this.children=[];this.handlers={};}
  append(element){this.children.push(element);}
  empty(){this.children=[];}
  addEventListener(event,callback){this.handlers[event]=callback;}
 }
 const wrapper=new Element('section'), dialogs=[],calls=[];
 let prompt,denied=false;
 const context={
  document:{createElement:tag=>new Element(tag)},crypto:{randomUUID:()=> 'key'},__:text=>text,
  fetch:async(url,options)=>{
   calls.push({url,args:JSON.parse(options.body)});
   if(denied) return {ok:false,status:403,json:async()=>({exc:'PermissionError'})};
   return {ok:true,json:async()=>({message:url.endsWith('get_matching_state') ? {matching:null,allowed_operations:['select_customer']} : {rows:[{customer:'one',customer_name:'Visible Customer',can_select:true}],has_next:false}})};
  },
  frappe:{msgprint(){},csrf_token:'test',posteingang:{},get_route_str:()=> 'Form/Incoming Letter/letter',user:{has_role:()=>true},prompt:(field,callback)=>{prompt=callback;},ui:{form:{on(){}},Dialog:class {
   constructor(){this.results=new Element('results');this.fields_dict={results:{$wrapper:this.results}};this.visible=false;this.$wrapper={is:()=>this.visible};dialogs.push(this);}
   show(){}
  }}}
 };
 vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/public/js/posteingang_matching.bundle.js','utf8'),context);
 context.frappe.posteingang.attach_matching({doc:{name:'letter'},is_new:()=>false,is_dirty:()=>false,dashboard:{add_section:()=>wrapper}});
 await new Promise(setImmediate);
 wrapper.children.find(e=>e.textContent==='Find another Mandant').handlers.click();
 await prompt({text:'Visible'});
 await new Promise(setImmediate);
 assert.equal(calls.length,1,'search must start after the shown event');
 const dialog=dialogs[0];dialog.visible=true;await dialog.on_page_show();
 assert.equal(calls.length,2);
 assert.ok(dialog.results.children.some(e=>e.textContent==='Visible Customer'));
 denied=true;await dialog.on_page_show();
 assert.equal(dialog.results.children.length,0);
 assert.equal(wrapper.children.length,0,'permission denial must purge the old matching panel too');
});
