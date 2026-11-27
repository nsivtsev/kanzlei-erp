frappe.pages['posteingang'].on_page_load = wrapper => {
 wrapper.ap09Ready=frappe.require(['posteingang_workspace.bundle.js','posteingang_simple.bundle.js']).then(()=>{wrapper.ap09=new PosteingangWorkspace(wrapper);return wrapper.ap09;});
};
frappe.pages['posteingang'].on_page_show = async wrapper => {await wrapper.ap09Ready;wrapper.ap09?.show();};

/* One-click Posteingang: a rail of letters, one proposal card, the original PDF. The server stays authoritative. */
class PosteingangWorkspace {
 static ICONS={
  up:'<path d="M12 16V4M6 10l6-6 6 6"/><path d="M4 20h16"/>',file:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  check:'<path d="M20 6 9 17l-5-5"/>',alert:'<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  cal:'<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',user:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  flag:'<path d="M4 22V4M4 4h12l-2 4 2 4H4"/>',eye:'<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  back:'<path d="M15 18l-6-6 6-6"/>',spin:'<path d="M21 12a9 9 0 1 1-6.2-8.6"/>',edit:'<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  archive:'<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8M10 12h4"/>',
  redo:'<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',link:'<path d="M7 17 17 7M8 7h9v9"/>'
 };
 // Translation context: lets Posteingang labels differ from shared rows such as Responsible or Undo.
 static CONTEXT='Posteingang';
 static CHIPS={ready:'check',need:'alert',busy:'spin',fail:'alert',done:'check'};
 static AMBIGUITY={multiple_matches:'Several Mandants match the letter. Please choose one.',multiple_addressees:'The letter is addressed to several recipients. Please choose one Mandant.',not_found:'No Mandant was found in the letter. Please search for it.'};
 static CONFLICTS=['REVISION_CONFLICT','CUSTOMER_CHANGED','STALE_RUN'];
 // TT.MM.JJJJ (ISO only when allowed) -> ISO; null when empty; false when invalid.
 static date(text,iso=false){
  const value=String(text ?? '').trim();if(!value)return null;
  let m=/^(\d{2})\.(\d{2})\.(\d{4})$/.exec(value),y,mo,d;
  if(m)[,d,mo,y]=m;else if(iso && (m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(value)))[,y,mo,d]=m;else return false;
  const t=new Date(Date.UTC(+y,+mo-1,+d));
  return t.getUTCFullYear()===+y && t.getUTCMonth()===+mo-1 && t.getUTCDate()===+d ? `${y}-${mo}-${d}` : false;
 }
 static german(iso){const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');return m ? `${m[3]}.${m[2]}.${m[1]}` : '';}
 constructor(wrapper) {
  this.page=frappe.ui.make_app_page({parent:wrapper,title:__('PDF inbox'),single_column:true});
  this.api=frappe.posteingang;this.client=new this.api.WorkspaceClient(this.transport.bind(this),()=>frappe.session.user);
  this.visible=false;this.stale=false;this.dialogs=new Set();this.letters=new Map();this.history=[];this.controls=[];this.targets={};this.pdfOpen=false;this.pdfPage=null;
  this.root=this.node(this.page.main[0],'div');this.root.className='posteingang-workspace';this.root.dataset.view='list';this.root.dataset.pdf='false';
  this.node(this.root,'p',__('Upload a PDF, check the proposal, accept it.')).className='pe-sub';
  this.notice=this.node(this.root,'div');this.notice.className='pe-notice';this.notice.setAttribute('role','status');
  // The query container; the fixed toast stays outside it.
  const shell=this.node(this.root,'div');shell.className='pe-shell';
  const body=this.node(shell,'div');body.className='pe-body';
  this.rail=this.node(body,'aside');this.rail.className='pe-rail';this.rail.setAttribute('aria-label',__('Documents',null,PosteingangWorkspace.CONTEXT));
  this.drawDrop();this.lists=this.node(this.rail,'div');
  this.main=this.node(body,'div');this.main.className='pe-main';
  this.pdf=this.node(body,'aside');this.pdf.className='pe-pdf';this.pdf.setAttribute('aria-label',__('Original PDF'));
  // Created once: a live region announces only content added to it later.
  this.toast=this.node(this.root,'div');this.toast.className='pe-toast-region';this.toast.setAttribute('role','status');this.toast.setAttribute('aria-live','polite');
  this.routeHandler=()=>{if(this.onRoute())this.show();else this.hide();};
  frappe.router.on('change',this.routeHandler);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)this.pause();else if(this.onRoute())this.show();});
  window.addEventListener('beforeunload',event=>{if(this.client.intent || [...this.letters.values()].some(ui=>ui.form?.dirty)){event.preventDefault();event.returnValue='';}});
 }
 node(parent,tag,value='') {const element=document.createElement(tag);element.textContent=value;parent.append(element);return element;}
 icon(parent,name){const span=this.node(parent,'span');span.className='pe-icon';span.setAttribute('aria-hidden','true');span.innerHTML=`<svg viewBox="0 0 24 24"${name==='spin' ? ' class="pe-spin"' : ''}>${PosteingangWorkspace.ICONS[name]}</svg>`;return span;}
 button(parent,label,handler,{primary=false,link=false,large=false,icon=null,args=null,mutation=false,requiresEnabled=true}={}) {
  const button=this.node(parent,'button');button.type='button';
  button.className=link ? 'btn btn-link pe-link' : 'btn '+(primary ? 'btn-primary' : 'btn-default')+(large ? ' pe-btn-lg' : '');
  if(icon)this.icon(button,icon);button.append(__(label,args,PosteingangWorkspace.CONTEXT));
  if(mutation){button.dataset.mutation='1';button.dataset.requiresEnabled=String(requiresEnabled);this.controls.push(button);}
  button.addEventListener('click',handler);return button;
 }
 lockable(control){this.controls.push(control);return control;}
 section(parent,title){const section=this.node(parent,'section');section.className='pe-sec';this.node(section,'h3',title).className='pe-sec-h';return section;}
 id(state,key){return 'pe-'+String(state.letter).replace(/[^A-Za-z0-9_-]/g,'_')+'-'+key;}
 person(user,name){return user===frappe.session.user ? __('Me') : name || user || __('Not assigned');}
 clearMain(){this.main.replaceChildren();this.controls=[];this.targets={};}
 clearPdf(){this.pdf.replaceChildren();this.frame=null;}
 onRoute(){return frappe.get_route()[0]==='posteingang';}
 async transport(operation,args) {
  const [module,method]=operation.split('.');
  if(method){
   if(args._file){const {_file,...values}=args;return this.api.review_rpc(method,this.api.correction_form_data(values,_file,values.request_key));}
   return this.api[`${module}_rpc`](method,args);
  }
  let response,body;
  try {response=await fetch(`/api/method/kanzlei_erp.posteingang_workspace.${operation}`,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-Frappe-CSRF-Token':frappe.csrf_token},body:JSON.stringify(args)});body=await response.json();}
  catch{throw Error('NETWORK');}
  if(!response.ok || body.exc)throw Error(response.status===403 ? 'PERMISSION' : body.code || 'INTERNAL_ERROR');
  return body.message;
 }
 message(error) {
  const messages={...this.api.analysis_messages,...this.api.matching_messages,...this.api.review_messages,
   UNKNOWN_OUTCOME:'Resolve the unknown request before another operation.'};
  return __(messages[error.message] || 'The operation could not finish. Refresh and try again.');
 }
 failure(error){
  if(!this.visible)return;
  const code=error?.message;
  if(code==='PERMISSION'){
   this.client.clear();this.letters.clear();this.history=[];this.cursor=null;this.openPages=1;this.clearMain();this.clearPdf();this.lists.replaceChildren();this.railSignature=null;this.hideToast();
   for(const d of this.dialogs)d.hide();
  }
  this.notice.replaceChildren();this.node(this.notice,'p',this.message(error));
  if(this.client.intent)this.button(this.notice,'Check and retry the same request',()=>this.retry());
  if(PosteingangWorkspace.CONFLICTS.includes(code))this.reload();
  if(code==='ASSIGNEE_NOT_ALLOWED')this.assigneeRejected();
 }
 // The server refused an assignee for the chosen Mandant: mark the unverified ones and open the picker.
 assigneeRejected(){
  const state=this.client.state;if(!state || !this.visible)return;
  const ui=this.ui(state),message='This person has no access to the selected Mandant.';
  if(state.card.state==='draft' || ui.mode==='manual'){
   const form=this.manualForm(state,ui),customer=form.customer?.customer;
   let rows=form.rows.map((row,i)=>row.verifiedFor===customer ? null : i).filter(i=>i!==null);if(!rows.length)rows=form.rows.map((_row,i)=>i);
   for(const i of rows)form.errors['assignee-'+i]=message;
   this.draw();this.pickAssignee(rows[0]);
  }else{
   let rows=state.card.tasks.map((_task,i)=>ui.choice.selected[i] && ui.assignees[i]?.verifiedFor!==ui.choice.customer ? i : null).filter(i=>i!==null);
   if(!rows.length)rows=state.card.tasks.map((_task,i)=>ui.choice.selected[i] ? i : null).filter(i=>i!==null);
   ui.assigneeErrors={};for(const i of rows)ui.assigneeErrors[i]=message;
   this.draw();if(rows.length)this.pickAssignee(rows[0]);
  }
 }
 // Assignee search is scoped to the Mandant currently chosen on the card or in the form.
 pickAssignee(index){
  const state=this.client.state;if(!state)return;
  const ui=this.ui(state),manual=state.card.state==='draft' || ui.mode==='manual',form=manual ? this.manualForm(state,ui) : null;
  const customer=manual ? form.customer?.customer : ui.choice.customer;
  this.search('assignees',person=>{
   const picked={assigned_to:person.user,assigned_to_name:person.full_name || person.user,verifiedFor:customer || null};
   if(manual){Object.assign(form.rows[index],picked);delete form.errors['assignee-'+index];form.dirty=true;}
   else{ui.assignees[index]=picked;delete ui.assigneeErrors[index];}
   this.draw();
  },customer);
 }
 // A definitive conflict blocks every mutation until the letter is loaded again.
 async reload(){
  const letter=this.client.state?.letter;if(!letter)return;
  try{
   if(!await this.client.load(letter) || !this.visible)return;
   this.draw();this.notice.replaceChildren();this.node(this.notice,'p',__('The letter changed. It was reloaded; check the card and try again.'));
  }catch(error){this.failure(error);}
 }
 pause(){clearTimeout(this.queueTimer);clearTimeout(this.statusTimer);}
 hide(){
  this.visible=false;this.stale=true;this.pause();this.client.suspend();for(const d of this.dialogs)d.hide();
  this.hideToast();this.clearMain();this.clearPdf();this.lists.replaceChildren();this.railSignature=null;this.notice.replaceChildren();
 }
 async show(){
  if(!this.onRoute() || document.hidden)return;
  const name=frappe.get_route()[1] || null;
  if(this.showing===name && this.visible)return;
  this.showing=name;this.visible=true;this.client.active=true;this.pause();
  try{
   if(name){
    const same=this.client.state?.letter===name;
    if(this.client.intent){if(same)await this.client.status();}
    else if(!same || this.stale){
     if(!same){this.notice.replaceChildren();this.pdfPage=null;for(const dialog of this.dialogs)dialog.hide();}
     await this.client.load(name);
    }
    this.stale=false;
    if(!this.visible || !this.onRoute() || frappe.get_route()[1]!==name)return;
    if(this.client.state?.letter===name)this.draw();else{this.clearMain();this.clearPdf();this.failure(Error('UNKNOWN_OUTCOME'));}
   }else{
    // The list view polls nothing new for the letter; reload it when it is opened again.
    this.stale=true;this.root.dataset.view='list';this.root.dataset.pdf='false';this.clearMain();this.clearPdf();
    this.node(this.main,'p',__('Choose a document from the list or upload a PDF.')).className='pe-empty';
   }
   await this.loadQueue();this.schedule();
  }catch(error){this.failure(error);}finally{this.showing=undefined;}
 }
 schedule(){
  this.pause();if(!this.visible || document.hidden || !this.onRoute() || this.client.user()!==this.client.owner)return;
  this.queueTimer=setTimeout(async()=>{await this.loadQueue();this.scheduleQueue();},15000);this.scheduleStatus();
 }
 scheduleQueue(){if(this.visible && !document.hidden)this.queueTimer=setTimeout(async()=>{await this.loadQueue();this.scheduleQueue();},15000);}
 scheduleStatus(){if(this.visible && !document.hidden)this.statusTimer=setTimeout(async()=>{
  if(!this.statusBusy && !this.client.busy && !this.operationBusy && this.client.state){
   this.statusBusy=true;const before=this.client.state;
   // Only the letter in the route owns the card; the list view keeps its placeholder.
   const shown=()=>frappe.get_route()[1]===before.letter;
   try{
    await this.client.status();
    // A changed letter also changes its rail chip; refresh the list now, not on the next 15 s tick.
    if(this.client.state!==before)this.loadQueue();
    if(!this.client.state){if(shown()){this.clearMain();this.clearPdf();}}else if((this.client.state!==before || this.client.referencesChanged) && shown()){this.client.referencesChanged=false;this.draw();}
   }
   catch(error){this.failure(error);}finally{this.statusBusy=false;}
  }
  this.scheduleStatus();
 },5000);}
 async loadQueue(more=false){
  if(!this.visible)return;
  // A request made while a load runs is replayed afterwards, "More" included.
  if(this.queueBusy){this.queueAgain=true;if(more)this.queueMore=true;return;}
  this.queueBusy=true;
  try{
   // One rows buffer per client: the open and done lists load one after the other.
   const filters={state:'open',uploaded_by:'all',query:'',sort:'oldest'};
   if(more){
    if(!this.cursor)return;
    const page=await this.client.queue(filters,this.cursor);if(!page || !this.visible)return;
    this.cursor=page.next_cursor;this.openPages=(this.openPages || 1)+1;
   }else{
    // A refresh keeps every page already loaded: the rail is the only way to reach a letter.
    const extent=this.openPages || 1;let page=await this.client.queue(filters,null),loaded=1;
    if(!page || !this.visible)return;
    while(page.next_cursor && loaded<extent){page=await this.client.queue(filters,page.next_cursor);if(!page || !this.visible)return;loaded++;}
    this.cursor=page.next_cursor;this.openPages=loaded;
    const sequence=this.client.queueSequence;
    const done=await this.transport('get_queue',{filters:{state:'history',uploaded_by:'all',query:'',sort:'newest'},cursor:null});
    if(sequence!==this.client.queueSequence || !this.client.active || !this.visible)return;
    this.client.check(done);this.history=done.items;
   }
   this.drawRail();
   // The uploader comes from the queue rows; draw the card again once it is known.
   const state=this.client.state;
   if(state && frappe.get_route()[1]===state.letter && this.uploader(state)!==this.drawnUploader)this.draw();
  }catch(error){if(error.message==='PERMISSION')this.client.clear();this.failure(error);}
  finally{this.queueBusy=false;if(this.queueAgain){const more=!!this.queueMore;this.queueAgain=this.queueMore=false;this.loadQueue(more);}}
 }
 drawDrop(){
  const drop=this.node(this.rail,'div');drop.className='pe-drop';
  this.node(drop,'strong',__('Drop PDF here'));this.node(drop,'span',__('or'));
  this.button(drop,'Choose PDF',()=>this.upload(),{primary:true,icon:'up'});
  drop.addEventListener('dragover',event=>{event.preventDefault();drop.classList.add('pe-over');});
  drop.addEventListener('dragleave',()=>drop.classList.remove('pe-over'));
  drop.addEventListener('drop',event=>{event.preventDefault();drop.classList.remove('pe-over');this.upload([...(event.dataTransfer?.files || [])]);});
 }
 upload(files=[]){this.api.open({files,onUploaded:()=>this.loadQueue(),onOpen:name=>this.select(name)});}
 drawRail(){
  const current=this.client.state?.letter,signature=JSON.stringify([this.client.rows,this.history,this.cursor,current]);
  if(signature===this.railSignature)return;this.railSignature=signature;
  this.lists.replaceChildren();
  this.node(this.lists,'h3',__('Open · {0}',[this.client.rows.length+(this.cursor ? '+' : '')])).className='pe-rail-h';
  if(!this.client.rows.length)this.node(this.lists,'p',__('No open documents.')).className='pe-rail-empty';
  for(const row of this.client.rows)this.railItem(row,current);
  if(this.cursor)this.button(this.lists,'More',()=>this.loadQueue(true),{link:true});
  this.node(this.lists,'h3',__('Done')).className='pe-rail-h';
  for(const row of this.history)this.railItem(row,current);
 }
 railItem(row,current){
  const item=this.node(this.lists,'button');item.type='button';item.className='pe-item';item.setAttribute('aria-current',String(row.letter===current));
  item.addEventListener('click',()=>this.select(row.letter));
  this.node(item,'span',row.sender || row.file_name).className='pe-item-t';
  this.node(item,'span',row.letter_type || (row.received_at ? frappe.datetime.str_to_user(row.received_at) : row.file_name)).className='pe-item-s';
  const chip=this.api.listChip(row),badge=this.node(item,'span');badge.className='pe-chip pe-chip-'+chip.tone;
  this.icon(badge,PosteingangWorkspace.CHIPS[chip.tone]);this.node(badge,'span',__(chip.text));
 }
 select(name){
  if(this.operationBusy || this.client.busy)return;
  if(this.client.intent){this.failure(Error('UNKNOWN_OUTCOME'));return;}
  frappe.set_route('posteingang',name);
 }
 async run(operation,args){
  if(this.operationBusy)return null;
  this.operationBusy=true;this.notice.replaceChildren();
  try{return await this.settle(operation,args,this.client.mutate(operation,args));}
  catch(error){this.failure(error);return null;}finally{this.operationBusy=false;this.syncControls();}
 }
 // Retries the frozen intent: same operation, same arguments, same request_key.
 async retry(){
  if(this.operationBusy || !this.client.intent)return null;
  const {operation,args}=this.client.intent;this.operationBusy=true;this.notice.replaceChildren();
  try{return await this.settle(operation,args,this.client.retry());}
  catch(error){this.failure(error);return null;}finally{this.operationBusy=false;this.syncControls();}
 }
 async settle(operation,args,pending){
  const label=this.label(this.client.state);this.syncControls();
  const result=await pending;
  if(!result || !this.visible)return null;
  const name=result.replacement || args.letter || this.client.state?.letter;
  const loaded=await this.client.load(name);
  if(!loaded || !this.visible)return null;
  if(result.replacement || frappe.get_route()[1]!==name)frappe.set_route('posteingang',name);
  if(operation==='review.confirm_simple'){this.letters.delete(name);this.showToast(args.tasks.length,label,name);}
  if(operation==='review.undo_confirm'){this.letters.delete(name);this.hideToast();}
  this.draw();await this.loadQueue();return result;
 }
 syncControls(){
  const locked=this.operationBusy || this.client.busy || !!this.client.intent || this.client.conflict;
  const caps=this.client.remote?.capabilities || this.client.state?.capabilities;
  for(const control of this.controls){
   if(control.dataset.originalDisabled===undefined)control.dataset.originalDisabled=control.disabled ? 'yes' : 'no';
   control.disabled=locked || control.dataset.originalDisabled==='yes' || (control.dataset.mutation==='1' && (!caps?.customer_write || (!caps?.enabled && control.dataset.requiresEnabled==='true')));
  }
 }
 showToast(count,label,letter){
  clearTimeout(this.toastTimer);this.toast.replaceChildren();
  const toast=this.node(this.toast,'div');toast.className='pe-toast';
  this.node(toast,'span',count ? (count===1 ? __('1 task created: {0}',[label]) : __('{0} tasks created: {1}',[count,label])) : __('Filed without task: {0}',[label]));
  // Bound to the confirmed letter, not to whichever letter is open when it is clicked.
  this.button(toast,'Undo',()=>this.undo(letter),{link:true});
  this.toastTimer=setTimeout(()=>this.hideToast(),5000);
 }
 hideToast(){clearTimeout(this.toastTimer);this.toast.replaceChildren();}
 async undo(letter){
  if(this.operationBusy || this.client.busy)return null;
  if(this.client.intent){this.failure(Error('UNKNOWN_OUTCOME'));return null;}
  this.hideToast();
  if(this.client.state?.letter!==letter){
   this.operationBusy=true;
   try{if(!await this.client.load(letter) || !this.visible)return null;}catch(error){this.failure(error);return null;}finally{this.operationBusy=false;}
   this.stale=false;this.notice.replaceChildren();frappe.set_route('posteingang',letter);this.draw();
  }
  // No client countdown: the server answers UNDO_EXPIRED.
  return this.run('review.undo_confirm',{letter,expected_revision:this.client.state.revision});
 }
 capture(){const sequence=this.client.sequence,name=this.client.state?.letter,owner=this.client.owner;return ()=>this.visible && this.client.active && !this.client.busy && !this.client.intent && !this.client.conflict && this.client.sequence===sequence && this.client.state?.letter===name && frappe.session.user===owner && this.onRoute() && frappe.get_route()[1]===name;}
 track(dialog){if(dialog){this.dialogs.add(dialog);const old=dialog.onhide;dialog.onhide=()=>{this.dialogs.delete(dialog);old?.();};}return dialog;}
 promptFields(fields,callback,title){const valid=this.capture();return this.track(frappe.prompt(fields,values=>{if(valid())return callback(values);},__(title)));}
 async search(kind,choose,customer=null){
  const name=this.client.state.letter,sequence=this.client.sequence,valid=this.capture();let start=0;
  const dialog=new frappe.ui.Dialog({title:__(kind==='customers' ? 'Find another Mandant' : 'Select assignee'),fields:[{fieldname:'text',fieldtype:'Data',label:__('Search')},{fieldname:'results',fieldtype:'HTML'}]});this.dialogs.add(dialog);
  const root=dialog.fields_dict.results.$wrapper[0];let request=0;
  const load=async more=>{const stamp=++request;try{
   const result=await this.transport(kind==='customers' ? 'matching.search_customers' : 'review.search_assignees',{letter:name,text:dialog.get_value('text') || '',...(kind==='customers' ? {start:more ? start : 0} : customer ? {customer} : {})});
   if(!valid() || stamp!==request || !dialog.$wrapper.is(':visible'))return;
   if(!more)root.replaceChildren();
   if(!result.rows.length)this.node(root,'p',__('No entries'));
   for(const row of result.rows){this.node(root,'p',row.customer_name || row.full_name || row.user);if(kind!=='customers' || row.can_select)this.button(root,'Select',()=>{if(!valid())return;dialog.hide();choose(row);});else this.node(root,'p',__('Read-only Customer; selection requires write access.'));}
   if(result.has_next){start=result.next_start;this.button(root,'More',()=>load(true));}
  }catch(error){if(sequence===this.client.sequence){root.replaceChildren();this.failure(error);}}};
  dialog.set_primary_action(__('Search'),()=>load(false));dialog.onhide=()=>this.dialogs.delete(dialog);dialog.on_page_show=()=>load(false);dialog.show();
 }
 // Per-letter page state survives re-renders; the choice is rebuilt when the card itself changes.
 ui(state){
  const card=state.card,signature=card.state+'|'+(card.run || '');let ui=this.letters.get(state.letter);
  if(!ui || ui.signature!==signature){
   ui={signature,choice:this.api.cardChoice(card),customerName:null,extra:null,showMissing:false,dateErrors:{},assignees:{},assigneeErrors:{},mode:ui?.form ? ui.mode : null,form:ui?.form || null};
   this.letters.set(state.letter,ui);
  }
  return ui;
 }
 docFields(state){
  const saved=state.review?.document_fields || {},proposal=state.review?.proposal?.extraction;
  const value=key=>saved[key] || proposal?.[key]?.value || null;
  return {sender:value('sender'),letter_type:value('letter_type'),summary:value('summary')};
 }
 label(state){return state ? this.docFields(state).sender || state.original?.file_name || state.letter : '';}
 cardTitle(state){
  const tasks=state.card.tasks;
  if(tasks.length===1)return tasks[0].title;
  if(tasks.length>1)return __('{0} tasks found',[tasks.length]);
  return this.docFields(state).letter_type || state.original.file_name;
 }
 customerName(card,ui){
  const id=ui.choice.customer;if(!id)return null;
  return ui.customerName || (card.customer.suggested?.customer===id ? card.customer.suggested.customer_name : card.customer.candidates.find(c=>c.customer===id)?.customer_name) || id;
 }
 draw(){
  const state=this.client.state;if(!state || !this.visible)return;
  const active=document.activeElement,focus=active && this.main.contains?.(active) ? active.id : null;
  const ui=this.ui(state),card=state.card;this.drawnUploader=this.uploader(state);
  this.clearMain();this.root.dataset.view='card';
  this.button(this.main,'All documents',()=>frappe.set_route('posteingang'),{icon:'back'}).classList.add('pe-back-list');
  const box=this.node(this.main,'section');box.className='pe-card';
  if(card.state==='done')this.drawDone(box,state);
  else if(card.state==='analyzing')this.drawAnalyzing(box,state);
  else if(card.state==='draft' || ui.mode==='manual')this.drawManual(box,state,ui);
  else if(card.state==='failed')this.drawFailed(box,state,ui);
  else if(card.state==='not_started')this.drawNotStarted(box,state,ui);
  else this.drawProposal(box,state,ui);
  this.drawCorrection(box,state);
  this.drawPdf(state);this.drawRail();this.syncControls();
  if(focus)document.getElementById(focus)?.focus();
 }
 drawHead(box,state,{title,summary}={}){
  const fields=this.docFields(state),original=state.original,head=this.node(box,'header');head.className='pe-card-head';
  const row=this.node(head,'div');row.className='pe-head-row';const grow=this.node(row,'div');grow.className='pe-grow';
  const meta=this.node(grow,'div');meta.className='pe-meta';
  for(const part of [fields.sender || original.file_name,fields.letter_type])if(part)this.node(meta,'span',part);
  // Read-only: the receipt date is set in the upload dialog; no operation changes it later.
  this.node(meta,'span',original.received_date_known && original.received_date ? __('Received: {0}',[frappe.datetime.str_to_user(original.received_date)]) : __('Received: unknown'));
  this.node(grow,'h2',title ?? this.cardTitle(state));
  const text=summary===undefined ? fields.summary : summary;if(text)this.node(grow,'p',text).className='pe-summary';
  const actions=this.node(row,'div');actions.className='pe-head-actions';
  const toggle=this.button(actions,this.pdfOpen ? 'Hide original' : 'Original',()=>{this.pdfOpen=!this.pdfOpen;if(!this.pdfOpen)this.pdfPage=null;this.draw();},{icon:'eye'});
  toggle.setAttribute('aria-pressed',String(this.pdfOpen));
  if(state.card.state!=='done' && state.analysis.allowed_operations.includes('cancel'))this.moreMenu(actions,state);
 }
 // The only exit for mail that belongs to no Mandant: confirm_simple requires one.
 moreMenu(parent){
  const menu=this.node(parent,'details');menu.className='pe-more';
  const summary=this.node(menu,'summary','…');summary.setAttribute('aria-label',__('More actions'));summary.title=__('More actions');
  const list=this.node(menu,'div');list.className='pe-more-menu';
  this.button(list,'Discard document',()=>{menu.open=false;this.discard();},{mutation:true,requiresEnabled:false});
 }
 discard(){
  const state=this.client.state;if(!state)return;
  this.promptFields({fieldname:'value',fieldtype:'Small Text',label:__('Reason'),reqd:1,description:__('For example advertising, misdirected mail or a wrong upload. No task is created.')},
   values=>this.run('analysis.cancel',{letter:state.letter,expected_revision:this.client.state.revision,reason:values.value}),'Discard document');
 }
 startAnalysis(){const state=this.client.state;return this.run('analysis.queue_analysis',{letter:state.letter,expected_revision:state.revision});}
 pageLink(parent,page){
  const count=this.client.state?.original.page_count;
  if(!Number.isInteger(page) || page<1 || (count && page>count))return null;
  const link=this.button(parent,'p. {0}',()=>this.openPage(page),{link:true,args:[page]});link.setAttribute('aria-label',__('Show page {0} of the original',[page]));return link;
 }
 openPage(page){this.pdfOpen=true;this.pdfPage=page;this.draw();}
 drawPdf(state){
  this.root.dataset.pdf=String(this.pdfOpen);
  if(!this.pdfOpen){this.clearPdf();return;}
  const original=state.original,src=original.preview_url+(this.pdfPage ? '#page='+this.pdfPage : '');
  if(this.frame?.isConnected && this.frame.dataset.original===original.preview_url){if(this.frame.dataset.src!==src){this.frame.dataset.src=src;this.frame.src=src;}return;}
  this.clearPdf();
  const bar=this.node(this.pdf,'div');bar.className='pe-pdf-bar';
  this.button(bar,'Back to proposal',()=>{this.pdfOpen=false;this.pdfPage=null;this.draw();},{icon:'back'}).classList.add('pe-back-pdf');
  const pages=original.page_count;
  this.node(bar,'span',[original.file_name,pages ? (pages===1 ? __('1 page') : __('{0} pages',[pages])) : null].filter(Boolean).join(' · ')).className='pe-grow';
  for(const [label,url] of [['Open original PDF',original.preview_url],['Download',original.download_url]]){const a=this.node(bar,'a',__(label));a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.className='btn btn-default btn-sm';}
  this.frame=this.node(this.pdf,'iframe');this.frame.dataset.original=original.preview_url;this.frame.dataset.src=src;this.frame.src=src;this.frame.title=__('Original PDF');
 }
 drawProposal(box,state,ui){
  this.drawHead(box,state);
  this.node(box,'p',__("Assistant's proposal · please compare briefly with the original")).className='pe-strip';
  this.drawCustomer(box,state,ui);this.drawTasks(box,state,ui);this.drawFooter(box,state,ui);
 }
 drawCustomer(box,state,ui){
  const card=state.card,section=this.section(box,__('Mandant')),suggested=card.customer.suggested;
  if(suggested){
   const who=this.node(section,'div');who.className='pe-who';this.node(who,'strong',suggested.customer_name);
   const why=this.node(who,'span',suggested.why);why.className='pe-why';if(suggested.page){why.append(' · ');this.pageLink(why,suggested.page);}
   return;
  }
  const missing=ui.showMissing && !ui.choice.customer,need=this.node(section,'div');need.className='pe-need';
  this.node(need,'p',__(PosteingangWorkspace.AMBIGUITY[card.customer.ambiguity] || 'Please choose the Mandant.')).className='pe-need-label';
  const group=this.node(need,'div');group.setAttribute('role','radiogroup');group.setAttribute('aria-label',__('Mandant'));
  const options=[...card.customer.candidates];
  if(ui.extra && !options.some(c=>c.customer===ui.extra.customer))options.push({...ui.extra,why:__('Chosen by search'),can_select:true});
  let first=null;
  for(const candidate of options){
   const option=this.node(group,'label');option.className='pe-opt'+(ui.choice.customer===candidate.customer ? ' pe-selected' : '')+(candidate.can_select ? '' : ' pe-disabled');
   const input=this.node(option,'input');input.type='radio';input.name=this.id(state,'customer');input.value=candidate.customer;input.checked=ui.choice.customer===candidate.customer;input.disabled=!candidate.can_select;
   input.addEventListener('change',()=>{if(!input.checked)return;ui.choice.customer=candidate.customer;ui.customerName=candidate.customer_name;this.draw();});
   this.lockable(input);if(candidate.can_select)first||=input;
   const text=this.node(option,'span');this.node(text,'strong',candidate.customer_name);this.node(text,'span',candidate.why).className='pe-why';
   if(!candidate.can_select)this.node(text,'span',__('Read-only Mandant; selection requires write access.')).className='pe-why';
  }
  const search=this.button(need,'Search other Mandant',()=>this.search('customers',row=>{ui.choice.customer=row.customer;ui.customerName=row.customer_name;ui.extra={customer:row.customer,customer_name:row.customer_name};this.draw();}),{link:true});
  if(missing){const error=this.node(need,'p',__('Select the Mandant.'));error.className='pe-error';error.id=this.id(state,'customer-error');group.setAttribute('aria-describedby',error.id);}
  this.targets.customer={box:need,input:first || search};
 }
 drawTasks(box,state,ui){
  const card=state.card,tasks=card.tasks;
  if(!tasks.length){
   const section=this.section(box,__('Tasks')),item=this.node(section,'div');item.className='pe-task';
   this.node(item,'strong',__('No task needed'));this.node(item,'p',card.no_task_reason || __('The assistant found no task in the letter.'));return;
  }
  const multi=tasks.length>1,count=ui.choice.selected.filter(Boolean).length;
  const section=this.section(box,multi ? __('Tasks · {0} of {1} selected',[count,tasks.length]) : __('Task'));
  const missing=new Set(ui.showMissing ? this.api.missingFields(card,ui.choice).map(m=>m.key) : []);
  tasks.forEach((task,i)=>this.drawTask(section,state,ui,task,i,multi,missing));
 }
 fact(parent,icon,key,value,hint){
  const fact=this.node(parent,'div');fact.className='pe-fact';const k=this.node(fact,'div');k.className='pe-fact-k';this.icon(k,icon);k.append(__(key,null,PosteingangWorkspace.CONTEXT));
  if(value!==null)this.node(fact,'div',value).className='pe-fact-v';
  const h=this.node(fact,'div',hint ? __(hint) : '');h.className='pe-fact-h';return {fact,hint:h};
 }
 drawTask(section,state,ui,task,i,multi,missing){
  const box=this.node(section,'div');box.className='pe-task'+(ui.choice.selected[i] ? '' : ' pe-off');
  const top=this.node(box,'div');top.className='pe-task-top';
  if(multi){
   const check=this.node(top,'input');check.type='checkbox';check.checked=!!ui.choice.selected[i];check.setAttribute('aria-label',__('Accept {0}',[task.title]));
   check.addEventListener('change',()=>{ui.choice.selected[i]=check.checked;this.draw();});this.lockable(check);
  }
  const head=this.node(top,'div');this.node(head,'strong',task.title);if(task.description)this.node(head,'p',task.description);
  const facts=this.node(box,'div');facts.className='pe-facts';
  const deadline=task.deadline,entered=['relative','uncertain'].includes(deadline.kind);
  if(deadline.kind==='explicit_date'){
   const {fact,hint}=this.fact(facts,'flag','Deadline in the letter',frappe.datetime.str_to_user(deadline.date),null);fact.classList.add('pe-legal');
   if(deadline.verified && deadline.page)this.pageLink(hint,deadline.page);
  }else if(entered){
   const id=this.id(state,'deadline-'+i),value=ui.choice.deadlines[i] || '',fact=this.node(facts,'div');
   fact.className='pe-fact '+(PosteingangWorkspace.date(value) ? 'pe-legal' : 'pe-need');
   const label=this.node(fact,'label');label.className='pe-fact-k';label.setAttribute('for',id);this.icon(label,'flag');label.append(__('Enter deadline'));
   const input=this.node(fact,'input');input.type='text';input.id=id;input.inputMode='numeric';input.autocomplete='off';input.placeholder=__('DD.MM.YYYY');input.value=value;
   input.addEventListener('input',()=>{ui.choice.deadlines[i]=input.value;});this.lockable(input);
   const hint=this.node(fact,'div',__('Not computed'));hint.className='pe-fact-h';hint.id=id+'-hint';
   const error=ui.dateErrors[i] ? 'Enter the date as DD.MM.YYYY.' : missing.has('deadline:'+i) ? 'Enter the deadline date from the letter.' : null;
   if(error){const e=this.node(fact,'div',__(error));e.className='pe-error';e.id=id+'-error';input.setAttribute('aria-invalid','true');}
   input.setAttribute('aria-describedby',[hint.id,error && id+'-error'].filter(Boolean).join(' '));
   this.targets['deadline:'+i]={box:fact,input};
  }else this.fact(facts,'flag','Deadline in the letter',__('none'),'not stated');
  const planned=deadline.kind==='explicit_date' ? '5 working days before the deadline' : deadline.kind==='not_found' ? 'No deadline: in 7 days' : 'Calculated from the deadline you enter';
  this.fact(facts,'cal','Work date',task.internal_date ? frappe.datetime.str_to_user(task.internal_date) : '–',planned);
  const who={...task,...ui.assignees[i]},responsible=this.fact(facts,'user','Responsible',this.person(who.assigned_to,who.assigned_to_name),who.assigned_to && who.assigned_to===this.uploader(state) ? 'uploaded the document' : null);
  responsible.hint.append(responsible.hint.textContent ? ' · ' : '');this.lockable(this.button(responsible.hint,'Change',()=>this.pickAssignee(i),{link:true}));
  if(ui.assigneeErrors[i]){responsible.fact.classList.add('pe-need');this.node(responsible.fact,'div',__(ui.assigneeErrors[i])).className='pe-error';}
  this.targets['assignee:'+i]={box:responsible.fact,input:null};
  const quote=deadline.quote || deadline.wording;
  if(quote){
   const line=this.node(box,'p');line.className='pe-quote';line.append(__('In the letter:')+' ');this.node(line,'q',quote);
   if(!deadline.verified)this.node(line,'span',__('unverified')).className='pe-unverified';
   else if(entered && deadline.page){line.append(' · ');this.pageLink(line,deadline.page);}
  }
 }
 drawFooter(box,state,ui){
  const card=state.card,action=this.api.primaryAction(card,ui.choice),foot=this.node(box,'footer');foot.className='pe-foot';
  const row=this.node(foot,'div');row.className='pe-foot-row';
  this.button(row,action.label,()=>this.accept(),{primary:true,large:true,mutation:true,args:[action.count],icon:action.kind==='archive' ? 'archive' : 'check'});
  this.button(row,'Create manually',()=>{ui.mode='manual';this.draw();},{large:true,icon:'edit'});
  if(ui.showMissing)this.missingLine(foot,state,ui);
  const name=this.customerName(card,ui) || __('the selected Mandant');
  let note;
  if(action.kind==='accept')note=action.count===1 ? __('Creates 1 task for {0}. With “Accept” you confirm Mandant, deadline and task.',[name]) : __('Creates {0} tasks for {1}. With “Accept {0} tasks” you confirm Mandant, deadlines and tasks.',[action.count,name]);
  else note=card.tasks.length ? __('No task selected. The letter is only filed.') : __('The letter is filed with {0} without a task.',[name]);
  this.node(foot,'p',note).className='pe-foot-note';
  if(!state.capabilities.enabled)this.node(foot,'p',__('New PDF intake is disabled')).className='pe-foot-note';
 }
 missingLine(foot,state,ui){
  const items=this.api.missingFields(state.card,ui.choice).map(m=>({key:m.key,text:m.text}));
  for(const index of Object.keys(ui.dateErrors))if(!items.some(m=>m.key==='deadline:'+index))items.push({key:'deadline:'+index,text:'Enter the date as DD.MM.YYYY.'});
  if(!items.length)return;
  const line=this.node(foot,'div');line.className='pe-foot-err';line.setAttribute('role','alert');this.icon(line,'alert');line.append(__('Still missing:')+' ');
  items.forEach((item,index)=>{if(index)line.append(', ');this.button(line,item.text,()=>this.jump(item.key),{link:true});});
 }
 jump(key){
  const target=this.targets[key];if(!target)return;
  const smooth=!window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
  target.box.scrollIntoView?.({block:'center',behavior:smooth ? 'smooth' : 'auto'});
  target.box.classList.remove('pe-flash');void target.box.offsetWidth;target.box.classList.add('pe-flash');target.input?.focus?.();
 }
 // Never blocked in advance: a click on a card with a gap points at the gap instead.
 accept(){
  const state=this.client.state;if(!state)return null;
  const card=state.card,ui=this.ui(state);ui.dateErrors={};
  card.tasks.forEach((task,i)=>{if(ui.choice.selected[i] && ['relative','uncertain'].includes(task.deadline.kind) && PosteingangWorkspace.date(ui.choice.deadlines[i])===false)ui.dateErrors[i]=true;});
  const missing=this.api.missingFields(card,ui.choice),invalid=Object.keys(ui.dateErrors);
  let payload=null;
  if(!missing.length && !invalid.length){try{payload=this.api.simplePayload(this.withAssignees(card,ui),ui.choice);}catch{payload=null;}}
  if(!payload){ui.showMissing=true;this.draw();this.jump(missing[0]?.key || 'deadline:'+invalid[0]);return null;}
  ui.showMissing=false;return this.confirm(payload);
 }
 withAssignees(card,ui){return {...card,tasks:card.tasks.map((task,i)=>ui.assignees[i] ? {...task,...ui.assignees[i]} : task)};}
 uploader(state){return [...this.client.rows,...this.history].find(row=>row.letter===state.letter)?.received_by || null;}
 confirm(payload){const state=this.client.state;return this.run('review.confirm_simple',{letter:state.letter,expected_revision:state.revision,...payload});}
 manualForm(state,ui){
  if(ui.form)return ui.form;
  const card=state.card,choice=ui.choice,picked=card.tasks.filter((_task,i)=>choice.selected[i]!==false);
  const rows=(picked.length ? picked : [null]).map(task=>this.formRow(task,task && ['relative','uncertain'].includes(task.deadline.kind) ? choice.deadlines[card.tasks.indexOf(task)] : null));
  // A legacy draft already names its Mandant on the letter; that wins over the analysis suggestion.
  const stored=card.state==='draft' && state.matching?.selected_customer?.can_select !== false ? state.matching?.selected_customer : null;
  const customer=stored ? {customer:stored.customer,customer_name:stored.customer_name} : choice.customer ? {customer:choice.customer,customer_name:this.customerName(card,ui)} : null;
  card.tasks.forEach((_task,i)=>{if(ui.assignees[i]){const row=rows[picked.indexOf(card.tasks[i])];if(row)Object.assign(row,ui.assignees[i]);}});
  ui.form={customer,rows,errors:{},dirty:false};
  return ui.form;
 }
 formRow(task=null,entered=null){
  const deadline=task?.deadline;
  return {title:task?.title || '',description:task?.description || '',
   external_date:entered || (deadline?.kind==='explicit_date' ? PosteingangWorkspace.german(deadline.date) : ''),
   internal_date:PosteingangWorkspace.german(task?.internal_date),
   // The server requires an assignee; without one the current user is responsible.
   assigned_to:task?.assigned_to || frappe.session.user,assigned_to_name:task?.assigned_to ? task.assigned_to_name : null};
 }
 formField(parent,state,key,label,{full=false,optional=false,hint=null,error=null,control='input',value='',placeholder=null,onInput=null}={}){
  const id=this.id(state,key),wrap=this.node(parent,'div');wrap.className='pe-field'+(full ? ' pe-full' : '');
  const caption=this.node(wrap,'label',__(label,null,PosteingangWorkspace.CONTEXT));caption.setAttribute('for',id);if(optional)this.node(caption,'span',' '+__('(optional)')).className='pe-muted';
  let input=null;
  if(control){
   input=this.node(wrap,control);input.id=id;if(control==='input'){input.type='text';input.autocomplete='off';}
   if(placeholder)input.placeholder=__(placeholder);input.value=value ?? '';input.addEventListener('input',()=>onInput(input.value));this.lockable(input);
  }
  if(hint){const h=this.node(wrap,'p',__(hint));h.className='pe-hint';h.id=id+'-hint';}
  if(error){const e=this.node(wrap,'p',__(error));e.className='pe-error';e.id=id+'-error';input?.setAttribute('aria-invalid','true');}
  const described=[hint && id+'-hint',error && id+'-error'].filter(Boolean).join(' ');if(input && described)input.setAttribute('aria-describedby',described);
  this.targets[key]={box:wrap,input};
  return {wrap,input,id};
 }
 drawManual(box,state,ui){
  const card=state.card,form=this.manualForm(state,ui),draft=card.state==='draft',errors=form.errors;
  this.drawHead(box,state);
  const section=this.section(box,card.tasks.length ? __(draft ? 'Create task manually · prefilled from the saved draft' : 'Create task manually · prefilled from the proposal') : __('Create task manually'));
  const grid=this.node(section,'div');grid.className='pe-form';
  const customer=this.formField(grid,state,'customer','Mandant',{full:true,control:null,error:errors.customer});
  const picked=this.node(customer.wrap,'div');picked.className='pe-picked';this.node(picked,'span',form.customer?.customer_name || __('Not selected'));
  const search=this.button(picked,form.customer ? 'Change' : 'Search Mandant',()=>this.search('customers',row=>{form.customer={customer:row.customer,customer_name:row.customer_name};form.dirty=true;delete form.errors.customer;this.draw();}),{link:!!form.customer});
  search.id=customer.id;if(errors.customer)search.setAttribute('aria-describedby',customer.id+'-error');this.lockable(search);this.targets.customer.input=search;
  form.rows.forEach((row,i)=>{
   const group=this.node(grid,'fieldset');group.className='pe-full pe-task-form';
   if(form.rows.length>1)this.node(group,'legend',__('Task {0}',[i+1]));
   const inner=this.node(group,'div');inner.className='pe-form';
   const edit=key=>value=>{row[key]=value;form.dirty=true;};
   this.formField(inner,state,'title-'+i,'Task title',{full:true,value:row.title,onInput:edit('title'),error:errors['title-'+i],placeholder:'e.g. Check the notice'});
   this.formField(inner,state,'description-'+i,'Description',{full:true,optional:true,control:'textarea',value:row.description,onInput:edit('description')});
   this.formField(inner,state,'external-'+i,'Deadline from the letter',{optional:true,hint:'Only a date stated in the letter',value:row.external_date,onInput:edit('external_date'),error:errors['external-'+i],placeholder:'DD.MM.YYYY'});
   this.formField(inner,state,'internal-'+i,'Work date',{hint:'When the firm works on it; empty: calculated from the deadline',value:row.internal_date,onInput:edit('internal_date'),error:errors['internal-'+i],placeholder:'DD.MM.YYYY'});
   const who=this.formField(inner,state,'assignee-'+i,'Responsible',{control:null}),value=this.node(who.wrap,'div');value.className='pe-picked';
   this.node(value,'span',this.person(row.assigned_to,row.assigned_to_name));
   if(row.assigned_to && row.assigned_to===this.uploader(state))this.node(value,'span',__('uploaded the document')).className='pe-muted';
   const change=this.button(value,'Change',()=>this.pickAssignee(i),{link:true});
   change.id=who.id;this.lockable(change);
   if(errors['assignee-'+i]){const e=this.node(who.wrap,'p',__(errors['assignee-'+i]));e.className='pe-error';e.id=who.id+'-error';change.setAttribute('aria-describedby',e.id);}
   if(form.rows.length>1)this.lockable(this.button(group,'Remove task',()=>{form.rows.splice(i,1);form.errors={};form.dirty=true;this.draw();},{link:true}));
  });
  this.lockable(this.button(section,'+ Another task',()=>{form.rows.push(this.formRow());form.dirty=true;this.draw();this.targets['title-'+(form.rows.length-1)]?.input?.focus?.();},{link:true}));
  const foot=this.node(box,'footer');foot.className='pe-foot';const row=this.node(foot,'div');row.className='pe-foot-row';
  this.button(row,form.rows.length>1 ? 'Create {0} tasks' : 'Create task',()=>this.submitManual(true),{primary:true,large:true,mutation:true,args:[form.rows.length],icon:'check'});
  if(!draft)this.button(row,'Cancel',()=>{ui.mode=null;ui.form=null;this.draw();},{large:true});
  this.node(row,'span').className='pe-grow';
  this.button(row,'File without task',()=>this.submitManual(false),{link:true,mutation:true});
  if(Object.keys(errors).length){const line=this.node(foot,'div');line.className='pe-foot-err';line.setAttribute('role','alert');this.icon(line,'alert');line.append(__('Please check the marked fields.'));}
  if(!state.capabilities.enabled)this.node(foot,'p',__('New PDF intake is disabled')).className='pe-foot-note';
 }
 submitManual(withTasks){
  const state=this.client.state;if(!state)return null;
  const ui=this.ui(state),form=this.manualForm(state,ui),errors={};
  if(!form.customer)errors.customer='Select the Mandant.';
  if(withTasks)form.rows.forEach((row,i)=>{
   if(!row.title.trim())errors['title-'+i]='Enter the task title.';
   if(PosteingangWorkspace.date(row.external_date,true)===false)errors['external-'+i]='Enter the date as DD.MM.YYYY.';
   if(PosteingangWorkspace.date(row.internal_date,true)===false)errors['internal-'+i]='Enter the date as DD.MM.YYYY.';
  });
  form.errors=errors;const first=Object.keys(errors)[0];
  if(first){this.draw();this.jump(first);return null;}
  return this.confirm(this.api.manualPayload({customer:form.customer.customer,rows:withTasks ? form.rows.map(row=>({...row,title:row.title.trim()})) : []}));
 }
 stateBlock(box,tone,icon,title,text){
  const block=this.node(box,'div');block.className='pe-state pe-'+tone;const mark=this.node(block,'div');mark.className='pe-state-icon';this.icon(mark,icon);
  this.node(block,'h3',__(title));if(text)this.node(block,'p',__(text));return block;
 }
 drawAnalyzing(box,state){
  box.setAttribute('aria-busy','true');
  this.drawHead(box,state,{title:state.original.file_name,summary:__('The assistant is reading the letter …')});
  for(const [title,widths] of [['Mandant',[45,30]],['Task',[70,90,60]]]){const section=this.section(box,__(title));for(const width of widths){const bar=this.node(section,'div');bar.className='pe-sk';bar.style.width=width+'%';}}
  const foot=this.node(box,'footer');foot.className='pe-foot';const line=this.node(foot,'p');line.className='pe-foot-note';
  this.icon(line,'spin');line.append(__('Usually under a minute. You can work on other documents meanwhile.'));
  const run=state.analysis.run;
  if(run?.status==='Queued' && run.next_attempt_at)this.node(foot,'p',__('Next attempt no earlier than')+' '+frappe.datetime.str_to_user(run.next_attempt_at)).className='pe-foot-note';
 }
 drawFailed(box,state,ui){
  this.drawHead(box,state,{summary:null});
  const block=this.stateBlock(box,'bad','alert','Automatic analysis failed','The letter could not be read reliably. Please check the original and create the task yourself. This does not mean that no task is needed.');
  const code=state.analysis.run?.error_code;if(code)this.node(block,'p',this.message(Error(code))).className='pe-muted';
  const row=this.node(block,'div');row.className='pe-foot-row pe-center';
  this.button(row,'Create manually',()=>{ui.mode='manual';this.draw();},{primary:true,large:true,icon:'edit'});
  if(state.analysis.allowed_operations.includes('queue_analysis'))this.button(row,'Analyse again',()=>this.startAnalysis(),{large:true,icon:'redo',mutation:true});
 }
 drawNotStarted(box,state,ui){
  this.drawHead(box,state,{summary:null});
  const can=state.analysis.allowed_operations.includes('queue_analysis');
  const block=this.stateBlock(box,'neutral','file','Not analysed yet',can ? 'Start the analysis or create the task yourself.' : 'Automatic analysis is not available. Please check the original and create the task yourself.');
  const row=this.node(block,'div');row.className='pe-foot-row pe-center';
  if(can)this.button(row,'Start analysis',()=>this.startAnalysis(),{primary:true,large:true,icon:'redo',mutation:true});
  this.button(row,'Create manually',()=>{ui.mode='manual';this.draw();},{primary:!can,large:true,icon:'edit'});
 }
 drawDone(box,state){
  this.drawHead(box,state,{summary:null});
  const cancelled=state.status==='Cancelled',receipt=state.review.receipt?.actions || [],count=receipt.length,customer=state.matching.selected_customer?.customer_name;
  // The count comes from the receipt: a Task hidden by permissions was still created.
  const title=cancelled ? 'Discarded' : count ? (count===1 ? '1 task created' : '{0} tasks created') : 'Filed without task';
  const block=this.node(box,'div');block.className='pe-state '+(cancelled ? 'pe-neutral' : 'pe-ok');const mark=this.node(block,'div');mark.className='pe-state-icon';this.icon(mark,cancelled ? 'archive' : 'check');
  this.node(block,'h3',__(title,[count]));
  this.node(block,'p',cancelled ? __('The document was discarded. No task was created.') : customer ? __('The original is filed with {0} under Documents.',[customer]) : __('The original is filed under Documents.'));
  if(receipt.some(action=>action.task)){
   const list=this.node(block,'div');list.className='pe-created';
   for(const action of receipt)if(action.task){
    const link=this.node(list,'a');link.href=frappe.utils.get_form_link('Task',action.task);this.icon(link,'link');
    const text=this.node(link,'span');this.node(text,'strong',state.review.review?.actions?.find(a=>a.action_id===action.action_id)?.title || action.task);if(customer)this.node(text,'small',customer);
   }
  }
  const row=this.node(block,'div');row.className='pe-foot-row pe-center';
  const others=this.client.rows.filter(r=>r.letter!==state.letter),next=others.find(r=>this.api.listChip(r).tone!=='busy') || others[0];
  if(next)this.button(row,'Next document',()=>this.select(next.letter),{primary:true,large:true});
  if(!cancelled && state.card.undo_until)this.button(row,'Undo',()=>this.undo(state.letter),{large:true,mutation:true});
 }
 // Administrative correction, moved from the old editor unchanged.
 drawCorrection(box,state){
  const allowed=state.review.allowed_operations,correction=state.review.correction;
  if(!allowed.includes('create_correction') && !correction)return;
  const section=this.section(box,__('Correction'));
  if(allowed.includes('create_correction'))this.button(section,'Create correction',()=>{
   const valid=this.capture();
   const dialog=new frappe.ui.Dialog({title:__('Create correction'),fields:[{fieldname:'reason',fieldtype:'Small Text',label:__('Correction reason'),reqd:1},{fieldname:'reuse_original',fieldtype:'Check',label:__('Reuse original PDF'),default:1},{fieldname:'received_date_known',fieldtype:'Check',label:__('Receipt date known'),default:0},{fieldname:'received_date',fieldtype:'Date',label:__('Receipt date')},{fieldname:'upload',fieldtype:'HTML'}],primary_action_label:__('Create correction'),primary_action:async values=>{
    if(!valid())return;if(!values.reuse_original && file.files.length!==1){frappe.msgprint(__('Choose one PDF for the replacement.'));return;}
    const result=await this.run('review.create_correction',{old_letter:state.letter,expected_revision:state.revision,reason:values.reason,reuse_original:!!values.reuse_original,received_date_known:!!values.received_date_known,received_date:values.received_date_known ? values.received_date : null,...(!values.reuse_original ? {_file:file.files[0]} : {})});if(result)dialog.hide();
   }});this.dialogs.add(dialog);const file=this.node(dialog.fields_dict.upload.$wrapper[0],'input');file.type='file';file.accept='.pdf,application/pdf';file.setAttribute('aria-label',__('Choose one PDF for the replacement.'));dialog.onhide=()=>this.dialogs.delete(dialog);dialog.show();
  },{mutation:true,requiresEnabled:false});
  if(correction){
   this.node(section,'p',__(correction.ready ? 'Administrative review is complete.' : 'Administrative review of the original tasks is required.'));
   if(allowed.includes('correction_disposition'))for(const task of correction.tasks || []){
    this.node(section,'p',task.task+' · '+__(task.status)+' · '+__(task.disposition || 'Pending'));
    const a=this.node(section,'a',__('Open task'));a.href=frappe.utils.get_form_link('Task',task.task);
    this.button(section,'Resolve original task',()=>this.promptFields([{fieldname:'disposition',fieldtype:'Select',label:__('Disposition'),options:['cancelled','historical'],reqd:1},{fieldname:'reason',fieldtype:'Small Text',label:__('Reason'),reqd:1}],values=>this.run('review.correction_disposition',{replacement:state.letter,expected_revision:state.revision,original_action_id:task.action_id,...values}),'Resolve original task'),{mutation:true,requiresEnabled:false});
   }
  }
 }
}
