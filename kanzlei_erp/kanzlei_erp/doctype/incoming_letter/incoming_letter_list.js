frappe.listview_settings["Incoming Letter"] = {
 onload(listview) {
  if (frappe.user.has_role("Posteingang Operator")) {
   listview.page.add_inner_button(__("Upload PDF"), () => frappe.posteingang.open());
  }
 }
};
