/* One-click Posteingang card model. Pure functions: no DOM, no network. */
(() => {
 const toGerman=iso=>{
  const m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(iso||"");
  return m?`${m[3]}.${m[2]}.${m[1]}`:null;
 };
 // Accepts TT.MM.JJJJ (ISO only with allowIso); returns ISO, null when empty, throws INVALID_DATE otherwise.
 const toIso=(text,allowIso=false)=>{
  const value=typeof text==="string"?text.trim():text;
  if(value===null || value===undefined || value==="")return null;
  let y,mo,d;
  let m=/^(\d{2})\.(\d{2})\.(\d{4})$/.exec(value);
  if(m)[d,mo,y]=[m[1],m[2],m[3]];
  else if(allowIso && (m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(value)))[y,mo,d]=[m[1],m[2],m[3]];
  else throw new Error("INVALID_DATE");
  const date=new Date(Date.UTC(+y,+mo-1,+d));
  if(date.getUTCFullYear()!==+y || date.getUTCMonth()!==+mo-1 || date.getUTCDate()!==+d)throw new Error("INVALID_DATE");
  return `${y}-${mo}-${d}`;
 };
 const userDated=task=>task.deadline.kind==="relative" || task.deadline.kind==="uncertain";
 const isEmpty=v=>v===null || v===undefined || String(v).trim()==="";
 const customerBlocked=(card,customer)=>
  !customer || (card.customer.candidates||[]).some(c=>c.customer===customer && c.can_select===false);

 function cardChoice(card) {
  return {
   customer:card.customer.suggested?.customer ?? null,
   selected:card.tasks.map(()=>true),
   deadlines:card.tasks.map(t=>t.deadline.kind==="explicit_date"?toGerman(t.deadline.date):null),
   manual:null,
  };
 }
 function missingFields(card,choice) {
  const out=[];
  if(customerBlocked(card,choice.customer))out.push({key:"customer",text:"Select the Mandant."});
  card.tasks.forEach((t,i)=>{
   if(choice.selected[i] && userDated(t) && isEmpty(choice.deadlines[i]))
    out.push({key:`deadline:${i}`,text:"Enter the deadline date from the letter."});
  });
  return out;
 }
 function primaryAction(card,choice) {
  if(card.state==="failed" || card.state==="not_started")return {kind:"manual",label:"Create manually",count:0};
  const count=choice.selected.filter(Boolean).length;
  if(!count)return {kind:"archive",label:"File without task",count:0};
  return {kind:"accept",label:count===1?"Accept":"Accept {0} tasks",count};
 }
 function simplePayload(card,choice) {
  if(missingFields(card,choice).length)throw new Error("MISSING");
  const tasks=[];
  card.tasks.forEach((t,i)=>{
   if(!choice.selected[i])return;
   const entered=userDated(t);
   const external=toIso(choice.deadlines[i]) ?? (t.deadline.kind==="explicit_date"?t.deadline.date:null);
   tasks.push({
    source_index:t.source_index,title:t.title,description:t.description,
    external_date:external,internal_date:entered?null:(t.internal_date ?? null),assigned_to:t.assigned_to ?? null,
   });
  });
  return {origin:"proposal",customer:choice.customer,tasks};
 }
 function manualPayload(form) {
  if(!form.customer)throw new Error("MISSING");
  return {origin:"manual",customer:form.customer,tasks:(form.rows||[]).map(r=>({
   source_index:null,title:r.title,description:r.description ?? "",
   external_date:toIso(r.external_date,true),internal_date:toIso(r.internal_date,true),assigned_to:r.assigned_to ?? null,
  }))};
 }
 function listChip(row) {
  const status=row.status;
  if(row.result_error || status==="Error")return {tone:"fail",text:"Check manually"};
  if(status==="Completed" || status==="Cancelled")return {tone:"done",text:"Done"};
  if(status==="Queued" || status==="Processing")return {tone:"busy",text:"Analysing"};
  if(status==="Received") {
   const a=row.analysis?.status;
   return a==="queued" || a==="processing"?{tone:"busy",text:"Analysing"}:{tone:"need",text:"Not analysed"};
  }
  const amb=row.matching?.ambiguity;
  const dl=row.deadline||{};
  if((amb && amb!=="none") || (dl.relative_count||0)+(dl.uncertain_count||0)>0)return {tone:"need",text:"Input needed"};
  return {tone:"ready",text:"Proposal ready"};
 }
 frappe.posteingang=Object.assign(frappe.posteingang||{},{cardChoice,missingFields,primaryAction,simplePayload,manualPayload,listChip});
})();
