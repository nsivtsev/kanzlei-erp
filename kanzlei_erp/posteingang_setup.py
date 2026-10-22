"""Additive Posteingang installation. Never assigns user roles or starts jobs."""

import frappe

DEFAULTS = {
	"enabled": 0,
	"external_analysis_enabled": 0,
	"provider": "fake",
	"max_file_bytes": 20971520,
	"max_pages": 50,
	"max_actions": 30,
	"max_retries": 2,
	"request_timeout_seconds": 120,
	"auto_analysis_enabled": 0,
	"retry_backoff_seconds": 60,
	"recovery_interval_seconds": 60,
	"recovery_batch_size": 100,
	"worker_timeout_seconds": 600,
	"analysis_configuration_epoch": 1,
	"recovery_cursor": "",
}


def ensure_role():
	if not frappe.db.exists("Role", "Posteingang Operator"):
		frappe.get_doc({"doctype": "Role", "role_name": "Posteingang Operator", "desk_access": 1}).insert(
			ignore_permissions=True
		)


def configure():
	ensure_role()
	if not frappe.db.exists("DocType", "Incoming Letter"):
		return
	for field, value in DEFAULTS.items():
		if frappe.db.get_single_value("Posteingang Settings", field) is None:
			frappe.db.set_single_value("Posteingang Settings", field, value)
	frappe.db.add_index("Incoming Letter", ["status", "received_at"], "ap02_queue")
	frappe.db.add_unique("Incoming Letter Run", ["letter", "generation"], "ap02_letter_generation")
	frappe.db.add_index("Incoming Letter Run", ["status", "next_attempt_at"], "ap05_due")
	frappe.db.add_index("Incoming Letter Run", ["status", "lease_expires_at"], "ap05_lease")
	frappe.db.add_unique("Incoming Letter Action", ["parent", "action_id"], "ap02_parent_action")

	# Frappe Int defaults to NOT NULL/0. A manual action needs SQL NULL to
	# distinguish it from proposal zero. Schema sync skips Int nullability;
	# preserve this explicitly and cover it in repeated-migration acceptance.
	column = frappe.db.sql("SHOW COLUMNS FROM `tabIncoming Letter Action` LIKE 'source_index'", as_dict=True)[0]
	if column.Null != "YES":
		frappe.db.sql_ddl("ALTER TABLE `tabIncoming Letter Action` MODIFY `source_index` int NULL DEFAULT NULL")
