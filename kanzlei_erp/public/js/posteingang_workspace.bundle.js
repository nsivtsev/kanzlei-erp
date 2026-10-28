/* AP-09: one in-memory draft and frozen mutation intent; no document storage. */
(() => {
 const clone=value=>JSON.parse(JSON.stringify(value));
 const sourceOperations=new Set(['matching.select_customer','review.add_manual_action','review.apply_result','analysis.queue_analysis','analysis.manual_review','analysis.cancel','review.create_correction','review.correction_disposition']);
 class WorkspaceClient {
  constructor(transport,user) {
   this.transport=transport;this.user=user;this.owner=user();this.sequence=0;this.queueSequence=0;
   this.review=new frappe.posteingang.ReviewClient();this.state=null;this.remote=null;this.rows=[];this.intent=null;this.busy=false;this.conflict=false;this.active=true;
  }
  check(data) {if(this.user()!==this.owner || data?.user!==this.owner){throw Error('PERMISSION');}return data;}
  clear() {this.sequence++;this.queueSequence++;this.state=null;this.remote=null;this.rows=[];this.review.clear();this.intent=null;this.conflict=false;}
  suspend() {this.active=false;this.sequence++;this.queueSequence++;}
  async load(letter) {
   if(this.state && this.state.letter!==letter && this.review.dirty)throw Error('DIRTY_REVIEW');
   const sequence=++this.sequence;this.active=true;this.state=null;this.remote=null;this.review.clear();this.conflict=false;
   try {
    const data=await this.transport('get_letter_workspace',{letter});
    if(sequence!==this.sequence || !this.active)return null;
    this.state=this.check(data);this.review.adopt(data.review);return data;
   }catch(error){if(sequence!==this.sequence || !this.active)return null;if(error.message==='PERMISSION')this.clear();throw error;}
  }
  async queue(filters,cursor=null) {
   const sequence=++this.queueSequence;
   try {
    const data=await this.transport('get_queue',{filters,cursor});
    if(sequence!==this.queueSequence || !this.active)return null;
    this.check(data);this.rows=cursor ? [...this.rows,...data.items] : data.items;return data;
   }catch(error){if(sequence!==this.queueSequence || !this.active)return null;if(error.message==='PERMISSION')this.clear();throw error;}
  }
  async status() {
   if(!this.state || !this.active)return null;
   const sequence=this.sequence,letter=this.state.letter;
   try {
    const data=await this.transport('get_workspace_status',{letter});
    if(sequence!==this.sequence || !this.active)return null;
    this.check(data);
    if(!data.capabilities.customer_read)throw Error('PERMISSION');
    const changed=data.revision!==this.state.revision || data.current_run!==this.state.current_run || data.latest_successful_run!==this.state.latest_successful_run;
    if(changed){this.review.humanVerified=false;this.review.retainedResult=null;}
    this.remote=data;
    if(changed && this.review.dirty)this.conflict=true;
    if(changed && !this.review.dirty && !this.intent)return await this.load(letter);
    if(data.visibility_key!==this.state.visibility_key){
     if(!this.review.dirty && !this.intent)return await this.load(letter);
     await this.references();this.review.humanVerified=false;this.review.retainedResult=null;
    }
    if(sequence!==this.sequence || !this.active)return null;
    this.state.capabilities=data.capabilities;this.state.analysis=data.analysis;
    return data;
   }catch(error){if(sequence!==this.sequence || !this.active)return null;if(error.message==='PERMISSION')this.clear();throw error;}
  }
  async references() {
   const sequence=this.sequence,letter=this.state.letter;
   const data=await this.transport('get_letter_workspace',{letter});
   if(sequence!==this.sequence || !this.active)return null;
   this.check(data);
   if(!data.capabilities.customer_read)throw Error('PERMISSION');
   if(data.revision!==this.state.revision){this.conflict=true;this.review.humanVerified=false;this.review.retainedResult=null;}
   this.state.matching=data.matching;
   for(const key of ['receipt','correction','allowed_operations'])this.state.review[key]=data.review[key];
   this.state.visibility_key=data.visibility_key;this.referencesChanged=true;return data;
  }
  preflight(args) {
   if(this.user()!==this.owner){this.clear();throw Error('PERMISSION');}
   const letter=args.letter || args.old_letter || args.replacement;
   if(!this.active || !this.state || (letter && letter!==this.state.letter))throw Error('STALE_CONTEXT');
  }
  async mutate(operation,args) {
   this.preflight(args);
   if(this.busy)return null;
   if(this.intent)throw Error('UNKNOWN_OUTCOME');
   if(this.conflict)throw Error('REVISION_CONFLICT');
   if(sourceOperations.has(operation) && this.review.dirty)throw Error('DIRTY_REVIEW');
   const {_file,...json}=args;
   this.intent={operation,args:{...clone(json),...(_file ? {_file} : {}),request_key:crypto.randomUUID()}};
   return this.retry();
  }
  async retry() {
   if(this.busy || !this.intent)return null;
   this.preflight(this.intent.args);
   const intent=this.intent,sequence=this.sequence;this.busy=true;
   try {
    const result=await this.transport(intent.operation,intent.args);
    if(sequence!==this.sequence || !this.active)return null;
    if(this.user()!==this.owner)throw Error('PERMISSION');
    this.intent=null;this.review.dirty=false;return result;
   }catch(error){
    if(sequence!==this.sequence || !this.active)return null;
    if(error.message==='PERMISSION')this.clear();
    else if(error.message!=='NETWORK')this.intent=null;
    if(['REVISION_CONFLICT','CUSTOMER_CHANGED','STALE_RUN'].includes(error.message)){this.conflict=true;this.review.humanVerified=false;this.review.retainedResult=null;}
    throw error;
   }finally{this.busy=false;}
  }
 }
 // UI guidance only. The service remains authoritative for confirmation.
 function reviewIssues(state,review,step) {
  const issues=[],blank=value=>!String(value || '').trim();
  const add=(message,action)=>issues.push({message,action_id:action?.action_id});
  if(step===0){
   if(!state.matching.selected_customer)add('Select a Mandant before continuing.');
   else if(blank(state.matching.customer_reason))add('Explain the Mandant selection.');
   if(state.matching.matching?.ambiguity==='multiple_addressees')add('Several addressees require clarification before selecting one Mandant.');
   return issues;
  }
  const actions=review.actions || [],accepted=actions.filter(a=>a.decision==='accept');
  for(const action of actions){
   if(action.decision==='pending')add('Decide whether this action is required.',action);
   if(action.decision==='reject' && blank(action.rejection_reason))add('Explain why this action is not required.',action);
   if(action.decision!=='accept')continue;
   if(blank(action.title) || blank(action.description))add('Enter an action title and description.',action);
   if(action.assigned_to ? action.assignment_decision!=='assigned' : action.assignment_decision!=='unassigned' || blank(action.assignment_reason))add('Select an assignee or explain why assignment is deferred.',action);
   if(action.internal_date ? action.scheduling_decision!=='scheduled' : action.scheduling_decision!=='unscheduled' || blank(action.scheduling_reason))add('Set an internal work date or explain why planning is deferred.',action);
   if(action.external_date && blank(action.external_date_basis))add('Enter the basis for the confirmed external deadline.',action);
  }
  if(!accepted.length && blank(review.no_actions_reason))add('Explain why no tasks are required.');
  if(accepted.length && !blank(review.no_actions_reason))add('Clear the no-actions reason when accepting a task.');
  return issues;
 }
 frappe.posteingang=Object.assign(frappe.posteingang || {},{WorkspaceClient,reviewIssues});
})();
