const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function harness(transport){
 const path='kanzlei_erp/public/js/posteingang_source.bundle.js';
 assert.ok(fs.existsSync(path),'AP-08 Task source client missing');
 const context={frappe:{posteingang:{}},__:(s,args=[])=>s.replace('{0}',args[0])};
 vm.createContext(context);vm.runInContext(fs.readFileSync(path,'utf8'),context);
 return {client:new context.frappe.posteingang.TaskSourceClient(transport),context};
}
const state=()=>({schema_version:'1.0',available:true,letter:{name:'letter',quarantined:false},action:{external_date:null,external_date_basis:null,evidence:[{page:1,quote:'<img onerror=attack()>',verification:'unverified'}]},original:{file_id:'file',file_name:'synthetic.pdf',page_count:1},preview_url:'/api/method/source?task=task&disposition=inline',download_url:'/api/method/source?task=task&disposition=attachment'});
test('late source response after leaving the Task cannot repopulate it',async()=>{
 let resolve,active=true;const {client}=harness(()=>new Promise(r=>resolve=r));
 const promise=client.load('task',()=>active);active=false;resolve(state());
 assert.equal(await promise,null);assert.equal(client.state,null);
});
test('a newer request wins and every refresh clears the old protected source',async()=>{
 const pending=[];const {client}=harness(()=>new Promise(r=>pending.push(r)));
 const first=client.load('old',()=>true);const second=client.load('new',()=>true);
 pending[1](state());await second;pending[0]({...state(),letter:{name:'wrong'}});await first;
 assert.equal(client.state.letter.name,'letter');
 client.transport=async()=>{throw Error('PERMISSION');};
 await assert.rejects(client.load('new',()=>true));assert.equal(client.state,null);
});
test('ordinary Task has no source and page links are constrained to the PDF',async()=>{
 const {client}=harness(async()=>({available:false}));await client.load('ordinary',()=>true);
 assert.equal(client.state.available,false);
 client.state=state();assert.equal(client.pageUrl(1),state().preview_url+'#page=1');
 for(const page of [0,2,'1',-1])assert.equal(client.pageUrl(page),null);
});
test('an older failed request cannot clear a newer successful source',async()=>{
 const pending=[];const {client}=harness(()=>new Promise((resolve,reject)=>pending.push({resolve,reject})));
 const first=client.load('task',()=>true);const second=client.load('task',()=>true);
 pending[1].resolve(state());await second;pending[0].reject(Error('PERMISSION'));
 assert.equal(await first,null);assert.equal(client.state.letter.name,'letter');
});
test('Task rendering uses text, keeps unknown deadline explicit and does not save or reload fields',async()=>{
 class Element{constructor(tag){this.tag=tag;this.children=[];this.textContent='';}append(child){this.children.push(child);}empty(){this.children=[];}closest(){return{remove(){}};}addEventListener(name,cb){this[name]=cb;}}
 const box=new Element('section');const {context}=harness();
 context.document={createElement:tag=>new Element(tag)};
 context.frappe.call=async()=>({message:state()});context.frappe.get_route_str=()=> 'Form/Task/task';
 context.frappe.set_route=()=>{};
 const doc={name:'task'},frm={doc,is_new:()=>false,dashboard:{add_section:()=>box},reload_doc:()=>assert.fail('must not reload Task'),save:()=>assert.fail('must not save Task')};
 context.frappe.posteingang.attach_task_source(frm);await new Promise(setImmediate);
 const all=e=>[e,...e.children.flatMap(all)];
 assert.ok(all(box).some(e=>e.textContent==='External deadline is not confirmed'));
 assert.ok(all(box).some(e=>e.textContent==='<img onerror=attack()>'));
 assert.equal(frm.doc,doc);
});
