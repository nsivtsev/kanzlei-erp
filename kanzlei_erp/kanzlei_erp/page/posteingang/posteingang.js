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
  this.filters={state:'open',uploaded_by:'all',query:'',sort:'oldest'};this.visible=false;this.dialogs=new Set();
  this.root=this.node(this.page.main[0],'div');this.root.className='posteingang-workspace';this.root.dataset.panel='queue';
  const tabs=this.node(this.root,'nav');tabs.className='ap09-tabs';tabs.setAttribute('aria-label',__('Workspace panels'));
  for(const [key,label] of [['queue','Queue'],['pdf','PDF'],['review','Human review']])this.button(tabs,label,()=>this.panel(key));
  this.notice=this.node(this.root,'div');this.notice.setAttribute('role','status');this.notice.className='ap09-notice';
  const layout=this.node(this.root,'div');layout.className='ap09-layout';
  this.queuePanel=this.node(layout,'section');this.queuePanel.className='ap09-panel ap09-queue';
  this.pdfPanel=this.node(layout,'section');this.pdfPanel.className='ap09-panel ap09-pdf';
  this.editor=this.node(layout,'section');this.editor.className='ap09-panel ap09-review';
  this.node(this.queuePanel,'h2',__('Queue'));
  this.control(this.queuePanel,'Queue state','open',v=>{this.filters.state=v;this.loadQueue();},{options:[['open','Unfinished letters'],['history','History'],['all','All'],...['Received','Queued','Processing','Needs Review','Needs Clarification','Error','Completed','Cancelled'].map(s=>[s,s])]});
  this.control(this.queuePanel,'Uploaded by','all',v=>{this.filters.uploaded_by=v;this.loadQueue();},{options:[['all','All'],['me','Me']]});
  this.control(this.queuePanel,'Search letters','',v=>{this.filters.query=v || '';clearTimeout(this.searchTimer);this.searchTimer=setTimeout(()=>this.loadQueue(),300);},{max:140});
  this.control(this.queuePanel,'Sort','oldest',v=>{this.filters.sort=v;this.loadQueue();},{options:[['oldest','Oldest first'],['newest','Newest first']]});
  this.button(this.queuePanel,'Refresh',()=>this.loadQueue());
  this.rows=this.node(this.queuePanel,'div');this.more=this.button(this.queuePanel,'More',()=>this.loadQueue(true));this.more.hidden=true;
  this.page.set_primary_action(__('Upload PDF'),()=>this.api.open({onUploaded:()=>this.loadQueue(),onOpen:name=>this.select(name)}),'add').attr('aria-label',__('Upload PDF'));
  this.routeHandler=()=>{if(this.onRoute())this.show();else this.hide();};
  frappe.router.on('change',this.routeHandler);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)this.pause();else if(this.onRoute())this.show();});
  window.addEventListener('beforeunload',event=>{if(this.client.review.dirty || this.client.intent){event.preventDefault();event.returnValue='';}});
 }
 node(parent,tag,value='') {const element=document.createElement(tag);element.textContent=value;parent.append(element);return element;}
 button(parent,label,handler,{mutation=false,disabled=false,requiresEnabled=true}={}) {
  const button=this.node(parent,'button',__(label));button.type='button';button.className='btn btn-default btn-sm';button.disabled=disabled;
  if(mutation){button.dataset.mutation='1';button.dataset.requiresEnabled=String(requiresEnabled);}button.addEventListener('click',handler);return button;
 }
 control(parent,label,value,onchange,{type='text',options=null,disabled=false,max=null}={}) {
  const wrap=this.node(parent,'label',__(label));const input=this.node(wrap,options ? 'select' : type==='textarea' ? 'textarea' : 'input');
  if(options)for(const [key,label] of options){const option=this.node(input,'option',__(label));option.value=key;}
  else if(type!=='textarea')input.type=type;
  input.className='form-control';input.value=value ?? '';input.disabled=disabled;input.setAttribute('aria-label',__(label));if(max)input.maxLength=max;
  if(type==='checkbox'){input.checked=!!value;input.className='';}
  const changed=()=>onchange(type==='checkbox' ? input.checked : input.value || null);
  input.addEventListener(options || type==='checkbox' || type==='date' ? 'change' : 'input',changed);return input;
 }
 panel(name){this.root.dataset.panel=name;}
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
  if(error.message==='PERMISSION'){this.client.clear();this.pdfPanel.replaceChildren();this.editor.replaceChildren();this.rows.replaceChildren();this.page.clear_primary_action();for(const d of this.dialogs)d.hide();}
  this.notice.textContent=this.message(error);
  if(this.client.intent)this.button(this.notice,'Check and retry the same request',()=>this.retry());
 }
 pause(){clearTimeout(this.queueTimer);clearTimeout(this.statusTimer);clearTimeout(this.searchTimer);}
 hide(){this.visible=false;this.pause();this.client.suspend();this.pdfPanel.replaceChildren();this.editor.replaceChildren();this.rows.replaceChildren();for(const d of this.dialogs)d.hide();}
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
    else if(!this.client.intent){for(const dialog of this.dialogs)dialog.hide();await this.client.load(name);}
    if(!this.visible || !this.onRoute() || frappe.get_route()[1]!==name)return;
    if(this.client.state?.letter===name)this.draw();else{this.pdfPanel.replaceChildren();this.editor.replaceChildren();this.failure(Error('UNKNOWN_OUTCOME'));}this.panel('review');
   }else{this.pdfPanel.replaceChildren();this.editor.replaceChildren();this.node(this.editor,'p',__('Choose a letter from the queue.'));this.panel('queue');}
   await this.loadQueue();this.schedule();
  }catch(error){this.failure(error);}finally{this.showing=undefined;}
 }
 schedule(){
  this.pause();if(!this.visible || document.hidden || !this.onRoute() || this.client.user()!==this.client.owner)return;
  this.queueTimer=setTimeout(async()=>{await this.loadQueue();this.scheduleQueue();},15000);this.scheduleStatus();
 }
 scheduleQueue(){if(this.visible && !document.hidden)this.queueTimer=setTimeout(async()=>{await this.loadQueue();this.scheduleQueue();},15000);}
 scheduleStatus(){if(this.visible && !document.hidden)this.statusTimer=setTimeout(async()=>{
  if(!this.statusBusy && !this.client.busy && this.client.state){this.statusBusy=true;const before=this.client.state;try{await this.client.status();if(!this.client.state){this.pdfPanel.replaceChildren();this.editor.replaceChildren();}else if(this.client.state!==before)this.draw();else {if(this.client.referencesChanged){this.drawMatching();this.drawReceipt();this.drawCorrection();this.client.referencesChanged=false;}this.statusNotice();}}catch(error){this.failure(error);}finally{this.statusBusy=false;}}
  this.scheduleStatus();
 },5000);}
 async loadQueue(more=false){
  if(!this.visible)return;if(this.queueBusy){this.queueAgain=true;return;}
  this.queueBusy=true;
  try{const result=await this.client.queue({...this.filters},more ? this.cursor : null);if(!result || !this.visible)return;this.cursor=result.next_cursor;this.drawRows();this.more.hidden=!this.cursor;}
  catch(error){this.failure(error);}finally{this.queueBusy=false;if(this.queueAgain){this.queueAgain=false;this.loadQueue();}}
 }
 drawRows(){
  this.rows.replaceChildren();if(!this.client.rows.length)this.node(this.rows,'p',__('No entries'));
  for(const row of this.client.rows){
   const button=this.button(this.rows,row.file_name,()=>this.select(row.letter));button.className='ap09-letter';button.setAttribute('aria-current',String(this.client.state?.letter===row.letter));
   this.node(button,'span',row.letter+' · '+__(row.status));this.node(button,'span',frappe.datetime.str_to_user(row.received_at)+' · '+row.received_by);
   const days=Math.max(0,Math.floor((Date.now()-frappe.datetime.str_to_obj(row.received_at).getTime())/86400000));this.node(button,'span',__('Age in days: {0}',[days]));
   this.node(button,'span',__('Received on')+': '+(row.received_date_known ? row.received_date : __('Unknown')));
   if(row.customer)this.node(button,'span',row.customer.customer_name);
   if(row.matching){for(const c of row.matching.candidates)this.node(button,'span',c.customer_name);if(row.matching.ambiguity!=='none')this.node(button,'span',this.assessment(row.matching.ambiguity));}
   else this.node(button,'span',__('No completed analysis is available.'));
   if(row.deadline){this.node(button,'span',__('Unverified proposed deadline')+': '+(row.deadline.proposed_date || __('No explicit date')));this.node(button,'span',__('Explicit dates: {0}; relative deadlines: {1}',[row.deadline.explicit_count,row.deadline.relative_count]));}
   if(row.result_error || row.analysis.run?.error_code)this.node(button,'span',this.message(Error(row.result_error || row.analysis.run.error_code)));
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
  if(this.human)this.human.checked=this.client.review.humanVerified;
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
  for(const button of this.editor.querySelectorAll('[data-mutation]'))button.disabled=locked || !caps?.customer_write || (!caps?.enabled && button.dataset.requiresEnabled==='true');
 }
 async compare(){
  const sequence=this.client.sequence,name=this.client.state.letter;
  try{const current=await this.transport('get_letter_workspace',{letter:name});if(sequence!==this.client.sequence || !this.visible)return;this.client.check(current);
   const dialog=new frappe.ui.Dialog({title:__('Compare with saved review'),size:'extra-large',fields:[{fieldtype:'HTML',fieldname:'versions'}]});this.dialogs.add(dialog);const box=dialog.fields_dict.versions.$wrapper[0];
   const version=(label,fields,review)=>{this.node(box,'h3',__(label));for(const [key,title] of [['sender','Sender'],['document_date','Document date'],['letter_type','Letter type'],['summary','Summary']])this.node(box,'p',__(title)+': '+(fields[key] || __('Unknown')));
    for(const action of review.actions){this.node(box,'h4',action.title);for(const [key,title] of [['description','Action description'],['decision','Decision'],['assigned_to','Assignee'],['internal_date','Internal work date'],['external_date','Confirmed external deadline'],['external_date_basis','External deadline basis'],['assignment_reason','Reason for no assignee'],['scheduling_reason','Reason for no internal date'],['rejection_reason','Rejection reason']])this.node(box,'p',__(title)+': '+(action[key] || __('Unknown')));}
    this.node(box,'p',__('Reason for no actions')+': '+(review.no_actions_reason || __('Unknown')));this.node(box,'p',__('Clarification reason')+': '+(review.clarification_reason || __('Unknown')));};
   version('Local review',this.client.review.documentFields,this.client.review.review);version('Saved review',current.review.document_fields,current.review.review);
   const valid=()=>sequence===this.client.sequence && this.visible && !this.client.busy && !this.client.intent && this.onRoute() && frappe.get_route()[1]===name && frappe.session.user===this.client.owner && this.client.state?.letter===name;this.button(box,'Discard local review and load saved version',async()=>{if(!valid())return;dialog.hide();await this.client.load(name);this.draw();});dialog.onhide=()=>this.dialogs.delete(dialog);dialog.show();
  }catch(error){this.failure(error);}
 }
 edited(){this.client.review.edited();if(this.human)this.human.checked=false;this.statusNotice();}
 evidence(parent,items){
  for(const item of items || []){this.node(parent,'p',__(item.verification)+': '+(item.quote || ''));
   if(Number.isInteger(item.page) && item.page>=1 && item.page<=this.client.state.original.page_count)this.button(parent,__('Page {0}',[item.page]),()=>{this.frame.src=this.client.state.original.preview_url+'#page='+item.page;this.panel('pdf');});}
 }
 draw(){
  const state=this.client.state;if(!state || !this.visible)return;
  this.statusNotice();this.pdfPanel.replaceChildren();this.editor.replaceChildren();
  this.node(this.pdfPanel,'h2',state.original.file_name);
  for(const [label,url] of [['Open original PDF',state.original.preview_url],['Download',state.original.download_url]]){const a=this.node(this.pdfPanel,'a',__(label));a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.className='btn btn-default btn-sm mr-2';}
  this.frame=this.node(this.pdfPanel,'iframe');this.frame.src=state.original.preview_url;this.frame.title=__('Original PDF');
  this.node(this.editor,'h2',state.letter);const actions=this.node(this.editor,'div');actions.className='ap09-actions';
  for(const op of state.analysis.allowed_operations)this.button(actions,({queue_analysis:'Start analysis',manual_review:'Switch to human review',cancel:'Cancel letter'})[op],async()=>{
   if(!await this.clean())return;
   const perform=reason=>this.run('analysis.'+op,{letter:state.letter,expected_revision:this.client.state.revision,...(reason ? {reason} : {})});
   if(op==='cancel')this.prompt('Reason',values=>perform(values.value));
   else if(op==='queue_analysis' && state.status==='Needs Review')this.ask(__('Analyze this letter again? Your current review edits will be preserved.'),()=>perform());else perform();
  },{mutation:true,requiresEnabled:op!=='cancel'});
  this.matchingBox=this.node(this.editor,'section');this.drawMatching();this.drawProposal();this.drawReview();this.receiptsBox=this.node(this.editor,'section');this.drawReceipt();this.correctionBox=this.node(this.editor,'section');this.drawCorrection();this.syncControls();
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
   for(const row of result.rows){this.node(root,'p',row.customer_name || row.full_name || row.user);if(kind!=='customers' || row.can_select)this.button(root,'Select',()=>{if(!valid())return;dialog.hide();choose(row);});else this.node(root,'p',__('Read-only Customer; selection requires write access.'));}
   if(result.has_next){start=result.next_start;this.button(root,'More',()=>load(true));}
  }catch(error){if(sequence===this.client.sequence){root.replaceChildren();this.failure(error);}}};
  dialog.set_primary_action(__('Search'),()=>load(false));dialog.onhide=()=>this.dialogs.delete(dialog);dialog.show();load(false);
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
  this.node(this.editor,'h3',__('Human review'));
  for(const [key,label,type,max] of [['sender','Sender','text',255],['document_date','Document date','date'],['letter_type','Letter type','text',255],['summary','Summary','textarea',4000]])this.control(this.editor,label,client.documentFields[key],value=>{client.editFields({[key]:value});this.edited();},{type,max,disabled:!editable});
  for(const action of client.review.actions)this.drawAction(action,editable);
  if(state.review.allowed_operations.includes('add_manual_action'))this.button(this.editor,'Add manual action',async()=>{
   if(!await this.clean())return;this.promptFields([{fieldname:'title',fieldtype:'Data',label:__('Action title'),reqd:1},{fieldname:'description',fieldtype:'Small Text',label:__('Action description'),reqd:1}],values=>this.run('review.add_manual_action',{letter:state.letter,...values,expected_revision:this.client.state.revision}),'Add manual action');
  },{mutation:true});
  for(const [key,label] of [['no_actions_reason','Reason for no actions'],['clarification_reason','Clarification reason']])this.control(this.editor,label,client.review[key],value=>{client.review[key]=value;this.edited();},{type:'textarea',max:1000,disabled:!editable});
  if(editable){
   this.human=this.control(this.editor,'I checked the original and all final decisions',client.humanVerified,value=>{client.humanVerified=value;},{type:'checkbox'});
   this.button(this.editor,'Save review',()=>this.submit('save_review'),{mutation:true});
   this.button(this.editor,'Needs clarification',()=>this.submit('save_review','clarification'),{mutation:true});
   if(state.review.allowed_operations.includes('confirm'))this.button(this.editor,'Confirm letter',()=>this.confirm(),{mutation:true});
  }
 }
 drawReceipt(){this.receiptsBox.replaceChildren();for(const action of this.client.state.review.receipt?.actions || [])if(action.task){const a=this.node(this.receiptsBox,'a',action.task);a.href=frappe.utils.get_form_link('Task',action.task);a.className='btn btn-default btn-sm mr-2';}}
 drawAction(action,editable){
  const client=this.client.review,section=this.node(this.editor,'section');section.className='ap09-action';
  const edit=(key,value)=>{client.editAction(action.action_id,{[key]:value});this.edited();};
  this.control(section,'Action title',action.title,v=>edit('title',v),{max:140,disabled:!editable});
  this.control(section,'Action description',action.description,v=>edit('description',v),{type:'textarea',max:4000,disabled:!editable});
  this.control(section,'Decision',action.decision,v=>edit('decision',v),{options:[['pending','Pending'],['accept','Accept action'],['reject','Reject action']],disabled:!editable});
  this.control(section,'Rejection reason',action.rejection_reason,v=>edit('rejection_reason',v),{type:'textarea',max:1000,disabled:!editable});
  const assigned=this.node(section,'p',action.assigned_to || __('Unassigned'));
  const assignment=this.control(section,'Assignment decision',action.assignment_decision,v=>{client.editAction(action.action_id,{assignment_decision:v,...(v==='unassigned' ? {assigned_to:null} : {})});if(v==='unassigned')assigned.textContent=__('Unassigned');this.edited();},{options:[['pending','Pending'],['assigned','Assign a person'],['unassigned','Leave unassigned with a reason']],disabled:!editable});
  if(editable)this.button(section,'Select assignee',()=>this.search('assignees',row=>{client.editAction(action.action_id,{assigned_to:row.user,assignment_decision:'assigned'});assigned.textContent=row.full_name || row.user;assignment.value='assigned';this.edited();}),{mutation:true});
  this.control(section,'Reason for no assignee',action.assignment_reason,v=>edit('assignment_reason',v),{type:'textarea',max:1000,disabled:!editable});
  const scheduling=this.control(section,'Scheduling decision',action.scheduling_decision,v=>{client.editAction(action.action_id,{scheduling_decision:v,...(v==='unscheduled' ? {internal_date:null} : {})});if(v==='unscheduled')internal.value='';this.edited();},{options:[['pending','Pending'],['scheduled','Schedule work'],['unscheduled','Plan the date later with a reason']],disabled:!editable});
  const internal=this.control(section,'Internal work date',action.internal_date,v=>{client.editAction(action.action_id,{internal_date:v,...(v ? {scheduling_decision:'scheduled'} : {})});if(v)scheduling.value='scheduled';this.edited();},{type:'date',disabled:!editable});
  this.control(section,'Reason for no internal date',action.scheduling_reason,v=>edit('scheduling_reason',v),{type:'textarea',max:1000,disabled:!editable});
  const external=this.control(section,'Confirmed external deadline',action.external_date,v=>edit('external_date',v),{type:'date',disabled:!editable});
  const basis=this.control(section,'External deadline basis',action.external_date_basis,v=>edit('external_date_basis',v),{type:'textarea',max:1000,disabled:!editable});
  const proposal=client.proposalFor(action.action_id)?.deadline;
  if(proposal){this.node(section,'p',__('Proposed deadline')+': '+(proposal.date || this.deadlineLabel(proposal)));if(editable && proposal.kind==='explicit_date')this.button(section,'Accept proposed external date',()=>this.prompt('External deadline basis',values=>{client.acceptDate(action.action_id,proposal.date,values.value);external.value=proposal.date;basis.value=values.value;this.edited();}),{mutation:true});}
  this.evidence(section,action.evidence);
 }
 async submit(operation,mode='draft'){
  const args=this.client.review.payload(operation,{mode});return await this.run('review.'+operation,args);
 }
 confirm(){
  const client=this.client.review;
  if(!client.humanVerified){frappe.msgprint(__('Check the original and explicitly confirm the human review.'));return;}
  const preview=()=>{const box=document.createElement('div');this.node(box,'p',__('Selected Mandant')+': '+(this.client.state.matching.selected_customer?.customer_name || __('Unknown')));
   const accepted=client.review.actions.filter(a=>a.decision==='accept');for(const action of accepted)this.node(box,'p',action.title+' · '+(action.assigned_to || __('Unassigned'))+' · '+(action.internal_date || __('Date requires planning')));
   if(!accepted.length)this.node(box,'p',__('No tasks will be created.')+' '+(client.review.no_actions_reason || ''));
   this.ask(box.innerHTML,()=>this.submit('confirm'));
  };
  if(client.state.proposal && client.state.proposal.run!==client.state.applied_run)this.prompt('Reason for keeping the current review',values=>{client.retainedResult={run:client.state.proposal.run,reason:values.value};preview();});else preview();
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
