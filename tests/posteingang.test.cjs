const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function harness(transport){
 const context={frappe:{posteingang:{}},crypto:{randomUUID:()=>String(Math.random())}};
 vm.createContext(context);
 vm.runInContext(fs.readFileSync('kanzlei_erp/public/js/posteingang_review.bundle.js','utf8'),context);
 const path='kanzlei_erp/public/js/posteingang_workspace.bundle.js';
 assert.ok(fs.existsSync(path),'AP-09 workspace controller missing');
 vm.runInContext(fs.readFileSync(path,'utf8'),context);
 return new context.frappe.posteingang.WorkspaceClient(transport,()=> 'operator');
}
const state=(letter='one',revision=1)=>({user:'operator',letter,revision,status:'Needs Review',current_run:null,applied_run:null,latest_successful_run:null,capabilities:{enabled:true,customer_read:true,customer_write:true},analysis:{revision,allowed_operations:[]},matching:{revision,allowed_operations:[],selected_customer:null},original:{preview_url:'/safe'},review:{letter,revision,status:'Needs Review',allowed_operations:['save_review','confirm'],document_fields:{sender:null,document_date:null,letter_type:null,summary:null},review:{actions:[],human_verified:false,no_actions_reason:null}}});
test('late workspace replies and failures cannot replace a newer letter',async()=>{
 const requests=[];const client=harness((op,args)=>new Promise((resolve,reject)=>requests.push({op,args,resolve,reject})));
 const a=client.load('one'),b=client.load('two');requests[1].resolve(state('two'));await b;
 requests[0].reject(Error('PERMISSION'));assert.equal(await a,null);assert.equal(client.state.letter,'two');
});
test('polling preserves dirty review and never advances its expected revision',async()=>{
 let remote=state();const client=harness(async()=>remote);await client.load('one');
 client.review.editFields({sender:'Local work'});client.review.humanVerified=true;
 remote=state('one',2);await client.status();
 assert.equal(client.review.documentFields.sender,'Local work');assert.equal(client.review.state.revision,1);
 assert.equal(client.conflict,true);assert.equal(client.review.humanVerified,false);
});
test('definitive access loss or a changed user purges all protected state',async()=>{
 const client=harness(async()=>state());await client.load('one');client.transport=async()=>{throw Error('PERMISSION');};
 await assert.rejects(client.status());assert.equal(client.state,null);assert.equal(client.review.review,null);
 client.transport=async()=>({...state(),user:'someone-else'});await assert.rejects(client.load('one'));assert.equal(client.state,null);
});
test('network failure preserves frozen intent and blocks other mutations',async()=>{
 const calls=[];const client=harness(async(op,args)=>{if(op==='get_letter_workspace')return state();calls.push(JSON.stringify([op,args]));throw Error('NETWORK');});
 await client.load('one');await assert.rejects(client.mutate('review.confirm',{letter:'one',review:{title:'Original'}}));
 await assert.rejects(client.mutate('analysis.cancel',{letter:'one'}),/UNKNOWN_OUTCOME/);
 await assert.rejects(client.retry());assert.equal(calls[0],calls[1]);
});
test('dirty edits prevent source-changing operations and conflict prevents confirmation',async()=>{
 const client=harness(async()=>state());await client.load('one');client.review.editFields({sender:'Local'});
 await assert.rejects(client.mutate('matching.select_customer',{letter:'one'}),/DIRTY_REVIEW/);
 client.conflict=true;await assert.rejects(client.mutate('review.confirm',{}),/REVISION_CONFLICT/);
});
test('suspending invalidates in-flight data without discarding a dirty buffer',async()=>{
 let resolve;const client=harness(async()=>state());await client.load('one');client.review.editFields({sender:'Local'});
 client.transport=()=>new Promise(r=>resolve=r);const pending=client.status();client.suspend();resolve(state('one',2));
 assert.equal(await pending,null);assert.equal(client.review.documentFields.sender,'Local');
});
test('queue rejects older filter replies and exposes no cross-user response',async()=>{
 const calls=[];const client=harness(()=>new Promise(resolve=>calls.push(resolve)));
 const a=client.queue({state:'open'}),b=client.queue({state:'history'});
 calls[1]({user:'operator',items:[{letter:'new'}]});await b;calls[0]({user:'operator',items:[{letter:'old'}]});await a;
 assert.equal(client.rows[0].letter,'new');
});
test('page is operator scoped and includes an accessible three-panel layout',()=>{
 const root='kanzlei_erp/kanzlei_erp/page/posteingang/';
 assert.ok(fs.existsSync(root+'posteingang.json'),'AP-09 Page missing');
 assert.deepEqual(JSON.parse(fs.readFileSync(root+'posteingang.json')).roles,[{role:'Posteingang Operator'}]);
 const css=fs.readFileSync(root+'posteingang.css','utf8');assert.match(css,/@media/);assert.match(css,/focus-visible/);assert.match(css,/var\(--/);
});
test('a loaded letter does not inherit remote status from the previous letter',async()=>{
 const client=harness(async(op,args)=>state(args.letter));await client.load('one');client.remote=state('one',8);
 await client.load('two');assert.equal(client.remote,null);
});
test('a definitive revision conflict retains the draft and exposes a conflict state',async()=>{
 const client=harness(async(op)=>{if(op==='get_letter_workspace')return state();throw Error('REVISION_CONFLICT');});
 await client.load('one');client.review.editFields({sender:'Local work'});
 await assert.rejects(client.mutate('review.save_review',{}),/REVISION_CONFLICT/);
 assert.equal(client.conflict,true);assert.equal(client.review.documentFields.sender,'Local work');
});
test('mutations from a stale letter callback and an inactive page never reach transport',async()=>{
 let writes=0;const client=harness(async op=>{if(op==='get_letter_workspace')return state('two');writes++;});await client.load('two');
 await assert.rejects(client.mutate('analysis.cancel',{letter:'one'}),/STALE_CONTEXT/);assert.equal(writes,0);
 client.suspend();await assert.rejects(client.mutate('analysis.cancel',{letter:'two'}),/STALE_CONTEXT/);assert.equal(writes,0);
});
test('direct route loading cannot silently discard a dirty letter',async()=>{
 const client=harness(async(op,args)=>state(args.letter));await client.load('one');client.review.editFields({sender:'Local'});
 await assert.rejects(client.load('two'),/DIRTY_REVIEW/);assert.equal(client.review.documentFields.sender,'Local');
});
test('visibility refresh removes hidden references without replacing the dirty draft',async()=>{
 let changed=false;
 const client=harness(async(op)=>{const value={...state(),visibility_key:changed ? 'new' : 'old'};value.matching.selected_customer=changed ? null : {customer_name:'Secret'};return value;});
 await client.load('one');client.review.editFields({sender:'Local'});changed=true;await client.status();
 assert.equal(client.state.matching.selected_customer,null);assert.equal(client.review.documentFields.sender,'Local');assert.equal(client.review.state.revision,1);
});
test('Page dialog callbacks are bound to the opening letter, route, sequence and user',()=>{
 const context={frappe:{pages:{posteingang:{}},session:{user:'operator'},get_route:()=>['posteingang','one'],prompt:(fields,callback)=>{context.answer=callback;return{};}},__:(s)=>s};
 vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js','utf8'),context);
 const prototype=vm.runInContext('PosteingangWorkspace.prototype',context);let writes=0;
 const page={visible:true,client:{active:true,sequence:1,state:{letter:'one'},owner:'operator'},onRoute:()=>true,track:d=>d,capture:prototype.capture};
 prototype.promptFields.call(page,[],()=>writes++,'Reason');
 page.client.state={letter:'two'};context.frappe.get_route=()=>['posteingang','two'];context.answer({value:'old reason'});assert.equal(writes,0);
});
test('a late nested visibility fetch cannot change the next letter status',async()=>{
 let referenceResolve;
 const client=harness(async(op,args)=>{if(op==='get_workspace_status')return {...state(),visibility_key:'new',capabilities:{enabled:false,customer_read:true}};if(referenceResolve===undefined && args.letter==='one')return {...state(),visibility_key:'old'};return state(args.letter);});
 await client.load('one');client.review.editFields({sender:'Local'});
 client.references=()=>new Promise(resolve=>referenceResolve=resolve);
 const status=client.status();await new Promise(setImmediate);client.review.dirty=false;await client.load('two');referenceResolve(null);await status;
 assert.equal(client.state.letter,'two');assert.equal(client.state.capabilities.enabled,true);
});
test('a superseded post-mutation load cannot navigate back to a correction',async()=>{
 const routes=[];const context={frappe:{pages:{posteingang:{}},set_route:(...args)=>routes.push(args)}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js','utf8'),context);
 const prototype=vm.runInContext('PosteingangWorkspace.prototype',context);
 const page={visible:true,client:{state:{letter:'one'},mutate:async()=>({replacement:'replacement'}),load:async()=>null},syncControls(){},draw(){assert.fail('must not draw superseded state');},loadQueue:async()=>{},failure:()=>{}};
 assert.equal(await prototype.run.call(page,'review.create_correction',{}),null);assert.equal(routes.length,0);
});
test('terminal analysis does not display a scheduled retry',()=>{
 const context={frappe:{pages:{posteingang:{}},datetime:{str_to_user:()=> '25.10.2026 12:00'}},__:(s)=>s};
 vm.createContext(context);vm.runInContext(fs.readFileSync('kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js','utf8'),context);
 const prototype=vm.runInContext('PosteingangWorkspace.prototype',context);const rendered=[];
 const run={status:'Succeeded',next_attempt_at:'2026-10-25 12:00:00.000001'};
 const page={client:{state:{status:'Completed',capabilities:{enabled:true},analysis:{run}},review:{dirty:false}},notice:{replaceChildren:()=>{rendered.length=0;}},node:(_parent,_tag,text)=>rendered.push(text),syncControls(){}};
 prototype.statusNotice.call(page);assert.deepEqual(rendered,['Completed']);
 run.status='Queued';prototype.statusNotice.call(page);assert.equal(rendered[1],'Next attempt no earlier than 25.10.2026 12:00');
});
