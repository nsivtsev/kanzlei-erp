const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load() {
 const context={frappe:{posteingang:{}}};
 vm.createContext(context);
 const path='kanzlei_erp/public/js/posteingang_simple.bundle.js';
 assert.ok(fs.existsSync(path),'one-click client model missing');
 vm.runInContext(fs.readFileSync(path,'utf8'),context);
 return context.frappe.posteingang;
}
const deadline=(kind,date=null)=>({kind,date,wording:'',page:1,quote:'',verified:kind==='explicit_date'});
const task=(over={})=>({source_index:0,title:'T',description:'D',deadline:deadline('explicit_date','2026-12-01'),internal_date:'2026-11-27',assigned_to:'a@x.de',assigned_to_name:'A',...over});
const card=(over={})=>({state:'proposal',run:'r',customer:{suggested:{customer:'C1',customer_name:'One',why:'',page:1},candidates:[],ambiguity:'none'},tasks:[task()],no_task_reason:null,undo_until:null,...over});
const ambiguous=()=>card({customer:{suggested:null,candidates:[{customer:'C1',customer_name:'One',why:'',can_select:true},{customer:'C2',customer_name:'Two',why:'',can_select:false}],ambiguity:'multiple_matches'}});
const clone=v=>JSON.parse(JSON.stringify(v));

test('cardChoice preselects suggestion, all tasks and explicit dates in German form',()=>{
 const p=load();
 const c=p.cardChoice(card({tasks:[task(),task({source_index:1,deadline:deadline('relative')})]}));
 assert.deepEqual(clone(c),{customer:'C1',selected:[true,true],deadlines:['01.12.2026',null],manual:null});
 assert.equal(p.cardChoice(ambiguous()).customer,null);
});

test('missing customer is reported until chosen; non-selectable candidate stays missing',()=>{
 const p=load();const k=ambiguous();const c=p.cardChoice(k);
 assert.deepEqual(clone(p.missingFields(k,c)).map(m=>m.key),['customer']);
 assert.ok(p.missingFields(k,c)[0].text);
 c.customer='C2';assert.deepEqual(clone(p.missingFields(k,c)).map(m=>m.key),['customer']);
 c.customer='C1';assert.deepEqual(clone(p.missingFields(k,c)),[]);
});

test('relative deadline needs user date; payload nulls internal_date',()=>{
 const p=load();
 const k=card({tasks:[task({deadline:deadline('relative')})]});const c=p.cardChoice(k);
 assert.deepEqual(clone(p.missingFields(k,c)).map(m=>m.key),['deadline:0']);
 assert.throws(()=>p.simplePayload(k,c),/MISSING/);
 c.deadlines[0]='10.12.2026';
 assert.deepEqual(clone(p.missingFields(k,c)),[]);
 const out=clone(p.simplePayload(k,c));
 assert.deepEqual(out,{origin:'proposal',customer:'C1',tasks:[{source_index:0,title:'T',description:'D',external_date:'2026-12-10',internal_date:null,assigned_to:'a@x.de'}]});
});

test('explicit task keeps card internal_date; unselected tasks are omitted and not required',()=>{
 const p=load();
 const k=card({tasks:[task(),task({source_index:1,deadline:deadline('uncertain')})]});const c=p.cardChoice(k);
 c.selected[1]=false;
 assert.deepEqual(clone(p.missingFields(k,c)),[]);
 const out=p.simplePayload(k,c);
 assert.equal(out.tasks.length,1);assert.equal(out.tasks[0].external_date,'2026-12-01');assert.equal(out.tasks[0].internal_date,'2026-11-27');
});

test('invalid or impossible dates throw INVALID_DATE',()=>{
 const p=load();
 const k=card({tasks:[task({deadline:deadline('relative')})]});
 for(const bad of['31.02.2027','2026-12-10','1.2.2026','abc']){
  const c=p.cardChoice(k);c.deadlines[0]=bad;
  assert.throws(()=>p.simplePayload(k,c),/INVALID_DATE/,bad);
 }
});

test('primaryAction: accept, count, archive, manual',()=>{
 const p=load();
 const k=card({tasks:[task(),task({source_index:1})]});const c=p.cardChoice(k);
 assert.deepEqual(clone(p.primaryAction(k,c)),{kind:'accept',label:'Accept {0} tasks',count:2});
 c.selected[1]=false;assert.deepEqual(clone(p.primaryAction(k,c)),{kind:'accept',label:'Accept',count:1});
 c.selected[0]=false;assert.deepEqual(clone(p.primaryAction(k,c)),{kind:'archive',label:'File without task',count:0});
 const out=p.simplePayload(k,c);assert.deepEqual(clone(out.tasks),[]);
 for(const state of['failed','not_started'])
  assert.deepEqual(clone(p.primaryAction(card({state,tasks:[]}),p.cardChoice(card({state,tasks:[]})))),{kind:'manual',label:'Create manually',count:0});
 assert.equal(p.primaryAction(card({tasks:[]}),p.cardChoice(card({tasks:[]}))).kind,'archive');
});

test('manualPayload maps rows',()=>{
 const p=load();
 const out=clone(p.manualPayload({customer:'C1',rows:[{title:'A',description:'d',external_date:'10.12.2026',internal_date:'2026-12-08',assigned_to:null}]}));
 assert.deepEqual(out,{origin:'manual',customer:'C1',tasks:[{source_index:null,title:'A',description:'d',external_date:'2026-12-10',internal_date:'2026-12-08',assigned_to:null}]});
 assert.throws(()=>p.manualPayload({customer:null,rows:[]}),/MISSING/);
});

test('listChip follows the card state of each row',()=>{
 const p=load();
 const one={ambiguity:'none',candidates:[{customer:'C1',can_select:true,conflicts:[]}]};
 const ok={card_state:'proposal',matching:one,deadline:{relative_count:0,uncertain_count:0}};
 const t=row=>clone(p.listChip(row));
 assert.deepEqual(t({status:'Queued',card_state:'analyzing'}),{tone:'busy',text:'Analysing'});
 assert.deepEqual(t({status:'Error',card_state:'failed'}),{tone:'fail',text:'Check manually'});
 assert.deepEqual(t({status:'Completed',card_state:'done'}),{tone:'done',text:'Done'});
 assert.deepEqual(t({status:'Received',card_state:'not_started'}),{tone:'need',text:'Not analysed'});
 assert.equal(t({status:'Received',card_state:'not_started',analysis:{status:'processing'}}).tone,'busy');
 assert.deepEqual(t({status:'Needs Review',card_state:'draft'}),{tone:'need',text:'Draft'});
 assert.deepEqual(t({status:'Needs Review',...ok}),{tone:'ready',text:'Proposal ready'});
 // An unassessed or unreadable letter in Needs Review is failed, never "ready".
 assert.deepEqual(t({status:'Needs Review',...ok,card_state:'failed'}),{tone:'fail',text:'Check manually'});
 assert.deepEqual(t({status:'Needs Review',card_state:'not_started',matching:null,deadline:null}),{tone:'need',text:'Not analysed'});
 assert.deepEqual(t({...ok,matching:{ambiguity:'multiple_matches',candidates:[]}}),{tone:'need',text:'Input needed'});
 assert.equal(t({...ok,deadline:{relative_count:1,uncertain_count:0}}).tone,'need');
 assert.equal(t({...ok,matching:{ambiguity:'none',candidates:[{customer:'C1',can_select:true,conflicts:[{code:'name_mismatch',identifier_type:null,evidence:[]}]}]}}).tone,'need','a conflicted Mandant is not preselected');
 assert.equal(t({...ok,matching:{ambiguity:'none',candidates:[{customer:'C1',can_select:false,conflicts:[]}]}}).tone,'need');
 assert.equal(t({...ok,result_error:'x'}).tone,'fail');
});
