/* AP-06: current-permission proposals and explicit human selection. */
(() => {
 const messages={INVALID_REQUEST:"Check the Mandant selection parameters.",INVALID_RESULT:"The analysis result could not be accepted. Human review is required.",CUSTOMER_CHANGED:"The Customer changed. Reload it before verifying an identifier.",REVISION_CONFLICT:"The letter changed. Reload the form before continuing.",IDEMPOTENCY_CONFLICT:"This request was already used with different data",MULTIPLE_ADDRESSEES:"Several addressees require clarification before selecting one Mandant.",INVALID_TRANSITION:"This operation is not available in the current state.",PERMISSION:"Posteingang operation is not permitted",NETWORK:"The result is unknown; check the request before retrying",INTERNAL_ERROR:"Could not load Mandant candidates. Reload and try again."};
 async function rpc(operation,args) {
  const module=operation==="verify_identifier" ? "mandant_identifiers" : "posteingang_matching";
  let response,body;
  try {response=await fetch(`/api/method/kanzlei_erp.${module}.${operation}`,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-Frappe-CSRF-Token":frappe.csrf_token},body:JSON.stringify(args)});body=await response.json();}
  catch {throw Error("NETWORK");}
  if(!response.ok || body.exc) throw Error(Object.keys(messages).find(k=>JSON.stringify(body).includes(k)) || (response.status===403 ? "PERMISSION" : "INTERNAL_ERROR"));
  return body.message;
 }
 class MatchingClient {
  constructor(transport) {this.transport=transport;this.sequence=0;this.busy=false;this.intent=null;}
  async load(frm,active,clear) {
   const sequence=++this.sequence,doc=frm.doc;clear();
   if(!active() || (frm.is_dirty() || frm.__posteingang_review_dirty)) return null;
   try {
    const result=await this.transport("get_matching_state",{letter:frm.doc.name});
    return active() && frm.doc===doc && !(frm.is_dirty() || frm.__posteingang_review_dirty) && sequence===this.sequence ? result : null;
   } catch(error) {if(error.message==="PERMISSION" && active() && frm.doc===doc && sequence===this.sequence)frappe.posteingang.revoke_view?.(frm);throw error;}
  }
  async select(letter,customer,reason,revision) {
   if(this.busy) return null;
   const semantic=JSON.stringify([letter,customer,reason]);
   if(this.intent?.semantic!==semantic) this.intent={semantic,args:{letter,customer,reason,expected_revision:revision,request_key:crypto.randomUUID()}};
   this.busy=true;
   try {const result=await this.transport("select_customer",this.intent.args);this.intent=null;return result;}
   catch(error) {if(error.message!=="NETWORK") this.intent=null;throw error;}
   finally {this.busy=false;}
  }
 }
 const showError=error=>frappe.msgprint(__(messages[error.message] || messages.INTERNAL_ERROR));
 function attach(frm) {
  if(frm.is_new() || !frappe.user.has_role("Posteingang Operator")) return;
  if(frm._ap06) frm._ap06.active=false;
  const context={active:true,client:frm._matching_client || new MatchingClient(rpc)};
  frm._matching_client=context.client;frm._ap06=context;
  const wrapper=frm.dashboard.add_section("",__("Mandant selection")),doc=frm.doc;
  context.wrapper=wrapper;
  const active=()=>context.active && frm.doc===doc && frappe.get_route_str()===`Form/Incoming Letter/${doc.name}`;
  const text=(tag,value,parent=wrapper)=>{const element=document.createElement(tag);element.textContent=value;parent.append(element);return element;};
  const button=(label,handler,parent=wrapper)=>{const element=text("button",__(label),parent);element.type="button";element.className="btn btn-default btn-sm mr-2 mb-2";element.addEventListener("click",handler);return element;};
  async function select(customer) {
   if(!active() || (frm.is_dirty() || frm.__posteingang_review_dirty) || context.client.busy) return;
   frappe.prompt({fieldname:"reason",fieldtype:"Small Text",label:__("Reason"),reqd:1},async values=>{
    if(!active() || (frm.is_dirty() || frm.__posteingang_review_dirty)) return;
    try {const result=await context.client.select(frm.doc.name,customer,values.reason,frm.doc.revision);if(result && active() && !(frm.is_dirty() || frm.__posteingang_review_dirty)) await frm.reload_doc();}
    catch(error) {if(active()) {wrapper.empty();if(error.message==="PERMISSION")frappe.posteingang.revoke_view?.(frm);showError(error);await load();}}
   },__(customer ? "Select Mandant" : "Clear Mandant selection"));
  }
  function draw(state) {
   if(state.selected_customer) text("p",__("Selected Mandant")+": "+state.selected_customer.customer_name+" — "+state.customer_reason);
   if(!state.matching) text("p",__("No completed analysis is available. You can search for a Mandant manually."));
   else {
    const status={none:"Review the proposed Mandant before selecting it.",not_found:"No Mandant candidates found.",multiple_matches:"Several Mandant candidates match. An explicit decision is required.",multiple_addressees:"Several addressees require clarification before selecting one Mandant."};
    text("p",__(status[state.matching.ambiguity]));
    for(const candidate of state.matching.candidates) {
     const section=text("div","");section.className="border rounded p-3 mb-2";
     text("strong",candidate.customer_name,section);
     for(const reason of candidate.reasons) {
      text("p",__(reason.kind==="exact_identifier" ? "Verified identifier match" : "Matching name")+(reason.identifier_type ? " · "+reason.identifier_type : ""),section);
      for(const proof of reason.evidence) text("p",__("Page {0}",[proof.page])+": "+(proof.quote || ""),section);
     }
     for(const conflict of candidate.conflicts) text("p",__(conflict.code==="identifier_conflict" ? "Conflicting identifiers require review." : "The extracted name differs from this Customer."),section);
     if(state.allowed_operations.includes("select_customer") && candidate.can_select) button("Select Mandant",()=>select(candidate.customer),section);
     else if(!candidate.can_select) text("p",__("Read-only Customer; selection requires write access."),section);
    }
   }
   if(state.allowed_operations.includes("select_customer")) {
    button("Find another Mandant",()=>frappe.prompt({fieldname:"text",fieldtype:"Data",label:__("Customer Name"),reqd:0},async values=>{
     if(!active() || (frm.is_dirty() || frm.__posteingang_review_dirty)) return;
     let start=0;
     const dialog=new frappe.ui.Dialog({title:__("Find another Mandant"),fields:[{fieldtype:"HTML",fieldname:"results"}]});
     const target=dialog.fields_dict.results.$wrapper;
     async function page() {
      target.empty();
      try {
       const result=await rpc("search_customers",{letter:frm.doc.name,text:values.text || "",start});
       if(!active() || (frm.is_dirty() || frm.__posteingang_review_dirty) || !dialog.$wrapper.is(":visible")) return;
       for(const row of result.rows) {text("p",row.customer_name,target);if(row.can_select) button("Select Mandant",()=>{dialog.hide();select(row.customer);},target);}
       if(!result.rows.length) text("p",__("No Mandant candidates found."),target);
       if(result.has_next) button("More",()=>{start=result.next_start;page();},target);
      } catch(error) {target.empty();if(active()) {if(error.message==="PERMISSION"){wrapper.empty();frappe.posteingang.revoke_view?.(frm);}showError(error);}}
     }
     dialog.on_page_show=page;dialog.show();
    },__("Find another Mandant")));
   }
   if(state.selected_customer && state.allowed_operations.includes("clear_customer")) button("Clear Mandant selection",()=>select(null));
   button("Refresh Mandant candidates",load);
  }
  async function load() {
   try {const state=await context.client.load(frm,active,()=>wrapper.empty());if(state && active()) draw(state);}
   catch(error) {if(active()) text("p",__(messages[error.message] || messages.INTERNAL_ERROR));}
  }
  load();
 }
 function attachIdentifiers(frm) {frm.fields_dict.kanzlei_identifiers?.grid.toggle_display("verify_identifier",!frm.is_new() && !!frm.perm?.some(p=>p.write));}
 async function verify(frm,row) {
  if(frm.is_new() || (frm.is_dirty() || frm.__posteingang_review_dirty)) {frappe.msgprint(__("Save the Customer before verifying an identifier."));return;}
  if(!frm.perm?.some(p=>p.write) || frm._identifier_verifying) return;
  const doc=frm.doc,name=doc.name, active=()=>frappe.get_route_str()===`Form/Customer/${name}` && frm.doc===doc;
  frappe.prompt({fieldname:"source",fieldtype:"Small Text",label:__("Verification Source"),reqd:1},async values=>{
   if(!active() || (frm.is_dirty() || frm.__posteingang_review_dirty) || frm._identifier_verifying) return;
   frm._identifier_verifying=true;
   try {await rpc("verify_identifier",{customer:name,identifier:row.name,expected_modified:frm.doc.modified,verification_source:values.source});if(active() && !(frm.is_dirty() || frm.__posteingang_review_dirty)) await frm.reload_doc();}
   catch(error) {if(active()) {showError(error);if(error.message==="NETWORK" && !(frm.is_dirty() || frm.__posteingang_review_dirty)) await frm.reload_doc();}}
   finally {frm._identifier_verifying=false;}
  },__("Verify identifier"));
 }
 const invalidate=(frm,cdt,cdn)=>{const row=locals[cdt][cdn];for(const field of ["verified_by","verified_at","verification_source"]) row[field]=null;frm.refresh_field("kanzlei_identifiers");};
 frappe.ui.form.on("Mandant Identifier",{verify_identifier:(frm,cdt,cdn)=>verify(frm,locals[cdt][cdn]),identifier_type:invalidate,raw_value:invalidate,jurisdiction:invalidate,authority:invalidate,other_type:invalidate});
 frappe.posteingang=Object.assign(frappe.posteingang || {},{MatchingClient,attach_matching:attach});
 frappe.mandant_identifiers={attach:attachIdentifiers};
})();
