"""Staff portal: existing application routes and permission-checked personal links."""

import json
from urllib.parse import quote

import frappe
from frappe.utils import get_datetime, now_datetime

HISTORY_KEY = "kanzlei_portal_history"
PINS_KEY = "kanzlei_portal_pins"
HISTORY_LIMIT = 30
PIN_LIMIT = 12
RECORD_TYPES = {"Customer", "Task", "FiBu Package", "FiBu Supplement", "File"}


def require_staff(expected_user=None):
	user = frappe.session.user
	if (
		user == "Guest"
		or (expected_user is not None and expected_user != user)
		or frappe.db.get_value("User", user, ["user_type", "enabled"]) != ("System User", 1)
	):
		frappe.throw(frappe._("Please log in as a staff user"), frappe.PermissionError)
	return user


def _drive_enabled():
	return "suite" in frappe.get_installed_apps()


def _validate_reference(doctype, name):
	if not isinstance(doctype, str) or doctype not in RECORD_TYPES or not isinstance(name, str) or not name or len(name) > 140:
		frappe.throw(frappe._("Invalid portal link."), frappe.ValidationError)


def _preferences(key, limit):
	# Read only this user's row; global/default inherited values are not personal state.
	raw = frappe.db.get_value("DefaultValue", {"parent": frappe.session.user, "defkey": key}, "defvalue")
	try:
		value = json.loads(raw or "[]")
	except (ValueError, TypeError):
		return []
	if not isinstance(value, list):
		return []
	return [row for row in value[:limit] if isinstance(row, dict)
		and isinstance(row.get("doctype"), str) and row["doctype"] in RECORD_TYPES
		and isinstance(row.get("name"), str) and 0 < len(row["name"]) <= 140]


def _save_preferences(key, rows):
	frappe.defaults.set_user_default(key, frappe.as_json(rows), user=frappe.session.user)


def _lock_preferences():
	# Both visit and pin mutations serialize per user, including first-time preferences.
	frappe.db.get_value("User", frappe.session.user, "name", for_update=True)


def _customer_label(name):
	if name and frappe.has_permission("Customer", "read", doc=name):
		return frappe.db.get_value("Customer", name, "customer_name")
	return None


def _active_drive_path(doc):
	seen = set()
	while doc:
		if doc.name in seen or len(seen) >= 64 or doc.status != "Active":
			return False
		seen.add(doc.name)
		if not doc.folder:
			return True
		doc = frappe.db.get_value("File", doc.folder, ["name", "folder", "status", "is_folder"], as_dict=True)
		if not doc or not doc.is_folder:
			return False
	return False


def _resolve(doctype, name):
	_validate_reference(doctype, name)
	if doctype == "File" and not _drive_enabled():
		return None
	try:
		doc = frappe.get_doc(doctype, name)
		if doctype == "File":
			from suite.drive.api.permissions import get_user_access

			if not _active_drive_path(doc) or not get_user_access(doc).get("read"):
				return None
			label = doc.file_name
			context = _customer_label(doc.get("kanzlei_drive_customer"))
			url = f"/drive/{'d' if doc.is_folder else 'f'}/{quote(name, safe='')}"
			app, kind = "Drive", "Folder" if doc.is_folder else "File"
		else:
			if not frappe.has_permission(doctype, "read", doc=doc):
				return None
			label = doc.get("customer_name") or doc.get("subject") or doc.get("title") or doc.name
			context = _customer_label(doc.get("kanzlei_customer") or doc.get("customer"))
			url = f"/desk/{frappe.scrub(doctype).replace('_', '-')}/{quote(name, safe='')}"
			app, kind = "Kanzlei", "Mandant" if doctype == "Customer" else doctype
		return {"doctype": doctype, "name": name, "label": label, "context": context,
			"url": url, "app": app, "kind": frappe._(kind)}
	except (frappe.DoesNotExistError, frappe.PermissionError):
		return None


def _require_record(doctype, name):
	row = _resolve(doctype, name)
	if row is None:
		frappe.throw(frappe._("This portal link is no longer available."), frappe.PermissionError)
	return row


@frappe.whitelist()
def get_portal():
	user = require_staff()
	workspaces = [{"key": "erp", "label": frappe._("Kanzlei"), "app": "ERP",
		"description": frappe._("Manage clients, plan tasks and prepare accounting periods."),
		"url": "/desk/kanzlei-overview", "shortcut": {"label": frappe._("Mandanten"), "url": "/desk/customer"}}]
	shortcuts = []
	try:
		can_read_customers = frappe.has_permission("Customer", "read") or bool(frappe.get_list("Customer", fields=["name"], limit=1))
	except frappe.PermissionError:
		can_read_customers = False
	if can_read_customers:
		shortcuts.append({"label": frappe._("Mandanten"), "url": "/desk/customer", "app": "Kanzlei", "icon": "users"})
	else:
		workspaces[0]["shortcut"] = None
	if _drive_enabled():
		workspaces.append({"key": "drive", "label": frappe._("Document workspace"), "app": "DRIVE",
			"description": frappe._("Find documents, work with files and open client folders."), "url": "/drive",
			"shortcut": {"label": frappe._("Client folders"), "url": "/drive/mandanten"}})
		shortcuts.append({"label": frappe._("Client folders"), "url": "/drive/mandanten", "app": "Drive", "icon": "folder"})
	profile = frappe.db.get_value("User", user, ["full_name", "desk_theme"], as_dict=True)
	return {"user": user, "full_name": profile.full_name or user, "theme": profile.desk_theme or "Light",
		"workspaces": workspaces, "shortcuts": shortcuts,
		"settings_url": "/desk/company" if "System Manager" in frappe.get_roles() else None,
		"profile_url": f"/desk/user/{quote(user, safe='')}"}


@frappe.whitelist()
def get_personal():
	user = require_staff()
	pinned = _preferences(PINS_KEY, PIN_LIMIT)
	pin_keys = {(row["doctype"], row["name"]) for row in pinned}
	pins = [resolved for row in pinned if (resolved := _resolve(row["doctype"], row["name"]))]
	history = _preferences(HISTORY_KEY, HISTORY_LIMIT)
	if _drive_enabled():
		history += [{"doctype": "File", "name": row.entity_name, "visited": str(row.last_interaction)}
			for row in frappe.get_all("Drive Entity Log", filters={"user": user},
				fields=["entity_name", "last_interaction"], order_by="last_interaction desc, name desc", limit=HISTORY_LIMIT)]
	# Preference data can have been edited or originate in an older version.
	def visited(row):
		try:
			return get_datetime(row.get("visited") or "1970-01-01")
		except (ValueError, TypeError, OverflowError):
			return get_datetime("1970-01-01")

	recent, seen = [], set()
	for row in sorted(history, key=visited, reverse=True):
		key = (row["doctype"], row["name"])
		if key in seen:
			continue
		seen.add(key)
		if resolved := _resolve(*key):
			resolved.update(visited=str(visited(row)), pinned=key in pin_keys)
			recent.append(resolved)
			if len(recent) == 5:
				break
	return {"user": user, "recent": recent, "pins": pins}


@frappe.whitelist(methods=["POST"])
def record_visit(doctype, name, expected_user=None):
	require_staff(expected_user)
	_require_record(doctype, name)
	_lock_preferences()
	rows = [row for row in _preferences(HISTORY_KEY, HISTORY_LIMIT) if (row["doctype"], row["name"]) != (doctype, name)]
	rows.insert(0, {"doctype": doctype, "name": name, "visited": str(now_datetime())})
	_save_preferences(HISTORY_KEY, rows[:HISTORY_LIMIT])


@frappe.whitelist(methods=["POST"])
def set_pin(doctype, name, pinned, expected_user=None):
	require_staff(expected_user)
	_validate_reference(doctype, name)
	if type(pinned) not in (int, bool) or pinned not in (0, 1):
		frappe.throw(frappe._("Invalid portal pin state."), frappe.ValidationError)
	if pinned:
		_require_record(doctype, name)
	_lock_preferences()
	rows = [row for row in _preferences(PINS_KEY, PIN_LIMIT)
		if (row["doctype"], row["name"]) != (doctype, name) and _resolve(row["doctype"], row["name"])]
	if pinned:
		if len(rows) >= PIN_LIMIT:
			frappe.throw(frappe._("You can pin up to {0} links.").format(PIN_LIMIT), frappe.ValidationError)
		rows.append({"doctype": doctype, "name": name})
	_save_preferences(PINS_KEY, rows)
	return {"user": frappe.session.user}


@frappe.whitelist(methods=["POST"])
def open_record(doctype, name, expected_user=None):
	require_staff(expected_user)
	row = _require_record(doctype, name)
	record_visit(doctype, name, expected_user)
	return {"user": frappe.session.user, "url": row["url"]}
