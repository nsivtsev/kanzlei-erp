frappe.ui.form.on("Incoming Letter", {
 refresh(frm) {
  frappe.posteingang.attach_analysis(frm);
  frappe.posteingang.attach_matching(frm);
  if (!frm.is_new() && frappe.user.has_role("Posteingang Operator")) {
   frm.add_custom_button(__("Upload PDF"), () => frappe.posteingang.open());
  }
 }
});
