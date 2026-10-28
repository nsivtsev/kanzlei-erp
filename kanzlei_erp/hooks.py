app_name = "kanzlei_erp"
app_title = "Kanzlei ERP"
app_publisher = "Kanzlei ERP"
app_description = "Mandant work calendar for ERPNext"
app_email = "dev@localhost"
app_license = "mit"

# Apps
# ------------------

required_apps = ["erpnext"]
override_whitelisted_methods = {
	"erpnext.projects.doctype.project.project.set_project_status": "kanzlei_erp.fibu_questions.set_project_status",
	"suite.drive.api.files.remove_or_restore": "kanzlei_erp.suite_setup.trash_or_restore",
	"suite.writer.api.docs.create_document": "kanzlei_erp.mandant_drive.create_document",
	"suite.sheets.api.create_sheet": "kanzlei_erp.mandant_drive.create_sheet",
	"suite.slides.doctype.presentation.presentation.create_presentation": "kanzlei_erp.mandant_drive.create_presentation",
}
extend_doctype_class = {"File": ["kanzlei_erp.suite_file.ERPAttachmentGuard"]}
write_file = "kanzlei_erp.object_storage.write_file"
file_content_from_url = ["kanzlei_erp.object_storage.url_content"]
drive_s3_client = ["kanzlei_erp.object_storage.client"]
drive_s3_content = ["kanzlei_erp.object_storage.open_binding"]
drive_s3_key = ["kanzlei_erp.object_storage.resolve_key"]
drive_s3_rebind = ["kanzlei_erp.object_storage.replace_content"]
drive_s3_write_intent = ["kanzlei_erp.object_storage.track_write"]
before_request = ["kanzlei_erp.s3_http.before_request"]
after_request = ["kanzlei_erp.object_storage.cleanup_temporary_files"]
after_job = ["kanzlei_erp.object_storage.cleanup_temporary_files"]
drive_entity_access = ["kanzlei_erp.mandant_drive.entity_access"]
drive_entity_access_batch = ["kanzlei_erp.mandant_drive.entity_access_batch"]
drive_entity_actions_batch = ["kanzlei_erp.mandant_drive.entity_actions_batch"]
drive_entity_navigation = ["kanzlei_erp.mandant_drive.entity_navigation"]
drive_file_permission_criterion = ["kanzlei_erp.mandant_drive.file_permission_criterion"]
file_access_override = ["kanzlei_erp.mandant_drive.file_access_override"]
file_query_condition_override = ["kanzlei_erp.mandant_drive.file_query_condition_override"]
drive_storage_mutation = ["kanzlei_erp.mandant_drive.storage_mutation"]
drive_storage_guard = ["kanzlei_erp.drive_storage.suite_storage_guard"]
drive_file_changed = ["kanzlei_erp.drive_realtime.changed"]
file_realtime_event = ["kanzlei_erp.drive_realtime.intercept_file_event"]
file_storage_path_guard = ["kanzlei_erp.drive_storage.require_path_access"]
file_privacy_guard = ["kanzlei_erp.drive_storage.guard_privacy_change"]
drive_authorized_read = ["kanzlei_erp.drive_storage.authorized_read"]
drive_storage_creation = ["kanzlei_erp.drive_storage.creation_binding"]
file_storage_creation = ["kanzlei_erp.drive_storage.creation_binding"]
app_home = "/desk/kanzlei-overview"
home_page = "/kanzlei-portal"
setup_wizard_url = "/desk/setup-wizard"
boot_session = "kanzlei_erp.navigation.configure_desk"
app_include_js = ["kanzlei_navigation.bundle.js", "portal_history.bundle.js", "mandant_documents.bundle.js", "drive_realtime.bundle.js", "/assets/kanzlei_erp/js/fibu_workflow.js", "fibu_questions.bundle.js", "/assets/kanzlei_erp/js/fibu_checklist.js"]
has_permission = {
	"Communication": "kanzlei_erp.navigation.communication_has_permission",
	"FiBu Package": "kanzlei_erp.fibu_permissions.package_has_permission",
	"FiBu Supplement": "kanzlei_erp.fibu_permissions.supplement_has_permission",
	"Task": "kanzlei_erp.fibu_permissions.linked_task_has_permission",
}
permission_query_conditions = {
	"FiBu Package": "kanzlei_erp.fibu_permissions.package_query_conditions",
	"FiBu Supplement": "kanzlei_erp.fibu_permissions.supplement_query_conditions",
	"Task": "kanzlei_erp.fibu_permissions.task_query_conditions",
}

fixtures = [
	{"dt": "Custom Field", "filters": [["name", "like", "%-kanzlei_%"]]},
	{"dt": "Property Setter", "filters": [["module", "=", "Kanzlei ERP"]]},
]
override_doctype_dashboards = {"Customer": "kanzlei_erp.navigation.customer_dashboard"}
doctype_js = {
	"Contact": "public/js/contact.js",
	"Drive Disk Settings": "public/js/s3_storage_settings.js",
	"Customer": "public/js/customer.js",
	"Task": "public/js/task.js",
	"FiBu Package": "public/js/fibu_package.js",
	"FiBu Supplement": "public/js/fibu_supplement.js",
}

doctype_calendar_js = {"Task": "public/js/task_calendar.js"}
doc_events = {
	"Contact": {"on_update": "kanzlei_erp.email_history.contact_updated"},
	"Drive Disk Settings": {"validate": "kanzlei_erp.object_storage.validate_settings", "on_update": "kanzlei_erp.drive_storage.invalidate"},
	"S3 File Object": {"validate": "kanzlei_erp.object_storage.protect_binding", "on_trash": "kanzlei_erp.object_storage.protect_binding"},
	"ToDo": {"validate": "kanzlei_erp.fibu_questions.validate_question_assignment"},
	"DocShare": {"validate": "kanzlei_erp.fibu_questions.validate_question_share"},
	"Customer": {"validate": ["kanzlei_erp.fibu_checklist.validate_customer_sources", "kanzlei_erp.mandant_drive.validate_customer"]},
	"Drive Permission": {"validate": "kanzlei_erp.mandant_drive.validate_share"},
	"Task": {
		"before_validate": "kanzlei_erp.fibu_questions.prepare_question",
		"after_insert": "kanzlei_erp.fibu_questions.finish_question_creation",
		"validate": ["kanzlei_erp.fibu_task.validate_task_package_link", "kanzlei_erp.fibu_questions.validate_question", "kanzlei_erp.work_schedule.set_task_calendar_title"],
		"on_trash": ["kanzlei_erp.fibu_task.protect_closed_task", "kanzlei_erp.fibu_questions.protect_question"],
	},
	"File": {
		"before_validate": ["kanzlei_erp.drive_storage.validate_binding_event", "kanzlei_erp.mandant_drive.prepare_file"],
		"validate": "kanzlei_erp.fibu_materials.validate_fibu_attachment",
		"on_trash": ["kanzlei_erp.fibu_materials.validate_fibu_attachment", "kanzlei_erp.mandant_drive.protect_root"],
		"after_insert": ["kanzlei_erp.drive_realtime.changed", "kanzlei_erp.object_storage.mark_mutation"],
		"on_update": ["kanzlei_erp.drive_realtime.changed", "kanzlei_erp.object_storage.mark_mutation"],
		"after_delete": ["kanzlei_erp.drive_realtime.changed", "kanzlei_erp.object_storage.mark_mutation"],
	},
}
scheduler_events = {"daily": ["kanzlei_erp.work_schedule.generate_due_tasks", "kanzlei_erp.fibu_questions.send_question_reminders"], "hourly": ["kanzlei_erp.object_storage.retry_deletions"]}
scheduler_events["cron"] = {"* * * * *": ["kanzlei_erp.posteingang_jobs.recover_runs"]}
after_migrate = ["kanzlei_erp.work_schedule.backfill_task_calendar_titles", "kanzlei_erp.suite_setup.configure"]

# Posteingang structures are additive; no user role assignments.
before_install = ["kanzlei_erp.posteingang_setup.ensure_role"]
before_migrate = ["kanzlei_erp.posteingang_setup.ensure_role"]
after_install = ["kanzlei_erp.posteingang_setup.configure"]
after_migrate += ["kanzlei_erp.posteingang_setup.configure"]

for _doctype in ("Incoming Letter", "Incoming Letter Run", "Incoming Letter Event", "Incoming Letter Action"):
	has_permission[_doctype] = "kanzlei_erp.posteingang_permissions.has_permission"
permission_query_conditions.update({
	"Incoming Letter": "kanzlei_erp.posteingang_permissions.letter_query",
	"Incoming Letter Run": "kanzlei_erp.posteingang_permissions.run_query",
	"Incoming Letter Event": "kanzlei_erp.posteingang_permissions.event_query",
})
file_access_override.insert(0, "kanzlei_erp.posteingang_permissions.file_access_override")
file_query_condition_override.append("kanzlei_erp.posteingang_permissions.file_query_condition_override")
drive_entity_access.insert(0, "kanzlei_erp.posteingang_permissions.drive_access")
drive_entity_access_batch.insert(0, "kanzlei_erp.posteingang_permissions.drive_access_batch")
drive_file_permission_criterion.append("kanzlei_erp.posteingang_permissions.drive_criterion")
before_request.insert(0, "kanzlei_erp.posteingang_files.before_request")
doc_events["File"]["before_validate"].insert(0, "kanzlei_erp.posteingang_files.validate_binding")
doc_events["File"]["on_trash"].insert(0, "kanzlei_erp.posteingang_files.validate_binding")

required_document_permission = ["kanzlei_erp.posteingang_permissions.required_permission"]
required_permission_query_conditions = ["kanzlei_erp.posteingang_permissions.required_query"]
drive_presign_guard = ["kanzlei_erp.posteingang_files.presign_guard"]
drive_token_guard = ["kanzlei_erp.posteingang_files.token_guard"]
drive_file_response = ["kanzlei_erp.posteingang_files.suite_response"]
drive_archive_guard = ["kanzlei_erp.posteingang_files.reject_archive"]

has_permission["Posteingang Settings"] = "kanzlei_erp.posteingang_permissions.settings_permission"

# Intake receipts are server-only, including mandatory DocShare guards.
has_permission["Incoming Letter Intake Receipt"] = "kanzlei_erp.posteingang_permissions.receipt_permission"
permission_query_conditions["Incoming Letter Intake Receipt"] = "kanzlei_erp.posteingang_permissions.receipt_query"
after_request.append("kanzlei_erp.posteingang.release_request_locks")

app_include_js.append("posteingang_intake.bundle.js")
app_include_js.append("posteingang_jobs.bundle.js")
app_include_js.append("posteingang_matching.bundle.js")
doc_events["Customer"]["validate"].append("kanzlei_erp.mandant_identifiers.validate_customer")
required_permission_query_conditions.append("kanzlei_erp.mandant_identifiers.required_query")
required_child_export_permission = ["kanzlei_erp.mandant_identifiers.child_export_permission"]

required_administrator_permission = ["kanzlei_erp.posteingang_permissions.receipt_administrator_permission"]

# Mandatory AP-07 guards compose with existing FiBu/attachment restrictions.
extend_doctype_class["Task"] = ["kanzlei_erp.posteingang_task_access.TaskGuard"]
extend_doctype_class["File"].append("kanzlei_erp.posteingang_task_access.TaskFileGuard")
extend_doctype_class["ToDo"] = ["kanzlei_erp.posteingang_task_access.ToDoGuard"]
file_access_override.insert(0, "kanzlei_erp.posteingang_task_access.file_access_override")
required_document_permission.append("kanzlei_erp.posteingang_task_access.required_permission")
required_permission_query_conditions.append("kanzlei_erp.posteingang_task_access.required_query")
required_child_export_permission.append("kanzlei_erp.posteingang_task_access.child_export_permission")
document_copy_guard = ["kanzlei_erp.posteingang_task_access.copy_guard"]
assignment_notification_override = ["kanzlei_erp.posteingang_task_access.assignment_notification"]
notification_delivery_guard = ["kanzlei_erp.posteingang_task_access.notification_delivery"]
assignment_read_guard = ["kanzlei_erp.posteingang_task_access.assignment_read"]
doc_events["Task"]["validate"].append("kanzlei_erp.posteingang_task_access.validate_task")
doc_events["Task"]["on_trash"].append("kanzlei_erp.posteingang_task_access.protect_task")
doc_events["ToDo"]["on_update"] = "kanzlei_erp.posteingang_task_access.update_planning"
doc_events["ToDo"]["after_insert"] = "kanzlei_erp.posteingang_task_access.update_planning"
doc_events["ToDo"]["after_delete"] = "kanzlei_erp.posteingang_task_access.update_planning"
doc_events["ToDo"]["on_trash"] = "kanzlei_erp.posteingang_task_access.validate_related"
doc_events["ToDo"]["validate"] = [doc_events["ToDo"]["validate"], "kanzlei_erp.posteingang_task_access.validate_todo"]
for _related in ("File", "Comment", "Communication", "Version", "DocShare"):
    _existing_related = doc_events.setdefault(_related, {}).get("validate", [])
    if isinstance(_existing_related, str):
        _existing_related = [_existing_related]
    doc_events[_related]["validate"] = [*_existing_related, "kanzlei_erp.posteingang_task_access.validate_related"]
override_whitelisted_methods.update({
    "erpnext.projects.doctype.task.task.check_if_child_exists": "kanzlei_erp.posteingang_task_access.check_if_child_exists",
    "erpnext.projects.doctype.task.task.make_timesheet": "kanzlei_erp.posteingang_task_access.make_timesheet",
    "frappe.desk.doctype.notification_log.notification_log.get_notification_logs": "kanzlei_erp.posteingang_task_access.get_notification_logs",
})
app_include_js.append("posteingang_review.bundle.js")
app_include_js.append("/assets/kanzlei_erp/js/posteingang_task_guard.js")
