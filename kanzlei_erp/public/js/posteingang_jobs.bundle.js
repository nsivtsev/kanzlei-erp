/* AP-05 management only; analysis never replaces the review draft here. */
(() => {
 const messages = {
  REVISION_CONFLICT: "The letter changed. Reload the form before continuing.",
  IDEMPOTENCY_CONFLICT: "This request was already used with different data",
  ANALYSIS_IN_PROGRESS: "Analysis is already in progress.",
  ANALYSIS_STOPPED: "Analysis stopped. Check access and settings before starting again.",
  CONFIGURATION_INVALID: "Analysis configuration is invalid. Contact a system manager.",
  PROVIDER_UNAVAILABLE: "The analysis service is temporarily unavailable.",
  OCR_FAILED: "The OCR result could not be accepted. Human review is required.",
  INVALID_RESULT: "The analysis result could not be accepted. Human review is required.",
  LIMIT_EXCEEDED: "Analysis exceeded an allowed limit. Human review is required.",
  FAKE_FIXTURE_NOT_FOUND: "The test provider supports synthetic fixture documents only.",
  WORKER_INTERRUPTED: "Analysis was interrupted. Start again explicitly; a request may already have been processed.",
  QUEUE_UNAVAILABLE: "Analysis is waiting for the queue to recover.",
  INTERNAL_ERROR: "Analysis could not finish. Retry explicitly or use human review.",
  INVALID_TRANSITION: "This operation is not available in the current state.",
  INVALID_REQUEST: "Check the analysis request parameters.",
  NETWORK: "The result is unknown; check the request before retrying",
  PERMISSION: "Posteingang operation is not permitted",
 };
 class AnalysisClient {
  constructor(transport) {this.transport = transport; this.busy = false; this.intent = null; this.stopped = false;}
  async mutate(operation, letter, revision, reason) {
   if (this.busy) return null;
   const semantic = JSON.stringify([operation,letter,revision,reason]);
   if (!this.intent || this.intent.semantic !== semantic) this.intent = {semantic,key:crypto.randomUUID()};
   this.busy = true;
   try {
    const args={letter,expected_revision:revision,request_key:this.intent.key};
    if (operation === "cancel") args.reason=reason;
    const result=await this.transport(operation,args);
    this.intent=null;return result;
   } finally {this.busy=false;}
  }
  async refresh(frm, isActive = () => true) {
   if (!isActive() || frm.is_dirty()) return null;
   try {
    const state=await this.transport("get_analysis_state",{letter:frm.doc.name});
    this.stopped=!["Queued","Processing"].includes(state.status);
    if (!isActive() || frm.is_dirty()) return null;
    if (state.revision !== frm.doc.revision) await frm.reload_doc();
    return state;
   } catch (error) {this.stopped=true;throw error;}
  }
 }
 async function rpc(operation,args) {
  let response;
  try {response=await fetch(`/api/method/kanzlei_erp.posteingang_jobs.${operation}`,{
   method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-Frappe-CSRF-Token":frappe.csrf_token},body:JSON.stringify(args)
  });} catch {throw new Error("NETWORK");}
  let body;
  try {body=await response.json();} catch {throw new Error("NETWORK");}
  if (!response.ok || body.exc) {
   const text=JSON.stringify(body);
   throw new Error(Object.keys(messages).find(key=>text.includes(key)) || (response.status === 403 ? "PERMISSION" : "INTERNAL_ERROR"));
  }
  return body.message;
 }
 const label = {queue_analysis:"Start analysis",manual_review:"Switch to human review",cancel:"Cancel letter"};
 function attach(frm) {
  if (frm.is_new()) return;
  if (frm._ap05) {frm._ap05.active=false;clearTimeout(frm._ap05.timer);}
  const context={active:true,client:frm._analysis_client || new AnalysisClient(rpc)};
  frm._analysis_client=context.client;frm._ap05=context;
  const onRoute=()=>frappe.get_route_str() === `Form/Incoming Letter/${frm.doc.name}`;
  const errorMessage=error=>frappe.msgprint(__(messages[error.message] || messages.INTERNAL_ERROR));
  async function perform(operation,reason) {
   if (frm.is_dirty()) {frappe.msgprint(__("Reload the form before managing analysis."));return;}
   try {
    await context.client.mutate(operation,frm.doc.name,frm.doc.revision,reason);
    await frm.reload_doc();
   } catch(error) {errorMessage(error);}
  }
  function draw(state) {
   const signature=JSON.stringify(state);
   if (signature === context.rendered) return;
   context.rendered=signature;
   for (const text of [...Object.values(label),"Analyze again"]) frm.remove_custom_button(__(text));
   for (const operation of state.allowed_operations) {
    const text=operation === "queue_analysis" && state.generation ? "Analyze again" : label[operation];
    frm.add_custom_button(__(text),()=>{
     if (operation === "cancel") {
      frappe.prompt({fieldname:"reason",fieldtype:"Small Text",label:__("Reason"),reqd:1},values=>perform(operation,values.reason),__(label.cancel));
     } else if (operation === "queue_analysis" && state.status === "Needs Review") {
      frappe.confirm(__("Analyze this letter again? Your current review edits will be preserved."),()=>perform(operation));
     } else perform(operation);
    });
   }
   let text=__(state.status);
   if (state.run?.error_code) text+=" — "+__(messages[state.run.error_code] || messages.INTERNAL_ERROR);
   if (state.run?.status === "Queued" && state.run.next_attempt_at) text+=" · "+__("Next attempt no earlier than")+" "+state.run.next_attempt_at;
   frm.dashboard.set_headline_alert(frappe.utils.escape_html(text));
  }
  async function poll() {
   if (!context.active || !onRoute()) return;
   try {
    const state=await context.client.refresh(frm,()=>context.active && onRoute());
    if (!context.active || !onRoute()) return;
    if (state) draw(state);
    if (!context.client.stopped) context.timer=setTimeout(poll,5000);
   } catch(error) {
    if (context.active) {
     for (const text of [...Object.values(label),"Analyze again"]) frm.remove_custom_button(__(text));
     frm.dashboard.set_headline_alert(frappe.utils.escape_html(__(messages[error.message] || messages.INTERNAL_ERROR)));
    }
   }
  }
  poll();
 }
 frappe.posteingang = Object.assign(frappe.posteingang || {},{AnalysisClient,attach_analysis:attach});
})();
