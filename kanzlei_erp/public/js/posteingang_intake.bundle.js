/* PDF intake deliberately bypasses the generic attachment uploader. */
(() => {
 const api = "kanzlei_erp.posteingang";
 const codes = {
  INVALID_REQUEST: "Check the receipt date and upload parameters",
  FILE_TOO_LARGE: "PDF exceeds the allowed size",
  PDF_INVALID: "The file is not a valid PDF",
  PDF_ENCRYPTED: "Encrypted PDF files are not accepted",
  PAGE_LIMIT_EXCEEDED: "PDF exceeds the allowed page count",
  PDF_VALIDATION_LIMIT: "PDF validation exceeded its resource limit",
  INTAKE_BUSY: "Intake is busy; retry with the same request",
  IDEMPOTENCY_CONFLICT: "This request was already used with different data",
  CANNOT_ACCEPT_FILE: "This file cannot be accepted",
  NETWORK: "The result is unknown; check the request before retrying",
 };
 class IntakeClient {
  constructor(transport) {
   this.transport = transport;
   this.user = frappe.session.user;
   this.storageKey = `posteingang-intake:${window.location.host}:${this.user}`;
  }
  pending() { return JSON.parse(localStorage.getItem(this.storageKey) || "[]"); }
  remember(row) {
   if (frappe.session.user !== this.user) throw Error("CANNOT_ACCEPT_FILE");
   const rows = this.pending().filter(r => r.key !== row.key);
   rows.push({key: row.key, known: row.sent.known, date: row.sent.date});
   localStorage.setItem(this.storageKey, JSON.stringify(rows));
  }
  forget(key) {
   localStorage.setItem(this.storageKey, JSON.stringify(this.pending().filter(r => r.key !== key)));
  }
  add(file, previous) {
   return {key: previous?.key || crypto.randomUUID(), file, known: previous?.known ?? false,
    date: previous?.date ?? null, sent: previous ? {known: previous.known, date: previous.date} : null,
    result: null, error: null, busy: false};
  }
  async upload(rows, changed = () => {}) {
   for (const row of rows) {
    if (!row.file || row.result) continue;
    row.busy = true; row.error = null; changed();
    try {
     row.sent ||= {known: row.known, date: row.known ? row.date : null};
     this.remember(row); // Fail closed if recovery metadata cannot be saved.
     row.result = await this.transport(row);
     this.forget(row.key);
    } catch (error) {
     row.error = codes[error.message] ? error.message : "NETWORK";
    } finally {row.busy = false; changed();}
   }
  }
  async recover(check) {
   const rows = [];
   for (const saved of this.pending()) {
    const row = this.add(null, saved);
    try {
     const result = await check(row.key);
     if (result.outcome !== "unknown") { row.result = result; this.forget(row.key); }
    } catch {row.error = "NETWORK";}
    rows.push(row);
   }
   return rows;
  }
 }
 async function rpc(method, args = {}) {
  const response = await frappe.call({method: `${api}.${method}`, args, type: "POST", silent: true});
  return response.message;
 }
 async function send(row) {
  const form = new FormData();
  form.append("file", row.file);
  form.append("request_key", row.key);
  form.append("received_date_known", String(row.sent.known));
  if (row.sent.known) form.append("received_date", row.sent.date || "");
  let response;
  try {
   response = await fetch(`/api/method/${api}.receive`, {method: "POST", body: form,
    credentials: "same-origin", cache: "no-store", headers: {"X-Frappe-CSRF-Token": frappe.csrf_token}});
  } catch {throw Error("NETWORK");}
  if (response.status === 413) throw Error("FILE_TOO_LARGE");
  let data;
  try {data = await response.json();} catch {throw Error("NETWORK");}
  if (!response.ok) {
   const code = Object.keys(codes).find(c => (data.exception || "").includes(c));
   throw Error(code || (response.status === 403 ? "CANNOT_ACCEPT_FILE" : "NETWORK"));
  }
  return data.message;
 }
 async function open() {
  const options = await rpc("get_intake_options");
  const client = new IntakeClient(send);
  let rows;
  try {rows = await client.recover(key => rpc("get_intake_result", {request_key: key}));}
  catch {frappe.msgprint(__("Browser storage is unavailable; PDF upload cannot start")); return;}
  let running = false;
  const dialog = new frappe.ui.Dialog({title: __("Upload PDF"), fields: [{fieldname: "body", fieldtype: "HTML"}],
   primary_action_label: __("Upload PDF"), primary_action: async () => {
    if (running || !options.enabled) return;
    running = true; render();
    await client.upload(rows, render);
    running = false; render();
    if (frappe.get_route()[0] === "List") cur_list?.refresh();
   }});
  const body = dialog.fields_dict.body.$wrapper;
  const controls = $("<div class='mb-3'></div>").appendTo(body);
  $("<p></p>").text(__("Receipt dates are separate from upload dates. Unknown dates stay unknown.")).appendTo(controls);
  if (!options.enabled) $("<p></p>").text(__("New PDF intake is disabled")).appendTo(controls);
  const files = $("<input type='file' accept='.pdf,application/pdf' multiple class='form-control mb-2'>").prop("disabled", !options.enabled).appendTo(controls);
  files.on("change", () => {for (const file of files[0].files) rows.push(client.add(file)); files.val(""); render();});
  const common = $("<input type='date' class='form-control mb-2'>").attr("aria-label", __("Receipt date")).appendTo(controls);
  const apply = $("<button type='button' class='btn btn-default mb-2'></button>").text(__("Apply date to all unsent files")).appendTo(controls);
  apply.on("click", () => {for (const row of rows) if (!row.sent) {row.known = Boolean(common.val()); row.date = common.val() || null;} render();});
  const list = $("<div></div>").appendTo(body);
  function render() {
   list.empty();
   files.prop("disabled", running || !options.enabled); apply.prop("disabled", running);
   dialog.get_primary_btn().prop("disabled", running || !options.enabled);
   for (const row of rows) {
    const box = $("<div class='border rounded p-3 mb-2'></div>").appendTo(list);
    $("<p></p>").text(row.file?.name || __("Recovered upload request")).appendTo(box);
    const known = $("<input type='checkbox'>").prop("checked", row.known).prop("disabled", Boolean(row.sent) || running);
    $("<label></label>").append(known, document.createTextNode(" " + __("Receipt date known"))).appendTo(box);
    const date = $("<input type='date' class='form-control mb-2'>").attr("aria-label", __("Receipt date")).val(row.date || "").prop("disabled", !row.known || Boolean(row.sent) || running).appendTo(box);
    known.on("change", () => {row.known = known.prop("checked"); row.date = row.known ? row.date : null; render();});
    date.on("change", () => {row.date = date.val() || null;});
    if (!row.file && !row.result) {
     $("<p></p>").text(__("Select the original file to retry this request")).appendTo(box);
     $("<input type='file' accept='.pdf,application/pdf' class='form-control'>").prop("disabled", running).on("change", function () {row.file = this.files[0] || null; render();}).appendTo(box);
    }
    if (row.busy) $("<p></p>").text(__("Uploading PDF")).appendTo(box);
    if (row.error) $("<p class='text-danger'></p>").text(__(codes[row.error])).appendTo(box);
    if (row.result) {
     $("<p></p>").text(row.result.outcome === "duplicate" ? __("Already uploaded") : __("PDF received")).appendTo(box);
     $("<button type='button' class='btn btn-default'></button>").text(__("Open Incoming Letter")).on("click", async () => {
      const current = await rpc("get_intake_result", {request_key: row.key});
      if (current.outcome === "unknown") {row.result = null; row.error = "CANNOT_ACCEPT_FILE"; render(); return;}
      frappe.set_route("Form", "Incoming Letter", current.letter); dialog.hide();
     }).appendTo(box);
    }
    if (!row.busy && !row.result) $("<button type='button' class='btn btn-link'></button>").text(__("Discard local request")).on("click", () => {client.forget(row.key); rows = rows.filter(r => r !== row); render();}).appendTo(box);
   }
  }
  dialog.show(); render();
 }
 frappe.posteingang = {IntakeClient, open};
})();
