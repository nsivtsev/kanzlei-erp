/* AP-07: explicit human review. The buffer stays in memory, never browser storage. */
(() => {
 const clone=value=>JSON.parse(JSON.stringify(value));
 const dirty=frm=>frm.is_dirty() || !!frm.__posteingang_review_dirty;
 function correctionFormData(args,file,key,Data=FormData) {
  const data=new Data();
  for(const [name,value]of Object.entries({...args,request_key:key}))if(value!==null && value!==undefined)data.append(name,String(value));
  data.append("file",file);return data;
 }
 const messages={
  INVALID_REQUEST:"Check the review parameters.",INVALID_RESULT:"The saved analysis could not be accepted. Human review is required.",
  INVALID_REVIEW:"Check every review decision, date and required reason.",REVISION_CONFLICT:"The letter changed. Reload the form before continuing.",
  CUSTOMER_CHANGED:"The Mandant selection changed. Reload the letter before saving the review.",STALE_RUN:"The analysis result changed. Reload before applying it.",REPLACE_ACKNOWLEDGEMENT_REQUIRED:"Explicitly confirm replacement of the current review.",
  IDEMPOTENCY_CONFLICT:"This request was already used with different data",INVALID_TRANSITION:"This operation is not available in the current state.",
  RESULT_NOT_APPLIED:"Apply the latest result or explicitly keep the current review with a reason.",MULTIPLE_ADDRESSEES:"Several addressees require clarification before selecting one Mandant.",
  ASSIGNEE_NOT_ALLOWED:"The assignee must be an active system user with access to this Mandant.",TASK_CREATION_FAILED:"The tasks could not be created. Nothing was confirmed.",
  CORRECTION_REVIEW_REQUIRED:"An administrator must resolve every task of the original letter before confirmation.",CORRECTION_EXISTS:"A replacement already exists for this letter.",
  ALREADY_COMPLETED:"This letter was already confirmed with different decisions.",PERMISSION:"Posteingang operation is not permitted",
  NETWORK:"The result is unknown; check the request before retrying",INTERNAL_ERROR:"The review operation could not finish. Reload and try again."
 };
 async function rpc(operation,args) {
  let response,body;
  try {
   const multipart=typeof FormData!=="undefined" && args instanceof FormData;
   response=await fetch(`/api/method/kanzlei_erp.posteingang_confirmation.${operation}`,{
    method:"POST",credentials:"same-origin",headers:multipart ? {"X-Frappe-CSRF-Token":frappe.csrf_token} : {"Content-Type":"application/json","X-Frappe-CSRF-Token":frappe.csrf_token},
    body:multipart ? args : JSON.stringify(args)
   });
   body=await response.json();
  } catch {throw Error("NETWORK");}
  if(!response.ok || body.exc) throw Error(Object.keys(messages).find(k=>JSON.stringify(body).includes(k)) || (response.status===403 ? "PERMISSION" : "INTERNAL_ERROR"));
  return body.message;
 }
 class ReviewClient {
  constructor(transport) {this.transport=transport;this.sequence=0;this.busy=false;this.intent=null;this.clear();}
  clear() {this.state=null;this.review=null;this.documentFields=null;this.dirty=false;this.humanVerified=false;this.retainedResult=null;}
  adopt(state) {this.clear();this.state=clone(state);this.review=clone(state.review);this.documentFields=clone(state.document_fields);}
  async load(frm,active=()=>true) {
   if(!active() || dirty(frm)) return null;
   const sequence=++this.sequence,doc=frm.doc;
   this.clear();
   try {
    const state=await this.transport("get_review_state",{letter:doc.name});
    if(!active() || frm.doc!==doc || dirty(frm) || sequence!==this.sequence) return null;
    this.adopt(state);return state;
   } catch(error) {if(sequence===this.sequence)this.clear();throw error;}
  }
  edited() {this.dirty=true;this.humanVerified=false;this.retainedResult=null;this.intent=null;}
  editFields(changes) {
   if(Object.keys(changes).some(k=>!["sender","document_date","letter_type","summary"].includes(k))) throw Error("INVALID_REQUEST");
   Object.assign(this.documentFields,changes);this.edited();
  }
  editAction(id,changes) {
   const allowed=["title","description","decision","assigned_to","assignment_decision","assignment_reason","external_date","external_date_basis","internal_date","scheduling_decision","scheduling_reason","rejection_reason"];
   if(Object.keys(changes).some(k=>!allowed.includes(k))) throw Error("INVALID_REQUEST");
   const action=this.review.actions.find(a=>a.action_id===id);
   if(!action)throw Error("INVALID_REQUEST");
   Object.assign(action,changes);
   action.needs_planning=!action.assigned_to || !action.internal_date;
   this.edited();
  }
  acceptDate(id,date,basis) {this.editAction(id,{external_date:date,external_date_basis:basis});}
  proposalFor(id) {
   if(this.state.action_proposals?.[id])return this.state.action_proposals[id];
   const action=this.review.actions.find(a=>a.action_id===id);
   return action && this.state.applied_run===this.state.proposal?.run ? this.state.proposal.extraction.actions[action.source_index] || null : null;
  }
  payload(operation,extra={}) {
   const review=clone(this.review);
   review.mode=operation==="confirm" ? "confirm" : (extra.mode || "draft");
   review.expected_revision=this.state.revision;
   review.human_verified=operation==="confirm" && this.humanVerified;
   const args={letter:this.state.letter,review,document_fields:clone(this.documentFields)};
   if(operation==="confirm")args.retained_result=clone(this.retainedResult);
   return args;
  }
  async mutate(operation,args) {
   if(this.busy)return null;
   const semantic=JSON.stringify([operation,args]);
   if(this.intent?.semantic!==semantic)this.intent={semantic,args:{...clone(args),request_key:crypto.randomUUID()}};
   this.busy=true;
   try {const result=await this.transport(operation,this.intent.args);this.intent=null;this.dirty=false;return result;}
   catch(error) {if(error.message!=="NETWORK")this.intent=null;throw error;}
   finally {this.busy=false;}
  }
  submit(operation,extra={}) {return this.mutate(operation,this.payload(operation,extra));}
 }
 const showError=error=>frappe.msgprint(__(messages[error.message] || messages.INTERNAL_ERROR));
 const text=(parent,tag,value)=>{const element=document.createElement(tag);element.textContent=value ?? "";parent.append(element);return element;};
 const button=(parent,label,handler)=>{const element=text(parent,"button",__(label));element.type="button";element.className="btn btn-default btn-sm mr-2 mb-2";element.addEventListener("click",handler);return element;};
 function input(parent,label,value,onchange,{type="text",options=null,disabled=false,maxLength=null}={}) {
  const wrap=text(parent,"label",__(label));wrap.className="d-block mb-3";
  const element=document.createElement(options ? "select" : type==="textarea" ? "textarea" : "input");
  if(options)for(const [key,label] of options){const option=text(element,"option",__(label));option.value=key;}
  else if(type!=="textarea")element.type=type;
  element.className="form-control mt-1";element.disabled=disabled;
  if(maxLength)element.maxLength=maxLength;
  element.value=value ?? "";wrap.append(element);
  if(type==="checkbox") {element.checked=!!value;element.className="ml-2";}
  const update=()=>onchange(type==="checkbox" ? element.checked : element.value || null);
  element.addEventListener("change",update);
  if(!options && type!=="checkbox")element.addEventListener("input",update);
  return element;
 }
 const choices={decision:[["pending","Pending"],["accept","Accept action"],["reject","Reject action"]],assignment:[["pending","Pending"],["assigned","Assign a person"],["unassigned","Leave unassigned with a reason"]],scheduling:[["pending","Pending"],["scheduled","Schedule work"],["unscheduled","Plan the date later with a reason"]]};
 function revoke(frm) {
  const review=frm._ap07,matching=frm._ap06,analysis=frm._ap05;
  if(review){review.active=false;review.client.sequence++;review.client.clear();review.dialog?.hide();review.wrapper?.empty();}
  if(matching){matching.active=false;matching.client.sequence++;matching.wrapper?.empty();}
  if(analysis){analysis.active=false;clearTimeout(analysis.timer);analysis.revoke?.();}
  frm.__posteingang_review_dirty=false;
  frappe.set_route?.("List","Incoming Letter");
 }
 function attach(frm) {
  if(frm.is_new())return;
  if(frm._ap07?.active && frm._ap07.doc===frm.doc && frm._ap07.client.dirty)return;
  if(frm._ap07){frm._ap07.active=false;if(frm._ap07.dialog)frm._ap07.dialog.hide();}
  frm.__posteingang_review_dirty=false;
  const context={active:true,doc:frm.doc,client:frm._review_client || new ReviewClient(rpc)},doc=frm.doc;
  frm._review_client=context.client;frm._ap07=context;
  const active=()=>context.active && frm.doc===doc && frappe.get_route_str()===`Form/Incoming Letter/${doc.name}`;
  const client=context.client;
  const wrapper=frm.dashboard.add_section("",__("Human review"));
  context.wrapper=wrapper;
  async function finish() {frm.__posteingang_review_dirty=false;context.dialog?.hide();if(active())await frm.reload_doc();}
  function failure(error) {if(active()){if(error.message==="PERMISSION")revoke(frm);showError(error);}}
  async function mutate(operation,args) {
   if(!active() || dirty(frm) || client.busy)return;
   try {if(await client.mutate(operation,args))await finish();}catch(error){failure(error);}
  }
  function preview() {
   const box=document.createElement("div");
   text(box,"p",__("Selected Mandant")+": "+(client.state.selected_customer?.customer_name || client.review.selected_customer || __("Unknown")));
   for(const action of client.review.actions.filter(a=>a.decision==="accept"))text(box,"p",`${action.title} · ${action.assigned_to || __("Unassigned")} · ${action.internal_date || __("Date requires planning")}`);
   if(!client.review.actions.some(a=>a.decision==="accept"))text(box,"p",__("No tasks will be created.")+" "+(client.review.no_actions_reason || ""));
   return box.innerHTML;
  }
  async function chooseAssignee(action,control,decision) {
   frappe.prompt({fieldname:"text",fieldtype:"Data",label:__("Search assignees")},values=>{
    const dialog=new frappe.ui.Dialog({title:__("Select assignee"),fields:[{fieldtype:"HTML",fieldname:"results"}]});
    const target=dialog.fields_dict.results.$wrapper;
    dialog.on_page_show=async()=>{
     target.empty();
     try {
      const result=await rpc("search_assignees",{letter:doc.name,text:values.text || ""});
      if(!active() || !dialog.$wrapper.is(":visible"))return;
      for(const row of result.rows || result){const name=row.user || row.name;button(target,row.full_name || name,()=>{client.editAction(action.action_id,{assigned_to:name,assignment_decision:"assigned"});control.textContent=name;decision.value="assigned";changed();dialog.hide();});}
     }catch(error){target.empty();failure(error);}
    };
    dialog.show();
   },__("Select assignee"));
  }
  let humanControl;
  function changed() {frm.__posteingang_review_dirty=client.dirty;if(humanControl)humanControl.checked=false;}
  function openReview() {
   if(!client.state || frm.is_dirty())return;
   client.humanVerified=false;client.retainedResult=null;
   const readonly=!client.state.allowed_operations.includes("save_review");
   const dialog=new frappe.ui.Dialog({title:__("Review incoming letter"),size:"extra-large",fields:[{fieldtype:"HTML",fieldname:"review"}]});
   context.dialog=dialog;
   const root=dialog.fields_dict.review.$wrapper;
   text(root,"p",__("Selected Mandant")+": "+(client.state.selected_customer?.customer_name || client.review.selected_customer || __("Unknown")));
   for(const [name,label,type,max] of [["sender","Sender","text",255],["document_date","Document date","date"],["letter_type","Letter type","text",255],["summary","Summary","textarea",4000]])input(root,label,client.documentFields[name],value=>{client.editFields({[name]:value});changed();},{type,disabled:readonly,maxLength:max});
   for(const action of client.review.actions) {
    const section=text(root,"section","");section.className="border rounded p-3 mb-3";
    input(section,"Action title",action.title,value=>{client.editAction(action.action_id,{title:value});changed();},{disabled:readonly,maxLength:140});
    input(section,"Action description",action.description,value=>{client.editAction(action.action_id,{description:value});changed();},{type:"textarea",disabled:readonly,maxLength:4000});
    input(section,"Decision",action.decision,value=>{client.editAction(action.action_id,{decision:value});changed();},{options:choices.decision,disabled:readonly});
    input(section,"Rejection reason",action.rejection_reason,value=>{client.editAction(action.action_id,{rejection_reason:value});changed();},{type:"textarea",disabled:readonly,maxLength:1000});
    const assignment=input(section,"Assignment decision",action.assignment_decision,value=>{client.editAction(action.action_id,{assignment_decision:value,...(value==="unassigned" ? {assigned_to:null} : {})});if(value==="unassigned")assignee.textContent=__("Unassigned");changed();},{options:choices.assignment,disabled:readonly});
    const assignee=text(section,"p",action.assigned_to || __("Unassigned"));
    if(!readonly)button(section,"Select assignee",()=>chooseAssignee(action,assignee,assignment));
    input(section,"Reason for no assignee",action.assignment_reason,value=>{client.editAction(action.action_id,{assignment_reason:value});changed();},{type:"textarea",disabled:readonly,maxLength:1000});
    const scheduling=input(section,"Scheduling decision",action.scheduling_decision,value=>{client.editAction(action.action_id,{scheduling_decision:value,...(value==="unscheduled" ? {internal_date:null} : {})});if(value==="unscheduled")internal.value="";changed();},{options:choices.scheduling,disabled:readonly});
    const internal=input(section,"Internal work date",action.internal_date,value=>{client.editAction(action.action_id,{internal_date:value,...(value ? {scheduling_decision:"scheduled"} : {})});if(value)scheduling.value="scheduled";changed();},{type:"date",disabled:readonly});
    input(section,"Reason for no internal date",action.scheduling_reason,value=>{client.editAction(action.action_id,{scheduling_reason:value});changed();},{type:"textarea",disabled:readonly,maxLength:1000});
    const date=input(section,"Confirmed external deadline",action.external_date,value=>{client.editAction(action.action_id,{external_date:value});changed();},{type:"date",disabled:readonly});
    const basis=input(section,"External deadline basis",action.external_date_basis,value=>{client.editAction(action.action_id,{external_date_basis:value});changed();},{type:"textarea",disabled:readonly,maxLength:1000});
    const proposed=client.proposalFor(action.action_id)?.deadline;
    if(proposed){text(section,"p",__("Proposed deadline")+": "+(proposed.wording || proposed.date || proposed.kind));if(!readonly && proposed.kind==="explicit_date" && proposed.date)button(section,"Accept proposed external date",()=>{frappe.prompt({fieldname:"basis",fieldtype:"Small Text",label:__("External deadline basis"),reqd:1},values=>{client.acceptDate(action.action_id,proposed.date,values.basis);date.value=proposed.date;basis.value=values.basis;changed();});});}
    for(const evidence of action.evidence || [])text(section,"p",__("Page {0}",[evidence.page])+" · "+__(evidence.verification)+": "+(evidence.quote || ""));
   }
   input(root,"Reason for no actions",client.review.no_actions_reason,value=>{client.review.no_actions_reason=value;client.edited();changed();},{type:"textarea",disabled:readonly,maxLength:1000});
   input(root,"Clarification reason",client.review.clarification_reason,value=>{client.review.clarification_reason=value;client.edited();changed();},{type:"textarea",disabled:readonly,maxLength:1000});
   if(!readonly){
    humanControl=input(root,"I checked the original and all final decisions",false,value=>{client.humanVerified=value;},{type:"checkbox"});
    async function submit(operation,mode) {
     if(client.busy || !active())return;
     try {const result=await client.submit(operation,{mode});if(result)await finish();}catch(error){failure(error);}
    }
    button(root,"Save review",()=>submit("save_review","draft"));
    button(root,"Needs clarification",()=>submit("save_review","clarification"));
    if(client.state.allowed_operations.includes("confirm"))button(root,"Confirm letter",()=>{
     if(!client.humanVerified){frappe.msgprint(__("Check the original and explicitly confirm the human review."));return;}
     const final=()=>frappe.confirm(preview(),()=>submit("confirm"));
     if(client.state.proposal && client.state.proposal.run!==client.state.applied_run){frappe.prompt({fieldname:"reason",fieldtype:"Small Text",label:__("Reason for keeping the current review"),reqd:1},values=>{client.retainedResult={run:client.state.proposal.run,reason:values.reason};final();},__("Keep the current review instead of the latest result"));}else final();
    });
   }
   dialog.onhide=()=>{if(!client.busy && client.dirty && active())frappe.show_alert({message:__("Review edits remain in this page. Reopen the review to save them."),indicator:"orange"});};
   dialog.show();
  }
  function correction() {
   const dialog=new frappe.ui.Dialog({title:__("Create correction"),fields:[{fieldname:"reason",fieldtype:"Small Text",label:__("Correction reason"),reqd:1},{fieldname:"reuse_original",fieldtype:"Check",label:__("Reuse original PDF"),default:1},{fieldname:"upload",fieldtype:"HTML"},{fieldname:"received_date_known",fieldtype:"Check",label:__("Receipt date is known"),default:0},{fieldname:"received_date",fieldtype:"Date",label:__("Receipt date"),depends_on:"eval:doc.received_date_known"}],primary_action_label:__("Create correction"),primary_action:async values=>{
    if(client.busy || !active())return;
    const args={old_letter:doc.name,expected_revision:client.state.revision,reason:values.reason,reuse_original:!!values.reuse_original,received_date_known:!!values.received_date_known,received_date:values.received_date_known ? values.received_date : null};
    try{
     let result;
     if(values.reuse_original)result=await client.mutate("create_correction",args);
     else{
      if(file.files.length!==1){frappe.msgprint(__("Choose one PDF for the replacement."));return;}
      const semantic=JSON.stringify([args,file.files[0].name,file.files[0].size,file.files[0].lastModified]);
      if(context.correctionIntent?.semantic!==semantic)context.correctionIntent={semantic,key:crypto.randomUUID()};
      const data=correctionFormData(args,file.files[0],context.correctionIntent.key);
      client.busy=true;try{result=await rpc("create_correction",data);context.correctionIntent=null;}catch(error){if(error.message!=="NETWORK")context.correctionIntent=null;throw error;}finally{client.busy=false;}
     }
     if(result){dialog.hide();await finish();const name=result.replacement || result.replacement_letter;if(name)frappe.set_route("Form","Incoming Letter",name);}
    }catch(error){failure(error);}
   }});
   const file=document.createElement("input");file.type="file";file.accept="application/pdf,.pdf";dialog.fields_dict.upload.$wrapper.append(file);dialog.show();
  }
  function draw(state) {
   wrapper.empty();
   if(state.proposal?.extraction){
    const proposal=state.proposal.extraction,details=text(wrapper,"details","");text(details,"summary",__("Latest analysis proposal"));
    const assessment={readable:"Readable",partial:"Partly readable",unreadable:"Unreadable",actions_found:"Actions proposed",none_found:"No actions proposed by AI",unknown:"Action assessment unknown"};
    text(details,"p",__(assessment[proposal.readability] || "Unknown")+" · "+__(assessment[proposal.action_assessment] || "Unknown"));
    for(const warning of proposal.warnings || [])text(details,"p",warning.message);
    for(const field of ["sender","document_date","letter_type","summary"]){
     if(proposal[field]?.value)text(details,"p",proposal[field].value);
     for(const proof of proposal[field]?.evidence || [])text(details,"p",__("Page {0}",[proof.page])+" · "+__(proof.verification)+": "+(proof.quote || ""));
    }
    for(const action of proposal.actions || []){text(details,"strong",action.title);text(details,"p",action.description);if(action.deadline?.wording)text(details,"p",action.deadline.wording);}
   }
   if(state.original_file){const original=state.original_file;if(typeof original==="object" && original.file_url){const link=text(wrapper,"a",__("Open original PDF"));link.href=original.file_url;link.target="_blank";link.rel="noopener";}else button(wrapper,"Open original PDF",()=>frappe.set_route("Form","File",original));}
   button(wrapper,state.status==="Completed" ? "View confirmed review" : "Review incoming letter",openReview);
   if(state.allowed_operations.includes("apply_result") && state.proposal)button(wrapper,"Apply latest analysis result",()=>{
    if(dirty(frm)){frappe.msgprint(__("Save the current review before this operation."));return;}
    const replace=!!state.applied_run || !!state.review.actions.length;
    const apply=()=>mutate("apply_result",{letter:doc.name,expected_run:state.proposal.run,expected_revision:state.revision,acknowledge_replace:replace});
    if(replace)frappe.confirm(__("Replace the current review? Its previous version will be preserved in history."),apply);else apply();
   });
   if(state.allowed_operations.includes("add_manual_action"))button(wrapper,"Add manual action",()=>{
    if(dirty(frm)){frappe.msgprint(__("Save the current review before this operation."));return;}
    frappe.prompt([{fieldname:"title",fieldtype:"Data",label:__("Action title"),reqd:1},{fieldname:"description",fieldtype:"Small Text",label:__("Action description"),reqd:1}],values=>mutate("add_manual_action",{letter:doc.name,title:values.title,description:values.description,expected_revision:state.revision}),__("Add manual action"));
   });
   if(state.receipt)for(const item of state.receipt.actions || state.receipt.tasks || [])if(item.task)button(wrapper,item.task,()=>frappe.set_route("Form","Task",item.task));
   if(state.allowed_operations.includes("create_correction"))button(wrapper,"Create correction",correction);
   if(state.correction){
    text(wrapper,"p",__(state.correction.ready ? "Administrative review is complete." : "Administrative review of the original tasks is required."));
    if(state.allowed_operations.includes("correction_disposition"))for(const task of state.correction.tasks || []){
     text(wrapper,"p",`${task.task} · ${__(task.status)} · ${task.disposition ? __(task.disposition) : __("Pending")}`);
     button(wrapper,"Resolve original task",()=>frappe.prompt([{fieldname:"disposition",fieldtype:"Select",label:__("Disposition"),options:["cancelled","historical"],reqd:1},{fieldname:"reason",fieldtype:"Small Text",label:__("Reason"),reqd:1}],values=>mutate("correction_disposition",{replacement:doc.name,expected_revision:state.revision,original_action_id:task.action_id,disposition:values.disposition,reason:values.reason}),__("Resolve original task")));
    }
   }
  }
  client.load(frm,active).then(state=>{if(state && active())draw(state);}).catch(failure);
 }
 frappe.posteingang=Object.assign(frappe.posteingang || {},{ReviewClient,attach_review:attach,revoke_view:revoke,correction_form_data:correctionFormData,review_rpc:rpc,review_messages:messages});
})();
