// Called by Frappe before no-copy fields are stripped from a document.
frappe.model.document_copy_guard = function (doc) {
    if (doc.doctype === "Task" && doc.kanzlei_incoming_letter) {
        frappe.throw(__("Posteingang tasks cannot be duplicated"));
    }
};
