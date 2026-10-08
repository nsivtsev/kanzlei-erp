app_name = "kanzlei_erp"
app_title = "Kanzlei ERP"
app_publisher = "Kanzlei ERP"
app_description = "Mandant work calendar for ERPNext"
app_email = "dev@localhost"
app_license = "mit"

# Apps
# ------------------

required_apps = ["erpnext"]
app_home = "/desk/customer"
boot_session = "kanzlei_erp.navigation.configure_desk"
app_include_js = ["kanzlei_navigation.bundle.js", "mandant_documents.bundle.js", "/assets/kanzlei_erp/js/fibu_workflow.js", "/assets/kanzlei_erp/js/fibu_checklist.js"]
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
	"Customer": {"validate": "kanzlei_erp.fibu_checklist.validate_customer_sources"},
	"Task": {
		"validate": ["kanzlei_erp.fibu_task.validate_task_package_link", "kanzlei_erp.work_schedule.set_task_calendar_title"],
		"on_trash": "kanzlei_erp.fibu_task.protect_closed_task",
	},
	"File": {"validate": "kanzlei_erp.fibu_materials.validate_fibu_attachment", "on_trash": "kanzlei_erp.fibu_materials.validate_fibu_attachment"},
}
scheduler_events = {"daily": ["kanzlei_erp.work_schedule.generate_due_tasks"]}
after_migrate = ["kanzlei_erp.work_schedule.backfill_task_calendar_titles"]
