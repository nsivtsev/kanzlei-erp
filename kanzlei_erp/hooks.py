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
home_page = app_home
setup_wizard_url = "/desk/setup-wizard"
boot_session = "kanzlei_erp.navigation.configure_desk"
app_include_js = ["kanzlei_navigation.bundle.js", "mandant_documents.bundle.js", "drive_realtime.bundle.js", "/assets/kanzlei_erp/js/fibu_workflow.js", "fibu_questions.bundle.js", "/assets/kanzlei_erp/js/fibu_checklist.js"]
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
	"Customer": "public/js/customer.js",
	"Task": "public/js/task.js",
	"FiBu Package": "public/js/fibu_package.js",
	"FiBu Supplement": "public/js/fibu_supplement.js",
}

doctype_calendar_js = {"Task": "public/js/task_calendar.js"}
doc_events = {
	"Drive Disk Settings": {"on_update": "kanzlei_erp.drive_storage.invalidate"},
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
		"after_insert": "kanzlei_erp.drive_realtime.changed",
		"on_update": "kanzlei_erp.drive_realtime.changed",
		"after_delete": "kanzlei_erp.drive_realtime.changed",
	},
}
scheduler_events = {"daily": ["kanzlei_erp.work_schedule.generate_due_tasks", "kanzlei_erp.fibu_questions.send_question_reminders"]}
after_migrate = ["kanzlei_erp.work_schedule.backfill_task_calendar_titles", "kanzlei_erp.suite_setup.configure"]
