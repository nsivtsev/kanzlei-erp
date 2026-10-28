const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function api(){const context={frappe:{posteingang:{}}};vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/public/js/posteingang_workspace.bundle.js','utf8'),context);return context.frappe.posteingang;}
const state=()=>({matching:{selected_customer:{customer:'customer',customer_name:'Synthetic'},customer_reason:'Original checked',matching:{ambiguity:'none'}}});
const action=()=>({action_id:'one',title:'Reply',description:'Prepare a reply',decision:'accept',assigned_to:'operator',assignment_decision:'assigned',internal_date:'2026-11-05',scheduling_decision:'scheduled',external_date:null});
test('document step requires an explicit Mandant and reason, including with one candidate',()=>{
 const a=api(),s=state();s.matching.selected_customer=null;
 assert.ok(a.reviewIssues(s,{actions:[]},0).length);
 s.matching.selected_customer={customer:'customer'};s.matching.customer_reason='  ';
 assert.ok(a.reviewIssues(s,{actions:[]},0).length);
 s.matching.customer_reason='Checked';assert.equal(a.reviewIssues(s,{actions:[]},0).length,0);
 s.matching.matching.ambiguity='multiple_addressees';assert.ok(a.reviewIssues(s,{actions:[]},0).length);
});
test('every action needs a decision and rejected actions need a reason',()=>{
 const a=api(),r={actions:[{...action(),decision:'pending'}]};
 assert.ok(a.reviewIssues(state(),r,1).length);
 r.actions[0]={...action(),decision:'reject'};r.no_actions_reason='No work needed';
 assert.ok(a.reviewIssues(state(),r,1).length);
 r.actions[0].rejection_reason='Already done';assert.equal(a.reviewIssues(state(),r,1).length,0);
});
test('accepted actions allow explicit later planning only with reasons',()=>{
 const a=api(),r={actions:[{...action(),assigned_to:null,internal_date:null,assignment_decision:'unassigned',scheduling_decision:'unscheduled'}]};
 assert.equal(a.reviewIssues(state(),r,1).length,2);
 Object.assign(r.actions[0],{assignment_reason:'Awaiting colleague',scheduling_reason:'Date to be agreed'});
 assert.equal(a.reviewIssues(state(),r,1).length,0);
});
test('internal dates never count as confirmed external dates and external dates require a basis',()=>{
 const a=api(),r={actions:[action()]};assert.equal(a.reviewIssues(state(),r,1).length,0);
 r.actions[0].external_date='2026-11-12';assert.ok(a.reviewIssues(state(),r,1).some(x=>x.message.includes('basis')));
 r.actions[0].external_date_basis='Original page 1';assert.equal(a.reviewIssues(state(),r,1).length,0);
});
test('an empty analysis is not a decision to finish without tasks',()=>{
 const a=api(),r={actions:[]};assert.ok(a.reviewIssues(state(),r,1).length);
 r.no_actions_reason='Original checked; informational letter';assert.equal(a.reviewIssues(state(),r,1).length,0);
 r.actions=[action()];assert.ok(a.reviewIssues(state(),r,1).length,'stale no-actions reason must not pass confirmation');
});
test('failed save cannot advance the review and a late save cannot advance another letter',async()=>{
 const context={frappe:{pages:{posteingang:{}}}};vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js','utf8'),context);
 const proto=vm.runInContext('PosteingangWorkspace.prototype',context);
 const page={step:0,visible:true,validateStep:()=>true,client:{state:{letter:'one'}},submit:async()=>null,draw:()=>assert.fail('must not advance')};
 await proto.nextStep.call(page);assert.equal(page.step,0);
 page.submit=async()=>{page.client.state={letter:'two'};return {};};await proto.nextStep.call(page);assert.equal(page.step,0);
});
test('a task hidden by current Task permissions is not reported as an outcome without tasks',()=>{
 const messages=[];const context={frappe:{pages:{posteingang:{}}},__:(s,args)=>args?s.replace('{0}',args[0]):s};vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js','utf8'),context);
 const proto=vm.runInContext('PosteingangWorkspace.prototype',context);
 const page={client:{state:{letter:'one',status:'Completed',matching:{selected_customer:{customer_name:'Synthetic'}},review:{receipt:{actions:[{task:null,action_id:'one'}]}}},review:{review:{no_actions_reason:null}}},editor:{},node:(_p,_t,s)=>{messages.push(s);return {};},button:()=>({}),drawReceipt(){},drawCorrection(){}};
 proto.drawResult.call(page);assert.ok(!messages.some(s=>s?.includes('No tasks will be created')));assert.ok(messages.some(s=>s?.includes('Tasks created')));
});
test('continuing a received letter enters human review only after its draft was saved',async()=>{
 const calls=[];const context={frappe:{pages:{posteingang:{}}}};vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js','utf8'),context);
 const proto=vm.runInContext('PosteingangWorkspace.prototype',context);
 const page={step:0,visible:true,validateStep:()=>true,client:{state:{letter:'one',revision:2,status:'Received',analysis:{allowed_operations:['manual_review']}}},submit:async()=>{calls.push('save');return {};},run:async(operation)=>{calls.push(operation);page.client.state.status='Needs Review';return {};},draw(){}};
 await proto.nextStep.call(page);assert.deepEqual(calls,['save','analysis.manual_review']);assert.equal(page.step,1);
});
test('page search waits for the shown dialog before reading a fast result',async()=>{
 const root={rows:[],replaceChildren(){this.rows=[];}},dialogs=[];let reads=0;
 const context={__:(s)=>s,frappe:{pages:{posteingang:{}},ui:{Dialog:class{
  constructor(){this.visible=false;this.fields_dict={results:{$wrapper:[root]}};this.$wrapper={is:()=>this.visible};dialogs.push(this);}
  set_primary_action(){} get_value(){return '';} show(){}
 }}}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js','utf8'),context);
 const proto=vm.runInContext('PosteingangWorkspace.prototype',context);
 const page={client:{state:{letter:'one'},sequence:1},dialogs:new Set(),capture:()=>()=>true,transport:async()=>{reads++;return {rows:[{customer:'customer',customer_name:'Synthetic',can_select:true}]};},node:(_p,_t,s)=>{root.rows.push(s);return {};},button(){},failure:assert.fail};
 await proto.search.call(page,'customers',()=>{});assert.equal(reads,0);
 dialogs[0].visible=true;await dialogs[0].on_page_show();assert.equal(reads,1);assert.ok(root.rows.includes('Synthetic'));
});
test('a date blur updates the review before Frappe finishes its async control callback',()=>{
 const input={value:'',disabled:false,dataset:{},handlers:{},setAttribute(){},addEventListener(name,callback){this.handlers[name]=callback;}};
 const wrap={isConnected:true},changes=[];
 const field={$input:[input],set_input(value){input.value=value;},get_value(){return input.value;}};
 const context={__:(s)=>s,frappe:{pages:{posteingang:{}},datetime:{validate:()=>true},ui:{form:{make_control:()=>field}}}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js','utf8'),context);
 const proto=vm.runInContext('PosteingangWorkspace.prototype',context);
 proto.control.call({node:()=>wrap,fieldIndex:0},{},'Internal work date','2026-11-01',value=>changes.push(value),{type:'date'});
 input.value='2026-11-05';input.handlers.change?.();assert.deepEqual(changes,['2026-11-05']);
});
test('native controls receive their initial document value before datepicker initialization',()=>{
 let options;const input={disabled:false,dataset:{},setAttribute(){},addEventListener(){}};
 const context={__:(s)=>s,frappe:{pages:{posteingang:{}},ui:{form:{make_control:opts=>{options=opts;return {$input:[input],set_input(){}};}}}}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js','utf8'),context);
 const proto=vm.runInContext('PosteingangWorkspace.prototype',context);
 proto.control.call({node:()=>({isConnected:true}),fieldIndex:0},{},'Internal work date','2026-11-01',()=>{},{type:'date'});
 assert.equal(options.doc?.[options.df.fieldname],'2026-11-01');
});
test('a transition cannot validate or submit while a letter is still loading',()=>{
 const context={frappe:{pages:{posteingang:{}}}};vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js','utf8'),context);
 const proto=vm.runInContext('PosteingangWorkspace.prototype',context);
 assert.equal(proto.validateStep.call({client:{state:null,review:{review:null}}},0),false);
});
