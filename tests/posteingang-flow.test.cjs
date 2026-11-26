const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const PAGE='kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js';

/* A small DOM: enough for the page's createElement/append/textContent/listener use. */
function makeDocument(){
 class Text{constructor(value){this.parentNode=null;this.textContent=String(value);}}
 class Element{
  constructor(tag){this.tagName=tag.toUpperCase();this.childNodes=[];this.parentNode=null;this.attributes={};this.dataset={};this.style={};this.listeners={};
   this.className='';this.hidden=false;this.disabled=false;this.value='';this.checked=false;this.open=false;this.id='';this._text='';this.innerHTML='';}
  get children(){return this.childNodes.filter(n=>n instanceof Element);}
  get textContent(){return this._text+this.childNodes.map(n=>n.textContent).join('');}
  set textContent(value){this._text=String(value ?? '');this.childNodes=[];}
  get classList(){const el=this,list=()=>el.className.split(/\s+/).filter(Boolean);
   return {add:(...c)=>{el.className=[...new Set([...list(),...c])].join(' ');},remove:(...c)=>{el.className=list().filter(x=>!c.includes(x)).join(' ');},
    contains:c=>list().includes(c),toggle:(c,force)=>{const on=force ?? !list().includes(c);if(on)el.classList.add(c);else el.classList.remove(c);return on;}};}
  append(...nodes){for(let node of nodes){if(!(node instanceof Element) && !(node instanceof Text))node=new Text(node);node.parentNode?.childNodes.splice(node.parentNode.childNodes.indexOf(node),1);node.parentNode=this;this.childNodes.push(node);}}
  appendChild(node){this.append(node);return node;}
  replaceChildren(...nodes){for(const node of this.childNodes)node.parentNode=null;this.childNodes=[];this.append(...nodes);}
  remove(){if(this.parentNode){this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this),1);this.parentNode=null;}}
  setAttribute(key,value){this.attributes[key]=String(value);if(key==='id')this.id=String(value);}
  getAttribute(key){return key in this.attributes ? this.attributes[key] : null;}
  removeAttribute(key){delete this.attributes[key];}
  hasAttribute(key){return key in this.attributes;}
  addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);}
  removeEventListener(type,fn){this.listeners[type]=(this.listeners[type] || []).filter(f=>f!==fn);}
  fire(type,extra={}){for(const fn of this.listeners[type] || [])fn({type,target:this,currentTarget:this,preventDefault(){},stopPropagation(){},...extra});}
  click(){if(!this.disabled)this.fire('click');}
  focus(){doc.activeElement=this;}
  scrollIntoView(){}
  contains(node){for(let n=node;n;n=n.parentNode)if(n===this)return true;return false;}
  get isConnected(){for(let n=this;n;n=n.parentNode)if(n===doc.body)return true;return false;}
 }
 const doc={hidden:false,activeElement:null,createElement:tag=>new Element(tag),createTextNode:value=>new Text(value),addEventListener(){},
  getElementById(id){return all(doc.body).find(n=>n.id===id) || null;}};
 doc.body=new Element('body');
 return doc;
}
function all(root){const out=[];const walk=n=>{for(const c of n.children){out.push(c);walk(c);}};walk(root);return out;}
const text=root=>root.textContent;
const buttons=(root,label)=>all(root).filter(n=>n.tagName==='BUTTON' && n.textContent.trim()===label);
const button=(root,label)=>{const found=buttons(root,label);assert.equal(found.length,1,`one button "${label}" expected, found ${found.length} in: ${text(root)}`);return found[0];};
const settle=()=>new Promise(resolve=>setImmediate(resolve));

const deadline=(kind,over={})=>({kind,date:kind==='explicit_date' ? '2026-12-01' : null,wording:kind==='relative' ? 'innerhalb eines Monats nach Bekanntgabe' : null,page:1,quote:'bis zum 01.12.2026',verified:true,...over});
const task=(over={})=>({source_index:0,title:'Belege senden',description:'Rechnungen einreichen',deadline:deadline('explicit_date'),internal_date:'2026-11-24',assigned_to:'operator',assigned_to_name:'Operator',...over});
const card=(over={})=>({state:'proposal',run:'run1',customer:{suggested:{customer:'C1',customer_name:'Muster GmbH',why:'Steuernummer im Schreiben',page:1},candidates:[{customer:'C1',customer_name:'Muster GmbH',why:'Steuernummer im Schreiben',can_select:true}],ambiguity:'none'},
 tasks:[task()],no_task_reason:null,undo_until:null,...over});
function workspace(letter='one',over={}){
 const {card:cardOver,review:reviewOver,analysis:analysisOver,...rest}=over;
 return {user:'operator',letter,revision:1,status:'Needs Review',current_run:'run1',latest_successful_run:'run1',applied_run:'run1',visibility_key:'v',
  capabilities:{enabled:true,customer_read:true,customer_write:true},
  analysis:{allowed_operations:['cancel'],run:null,...analysisOver},
  matching:{allowed_operations:[],selected_customer:null,matching:null},
  original:{file_name:letter+'.pdf',preview_url:'/preview/'+letter,download_url:'/download/'+letter,page_count:2,received_date_known:false,received_date:null},
  review:{letter,revision:1,status:'Needs Review',allowed_operations:['save_review'],document_fields:{sender:'Finanzamt '+letter,document_date:null,letter_type:'Anforderung',summary:'Das Finanzamt fordert Belege an.'},
   review:{actions:[]},proposal:null,receipt:null,correction:null,...reviewOver},
  card:card(cardOver),...rest};
}

function harness({letters={one:workspace('one')},route=['posteingang','one'],handler}={}){
 const document=makeDocument(),calls=[],routes=[],prompts=[],opened=[],timers=[],dialogs=[];
 let current=route;
 const frappe={pages:{posteingang:{}},session:{user:'operator'},csrf_token:'t',
  get_route:()=>current,set_route:(...args)=>{routes.push(args);current=args;},router:{on(){}},
  ui:{make_app_page:({parent})=>{const main=document.createElement('div');parent.append(main);return {main:[main],set_title(){},clear_primary_action(){},set_primary_action(){}};},
   Dialog:class{constructor(opts){this.opts=opts;this.visible=false;this.fields_dict={results:{$wrapper:[document.createElement('div')]},upload:{$wrapper:[document.createElement('div')]}};this.$wrapper={is:()=>this.visible};}
    show(){this.visible=true;dialogs.push(this);}hide(){this.visible=false;this.onhide?.();}set_primary_action(){}get_value(){return '';}}},
  prompt:(fields,callback,title)=>{const dialog={fields,callback,title,hide(){}};prompts.push(dialog);return dialog;},
  confirm:(message,callback)=>({message,callback}),show_alert(){},msgprint(){},
  datetime:{str_to_user:value=>{const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(value || '');return m ? `${m[3]}.${m[2]}.${m[1]}` : value;}},
  utils:{get_form_link:(doctype,name)=>`/app/${doctype}/${name}`},
  posteingang:{open:options=>opened.push(options)}};
 const context={frappe,document,window:{addEventListener(){}},console,crypto:{randomUUID:(()=>{let n=0;return ()=>'key-'+(++n);})()},
  setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){},setImmediate,
  __:(s,args)=>args ? String(s).replace(/\{(\d)\}/g,(_m,i)=>args[i]) : s};
 vm.createContext(context);
 for(const file of ['kanzlei_erp/public/js/posteingang_review.bundle.js','kanzlei_erp/public/js/posteingang_workspace.bundle.js','kanzlei_erp/public/js/posteingang_simple.bundle.js',PAGE])
  vm.runInContext(fs.readFileSync(file,'utf8'),context);
 const wrapper=document.createElement('div');document.body.append(wrapper);
 const page=new (vm.runInContext('PosteingangWorkspace',context))(wrapper);
 const queue={open:[],history:[]};
 const transport=async(operation,args)=>{
  calls.push({operation,args:JSON.parse(JSON.stringify(args))});
  const custom=handler && await handler(operation,args,{letters,queue});
  if(custom!==undefined)return custom;
  if(operation==='get_letter_workspace')return JSON.parse(JSON.stringify(letters[args.letter]));
  if(operation==='get_workspace_status')return letters[args.letter];
  if(operation==='get_queue')return {user:'operator',items:queue[args.filters.state],next_cursor:null};
  return {letter:args.letter,revision:2,status:'Completed'};
 };
 page.transport=transport;page.client.transport=transport;
 return {page,document,context,frappe,calls,routes,prompts,opened,letters,queue,timers,dialogs,root:page.root,
  writes:()=>calls.filter(c=>c.operation.includes('.')),
  go:async(...next)=>{current=['posteingang',...next];await page.show();await settle();},
  main:()=>page.main};
}
async function shown(options){const h=harness(options);await h.page.show();await settle();return h;}

test('the three-stage editor is gone and the one-click model is loaded with the page',()=>{
 const source=fs.readFileSync(PAGE,'utf8');
 for(const label of ['Step {0} of 3','Document and Mandant','Tasks and deadlines','Final review','I checked the original and all final decisions','Reason for keeping the current review',
  'Apply latest analysis result','Save and continue','Previous step','Needs clarification','Compare with saved review','Reason for no assignee','Leave assignment for later'])
  assert.ok(!source.includes(label),label);
 assert.match(source,/frappe\.require\(\['posteingang_workspace\.bundle\.js','posteingang_simple\.bundle\.js'\]\)/);
 assert.equal(fs.readFileSync('kanzlei_erp/public/js/posteingang_workspace.bundle.js','utf8').includes('reviewIssues'),false);
});

test('a proposal card shows the Mandant, the cited deadline and the one-click footer',async()=>{
 const h=await shown();const main=text(h.main());
 for(const part of ['Finanzamt one','Anforderung','Received: unknown','Belege senden','Das Finanzamt fordert Belege an.',"Assistant's proposal",'Muster GmbH','Steuernummer im Schreiben',
  'Deadline in the letter','01.12.2026','Work date','24.11.2026','Responsible','Me','bis zum 01.12.2026','Creates 1 task for Muster GmbH.'])assert.ok(main.includes(part),part);
 assert.ok(!main.includes('unverified'));
 assert.equal(buttons(h.main(),'p. 1').length,2,'Mandant and deadline cite page 1');
 button(h.main(),'Accept');button(h.main(),'Create manually');button(h.main(),'Discard document');button(h.main(),'Original');
});

test('a known receipt date is shown read-only in the header',async()=>{
 const one=workspace('one');Object.assign(one.original,{received_date_known:true,received_date:'2026-11-20'});
 const h=await shown({letters:{one}});
 assert.ok(text(h.main()).includes('Received: 20.11.2026'));assert.equal(buttons(h.main(),'change').length,0);
});

test('an unverified deadline quote is marked and cites no page',async()=>{
 const one=workspace('one',{card:{customer:{suggested:{customer:'C1',customer_name:'Muster GmbH',why:'Name im Schreiben',page:null},candidates:[],ambiguity:'none'},tasks:[task({deadline:deadline('explicit_date',{verified:false,page:null})})]}});
 const h=await shown({letters:{one}});
 assert.ok(text(h.main()).includes('unverified'));assert.equal(all(h.main()).filter(n=>n.tagName==='BUTTON' && /^p\. /.test(n.textContent)).length,0);
});

test('a page link opens the original at that page',async()=>{
 const h=await shown();
 buttons(h.main(),'p. 1')[1].click();
 assert.equal(h.root.dataset.pdf,'true');
 const frame=all(h.root).find(n=>n.tagName==='IFRAME');assert.equal(frame.src,'/preview/one#page=1');
 button(h.root,'Back to proposal');
 button(h.main(),'Hide original').click();assert.equal(h.root.dataset.pdf,'false');
});

test('each card state renders its own content and actions',async()=>{
 const cases=[
  [{status:'Processing',card:{state:'analyzing',tasks:[]}},['Usually under a minute. You can work on other documents meanwhile.'],['Accept','Create manually']],
  [{status:'Error',analysis:{allowed_operations:['queue_analysis','cancel']},card:{state:'failed',tasks:[]}},['Automatic analysis failed','This does not mean that no task is needed.','Create manually','Analyse again'],['Accept']],
  [{status:'Received',analysis:{allowed_operations:['queue_analysis','cancel']},card:{state:'not_started',tasks:[]}},['Start analysis','Create manually'],['Accept']],
  [{status:'Received',analysis:{allowed_operations:['cancel']},card:{state:'not_started',tasks:[]}},['Create manually'],['Start analysis','Accept']],
  [{card:{tasks:[],no_task_reason:'Reines Informationsschreiben.'}},['No task needed','Reines Informationsschreiben.','File without task'],['Accept']],
 ];
 for(const [over,present,absent] of cases){
  const h=await shown({letters:{one:workspace('one',over)}});const main=text(h.main());
  for(const part of present)assert.ok(main.includes(part),`${over.card.state}: ${part}`);
  for(const label of absent)assert.equal(buttons(h.main(),label).length,0,`${over.card.state}: ${label}`);
 }
});

test('an analyzing card shows a scheduled retry only while the run is queued',async()=>{
 const run={status:'Queued',next_attempt_at:'2026-10-25 12:00:00'};
 let h=await shown({letters:{one:workspace('one',{status:'Queued',analysis:{run},card:{state:'analyzing',tasks:[]}})}});
 assert.ok(text(h.main()).includes('Next attempt no earlier than 25.10.2026'));
 h=await shown({letters:{one:workspace('one',{status:'Processing',analysis:{run:{...run,status:'Succeeded'}},card:{state:'analyzing',tasks:[]}})}});
 assert.ok(!text(h.main()).includes('Next attempt'));
});

test('a legacy draft opens directly in the prefilled manual form',async()=>{
 const one=workspace('one',{card:{state:'draft',tasks:[task({source_index:null,title:'Draft title',description:'Draft text'})]}});
 const h=await shown({letters:{one}});
 const inputs=all(h.main()).filter(n=>n.tagName==='INPUT' || n.tagName==='TEXTAREA');
 assert.ok(inputs.some(n=>n.value==='Draft title'));assert.ok(inputs.some(n=>n.value==='01.12.2026'));assert.ok(inputs.some(n=>n.value==='24.11.2026'));
 button(h.main(),'Create task');button(h.main(),'File without task');assert.equal(buttons(h.main(),'Cancel').length,0);assert.equal(buttons(h.main(),'Accept').length,0);
 button(h.main(),'Create task').click();await settle();
 const write=h.writes()[0];assert.equal(write.operation,'review.confirm_simple');assert.equal(write.args.origin,'manual');
 assert.deepEqual(write.args.tasks,[{source_index:null,title:'Draft title',description:'Draft text',external_date:'2026-12-01',internal_date:'2026-11-24',assigned_to:'operator'}]);
});

test('Accept sends one confirm_simple on a double click and offers undo in a polite toast',async()=>{
 let resolve;const h=await shown({handler:(operation,args,{letters})=>{
  if(operation==='review.confirm_simple')return new Promise(r=>{resolve=()=>{letters.one=workspace('one',{status:'Completed',revision:2,card:{state:'done',tasks:[],undo_until:'2026-11-26T10:10:00'},review:{receipt:{actions:[{action_id:'a1',task:'TASK-1'}]},review:{actions:[{action_id:'a1',title:'Belege senden'}]},allowed_operations:[]}});r({letter:'one',revision:2,status:'Completed',actions:[{action_id:'a1',task:'TASK-1'}]});};});
 }});
 const accept=button(h.main(),'Accept');accept.click();accept.click();button(h.main(),'Accept').click();
 assert.equal(h.writes().length,1);
 const {args}=h.writes()[0];
 assert.equal(args.letter,'one');assert.equal(args.expected_revision,1);assert.equal(args.origin,'proposal');assert.equal(args.customer,'C1');assert.ok(args.request_key);
 assert.deepEqual(args.tasks,[{source_index:0,title:'Belege senden',description:'Rechnungen einreichen',external_date:'2026-12-01',internal_date:'2026-11-24',assigned_to:'operator'}]);
 resolve();await settle();await settle();
 assert.equal(h.writes().length,1);
 const toast=all(h.root).find(n=>n.getAttribute('aria-live')==='polite');
 assert.ok(toast.textContent.includes('1 task created: Finanzamt one'));button(toast,'Undo');
 const main=text(h.main());assert.ok(main.includes('1 task created'));assert.ok(all(h.main()).some(n=>n.tagName==='A' && n.href==='/app/Task/TASK-1' && n.textContent.includes('Belege senden')));
 button(h.main(),'Undo');
});

test('an unknown outcome is retried with the same request key',async()=>{
 let fail=true;const h=await shown({handler:operation=>{if(operation==='review.confirm_simple' && fail){fail=false;throw Error('NETWORK');}}});
 button(h.main(),'Accept').click();await settle();
 button(h.root,'Check and retry the same request').click();await settle();
 const writes=h.writes();assert.equal(writes.length,2);assert.equal(writes[0].args.request_key,writes[1].args.request_key);
});

test('undo from the toast targets the confirmed letter after switching letters',async()=>{
 const letters={one:workspace('one'),two:workspace('two')};
 const h=await shown({letters,handler:(operation,args)=>{if(operation==='review.confirm_simple'){letters.one=workspace('one',{status:'Completed',revision:2,card:{state:'done',tasks:[],undo_until:'2026-11-26T10:10:00'}});return {letter:'one',revision:2,actions:[]};}}});
 button(h.main(),'Accept').click();await settle();await settle();
 const toast=all(h.root).find(n=>n.getAttribute('aria-live')==='polite');
 await h.go('two');assert.ok(text(h.main()).includes('Finanzamt two'));
 button(toast,'Undo').click();await settle();await settle();
 const undo=h.writes().find(c=>c.operation==='review.undo_confirm');
 assert.equal(undo.args.letter,'one');assert.equal(undo.args.expected_revision,2);assert.deepEqual(h.routes.at(-1),['posteingang','one']);
});

test('a missing Mandant or relative deadline blocks Accept with a hint at the field',async()=>{
 const one=workspace('one',{card:{customer:{suggested:null,candidates:[{customer:'C1',customer_name:'Schmidt Bau',why:'Name passt',can_select:true},{customer:'C2',customer_name:'Schmidt Immobilien',why:'Adresse passt',can_select:false}],ambiguity:'multiple_matches'},
  tasks:[task({deadline:deadline('relative',{quote:'innerhalb eines Monats',page:2})})]}});
 const h=await shown({letters:{one}});
 const radios=all(h.main()).filter(n=>n.type==='radio');assert.equal(radios.length,2);assert.equal(radios[1].disabled,true);assert.ok(radios.every(r=>!r.checked));
 assert.ok(text(h.main()).includes('Not computed'));button(h.main(),'Search other Mandant');
 button(h.main(),'Accept').click();await settle();
 assert.equal(h.writes().length,0);
 const main=text(h.main());assert.ok(main.includes('Still missing:'));assert.ok(main.includes('Select the Mandant.'));assert.ok(main.includes('Enter the deadline date from the letter.'));
 const pick=all(h.main()).filter(n=>n.type==='radio')[0];pick.checked=true;pick.fire('change');
 const input=all(h.main()).find(n=>n.tagName==='INPUT' && n.type==='text');input.value='32.01.2027';input.fire('input');
 button(h.main(),'Accept').click();await settle();assert.equal(h.writes().length,0);assert.ok(text(h.main()).includes('Enter the date as DD.MM.YYYY.'));
 const again=all(h.main()).find(n=>n.tagName==='INPUT' && n.type==='text');assert.equal(again.value,'32.01.2027');again.value='15.01.2027';again.fire('input');
 button(h.main(),'Accept').click();await settle();
 const {args}=h.writes()[0];assert.equal(args.customer,'C1');assert.equal(args.tasks[0].external_date,'2027-01-15');assert.equal(args.tasks[0].internal_date,null);
});

test('deselecting every task files the letter without a task',async()=>{
 const one=workspace('one',{card:{tasks:[task(),task({source_index:1,title:'Umfrage',deadline:deadline('not_found',{date:null,quote:null,page:null,verified:false})})]}});
 const h=await shown({letters:{one}});
 assert.ok(text(h.main()).includes('Tasks · 2 of 2 selected'));button(h.main(),'Accept 2 tasks');
 for(let i=0;i<2;i++){const box=all(h.main()).filter(n=>n.type==='checkbox')[i];box.checked=false;box.fire('change');}
 assert.ok(text(h.main()).includes('No task selected. The letter is only filed.'));
 button(h.main(),'File without task').click();await settle();
 const {args}=h.writes()[0];assert.equal(args.origin,'proposal');assert.deepEqual(args.tasks,[]);
});

test('Discard document asks for a reason and calls analysis.cancel',async()=>{
 const h=await shown();
 button(h.main(),'Discard document').click();
 assert.equal(h.prompts.length,1);assert.equal(h.prompts[0].fields.reqd,1);
 h.prompts[0].callback({value:'Werbung'});await settle();
 const write=h.writes()[0];assert.equal(write.operation,'analysis.cancel');assert.deepEqual({...write.args,request_key:undefined},{letter:'one',expected_revision:1,reason:'Werbung',request_key:undefined});
});

test('Create correction appears only when the server allows it',async()=>{
 const done=allowed=>workspace('one',{status:'Completed',card:{state:'done',tasks:[]},review:{allowed_operations:allowed,receipt:{actions:[]}}});
 let h=await shown({letters:{one:done(['create_correction'])}});button(h.main(),'Create correction');
 assert.ok(text(h.main()).includes('Filed without task'));assert.equal(buttons(h.main(),'Undo').length,0);assert.equal(buttons(h.main(),'Discard document').length,0);
 h=await shown({letters:{one:done([])}});assert.equal(buttons(h.main(),'Create correction').length,0);
});

test('a replacement letter keeps the original-task disposition block',async()=>{
 const one=workspace('one',{review:{allowed_operations:['correction_disposition'],correction:{ready:false,tasks:[{task:'TASK-9',action_id:'old',status:'Open',disposition:null}]}}});
 const h=await shown({letters:{one}});
 assert.ok(text(h.main()).includes('Administrative review of the original tasks is required.'));
 button(h.main(),'Resolve original task').click();h.prompts[0].callback({disposition:'historical',reason:'Done'});await settle();
 const write=h.writes()[0];assert.equal(write.operation,'review.correction_disposition');assert.equal(write.args.replacement,'one');assert.equal(write.args.original_action_id,'old');
});

test('a hidden task is still counted as created, a cancelled letter reads as discarded',async()=>{
 let h=await shown({letters:{one:workspace('one',{status:'Completed',card:{state:'done',tasks:[]},review:{receipt:{actions:[{action_id:'a',task:null}]}}})}});
 assert.ok(text(h.main()).includes('1 task created'));assert.ok(!text(h.main()).includes('Filed without task'));
 h=await shown({letters:{one:workspace('one',{status:'Cancelled',card:{state:'done',tasks:[]}})}});
 assert.ok(text(h.main()).includes('Discarded'));assert.equal(buttons(h.main(),'Undo').length,0);
});

test('Start analysis follows analysis.allowed_operations',async()=>{
 const h=await shown({letters:{one:workspace('one',{status:'Received',analysis:{allowed_operations:['queue_analysis','cancel']},card:{state:'not_started',tasks:[]}})}});
 button(h.main(),'Start analysis').click();await settle();
 const write=h.writes()[0];assert.equal(write.operation,'analysis.queue_analysis');assert.equal(write.args.letter,'one');assert.equal(write.args.expected_revision,1);
});

test('the manual form keeps typed values, shows field errors before any request and submits origin manual',async()=>{
 const h=await shown();
 button(h.main(),'Create manually').click();
 const field=label=>{const el=all(h.main()).find(n=>n.getAttribute('aria-label')===label || all(h.main()).some(l=>l.tagName==='LABEL' && l.getAttribute('for')===n.id && n.id && l.textContent.startsWith(label)));assert.ok(el,label);return el;};
 const title=field('Task');assert.equal(title.value,'Belege senden');title.value='';title.fire('input');
 button(h.main(),'Create task').click();await settle();
 assert.equal(h.writes().length,0);assert.ok(text(h.main()).includes('Enter the task title.'));
 const again=field('Task');assert.equal(again.getAttribute('aria-invalid'),'true');assert.ok(again.getAttribute('aria-describedby'));
 again.value='Bescheid prüfen';again.fire('input');
 const date=field('Deadline from the letter');date.value='15.12.2026';date.fire('input');
 button(h.main(),'+ Another task').click();
 assert.equal(field('Task').value,'Bescheid prüfen','typed values survive a re-render');
 const second=all(h.main()).filter(n=>n.tagName==='INPUT' && n.id.includes('title'))[1];second.value='Zweite Aufgabe';second.fire('input');
 h.page.draw();
 button(h.main(),'Create 2 tasks').click();await settle();
 const {operation,args}=h.writes()[0];assert.equal(operation,'review.confirm_simple');assert.equal(args.origin,'manual');assert.equal(args.customer,'C1');
 assert.deepEqual(args.tasks.map(t=>[t.source_index,t.title,t.external_date,t.assigned_to]),[[null,'Bescheid prüfen','2026-12-15','operator'],[null,'Zweite Aufgabe',null,'operator']]);
});

test('the manual form requires a Mandant before any request',async()=>{
 const h=await shown({letters:{one:workspace('one',{status:'Error',card:{state:'failed',tasks:[],customer:{suggested:null,candidates:[],ambiguity:null}}})}});
 button(h.main(),'Create manually').click();
 const title=all(h.main()).find(n=>n.tagName==='INPUT' && n.id.includes('title'));title.value='Prüfen';title.fire('input');
 button(h.main(),'Create task').click();button(h.main(),'File without task').click();await settle();
 assert.equal(h.writes().length,0);assert.ok(text(h.main()).includes('Select the Mandant.'));
 button(h.main(),'Cancel').click();assert.ok(text(h.main()).includes('Automatic analysis failed'));
});

test('a revision conflict reloads the letter and says so',async()=>{
 const h=await shown({handler:operation=>{if(operation==='review.confirm_simple')throw Error('REVISION_CONFLICT');}});
 const loads=()=>h.calls.filter(c=>c.operation==='get_letter_workspace').length,before=loads();
 button(h.main(),'Accept').click();await settle();await settle();
 assert.equal(loads(),before+1);assert.equal(h.page.client.conflict,false);
 assert.ok(text(h.root).includes('The letter changed. It was reloaded; check the card and try again.'));
 button(h.main(),'Accept').click();await settle();assert.equal(h.writes().length,2,'a reloaded card can be confirmed again');
});

test('a permission failure clears every protected view',async()=>{
 const h=await shown({handler:operation=>{if(operation==='review.confirm_simple')throw Error('PERMISSION');}});
 h.queue.open=[{letter:'one',status:'Needs Review',sender:'Finanzamt one',letter_type:'Anforderung',file_name:'one.pdf',analysis:{},matching:{ambiguity:'none'},deadline:{}}];await h.page.loadQueue();
 assert.ok(text(h.page.rail).includes('Finanzamt one'));
 button(h.main(),'Accept').click();await settle();
 assert.equal(h.page.client.state,null);assert.equal(text(h.main()),'');assert.ok(!text(h.page.rail).includes('Finanzamt'));assert.equal(h.page.letters.size,0);
 assert.ok(text(h.root).includes('Posteingang operation is not permitted'));
});

test('the rail lists open and done letters with a text status chip',async()=>{
 const h=harness();
 h.queue.open=[{letter:'one',status:'Needs Review',sender:'Finanzamt one',letter_type:'Anforderung',file_name:'one.pdf',analysis:{},matching:{ambiguity:'none'},deadline:{}},
  {letter:'two',status:'Processing',sender:null,letter_type:null,file_name:'scan.pdf',analysis:{},matching:null,deadline:null}];
 h.queue.history=[{letter:'old',status:'Completed',sender:'Stadt',letter_type:'Bescheid',file_name:'old.pdf',analysis:{},matching:null,deadline:null}];
 await h.page.show();await settle();
 const rail=text(h.page.rail);
 for(const part of ['Drop PDF here','Choose PDF','Open · 2','Done','Proposal ready','scan.pdf','Analysing','Stadt'])assert.ok(rail.includes(part),part);
 const current=all(h.page.rail).find(n=>n.getAttribute('aria-current')==='true');assert.ok(current.textContent.includes('Finanzamt one'));
 assert.deepEqual(h.calls.filter(c=>c.operation==='get_queue').map(c=>c.args.filters.state),['open','history']);
 all(h.page.rail).find(n=>n.tagName==='BUTTON' && n.textContent.includes('scan.pdf')).click();assert.deepEqual(h.routes.at(-1),['posteingang','two']);
});

test('Choose PDF and a dropped file open the intake dialog with those files',async()=>{
 const h=await shown();
 button(h.page.rail,'Choose PDF').click();assert.deepEqual([...h.opened[0].files],[]);
 const drop=all(h.page.rail).find(n=>n.className.includes('pe-drop'));const file={name:'brief.pdf',type:'application/pdf'};
 drop.fire('drop',{dataTransfer:{files:[file]}});assert.equal(h.opened[1].files[0],file);
 await h.opened[1].onOpen('two');assert.deepEqual(h.routes.at(-1),['posteingang','two']);
});

test('Next document opens the next open letter only on request',async()=>{
 const h=await shown({letters:{one:workspace('one',{status:'Completed',card:{state:'done',tasks:[]},review:{receipt:{actions:[]}}})}});
 h.queue.open=[{letter:'two',status:'Needs Review',sender:'Zwei',file_name:'two.pdf',analysis:{},matching:null,deadline:null}];await h.page.loadQueue();h.page.draw();
 assert.equal(h.routes.length,0);button(h.main(),'Next document').click();assert.deepEqual(h.routes.at(-1),['posteingang','two']);
});

test('status polling never draws a letter card over the list view',async()=>{
 const h=await shown();
 await h.go();assert.equal(h.root.dataset.view,'list');
 h.letters.one=workspace('one',{revision:2,status:'Completed',card:{state:'done',tasks:[]}});
 const poll=h.timers.filter(t=>t.ms===5000).at(-1);await poll.fn();await settle();
 assert.equal(h.root.dataset.view,'list');assert.ok(text(h.main()).includes('Choose a document from the list or upload a PDF.'));
 assert.equal(h.page.client.state.revision,2,'the polled letter itself is refreshed');
});

const queueRow=n=>({letter:'L'+n,status:'Needs Review',sender:'Sender '+n,letter_type:'Typ',file_name:n+'.pdf',analysis:{},matching:{ambiguity:'none'},deadline:{}});
const pagedQueue=(open,gate)=>async(operation,args)=>{
 if(operation!=='get_queue')return undefined;
 if(args.filters.state==='history')return {user:'operator',items:[],next_cursor:null};
 if(gate && !args.cursor)await gate();
 return args.cursor==='p2' ? {user:'operator',items:open.slice(20),next_cursor:null} : {user:'operator',items:open.slice(0,20),next_cursor:'p2'};
};

test('a refresh keeps the open letters loaded with More',async()=>{
 const open=Array.from({length:25},(_v,i)=>queueRow(i+1));
 const h=await shown({handler:pagedQueue(open)});
 assert.ok(text(h.page.rail).includes('Open · 20+'));assert.ok(!text(h.page.rail).includes('Sender 25'));
 button(h.page.rail,'More').click();await settle();
 assert.ok(text(h.page.rail).includes('Sender 25'));assert.ok(text(h.page.rail).includes('Open · 25'));
 await h.page.loadQueue();await settle();
 assert.ok(text(h.page.rail).includes('Sender 25'),'the 15 s refresh and every mutation reload the loaded extent');
 const cursors=h.calls.filter(c=>c.operation==='get_queue' && c.args.filters.state==='open').map(c=>c.args.cursor);
 assert.deepEqual(cursors,[null,'p2',null,'p2']);
});

test('More pressed during a running refresh still loads the next page',async()=>{
 const open=Array.from({length:25},(_v,i)=>queueRow(i+1));let release=null;
 const h=await shown({handler:pagedQueue(open,()=>release ? new Promise(r=>{release=r;}) : null)});
 release=true;const refresh=h.page.loadQueue();await settle();
 button(h.page.rail,'More').click();release();await refresh;await settle();await settle();
 assert.ok(text(h.page.rail).includes('Sender 25'));
});

test('an assignee refused for the Mandant opens the picker scoped to that Mandant',async()=>{
 let refuse=true;
 const h=await shown({handler:(operation,args)=>{
  if(operation==='review.confirm_simple' && refuse){refuse=false;throw Error('ASSIGNEE_NOT_ALLOWED');}
  if(operation==='review.search_assignees')return {rows:[{user:'colleague',full_name:'Colleague'}]};
 }});
 button(h.main(),'Accept').click();await settle();
 assert.ok(text(h.main()).includes('This person has no access to the selected Mandant.'));
 const dialog=h.dialogs.at(-1);assert.equal(dialog.opts.title,'Select assignee');
 await dialog.on_page_show();
 const search=h.calls.find(c=>c.operation==='review.search_assignees');assert.equal(search.args.customer,'C1');assert.equal(search.args.letter,'one');
 button(dialog.fields_dict.results.$wrapper[0],'Select').click();
 assert.ok(!text(h.main()).includes('This person has no access'));assert.ok(text(h.main()).includes('Colleague'));
 button(h.main(),'Accept').click();await settle();
 const writes=h.writes().filter(c=>c.operation==='review.confirm_simple');assert.equal(writes.length,2);assert.equal(writes[1].args.tasks[0].assigned_to,'colleague');
});

test('the manual form searches assignees for its Mandant and marks a refused one',async()=>{
 const h=await shown({handler:operation=>{if(operation==='review.confirm_simple')throw Error('ASSIGNEE_NOT_ALLOWED');if(operation==='review.search_assignees')return {rows:[]};}});
 button(h.main(),'Create manually').click();
 button(h.main(),'Create task').click();await settle();
 assert.ok(text(h.main()).includes('This person has no access to the selected Mandant.'));
 await h.dialogs.at(-1).on_page_show();assert.equal(h.calls.find(c=>c.operation==='review.search_assignees').args.customer,'C1');
});

test('the responsible fact names the uploader and a legacy draft keeps its stored Mandant',async()=>{
 const one=workspace('one',{card:{state:'draft',customer:{suggested:{customer:'C1',customer_name:'Muster GmbH',why:'x',page:1},candidates:[],ambiguity:'none'},tasks:[task({source_index:null})]}});
 one.matching.selected_customer={customer:'C9',customer_name:'Stored GmbH',can_select:true};
 const h=harness({letters:{one}});h.queue.open=[{...queueRow(1),letter:'one',received_by:'operator'}];await h.page.show();await settle();
 assert.ok(text(h.main()).includes('Stored GmbH'));assert.ok(text(h.main()).includes('uploaded the document'));
 button(h.main(),'Create task').click();await settle();assert.equal(h.writes()[0].args.customer,'C9');
 const p=harness();p.queue.open=[{...queueRow(1),letter:'one',received_by:'operator'}];await p.page.show();await settle();
 assert.ok(text(p.main()).includes('uploaded the document'));
});

test('page search waits for the shown dialog before reading a fast result',async()=>{
 const root={rows:[],replaceChildren(){this.rows=[];}},dialogs=[];let reads=0;
 const context={__:(s)=>s,frappe:{pages:{posteingang:{}},ui:{Dialog:class{
  constructor(){this.visible=false;this.fields_dict={results:{$wrapper:[root]}};this.$wrapper={is:()=>this.visible};dialogs.push(this);}
  set_primary_action(){} get_value(){return '';} show(){}
 }}}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(PAGE,'utf8'),context);
 const proto=vm.runInContext('PosteingangWorkspace.prototype',context);
 const page={client:{state:{letter:'one'},sequence:1},dialogs:new Set(),capture:()=>()=>true,transport:async()=>{reads++;return {rows:[{customer:'customer',customer_name:'Synthetic',can_select:true}]};},node:(_p,_t,s)=>{root.rows.push(s);return {};},button(){},failure:assert.fail};
 await proto.search.call(page,'customers',()=>{});assert.equal(reads,0);
 dialogs[0].visible=true;await dialogs[0].on_page_show();assert.equal(reads,1);assert.ok(root.rows.includes('Synthetic'));
});
