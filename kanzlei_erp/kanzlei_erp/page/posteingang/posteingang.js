frappe.pages['posteingang'].on_page_load = wrapper => {
 wrapper.ap09Ready=frappe.require('posteingang_workspace.bundle.js').then(()=>{wrapper.ap09=new PosteingangWorkspace(wrapper);return wrapper.ap09;});
};
frappe.pages['posteingang'].on_page_show = async wrapper => {await wrapper.ap09Ready;wrapper.ap09?.show();};

class PosteingangWorkspace {
 assessment(value){return __(({readable:'Readable',partial:'Partly readable',unreadable:'Unreadable',actions_found:'Actions proposed',none_found:'No actions proposed by AI',unknown:'Action assessment unknown',none:'Review the proposed Mandant before selecting it.',not_found:'No Mandant candidates found.',multiple_matches:'Several Mandant candidates match. An explicit decision is required.',multiple_addressees:'Several addressees require clarification before selecting one Mandant.'})[value] || 'Unknown');}
 deadlineLabel(deadline){return deadline.wording || __(({explicit_date:'Explicit date',relative:'Relative deadline',not_found:'No deadline stated',uncertain:'Uncertain deadline'})[deadline.kind] || 'Unknown');}
 constructor(wrapper) {
  this.page=frappe.ui.make_app_page({parent:wrapper,title:__('PDF inbox'),single_column:true});
  this.api=frappe.posteingang;this.client=new this.api.WorkspaceClient(this.transport.bind(this),()=>frappe.session.user);
  this.filters={state:'review',uploaded_by:'all',query:'',sort:'oldest'};this.visible=false;this.dialogs=new Set();this.step=0;this.fieldIndex=0;
  this.root=this.node(this.page.main[0],'div');this.root.className='posteingang-workspace';this.root.dataset.panel='queue';
  this.documentHeader=this.node(this.root,'div');this.documentHeader.className='ap09-document-header';
  this.button(this.documentHeader,'Back to inbox',async()=>{if(this.client.intent){this.failure(Error('UNKNOWN_OUTCOME'));return;}if(await this.clean())frappe.set_route('posteingang');});
  this.documentTitle=this.node(this.documentHeader,'h2');
  const tabs=this.node(this.documentHeader,'div');tabs.className='ap09-mobile-tabs';
  this.button(tabs,'Original PDF',()=>this.panel('pdf'));this.button(tabs,'Back to review',()=>this.panel('review'));
  this.steps=this.node(this.root,'nav');this.steps.className='ap09-steps';this.steps.setAttribute('aria-label',__('Review steps'));
  this.notice=this.node(this.root,'div');this.notice.setAttribute('role','status');this.notice.className='ap09-notice';
  const layout=this.node(this.root,'div');layout.className='ap09-layout';
  this.queuePanel=this.node(layout,'section');this.queuePanel.className='ap09-panel ap09-queue';
  this.pdfPanel=this.node(layout,'section');this.pdfPanel.className='ap09-panel ap09-pdf';
  this.editor=this.node(layout,'section');this.editor.className='ap09-panel ap09-review';
  this.node(this.queuePanel,'h2',__('Review incoming documents'));
  this.node(this.queuePanel,'p',__('Check the original, select the Mandant and prepare the next steps.')).className='text-muted';
  this.views=this.node(this.queuePanel,'nav');this.views.className='ap09-views';this.views.setAttribute('aria-label',__('Inbox views'));
  for(const [value,label] of [['review','To review'],['processing','In progress'],['Needs Clarification','Needs clarification'],['history','Completed documents']]){
   const button=this.button(this.views,label,()=>{this.filters.state=value;this.loadQueue();});button.dataset.view=value;
  }
  const filters=this.node(this.queuePanel,'div');filters.className='ap09-filters';
  this.control(filters,'Search letters','',v=>{this.filters.query=v || '';clearTimeout(this.searchTimer);this.searchTimer=setTimeout(()=>this.loadQueue(),300);},{max:140});
  this.control(filters,'Sort','oldest',v=>{this.filters.sort=v;this.loadQueue();},{options:[['oldest','Oldest first'],['newest','Newest first']]});
  this.button(filters,'Refresh',()=>this.loadQueue());
  const advanced=this.node(this.queuePanel,'details');this.node(advanced,'summary',__('More filters'));
  const extra=this.node(advanced,'div');extra.className='ap09-filters';
  this.control(extra,'Uploaded by','all',v=>{this.filters.uploaded_by=v;this.loadQueue();},{options:[['all','All'],['me','Me']]});
  this.stateFilter=this.control(extra,'Queue state','review',v=>{this.filters.state=v;this.loadQueue();},{options:[['review','To review'],['processing','In progress'],['open','Unfinished letters'],['history','History'],['all','All'],...['Received','Queued','Processing','Needs Review','Needs Clarification','Error','Completed','Cancelled'].map(s=>[s,s])]});
  this.rows=this.node(this.queuePanel,'div');this.more=this.button(this.queuePanel,'More',()=>this.loadQueue(true));this.more.hidden=true;
  this.node(this.queuePanel,'p',__('Tasks are created only after your final confirmation.')).className='ap09-queue-note text-muted';
  this.panel('queue');
  this.routeHandler=()=>{if(this.onRoute())this.show();else this.hide();};
  frappe.router.on('change',this.routeHandler);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)this.pause();else if(this.onRoute())this.show();});
  window.addEventListener('beforeunload',event=>{if(this.client.review.dirty || this.client.intent){event.preventDefault();event.returnValue='';}});
 }
 node(parent,tag,value='') {const element=document.createElement(tag);element.textContent=value;parent.append(element);return element;}
 button(parent,label,handler,{mutation=false,disabled=false,requiresEnabled=true,primary=false}={}) {
  const button=this.node(parent,'button',__(label));button.type='button';button.className='btn btn-default btn-sm';button.disabled=disabled;
  if(primary)button.className='btn btn-primary btn-sm';
  if(mutation){button.dataset.mutation='1';button.dataset.requiresEnabled=String(requiresEnabled);button.dataset.originalDisabled=disabled ? 'yes' : 'no';}button.addEventListener('click',handler);return button;
 }
 control(parent,label,value,onchange,{type='text',options=null,disabled=false,max=null}={}) {
  const wrap=this.node(parent,'div');wrap.className='ap09-field';
  const fieldname='inbox_'+(++this.fieldIndex),initial=type==='checkbox' ? Number(!!value) : value ?? '';
  // Like Frappe Layout, supply a document before constructing the control.
  // A second initialization after Date's picker is mounted queues stale changes.
  const field=frappe.ui.form.make_control({parent:wrap,doc:{[fieldname]:initial},df:{fieldname,label:__(label),fieldtype:options ? 'Select' : ({textarea:'Small Text',date:'Date',checkbox:'Check'}[type] || 'Data'),options:options?.map(([value,label])=>({value,label:__(label)})),onchange:()=>{if(!wrap.isConnected || field.$input?.prop('disabled'))return;onchange(type==='checkbox' ? !!field.get_value() : field.get_value() || null);}},render_input:true});
  const input=field.$input[0];input.disabled=disabled;input.dataset.originalDisabled=disabled ? 'yes' : 'no';input.setAttribute('aria-label',__(label));if(max)input.maxLength=max;
  if(!options && ['text','textarea'].includes(type))input.addEventListener('input',()=>{if(wrap.isConnected && !input.disabled)onchange(input.value || null);});
  // A blur/change can be followed immediately by Save; do not wait for
  // Frappe's asynchronous model callback before updating our review buffer.
  if(type==='date')input.addEventListener('change',()=>{if(!wrap.isConnected || input.disabled)return;const date=field.get_value();onchange(date && frappe.datetime.validate(date) ? date : null);});
  input.ap09Control=field;return input;
 }
 setValue(input,value){const field=input?.ap09Control;if(field){field.doc[field.df.fieldname]=value ?? '';field.set_input(value ?? '');}}
 disposeFields(parent){for(const element of parent.querySelectorAll('.frappe-control'))element.fieldobj?.datepicker?.destroy();}
 clearEditor(){this.disposeFields(this.editor);this.editor.replaceChildren();this.human=null;}
 panel(name){
  this.root.dataset.panel=name;
  this.documentHeader.hidden=name==='queue';this.steps.hidden=name==='queue';
  if(name==='queue')this.page.set_primary_action(__('Upload PDF'),()=>this.api.open({onUploaded:()=>this.loadQueue(),onOpen:name=>this.select(name)}),'add').attr('aria-label',__('Upload PDF'));
  else this.page.clear_primary_action();
 }
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
   UNKNOWN_OUTCOME:'Resolve the unknown request before another operation.',DIRTY_REVIEW:'Save or discard the current review before continuing.'};
  return __(messages[error.message] || 'The operation could not finish. Refresh and try again.');
 }
 failure(error){
  if(!this.visible)return;
  if(error.message==='PERMISSION'){this.client.clear();this.pdfPanel.replaceChildren();this.clearEditor();this.rows.replaceChildren();this.queueSignature=null;this.documentTitle.textContent='';this.steps.replaceChildren();this.page.clear_primary_action();for(const d of this.dialogs)d.hide();}
  this.notice.textContent=this.message(error);
  if(this.client.intent)this.button(this.notice,'Check and retry the same request',()=>this.retry());
 }
 pause(){clearTimeout(this.queueTimer);clearTimeout(this.statusTimer);clearTimeout(this.searchTimer);}
 hide(){this.visible=false;this.pause();this.client.suspend();this.pdfPanel.replaceChildren();this.clearEditor();this.rows.replaceChildren();this.queueSignature=null;this.documentTitle.textContent='';this.steps.replaceChildren();for(const d of this.dialogs)d.hide();}
 async show(){
  if(!this.onRoute() || document.hidden)return;
  const name=frappe.get_route()[1] || null;
  if(this.navigationPending)return;
  if(this.client.state && this.client.state.letter!==name && this.client.review.dirty){
   const previous=this.client.state.letter;this.visible=true;this.client.active=true;this.navigationPending=true;
   try{await this.client.status();if(!this.client.state)return;const proceed=await this.clean();if(!proceed){frappe.set_route('posteingang',previous);return;}}catch(error){this.failure(error);return;}finally{this.navigationPending=false;}
   return this.show();
  }
  if(this.showing===name && this.visible)return;
  this.showing=name;this.visible=true;this.client.active=true;this.pause();
  try{
   if(name){
    if(this.client.state?.letter===name && (this.client.review.dirty || this.client.intent))await this.client.status();
    else if(!this.client.intent){for(const dialog of this.dialogs)dialog.hide();this.clearEditor();this.pdfPanel.replaceChildren();this.documentTitle.textContent='';this.steps.replaceChildren();await this.client.load(name);}
    if(!this.visible || !this.onRoute() || frappe.get_route()[1]!==name)return;
    if(this.client.state?.letter===name)this.draw();else{this.pdfPanel.replaceChildren();this.clearEditor();this.failure(Error('UNKNOWN_OUTCOME'));}this.panel('review');
   }else{this.pdfPanel.replaceChildren();this.clearEditor();this.node(this.editor,'p',__('Choose a letter from the queue.'));this.panel('queue');}
   await this.loadQueue();this.schedule();
  }catch(error){this.failure(error);}finally{this.showing=undefined;}
 }
 schedule(){
  this.pause();if(!this.visible || document.hidden || !this.onRoute() || this.client.user()!==this.client.owner)return;
  this.queueTimer=setTimeout(async()=>{await this.loadQueue();this.scheduleQueue();},15000);this.scheduleStatus();
 }
 scheduleQueue(){if(this.visible && !document.hidden)this.queueTimer=setTimeout(async()=>{await this.loadQueue();this.scheduleQueue();},15000);}
 scheduleStatus(){if(this.visible && !document.hidden)this.statusTimer=setTimeout(async()=>{
  if(!this.statusBusy && !this.client.busy && this.client.state){this.statusBusy=true;const before=this.client.state;try{await this.client.status();if(!this.client.state){this.pdfPanel.replaceChildren();this.clearEditor();}else if(this.client.state!==before)this.draw();else {if(this.client.referencesChanged){this.client.referencesChanged=false;this.draw();}this.statusNotice();}}catch(error){this.failure(error);}finally{this.statusBusy=false;}}
  this.scheduleStatus();
 },5000);}
 async loadQueue(more=false){
  if(!this.visible)return;if(this.queueBusy){this.queueAgain=true;return;}
  this.queueBusy=true;
  try{const result=await this.client.queue({...this.filters},more ? this.cursor : null);if(!result || !this.visible)return;this.cursor=result.next_cursor;this.drawRows();this.more.hidden=!this.cursor;}
  catch(error){this.failure(error);}finally{this.queueBusy=false;if(this.queueAgain){this.queueAgain=false;this.loadQueue();}}
 }
 drawRows(){
  for(const button of this.views.querySelectorAll('[data-view]'))button.setAttribute('aria-pressed',String(button.dataset.view===this.filters.state));
  this.setValue(this.stateFilter,this.filters.state);
  const signature=JSON.stringify(this.client.rows);if(signature===this.queueSignature)return;this.queueSignature=signature;
  this.rows.replaceChildren();if(!this.client.rows.length)this.node(this.rows,'p',__(this.filters.query ? 'No matching documents.' : 'No documents in this view.')).className='ap09-empty text-muted';
  for(const row of this.client.rows){
   const button=this.button(this.rows,row.file_name,()=>this.select(row.letter));button.className='ap09-letter';button.setAttribute('aria-current',String(this.client.state?.letter===row.letter));
   button.textContent='';const identity=this.node(button,'span');identity.className='ap09-letter-identity';this.node(identity,'strong',row.letter_type || row.file_name);
   this.node(identity,'span',[row.sender,row.file_name,frappe.datetime.str_to_user(row.received_at)].filter(Boolean).join(' · ')).className='text-muted small';
   const customer=this.node(button,'span',row.customer?.customer_name || row.matching?.candidates?.map(c=>c.customer_name).join(' / ') || __('Not assigned to a Mandant'));
   if(!row.customer)this.node(customer,'span',__('Not yet confirmed')).className='text-muted small';
   const detail=this.node(button,'span');this.node(detail,'span',__(row.status)).className='indicator-pill '+({Completed:'green',Error:'red','Needs Clarification':'orange',Processing:'blue',Queued:'blue'}[row.status] || 'gray');
   if(row.deadline?.proposed_date)this.node(detail,'span',__('Unverified proposed deadline')+': '+frappe.datetime.str_to_user(row.deadline.proposed_date)).className='text-muted small';
   if(row.result_error || row.analysis.run?.error_code)this.node(detail,'span',this.message(Error(row.result_error || row.analysis.run.error_code))).className='text-danger small';
   this.node(button,'span',__(row.status==='Completed' ? 'View result' : 'Open review')).className='ap09-open text-primary';
  }
 }
 async clean(){
  if(!this.client.review.dirty)return true;
  return new Promise(resolve=>{
   const dialog=new frappe.ui.Dialog({title:__('Unsaved review'),fields:[{fieldtype:'HTML',fieldname:'choices'}]});this.dialogs.add(dialog);let settled=false;
   const end=value=>{settled=true;dialog.hide();resolve(value);};const root=dialog.fields_dict.choices.$wrapper[0];
   this.button(root,'Save and continue',async()=>{if(await this.submit('save_review'))end(true);});
   this.button(root,'Discard',()=>{this.client.review.dirty=false;end(true);});this.button(root,'Stay',()=>end(false));
   dialog.onhide=()=>{this.dialogs.delete(dialog);if(!settled)resolve(false);};dialog.show();
  });
 }
 async select(name){if(this.operationBusy || this.client.busy)return;if(this.client.intent){this.failure(Error('UNKNOWN_OUTCOME'));return;}if(await this.clean())frappe.set_route('posteingang',name);}
 async run(operation,args){
  if(this.operationBusy)return null;
  this.operationBusy=true;
  try{
   const pending=this.client.mutate(operation,args);this.syncControls();const result=await pending;
   if(!result || !this.visible)return null;
   const loaded=await this.client.load(result.replacement || this.client.state.letter);
   if(!loaded || !this.visible)return null;
   if(result.replacement)frappe.set_route('posteingang',result.replacement);
   this.draw();await this.loadQueue();return result;
  }catch(error){this.failure(error);return null;}finally{this.operationBusy=false;this.syncControls();}
 }
 async retry(){
  if(this.operationBusy)return;
  this.operationBusy=true;
  try{
   const result=await this.client.retry();if(!result || !this.visible)return;
   const name=result.replacement || this.client.state.letter,loaded=await this.client.load(name);
   if(!loaded || !this.visible)return;
   frappe.set_route('posteingang',name);this.draw();await this.loadQueue();
  }catch(error){this.failure(error);}finally{this.operationBusy=false;this.syncControls();}
 }
 statusNotice(){
  if(!this.client.state)return;
  this.notice.replaceChildren();const state=this.client.remote || this.client.state;
  this.node(this.notice,'span',__(state.status)+(this.client.review.dirty ? ' · '+__('Unsaved review') : ''));
  if(this.human)this.setValue(this.human,Number(this.client.review.humanVerified));
  if(!state.capabilities.enabled)this.node(this.notice,'p',__('New PDF intake is disabled'));
  if(this.client.conflict){this.node(this.notice,'p',__('The letter changed. Your local review has been preserved.'));
   this.button(this.notice,'Compare with saved review',()=>this.compare());}
  const run=state.analysis?.run;
  if(run?.error_code)this.node(this.notice,'p',this.message(Error(run.error_code)));
  if(run?.status==='Queued' && run.next_attempt_at)this.node(this.notice,'p',__('Next attempt no earlier than')+' '+frappe.datetime.str_to_user(run.next_attempt_at));
  if(this.client.intent)this.button(this.notice,'Check and retry the same request',()=>this.retry());
  this.syncControls();
 }
 syncControls(){
  const locked=this.operationBusy || this.client.busy || !!this.client.intent || this.client.conflict;
  for(const control of this.editor.querySelectorAll('input,select,textarea')){if(!control.dataset.originalDisabled)control.dataset.originalDisabled=control.disabled ? 'yes' : 'no';control.disabled=locked || control.dataset.originalDisabled==='yes';}
  const caps=this.client.remote?.capabilities || this.client.state?.capabilities;
  for(const button of this.editor.querySelectorAll('[data-mutation]'))button.disabled=locked || button.dataset.originalDisabled==='yes' || !caps?.customer_write || (!caps?.enabled && button.dataset.requiresEnabled==='true');
  for(const button of this.root.querySelectorAll('[data-navigation]'))button.disabled=locked;
 }
 async compare(){
  const sequence=this.client.sequence,name=this.client.state.letter;
  try{const current=await this.transport('get_letter_workspace',{letter:name});if(sequence!==this.client.sequence || !this.visible)return;this.client.check(current);
   const dialog=new frappe.ui.Dialog({title:__('Compare with saved review'),size:'extra-large',fields:[{fieldtype:'HTML',fieldname:'versions'}]});this.dialogs.add(dialog);const box=dialog.fields_dict.versions.$wrapper[0];
   const version=(label,fields,review)=>{this.node(box,'h3',__(label));for(const [key,title] of [['sender','Sender'],['document_date','Document date'],['letter_type','Letter type'],['summary','Summary']])this.node(box,'p',__(title)+': '+(fields[key] || __('Unknown')));
    for(const action of review.actions){this.node(box,'h4',action.title);for(const [key,title] of [['description','Action description'],['decision','Decision'],['assigned_to','Responsible'],['internal_date','Internal work date'],['external_date','Confirmed external deadline'],['external_date_basis','External deadline basis'],['assignment_reason','Reason for no assignee'],['scheduling_reason','Reason for no internal date'],['rejection_reason','Rejection reason']])this.node(box,'p',__(title)+': '+(action[key] || __('Unknown')));}
    this.node(box,'p',__('Reason for no actions')+': '+(review.no_actions_reason || __('Unknown')));this.node(box,'p',__('Clarification reason')+': '+(review.clarification_reason || __('Unknown')));};
   version('Local review',this.client.review.documentFields,this.client.review.review);version('Saved review',current.review.document_fields,current.review.review);
   const valid=()=>sequence===this.client.sequence && this.visible && !this.client.busy && !this.client.intent && this.onRoute() && frappe.get_route()[1]===name && frappe.session.user===this.client.owner && this.client.state?.letter===name;this.button(box,'Discard local review and load saved version',async()=>{if(!valid())return;dialog.hide();await this.client.load(name);this.draw();});dialog.onhide=()=>this.dialogs.delete(dialog);dialog.show();
  }catch(error){this.failure(error);}
 }
 edited(){this.client.review.edited();if(this.human)this.human.checked=false;this.statusNotice();}
 evidence(parent,items){
  for(const item of items || []){this.node(parent,'p',__(item.verification)+': '+(item.quote || ''));
   if(Number.isInteger(item.page) && item.page>=1 && item.page<=this.client.state.original.page_count)this.button(parent,__('Page {0}',[item.page]),()=>{this.pdfPage=item.page;this.frame.src=this.client.state.original.preview_url+'#page='+item.page;this.panel('pdf');});}
 }
 draw(){
  const state=this.client.state;if(!state || !this.visible)return;
  if(this.activeLetter!==state.letter){this.activeLetter=state.letter;this.step=0;this.pdfPage=null;}
  this.human=null;this.clearEditor();this.steps.replaceChildren();
  this.documentTitle.textContent=state.original.file_name;
  const finished=['Completed','Cancelled'].includes(state.status);
  if(!finished)for(const [index,label] of ['Document and Mandant','Tasks and deadlines','Final review'].entries()){
   const button=this.button(this.steps,(index+1)+'. '+__(label),()=>{if(index<this.step){this.step=index;this.client.review.humanVerified=false;this.draw();}},{disabled:index>this.step});
   button.classList.add('ap09-step');button.setAttribute('aria-current',index===this.step ? 'step' : 'false');if(index<=this.step)button.dataset.navigation='1';
  }
  if(!this.frame?.isConnected || this.frame.dataset.original!==state.original.preview_url){
  this.pdfPanel.replaceChildren();const pdfTools=this.node(this.pdfPanel,'div');pdfTools.className='ap09-pdf-tools';this.node(pdfTools,'h3',__('Original PDF'));
  for(const [label,url] of [['Open original PDF',state.original.preview_url],['Download',state.original.download_url]]){const a=this.node(this.pdfPanel,'a',__(label));a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.className='btn btn-default btn-sm mr-2';}
  this.frame=this.node(this.pdfPanel,'iframe');this.frame.dataset.original=state.original.preview_url;this.frame.src=state.original.preview_url+(this.pdfPage ? '#page='+this.pdfPage : '');this.frame.title=__('Original PDF');
  }
  if(finished){this.drawResult();this.statusNotice();return;}
  this.node(this.editor,'div',__('Step {0} of 3',[this.step+1])).className='text-muted small';
  this.node(this.editor,'h3',__([ 'Document and Mandant','Tasks and deadlines','Final review'][this.step]));
  this.node(this.editor,'p',__([ 'Check the original and explicitly select the Mandant.','Decide what needs to be done and plan the work.','Check exactly what will be created before confirming.'][this.step])).className='text-muted';
  if(this.step===0)this.drawAnalysis();
  this.matchingBox=this.node(this.editor,'section');this.matchingBox.hidden=this.step!==0;
  if(this.step===0){this.drawMatching();this.drawProposal();}
  this.drawReview();
  this.receiptsBox=this.node(this.editor,'section');this.drawReceipt();this.correctionBox=this.node(this.editor,'section');this.drawCorrection();this.statusNotice();
 }
 drawAnalysis(){
  const state=this.client.state,editable=state.review.allowed_operations.includes('save_review'),preparing=!editable || ['Received','Error'].includes(state.status);
  const actions=this.node(this.editor,preparing ? 'div' : 'details');actions.className='ap09-analysis';
  if(!preparing)this.node(actions,'summary',__('Analysis and document options'));
  for(const op of state.analysis.allowed_operations)this.button(actions,({queue_analysis:'Start analysis',manual_review:'Switch to human review',cancel:'Cancel letter'})[op],async()=>{
   if(!await this.clean())return;
   const perform=reason=>this.run('analysis.'+op,{letter:state.letter,expected_revision:this.client.state.revision,...(reason ? {reason} : {})});
   if(op==='cancel')this.prompt('Reason',values=>perform(values.value));
   else if(op==='queue_analysis' && state.status==='Needs Review')this.ask(__('Analyze this letter again? Your current review edits will be preserved.'),()=>perform());else perform();
  },{mutation:true,requiresEnabled:op!=='cancel',primary:preparing && op==='queue_analysis'});
  if(preparing)this.node(actions,'p',__('Start the analysis or switch to human review to prepare a draft.')).className='text-muted small';
 }
 capture(){const sequence=this.client.sequence,name=this.client.state?.letter,owner=this.client.owner;return ()=>this.visible && this.client.active && !this.client.busy && !this.client.intent && !this.client.conflict && this.client.sequence===sequence && this.client.state?.letter===name && frappe.session.user===owner && this.onRoute() && frappe.get_route()[1]===name;}
 track(dialog){if(dialog){this.dialogs.add(dialog);const old=dialog.onhide;dialog.onhide=()=>{this.dialogs.delete(dialog);old?.();};}return dialog;}
 promptFields(fields,callback,title){const valid=this.capture();return this.track(frappe.prompt(fields,values=>{if(valid())return callback(values);},__(title)));}
 prompt(label,callback){return this.promptFields({fieldname:'value',fieldtype:'Small Text',label:__(label),reqd:1},callback,label);}
 ask(message,callback){const valid=this.capture();return this.track(frappe.confirm(message,()=>{if(valid())return callback();}));}
 async search(kind,choose){
  const name=this.client.state.letter,sequence=this.client.sequence,valid=this.capture();let start=0;
  const dialog=new frappe.ui.Dialog({title:__(kind==='customers' ? 'Find another Mandant' : 'Select assignee'),fields:[{fieldname:'text',fieldtype:'Data',label:__('Search')},{fieldname:'results',fieldtype:'HTML'}]});this.dialogs.add(dialog);
  const root=dialog.fields_dict.results.$wrapper[0];let request=0;
  const load=async more=>{const stamp=++request;try{
   const result=await this.transport(kind==='customers' ? 'matching.search_customers' : 'review.search_assignees',{letter:name,text:dialog.get_value('text') || '',...(kind==='customers' ? {start:more ? start : 0} : {})});
   if(!valid() || stamp!==request || !dialog.$wrapper.is(':visible'))return;
   if(!more)root.replaceChildren();
   if(!result.rows.length)this.node(root,'p',__('No entries'));
   for(const row of result.rows){this.node(root,'p',row.customer_name || row.full_name || row.user);if(kind!=='customers' || row.can_select)this.button(root,'Select',()=>{if(!valid())return;dialog.hide();choose(row);});else this.node(root,'p',__('Read-only Customer; selection requires write access.'));}
   if(result.has_next){start=result.next_start;this.button(root,'More',()=>load(true));}
  }catch(error){if(sequence===this.client.sequence){root.replaceChildren();this.failure(error);}}};
  dialog.set_primary_action(__('Search'),()=>load(false));dialog.onhide=()=>this.dialogs.delete(dialog);dialog.on_page_show=()=>load(false);dialog.show();
 }
 drawMatching(){
  this.matchingBox.replaceChildren();const state=this.client.state,matching=state.matching;this.node(this.matchingBox,'h3',__('Mandant selection'));
  this.button(this.matchingBox,'Refresh Mandant candidates',async()=>{try{await this.client.references();this.drawMatching();this.drawReceipt();this.drawCorrection();this.statusNotice();}catch(error){this.failure(error);}});
  if(matching.selected_customer)this.node(this.matchingBox,'p',matching.selected_customer.customer_name+' · '+matching.customer_reason);
  const select=async customer=>{if(!await this.clean())return;this.prompt('Reason',values=>this.run('matching.select_customer',{letter:state.letter,customer,reason:values.value,expected_revision:this.client.state.revision}));};
  if(!matching.matching)this.node(this.matchingBox,'p',__('No completed analysis is available. You can search for a Mandant manually.'));
  else{this.node(this.matchingBox,'p',this.assessment(matching.matching.ambiguity));for(const candidate of matching.matching.candidates){
   this.node(this.matchingBox,'strong',candidate.customer_name);for(const reason of candidate.reasons){this.node(this.matchingBox,'p',__(reason.kind==='exact_identifier' ? 'Verified identifier match' : 'Matching name'));this.evidence(this.matchingBox,reason.evidence);}
   for(const conflict of candidate.conflicts)this.node(this.matchingBox,'p',__(conflict.code==='identifier_conflict' ? 'Conflicting identifiers require review.' : 'The extracted name differs from this Customer.'));
   if(candidate.can_select && matching.allowed_operations.includes('select_customer'))this.button(this.matchingBox,'Select Mandant',()=>select(candidate.customer),{mutation:true});
  }}
  if(matching.allowed_operations.includes('select_customer'))this.button(this.matchingBox,'Find another Mandant',async()=>{if(await this.clean())this.search('customers',row=>select(row.customer));},{mutation:true});
  if(matching.allowed_operations.includes('clear_customer'))this.button(this.matchingBox,'Clear Mandant selection',()=>select(null),{mutation:true});
 }
 drawProposal(){
  const state=this.client.state,proposal=state.review.proposal;if(!proposal)return;
  const box=this.node(this.editor,'details');this.node(box,'summary',__('Latest analysis proposal'));this.node(box,'p',this.assessment(proposal.extraction.readability)+' · '+this.assessment(proposal.extraction.action_assessment));
  for(const warning of proposal.extraction.warnings || [])this.node(box,'p',warning.message);
  for(const key of ['sender','document_date','letter_type','summary']){this.node(box,'p',proposal.extraction[key].value || __('Unknown'));this.evidence(box,proposal.extraction[key].evidence);}
  for(const action of proposal.extraction.actions){this.node(box,'strong',action.title);this.node(box,'p',action.description);this.node(box,'p',this.deadlineLabel(action.deadline));this.evidence(box,action.evidence);}
  if(state.review.allowed_operations.includes('apply_result'))this.button(this.editor,'Apply latest analysis result',async()=>{
   if(!await this.clean())return;const replace=!!state.review.applied_run || !!this.client.review.review.actions.length || Object.values(this.client.review.documentFields).some(Boolean);
   const apply=()=>this.run('review.apply_result',{letter:state.letter,expected_run:proposal.run,expected_revision:this.client.state.revision,acknowledge_replace:replace});
   if(replace)this.ask(__('Replace the current review? Its previous version will be preserved in history.'),apply);else apply();
  },{mutation:true});
 }
 drawReview(){
  const client=this.client.review,state=this.client.state,editable=state.review.allowed_operations.includes('save_review');
  if(this.step===0){
   const facts=this.node(this.editor,'section');facts.className='ap09-section';
   this.node(facts,'h4',__('Document details'));
   this.receiptDates(facts);
   for(const [key,label,type,max] of [['sender','Sender','text',255],['document_date','Document date','date'],['letter_type','Letter type','text',255],['summary','Summary','textarea',4000]])this.control(facts,label,client.documentFields[key],value=>{client.editFields({[key]:value});this.edited();},{type,max,disabled:!editable});
  }else if(this.step===1){
   for(const action of client.review.actions)this.drawAction(action,editable);
   if(state.review.allowed_operations.includes('add_manual_action'))this.button(this.editor,'Add manual action',async()=>{
    if(!await this.clean())return;this.promptFields([{fieldname:'title',fieldtype:'Data',label:__('Action title'),reqd:1},{fieldname:'description',fieldtype:'Small Text',label:__('Action description'),reqd:1}],values=>this.run('review.add_manual_action',{letter:state.letter,...values,expected_revision:this.client.state.revision}),'Add manual action');
   },{mutation:true});
   if(client.review.actions.every(a=>a.decision==='reject')){
    const section=this.node(this.editor,'section');section.className='ap09-section';this.node(section,'h4',__('Finish without tasks'));
    this.node(section,'p',__('No tasks will be created. Explain your decision after checking the original.')).className='text-muted small';
    this.control(section,'Reason for no actions',client.review.no_actions_reason,value=>{client.review.no_actions_reason=value;this.edited();},{type:'textarea',max:1000,disabled:!editable});
   }
  }else this.drawSummary();
  if(editable){
   if(this.step===2){
    const proposal=state.review.proposal;
    if(proposal && proposal.run!==state.review.applied_run){
     this.node(this.editor,'p',__('A newer analysis is available. Apply it or explain why you are keeping this review.')).className='text-warning';
     this.control(this.editor,'Reason for keeping the current review',client.retainedResult?.reason,value=>{this.edited();client.retainedResult=value ? {run:proposal.run,reason:value} : null;},{type:'textarea',max:1000});
    }
    this.human=this.control(this.editor,'I checked the original and all final decisions',client.humanVerified,value=>{client.humanVerified=value;},{type:'checkbox'});
   }
   this.errors=this.node(this.editor,'div');this.errors.className='ap09-errors text-danger';this.errors.setAttribute('role','alert');this.errors.tabIndex=-1;
   const footer=this.node(this.editor,'div');footer.className='ap09-footer';
   if(this.step>0){const back=this.button(footer,'Previous step',()=>{this.step--;client.humanVerified=false;this.draw();});back.dataset.navigation='1';}
   this.button(footer,'Save review',async()=>{if(await this.submit('save_review'))frappe.show_alert({message:__('Review saved. No tasks have been created.'),indicator:'green'});},{mutation:true});
   if(this.step<2)this.button(footer,this.step===0 ? 'Save and continue' : 'Save and review result',()=>this.nextStep(),{mutation:true,primary:true});
   else if(state.review.allowed_operations.includes('confirm')){
    const accepted=client.review.actions.filter(a=>a.decision==='accept').length;
    this.button(footer,accepted===0 ? 'Finish without tasks' : accepted===1 ? 'Confirm and create one task' : __('Confirm and create {0} tasks',[accepted]),()=>this.confirm(),{mutation:true,primary:true});
   }
   this.button(this.editor,'Needs clarification',()=>this.prompt('Clarification reason',async values=>{client.review.clarification_reason=values.value;this.edited();await this.submit('save_review','clarification');}),{mutation:true});
  }
 }
 drawAction(action,editable){
  const section=this.node(this.editor,'section');section.className='ap09-action';section.dataset.actionId=action.action_id;
  const render=()=>{
   this.disposeFields(section);section.replaceChildren();const client=this.client.review;
   const edit=(key,value)=>{client.editAction(action.action_id,{[key]:value});this.edited();};
   const change=(key,value)=>{edit(key,value);render();this.syncControls();};
   this.node(section,'h4',action.title || __('Action title'));
   this.control(section,'Decision',action.decision,value=>{edit('decision',value);if(value==='accept')client.review.no_actions_reason=null;this.draw();},{options:[['pending','Pending'],['accept','Accept action'],['reject','Reject action']],disabled:!editable});
   if(action.decision==='reject'){
    this.control(section,'Rejection reason',action.rejection_reason,value=>edit('rejection_reason',value),{type:'textarea',max:1000,disabled:!editable});return;
   }
   const details=this.node(section,'details');this.node(details,'summary',__('Action details'));
   this.control(details,'Action title',action.title,value=>edit('title',value),{max:140,disabled:!editable});
   this.control(details,'Action description',action.description,value=>edit('description',value),{type:'textarea',max:4000,disabled:!editable});
   this.evidence(section,action.evidence);
   if(action.decision!=='accept')return;
   const internal=this.node(section,'div');internal.className='ap09-section';this.node(internal,'h4',__('Internal planning'));
   this.node(internal,'p',action.assigned_to || __('Unassigned')).className='ap09-assignee';
   if(editable)this.button(internal,'Select assignee',()=>this.search('assignees',row=>{client.editAction(action.action_id,{assigned_to:row.user,assignment_decision:'assigned',assignment_reason:null});this.edited();render();this.syncControls();}),{mutation:true});
   this.control(internal,'Leave assignment for later',action.assignment_decision==='unassigned',value=>{
    client.editAction(action.action_id,{assigned_to:null,assignment_decision:value ? 'unassigned' : 'pending',assignment_reason:null});this.edited();render();this.syncControls();
   },{type:'checkbox',disabled:!editable});
   if(action.assignment_decision==='unassigned')this.control(internal,'Reason for no assignee',action.assignment_reason,value=>edit('assignment_reason',value),{type:'textarea',max:1000,disabled:!editable});
   this.control(internal,'Internal work date',action.internal_date,value=>{client.editAction(action.action_id,{internal_date:value,scheduling_decision:value ? 'scheduled' : 'pending',scheduling_reason:null});this.edited();},{type:'date',disabled:!editable || action.scheduling_decision==='unscheduled'});
   this.node(internal,'p',__('When should the firm work on this task?')).className='text-muted small';
   this.control(internal,'Plan the work date later',action.scheduling_decision==='unscheduled',value=>{
    client.editAction(action.action_id,{internal_date:null,scheduling_decision:value ? 'unscheduled' : 'pending',scheduling_reason:null});this.edited();render();this.syncControls();
   },{type:'checkbox',disabled:!editable});
   if(action.scheduling_decision==='unscheduled')this.control(internal,'Reason for no internal date',action.scheduling_reason,value=>edit('scheduling_reason',value),{type:'textarea',max:1000,disabled:!editable});
   const external=this.node(section,'div');external.className='ap09-section';this.node(external,'h4',__('Deadline in the letter'));
   const proposal=client.proposalFor(action.action_id)?.deadline;
   if(proposal){
    this.node(external,'p',__('Proposed deadline')+': '+(proposal.kind==='explicit_date' ? frappe.datetime.str_to_user(proposal.date) : this.deadlineLabel(proposal))).className='text-warning';
    this.node(external,'p',__('The proposed date is unverified until you explicitly confirm it.')).className='text-muted small';
    if(editable && proposal.kind==='explicit_date')this.button(external,'Accept proposed external date',()=>this.prompt('External deadline basis',values=>{client.acceptDate(action.action_id,proposal.date,values.value);this.edited();render();this.syncControls();}),{mutation:true});
   }
   this.control(external,'Confirmed external deadline',action.external_date,value=>change('external_date',value),{type:'date',disabled:!editable});
   if(action.external_date)this.control(external,'External deadline basis',action.external_date_basis,value=>edit('external_date_basis',value),{type:'textarea',max:1000,disabled:!editable});
   else this.node(external,'p',__('No confirmed external deadline.')).className='text-muted small';
  };render();
 }
 drawSummary(){
  const state=this.client.state,client=this.client.review,accepted=client.review.actions.filter(a=>a.decision==='accept');
  this.node(this.editor,'p',__('No tasks have been created yet.')).className='text-muted';
  const document=this.node(this.editor,'section');document.className='ap09-section';
  this.node(document,'h4',state.matching.selected_customer?.customer_name || __('Unknown'));
  this.node(document,'p',state.matching.customer_reason || '');
  this.receiptDates(document);
  for(const [key,label] of [['sender','Sender'],['document_date','Document date'],['letter_type','Letter type'],['summary','Summary']])this.node(document,'p',__(label)+': '+(client.documentFields[key] || __('Unknown')));
  this.button(document,'Change document or Mandant',()=>{this.step=0;client.humanVerified=false;this.draw();}).dataset.navigation='1';
  for(const action of client.review.actions){
   const section=this.node(this.editor,'section');section.className='ap09-section';this.node(section,'h4',action.title);
   this.node(section,'p',__(action.decision==='accept' ? 'Accept action' : action.decision==='reject' ? 'Reject action' : 'Pending'));
   if(action.decision==='accept'){
    for(const [key,label] of [['description','Action description'],['assigned_to','Responsible'],['internal_date','Internal work date'],['external_date','Confirmed external deadline']])this.node(section,'p',__(label)+': '+(action[key] || __(key==='external_date' ? 'No confirmed external deadline.' : 'Date requires planning')));
    for(const [key,label] of [['external_date_basis','External deadline basis'],['assignment_reason','Reason for no assignee'],['scheduling_reason','Reason for no internal date']])if(action[key])this.node(section,'p',__(label)+': '+action[key]);
   }else if(action.rejection_reason)this.node(section,'p',__('Rejection reason')+': '+action.rejection_reason);
  }
  if(!accepted.length)this.node(this.editor,'p',__('No tasks will be created.')+' '+(client.review.no_actions_reason || ''));
  this.button(this.editor,'Change tasks and dates',()=>{this.step=1;client.humanVerified=false;this.draw();}).dataset.navigation='1';
 }
 drawReceipt(){
  this.receiptsBox.replaceChildren();for(const action of this.client.state.review.receipt?.actions || [])if(action.task){
   const row=this.node(this.receiptsBox,'div');row.className='ap09-result-task';
   const confirmed=this.client.review.review.actions.find(a=>a.action_id===action.action_id);
   const link=this.node(row,'a',confirmed?.title || action.task);link.href=frappe.utils.get_form_link('Task',action.task);
   for(const [key,label] of [['assigned_to','Responsible'],['internal_date','Internal work date'],['external_date','Confirmed external deadline']])if(confirmed?.[key])this.node(row,'p',__(label)+': '+confirmed[key]);
  }
 }
 receiptDates(parent){
  const original=this.client.state.original;
  this.node(parent,'p',__('Received on')+': '+(original.received_date_known && original.received_date ? frappe.datetime.str_to_user(original.received_date) : __('Unknown'))).className='text-muted small';
  if(original.uploaded_at)this.node(parent,'p',__('Uploaded at')+': '+frappe.datetime.str_to_user(original.uploaded_at)).className='text-muted small';
 }
 drawResult(){
  const state=this.client.state;this.node(this.editor,'h3',__(state.status==='Completed' ? 'Letter completed' : 'Cancelled'));
  this.node(this.editor,'p',state.matching.selected_customer?.customer_name || '');
  this.receiptsBox=this.node(this.editor,'section');this.drawReceipt();
  const count=state.review.receipt?.actions?.length || 0;
  if(state.status==='Completed' && count)this.node(this.editor,'p',__('Tasks created: {0}',[count]));
  if(state.status==='Completed' && !count)this.node(this.editor,'p',__('No tasks will be created.')+' '+(this.client.review.review.no_actions_reason || ''));
  const footer=this.node(this.editor,'div');footer.className='ap09-footer';this.button(footer,'Back to inbox',()=>frappe.set_route('posteingang'));
  this.button(footer,'Review next document',async()=>{
   try{const result=await this.transport('get_queue',{filters:{state:'review',uploaded_by:'all',query:'',sort:'oldest'}});this.client.check(result);if(!this.visible || this.client.state?.letter!==state.letter)return;
    const next=result.items.find(row=>row.letter!==state.letter);frappe.set_route('posteingang',...(next ? [next.letter] : []));
   }catch(error){this.failure(error);}
  },{primary:true});
  this.correctionBox=this.node(this.editor,'section');this.drawCorrection();
 }
 validateStep(step){
  if(!this.client.state || !this.client.review.review)return false;
  const issues=this.api.reviewIssues(this.client.state,this.client.review.review,step);
  this.errors?.replaceChildren();
  if(issues.length){for(const issue of issues)this.node(this.errors,'p',__(issue.message)+(issue.action_id ? ' · '+(this.client.review.review.actions.find(a=>a.action_id===issue.action_id)?.title || '') : ''));this.errors.focus();return false;}
  return true;
 }
 async nextStep(){
  if(!this.validateStep(this.step))return;
  const letter=this.client.state.letter,step=this.step;
  const current=()=>this.visible && this.client.state?.letter===letter && this.step===step;
  if(!await this.submit('save_review') || !current())return;
  if(step===0 && this.client.state.analysis?.allowed_operations.includes('manual_review')){
   if(!await this.run('analysis.manual_review',{letter,expected_revision:this.client.state.revision}) || !current())return;
  }
  this.step++;this.draw();
 }
 async submit(operation,mode='draft'){
  const args=this.client.review.payload(operation,{mode});return await this.run('review.'+operation,args);
 }
 confirm(){
  const client=this.client.review;
  if(!this.validateStep(0) || !this.validateStep(1))return;
  if(!client.humanVerified){this.errors.textContent=__('Check the original and explicitly confirm the human review.');this.errors.focus();return;}
  const proposal=client.state.proposal;
  if(proposal && proposal.run!==client.state.applied_run && (client.retainedResult?.run!==proposal.run || !client.retainedResult?.reason?.trim())){
   this.errors.textContent=__('A newer analysis is available. Apply it or explain why you are keeping this review.');this.errors.focus();return;
  }
  return this.submit('confirm');
 }
 drawCorrection(){
  this.correctionBox.replaceChildren();const state=this.client.state,allowed=state.review.allowed_operations;
  if(allowed.includes('create_correction'))this.button(this.correctionBox,'Create correction',()=>{
   const valid=this.capture();
   const dialog=new frappe.ui.Dialog({title:__('Create correction'),fields:[{fieldname:'reason',fieldtype:'Small Text',label:__('Correction reason'),reqd:1},{fieldname:'reuse_original',fieldtype:'Check',label:__('Reuse original PDF'),default:1},{fieldname:'received_date_known',fieldtype:'Check',label:__('Receipt date known'),default:0},{fieldname:'received_date',fieldtype:'Date',label:__('Receipt date')},{fieldname:'upload',fieldtype:'HTML'}],primary_action_label:__('Create correction'),primary_action:async values=>{
    if(!valid())return;if(!values.reuse_original && file.files.length!==1){frappe.msgprint(__('Choose one PDF for the replacement.'));return;}
    const result=await this.run('review.create_correction',{old_letter:state.letter,expected_revision:state.revision,reason:values.reason,reuse_original:!!values.reuse_original,received_date_known:!!values.received_date_known,received_date:values.received_date_known ? values.received_date : null,...(!values.reuse_original ? {_file:file.files[0]} : {})});if(result)dialog.hide();
   }});this.dialogs.add(dialog);const file=this.node(dialog.fields_dict.upload.$wrapper[0],'input');file.type='file';file.accept='.pdf,application/pdf';file.setAttribute('aria-label',__('Choose one PDF for the replacement.'));dialog.onhide=()=>this.dialogs.delete(dialog);dialog.show();
  },{mutation:true,requiresEnabled:false});
  if(state.review.correction){
   this.node(this.correctionBox,'p',__(state.review.correction.ready ? 'Administrative review is complete.' : 'Administrative review of the original tasks is required.'));
   if(allowed.includes('correction_disposition'))for(const task of state.review.correction.tasks || []){
    this.node(this.correctionBox,'p',task.task+' · '+__(task.status)+' · '+__(task.disposition || 'Pending'));
    const a=this.node(this.correctionBox,'a',__('Open task'));a.href=frappe.utils.get_form_link('Task',task.task);
    this.button(this.correctionBox,'Resolve original task',()=>this.promptFields([{fieldname:'disposition',fieldtype:'Select',label:__('Disposition'),options:['cancelled','historical'],reqd:1},{fieldname:'reason',fieldtype:'Small Text',label:__('Reason'),reqd:1}],values=>this.run('review.correction_disposition',{replacement:state.letter,expected_revision:state.revision,original_action_id:task.action_id,...values}),'Resolve original task'),{mutation:true,requiresEnabled:false});
   }
  }
 }
}
