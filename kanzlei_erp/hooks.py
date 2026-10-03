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
app_include_js = ["kanzlei_navigation.bundle.js"]

fixtures = [
	{"dt": "Custom Field", "filters": [["name", "like", "Task-kanzlei_%"]]},
	{"dt": "Property Setter", "filters": [["module", "=", "Kanzlei ERP"]]},
]
override_doctype_dashboards = {"Customer": "kanzlei_erp.navigation.customer_dashboard"}

doctype_calendar_js = {"Task": "public/js/task_calendar.js"}
doc_events = {"Task": {"validate": "kanzlei_erp.work_schedule.set_task_calendar_title"}}
scheduler_events = {"daily": ["kanzlei_erp.work_schedule.generate_due_tasks"]}
after_migrate = ["kanzlei_erp.work_schedule.backfill_task_calendar_titles"]
