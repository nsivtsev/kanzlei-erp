const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness(transport) {
 const context={frappe:{posteingang:{}},crypto:{randomUUID:()=>`request-${Math.random()}`},setTimeout,clearTimeout};
 vm.createContext(context);
 const path='kanzlei_erp/public/js/posteingang_review.bundle.js';
 assert.ok(fs.existsSync(path),'AP-07 review client missing');
 vm.runInContext(fs.readFileSync(path,'utf8'),context);
 return new context.frappe.posteingang.ReviewClient(transport);
}
const state=()=>({schema_version:'1.0',letter:'letter',revision:7,status:'Needs Review',applied_run:'old',proposal:{run:'new',extraction:{actions:[]}},document_fields:{sender:null,document_date:null,letter_type:null,summary:null},review:{schema_version:'1.0',mode:'draft',expected_revision:7,selected_customer:'customer',customer_reason:'checked',human_verified:false,no_actions_reason:null,clarification_reason:null,actions:[{action_id:'server-id',source_index:0,title:'Action',description:'Do it',decision:'pending',assigned_to:null,assignment_decision:'pending',assignment_reason:null,external_date:null,external_date_basis:null,internal_date:null,scheduling_decision:'pending',scheduling_reason:null,rejection_reason:null,needs_planning:true,evidence:[]}]},allowed_operations:['save_review','confirm']});
const form=()=>({doc:{name:'letter',revision:7},is_dirty:()=>false});

test('load rejects a late response and clears protected state on lost permission',async()=>{
 let resolve;
 const client=harness(()=>new Promise(r=>{resolve=r;}));
 let active=true;
 const pending=client.load(form(),()=>active);
 active=false;resolve(state());
 assert.equal(await pending,null);assert.equal(client.state,null);
 client.transport=async()=>state();await client.load(form(),()=>true);
 client.transport=async()=>{throw Error('PERMISSION');};
 await assert.rejects(client.load(form(),()=>true),/PERMISSION/);
 assert.equal(client.state,null);assert.equal(client.review,null);
});

test('edits reset explicit human and retained-result confirmation',async()=>{
 const client=harness(async()=>state());await client.load(form(),()=>true);
 client.humanVerified=true;client.retainedResult={run:'new',reason:'Keep reviewed work'};
 client.editAction('server-id',{title:'Updated'});
 assert.equal(client.review.actions[0].title,'Updated');
 assert.equal(client.humanVerified,false);assert.equal(client.retainedResult,null);assert.equal(client.dirty,true);
 assert.equal(client.review.actions[0].action_id,'server-id');
 assert.throws(()=>client.editAction('server-id',{action_id:'forged'}));
});

test('lost confirmation response reuses frozen payload and blocks a double click',async()=>{
 const calls=[];let reject;
 const client=harness((operation,args)=>{calls.push({operation,args});return new Promise((resolve,r)=>{reject=r;});});
 client.adopt(state());client.humanVerified=true;
 client.retainedResult={run:'new',reason:'Already reviewed'};
 const first=client.submit('confirm');
 assert.equal(await client.submit('confirm'),null);
 reject(Error('NETWORK'));await assert.rejects(first);
 const second=client.submit('confirm');
 assert.equal(calls[0].args.request_key,calls[1].args.request_key);
 assert.equal(JSON.stringify(calls[0].args),JSON.stringify(calls[1].args));
 assert.equal(calls[1].args.review.human_verified,true);
 reject(Error('NETWORK'));await assert.rejects(second);
});

test('a definitive rejection ends an intent and saving never persists the human checkbox',async()=>{
 const calls=[];let fail=true;
 const client=harness(async(operation,args)=>{calls.push(args);if(fail)throw Error('REVISION_CONFLICT');return {revision:8};});
 client.adopt(state());client.humanVerified=true;
 await assert.rejects(client.submit('save_review'));
 fail=false;await client.submit('save_review');
 assert.notEqual(calls[0].request_key,calls[1].request_key);
 assert.equal(calls[1].review.human_verified,false);
 assert.equal(calls[1].review.mode,'draft');
});

test('proposed date is accepted only explicitly and action omissions remain pending',async()=>{
 const client=harness(async()=>state());client.adopt(state());
 assert.equal(client.review.actions[0].external_date,null);
 client.acceptDate('server-id','2026-12-01','Read on page 1');
 assert.equal(client.review.actions[0].external_date,'2026-12-01');
 assert.equal(client.review.actions[0].external_date_basis,'Read on page 1');
 assert.equal(client.review.actions[0].decision,'pending');
 assert.equal(client.review.actions[0].internal_date,null);
});

test('latest analysis is never shown as the proposal of an older reviewed action',()=>{
 const client=harness();const value=state();
 value.proposal.extraction.actions=[{deadline:{kind:'explicit_date',date:'2030-01-01'}}];
 client.adopt(value);
 assert.equal(client.proposalFor('server-id'),null);
 value.action_proposals={'server-id':{deadline:{kind:'relative',date:null,wording:'Within a month'}}};
 client.adopt(value);
 assert.equal(client.proposalFor('server-id').deadline.kind,'relative');
});

test('review buffer prevents an analysis poll from discarding edits',async()=>{
 const context={frappe:{posteingang:{}},crypto:{randomUUID:()=>''},setTimeout,clearTimeout};vm.createContext(context);
 vm.runInContext(fs.readFileSync('kanzlei_erp/public/js/posteingang_jobs.bundle.js','utf8'),context);
 let reads=0,reloads=0;
 const client=new context.frappe.posteingang.AnalysisClient(async()=>{reads++;return{status:'Processing',revision:8};});
 const frm={...form(),__posteingang_review_dirty:true,reload_doc:()=>reloads++};
 assert.equal(await client.refresh(frm),null);assert.equal(reads,0);assert.equal(reloads,0);
});

test('definitive access loss purges all panels, closes review and invalidates replies',()=>{
 const context={frappe:{posteingang:{},set_route:(...route)=>{context.route=route;}},crypto:{randomUUID:()=> 'request'},setTimeout,clearTimeout};vm.createContext(context);
 vm.runInContext(fs.readFileSync('kanzlei_erp/public/js/posteingang_review.bundle.js','utf8'),context);
 const client=new context.frappe.posteingang.ReviewClient();client.adopt(state());client.dirty=true;
 let emptied=0,hidden=0,analysisPurged=0;
 const frm={__posteingang_review_dirty:true,_ap07:{active:true,client,wrapper:{empty:()=>emptied++},dialog:{hide:()=>hidden++}},_ap06:{active:true,client:{sequence:4},wrapper:{empty:()=>emptied++}},_ap05:{active:true,revoke:()=>analysisPurged++}};
 context.frappe.posteingang.revoke_view(frm);
 assert.equal(emptied,2);assert.equal(hidden,1);assert.equal(analysisPurged,1);
 assert.equal(client.state,null);assert.equal(client.review,null);assert.equal(frm.__posteingang_review_dirty,false);
 assert.equal(frm._ap06.client.sequence,5);assert.equal(frm._ap05.active,false);
 assert.equal(JSON.stringify(context.route),JSON.stringify(['List','Incoming Letter']));
});

test('analysis and matching purge on a current permission denial, never on network failure',async()=>{
 let purges=0;
 const context={frappe:{posteingang:{revoke_view:()=>purges++},ui:{form:{on(){}}}},crypto:{randomUUID:()=> 'request'},setTimeout,clearTimeout};vm.createContext(context);
 for(const name of ['jobs','matching'])vm.runInContext(fs.readFileSync(`kanzlei_erp/public/js/posteingang_${name}.bundle.js`,'utf8'),context);
 for(const code of ['NETWORK','PERMISSION']){
  const transport=async()=>{throw Error(code);};
  await assert.rejects(new context.frappe.posteingang.AnalysisClient(transport).refresh(form()));
  await assert.rejects(new context.frappe.posteingang.MatchingClient(transport).load(form(),()=>true,()=>{}));
  assert.equal(purges,code==='NETWORK' ? 0 : 2);
 }
});

test('replacement multipart omits unknown date instead of sending the string null',()=>{
 const context={frappe:{posteingang:{}},crypto:{randomUUID:()=> 'request'},setTimeout,clearTimeout};vm.createContext(context);
 vm.runInContext(fs.readFileSync('kanzlei_erp/public/js/posteingang_review.bundle.js','utf8'),context);
 class Data {constructor(){this.rows=[];}append(key,value){this.rows.push([key,value]);}}
 const file={name:'synthetic.pdf'};
 const data=context.frappe.posteingang.correction_form_data({received_date_known:false,received_date:null,reuse_original:false},file,'key',Data);
 assert.equal(data.rows.some(([key])=>key==='received_date'),false);
 assert.equal(data.rows.find(([key])=>key==='received_date_known')[1],'false');
 assert.equal(data.rows.find(([key])=>key==='file')[1],file);
 assert.equal(data.rows.find(([key])=>key==='request_key')[1],'key');
});

test('the form shows proposal uncertainty as text and marks input dirty before blur',async()=>{
 class Element {
  constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.textContent='';}
  append(child){this.children.push(child);}empty(){this.children=[];}
  addEventListener(name,callback){this.handlers[name]=callback;}
 }
 const wrapper=new Element('section'),dialogs=[],data=state();
 data.review.actions[0].assigned_to='old-assignee';data.review.actions[0].internal_date='2026-12-01';
 data.proposal.extraction={actions:[],readability:'unreadable',action_assessment:'unknown',warnings:[{message:'<img src=x onerror=attack()>',evidence:[]}],summary:{value:null,evidence:[]}};
 const context={document:{createElement:tag=>new Element(tag)},crypto:{randomUUID:()=> 'request'},__:value=>value,setTimeout,clearTimeout,
 fetch:async()=>({ok:true,json:async()=>({message:data})}),frappe:{posteingang:{},csrf_token:'token',get_route_str:()=> 'Form/Incoming Letter/letter',ui:{Dialog:class{
  constructor(){this.fields_dict={review:{$wrapper:new Element('review')}};dialogs.push(this);}
  show(){}hide(){}
 }}}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/public/js/posteingang_review.bundle.js','utf8'),context);
 const frm={...form(),is_new:()=>false,dashboard:{add_section:()=>wrapper}};
 context.frappe.posteingang.attach_review(frm);await new Promise(setImmediate);
 const descendants=element=>[element,...element.children.flatMap(descendants)];
 assert.ok(descendants(wrapper).some(e=>e.textContent==='<img src=x onerror=attack()>'));
 wrapper.children.find(e=>e.textContent==='Review incoming letter').handlers.click();
 const dialog=dialogs[0],elements=descendants(dialog.fields_dict.review.$wrapper);
 const human=elements.find(e=>e.textContent==='I checked the original and all final decisions').children[0];
 human.checked=true;human.handlers.change();
 const summary=elements.find(e=>e.textContent==='Summary').children[0];summary.value='Human summary';summary.handlers.input();
 assert.equal(frm.__posteingang_review_dirty,true);assert.equal(human.checked,false);
 assert.equal(frm._review_client.documentFields.summary,'Human summary');
 const assignment=elements.find(e=>e.textContent==='Assignment decision').children[0];assignment.value='unassigned';assignment.handlers.change();
 assert.equal(elements.find(e=>e.tag==='p' && e.textContent==='old-assignee'),undefined,'the visible assignee must be cleared too');
 const scheduling=elements.find(e=>e.textContent==='Scheduling decision').children[0];scheduling.value='unscheduled';scheduling.handlers.change();
 assert.equal(elements.find(e=>e.textContent==='Internal work date').children[0].value,'');
 context.frappe.posteingang.attach_review(frm);
 assert.equal(frm._review_client.documentFields.summary,'Human summary','same-document refresh must retain the buffer');
});
