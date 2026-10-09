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
}
app_home = "/desk/customer"
boot_session = "kanzlei_erp.navigation.configure_desk"
app_include_js = ["kanzlei_navigation.bundle.js", "mandant_documents.bundle.js", "/assets/kanzlei_erp/js/fibu_workflow.js", "fibu_questions.bundle.js", "/assets/kanzlei_erp/js/fibu_checklist.js"]
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
	"ToDo": {"validate": "kanzlei_erp.fibu_questions.validate_question_assignment"},
	"DocShare": {"validate": "kanzlei_erp.fibu_questions.validate_question_share"},
	"Customer": {"validate": "kanzlei_erp.fibu_checklist.validate_customer_sources"},
	"Task": {
		"before_validate": "kanzlei_erp.fibu_questions.prepare_question",
		"after_insert": "kanzlei_erp.fibu_questions.finish_question_creation",
		"validate": ["kanzlei_erp.fibu_task.validate_task_package_link", "kanzlei_erp.fibu_questions.validate_question", "kanzlei_erp.work_schedule.set_task_calendar_title"],
		"on_trash": ["kanzlei_erp.fibu_task.protect_closed_task", "kanzlei_erp.fibu_questions.protect_question"],
	},
	"File": {"validate": "kanzlei_erp.fibu_materials.validate_fibu_attachment", "on_trash": "kanzlei_erp.fibu_materials.validate_fibu_attachment"},
}
scheduler_events = {"daily": ["kanzlei_erp.work_schedule.generate_due_tasks", "kanzlei_erp.fibu_questions.send_question_reminders"]}
after_migrate = ["kanzlei_erp.work_schedule.backfill_task_calendar_titles"]
