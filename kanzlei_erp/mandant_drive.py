"""One private Drive folder per Mandant; access follows current ERP permissions."""

import re
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import quote, urlsplit

import frappe

_creating_customer = ContextVar("kanzlei_drive_creating_customer", default=None)
_changing_storage = ContextVar("kanzlei_drive_changing_storage", default=False)
CUSTOMER_FOLDER = "kanzlei_drive_folder"
FILE_CUSTOMER = "kanzlei_drive_customer"
ACCESS_TYPES = ("read", "write", "upload", "comment", "share")


def _staff(user):
	return user != "Guest" and frappe.db.get_value("User", user, "user_type") == "System User"


def _require_customer(customer, ptype="read", *, for_update=False):
	if not _staff(frappe.session.user):
		frappe.throw(frappe._("Please log in as a staff user"), frappe.PermissionError)
	doc = frappe.get_doc("Customer", customer, for_update=for_update)
	doc.check_permission(ptype)
	return doc


def _folder_result(customer):
	folder = customer.get(CUSTOMER_FOLDER)
	if folder:
		file = frappe.db.get_value("File", folder, ["is_folder", "status", FILE_CUSTOMER], as_dict=True)
		if not file or not file.is_folder or file.status != "Active" or file.get(FILE_CUSTOMER) != customer.name:
			frappe.throw(frappe._("The Mandant folder is unavailable. Contact an administrator."), frappe.ValidationError)
	return {
		"enabled": True,
		"folder": folder or None,
		"url": f"/drive/d/{quote(folder, safe='')}" if folder else None,
		"can_create": not folder and bool(frappe.has_permission("Customer", "write", doc=customer)),
	}


@frappe.whitelist()
def get_folder(customer):
	doc = _require_customer(customer)
	if "suite" not in frappe.get_installed_apps():
		return {"enabled": False}
	return _folder_result(doc)


def _customer_url(customer):
	return f"/desk/customer/{quote(customer, safe='')}"


def _valid_root(file, customer):
	return bool(file and file.is_folder and file.status == "Active" and file.get(FILE_CUSTOMER) == customer)


@frappe.whitelist()
def list_mandanten(search="", start=0, page_length=50):
	"""List permitted Customers; pagination offsets count only readable documents."""
	user = frappe.session.user
	if not _staff(user):
		frappe.throw(frappe._("Please log in as a staff user"), frappe.PermissionError)
	empty = {"rows": [], "has_next": False, "next_start": None}
	if "suite" not in frappe.get_installed_apps():
		return empty
	start = max(0, frappe.utils.cint(start))
	page_length = min(100, max(1, frappe.utils.cint(page_length)))
	search = str(search or "").strip()
	query = {"or_filters": [["customer_name", "like", f"%{search}%"], ["name", "like", f"%{search}%"]]} if search else {}
	readable, offset, skipped = [], 0, 0
	while len(readable) <= page_length:
		try:
			candidates = frappe.get_list("Customer", fields=["name"],
				order_by="customer_name asc, name asc", start=offset, limit=100, **query)
		except frappe.PermissionError:
			return empty
		for customer in candidates:
			if not frappe.has_permission("Customer", "read", doc=customer.name, user=user):
				continue
			if skipped < start:
				skipped += 1
				continue
			readable.append(customer)
			if len(readable) > page_length:
				break
		if len(candidates) < 100:
			break
		offset += len(candidates)
	has_next = len(readable) > page_length
	# A DocShare-only reader may have no role-level field permissions. Read the
	# display/link fields only for IDs whose document permission passed above.
	names = [customer.name for customer in readable[:page_length]]
	by_name = {doc.name: doc for doc in frappe.get_all("Customer", filters={"name": ["in", names]},
		fields=["name", "customer_name", CUSTOMER_FOLDER])} if names else {}
	customers = [by_name[name] for name in names if name in by_name]
	folders = [customer.get(CUSTOMER_FOLDER) for customer in customers if customer.get(CUSTOMER_FOLDER)]
	files = {row.name: row for row in frappe.get_all("File", filters={"name": ["in", folders]},
		fields=["name", "is_folder", "status", FILE_CUSTOMER])} if folders else {}
	rows = []
	for customer in customers:
		folder = customer.get(CUSTOMER_FOLDER)
		state = "missing" if not folder else "ready" if _valid_root(files.get(folder), customer.name) else "unavailable"
		rows.append({"customer": customer.name, "customer_name": customer.customer_name,
			"customer_url": _customer_url(customer.name), "state": state,
			"folder": folder if state == "ready" else None,
			"url": f"/drive/d/{quote(folder, safe='')}" if state == "ready" else None,
			"can_create": state == "missing" and bool(frappe.has_permission("Customer", "write", doc=customer.name, user=user))})
	return {"rows": rows, "has_next": has_next, "next_start": start + len(rows) if has_next else None}


def entity_navigation(entity, user):
	"""Return persisted Mandant context only after current staff/Customer checks."""
	if not _staff(user) or not frappe.get_meta("File").has_field(FILE_CUSTOMER):
		return None
	name = entity if isinstance(entity, str) else entity.get("name")
	customer = frappe.db.get_value("File", name, FILE_CUSTOMER) if name else None
	if not customer or not frappe.db.exists("Customer", customer) or not frappe.has_permission("Customer", "read", doc=customer, user=user):
		return None
	doc = frappe.db.get_value("Customer", customer, ["customer_name", CUSTOMER_FOLDER], as_dict=True)
	folder = doc.get(CUSTOMER_FOLDER)
	file = frappe.db.get_value("File", folder, ["is_folder", "status", FILE_CUSTOMER], as_dict=True) if folder else None
	if not _valid_root(file, customer):
		return None
	return {"customer": customer, "customer_name": doc.customer_name, "folder": folder, "customer_url": _customer_url(customer)}


def _require_uploaded_content(parent):
	if parent and frappe.get_meta("File").has_field(FILE_CUSTOMER) and frappe.db.get_value("File", parent, FILE_CUSTOMER):
		_reject_native_content()


def _reject_native_content():
	frappe.throw(frappe._("Upload files to Mandant folders. Create Suite documents outside these folders."), frappe.PermissionError)


@frappe.whitelist()
def create_document(title=None, parent=None, template=None):
	from suite.writer.api.docs import create_document as create

	_require_uploaded_content(parent)
	return create(title=title, parent=parent, template=template)


@frappe.whitelist()
def create_sheet(title="", parent=""):
	from suite.sheets.api import create_sheet as create

	_require_uploaded_content(parent)
	return create(title=title, parent=parent)


@frappe.whitelist()
def create_presentation(template=None, duplicate_from=None, parent=None):
	from suite.slides.doctype.presentation.presentation import create_presentation as create

	_require_uploaded_content(parent)
	return create(template=template, duplicate_from=duplicate_from, parent=parent)


@frappe.whitelist(methods=["POST"])
def create_folder(customer):
	if "suite" not in frappe.get_installed_apps():
		frappe.throw(frappe._("Drive is not installed"), frappe.ValidationError)

	from suite.drive.utils import create_drive_file, get_new_file_name, get_user_folder
	from suite.drive.utils.files import FileManager

	# MariaDB snapshot isolation may reject a waiting lock after another
	# creator commits. Retry only this read, before any storage mutation.
	for attempt in range(3):
		try:
			doc = _require_customer(customer, "write", for_update=True)
			break
		except frappe.QueryDeadlockError:
			if attempt == 2:
				raise
			frappe.db.rollback()
	if doc.get(CUSTOMER_FOLDER):
		return _folder_result(doc)
	parent = get_user_folder("Administrator")
	# The storage path is keyed by File ID; the display name need not track later Customer renames.
	title = re.sub(r"[\x00-\x1f/\\]", "_", f"Mandant {doc.name} — {doc.customer_name}")[:140]
	title = get_new_file_name(title, parent.name, "Folder")
	manager = FileManager()
	token = _creating_customer.set(doc.name)
	try:
		folder = create_drive_file(title, parent.name, "Folder", manager.create_folder, owner="Administrator")
	finally:
		_creating_customer.reset(token)
	doc.db_set(CUSTOMER_FOLDER, folder.name, update_modified=False)
	return _folder_result(doc)


def entity_access(entity, user):
	"""Suite access provider: None delegates ordinary files to Suite unchanged."""
	if not frappe.get_meta("File").has_field(FILE_CUSTOMER):
		return None
	name = entity if isinstance(entity, str) else entity.get("name")
	from kanzlei_erp.drive_storage import is_alias
	row = frappe.db.get_value("File", name, ["name", "file_url", "is_folder"], as_dict=True) if name else None
	if row and is_alias(row):
		return {**dict.fromkeys(ACCESS_TYPES, 0), "type": "guest"}
	# Read the persisted marker, not client input or a possibly stale cached File.
	customer = frappe.db.get_value("File", name, FILE_CUSTOMER) if name else None
	if not customer:
		return None
	return _customer_access(customer, user)


def entity_access_batch(entities, user):
	"""Read persisted markers once per WebDAV listing, never from client rows."""
	if not entities or not frappe.get_meta("File").has_field(FILE_CUSTOMER):
		return {}
	names = [entity if isinstance(entity, str) else entity.get("name") for entity in entities]
	from kanzlei_erp.drive_storage import _index, is_alias
	rows = frappe.get_all("File", filters={"name": ["in", names]}, fields=["name", "file_url", FILE_CUSTOMER])
	index = _index()
	customers = {row.get(FILE_CUSTOMER) for row in rows if row.get(FILE_CUSTOMER)}
	access = {customer: _customer_access(customer, user) for customer in customers}
	result = {}
	for row in rows:
		if is_alias(row, index=index):
			result[row.name] = {**dict.fromkeys(ACCESS_TYPES, 0), "type": "guest"}
		elif row.get(FILE_CUSTOMER):
			result[row.name] = access[row.get(FILE_CUSTOMER)]
	return result


def _customer_access(customer, user):
	access = dict.fromkeys(ACCESS_TYPES, 0)
	if _staff(user) and frappe.db.exists("Customer", customer):
		read = bool(frappe.has_permission("Customer", "read", doc=customer, user=user))
		write = read and bool(frappe.has_permission("Customer", "write", doc=customer, user=user))
		access.update(read=int(read), write=int(write), upload=int(write), comment=int(write))
	# Sharing is exclusively controlled by Customer permissions, even for a Drive administrator.
	return {**access, "type": "user" if access["write"] else "guest"}


def file_permission_criterion(user, table, criterion):
	"""Restrict ORM File lists, including rows owned by a now-revoked uploader."""
	if not frappe.get_meta("File").has_field(FILE_CUSTOMER):
		return criterion
	file = table or frappe.qb.DocType("File")
	from kanzlei_erp.drive_storage import alias_names
	aliases = alias_names()
	unmanaged = file[FILE_CUSTOMER].isnull() | (file[FILE_CUSTOMER] == "")
	if aliases:
		unmanaged &= file.name.notin(aliases)
	if criterion is not None:
		unmanaged &= criterion
	if not _staff(user):
		return unmanaged
	# get_list applies role permissions, User Permissions and Customer DocShares.
	customers = _readable_customers(user)
	return unmanaged | file[FILE_CUSTOMER].isin(customers) if customers else unmanaged


def _readable_customers(user):
	if not _staff(user) or not frappe.has_permission("Customer", "read", user=user):
		return []
	return [name for name in frappe.get_list("Customer", user=user, pluck="name", limit=0)
		if frappe.has_permission("Customer", "read", doc=name, user=user)]


def file_access_override(doc, ptype, user, debug=False):
	access = entity_access(doc, user)
	if access is None:
		return None
	permission = "read" if ptype in ("read", "select", "print", "export") else "write" if ptype in ("write", "delete", "create") else ptype
	return bool(access.get(permission))


def file_query_condition_override(user, condition):
	if not frappe.get_meta("File").has_field(FILE_CUSTOMER):
		return condition
	file = frappe.qb.DocType("File")
	from kanzlei_erp.drive_storage import alias_names
	aliases = alias_names()
	unmanaged = file[FILE_CUSTOMER].isnull() | (file[FILE_CUSTOMER] == "")
	if aliases:
		unmanaged &= file.name.notin(aliases)
	quote_char = '"' if frappe.conf.db_type == "postgres" else "`"
	base = f"({condition or '1=1'}) AND ({unmanaged.get_sql(quote_char=quote_char, with_namespace=True)})"
	customers = _readable_customers(user)
	if customers:
		managed = file[FILE_CUSTOMER].isin(customers).get_sql(quote_char=quote_char, with_namespace=True)
		return f"(({base}) OR ({managed}))"
	return f"({base})"


@contextmanager
def storage_mutation(doc, target=None):
	from kanzlei_erp.suite_file import require_drive_material
	from kanzlei_erp.drive_storage import binding_change

	require_drive_material(doc)
	token = _changing_storage.set(doc.name)
	try:
		with binding_change(doc, target):
			yield
	finally:
		_changing_storage.reset(token)


def validate_customer(doc, method=None):
	old = doc.get_doc_before_save()
	if (doc.get(CUSTOMER_FOLDER) and doc.is_new()) or (old and doc.get(CUSTOMER_FOLDER) != old.get(CUSTOMER_FOLDER)):
		frappe.throw(frappe._("Manage the Mandant folder through its Drive action"), frappe.PermissionError)


def prepare_file(doc, method=None):
	"""Only new children inherit a marker; ordinary saves cannot forge or clear it."""
	if not frappe.get_meta("File").has_field(FILE_CUSTOMER):
		return
	old = doc.get_doc_before_save()
	parent_customer = frappe.db.get_value("File", doc.folder, FILE_CUSTOMER) if doc.folder else None
	if doc.is_new():
		if doc.get(FILE_CUSTOMER):
			frappe.throw(frappe._("Manage the Mandant folder through its Drive action"), frappe.PermissionError)
		customer = parent_customer or _creating_customer.get()
		if customer:
			if doc.get("content_doctype") or doc.get("content_docname"):
				_reject_native_content()
			if parent_customer and not frappe.has_permission("Customer", "write", doc=customer):
				frappe.throw(frappe._("No write access to this Mandant"), frappe.PermissionError)
			doc.set(FILE_CUSTOMER, customer)
			doc.is_private = 1
			if doc.file_url:
				_validate_initial_storage_key(doc)
	elif old:
		if doc.get(FILE_CUSTOMER) != old.get(FILE_CUSTOMER):
			frappe.throw(frappe._("Manage the Mandant folder through its Drive action"), frappe.PermissionError)
		if old.get(FILE_CUSTOMER):
			if doc.get("content_doctype") or doc.get("content_docname"):
				_reject_native_content()
			if (doc.file_type == "Link") != (old.file_type == "Link"):
				frappe.throw(frappe._("Manage Mandant files through Drive"), frappe.PermissionError)
			if doc.is_folder != old.is_folder:
				frappe.throw(frappe._("Manage Mandant files through Drive"), frappe.PermissionError)
			if is_root(old) and any(doc.get(field) != old.get(field) for field in ("folder", "status", "file_name", "file_url")):
				protect_root(old)
			if not _changing_storage.get():
				if any(doc.get(field) != old.get(field) for field in ("folder", "status", "file_name")):
					frappe.throw(frappe._("Manage Mandant files through Drive"), frappe.PermissionError)
				if doc.file_url != old.file_url:
					if old.file_url:
						frappe.throw(frappe._("Manage Mandant files through Drive"), frappe.PermissionError)
					_validate_initial_storage_key(doc)
		if doc.folder != old.folder:
			validate_move(old, doc.folder)
	if doc.get(FILE_CUSTOMER) and not doc.is_private:
		frappe.throw(frappe._("Mandant Drive files must remain private"), frappe.PermissionError)
	if doc.get(FILE_CUSTOMER) and doc.file_type == "Link":
		_validate_link(doc)


def _validate_initial_storage_key(doc):
	from suite.drive.utils.files import FileManager, storage_key

	if doc.file_type == "Link":
		_validate_link(doc)
		return
	if str(storage_key(doc.file_url)).rstrip("/") != str(FileManager().get_disk_path(doc)).rstrip("/"):
		frappe.throw(frappe._("Manage Mandant files through Drive"), frappe.PermissionError)


def _validate_link(doc):
	url = urlsplit(doc.file_url or "")
	if url.scheme not in ("http", "https") or not url.hostname:
		frappe.throw(frappe._("Mandant Drive links must use HTTP or HTTPS"), frappe.PermissionError)


def is_root(doc):
	return bool(doc.get(FILE_CUSTOMER) and frappe.db.exists("Customer", {"name": doc.get(FILE_CUSTOMER), CUSTOMER_FOLDER: doc.name}))


def protect_root(doc, method=None):
	if is_root(doc):
		frappe.throw(frappe._("The Mandant root folder cannot be moved or deleted"), frappe.PermissionError)


def validate_move(doc, new_parent):
	if not frappe.get_meta("File").has_field(FILE_CUSTOMER):
		return
	protect_root(doc)
	parent_customer = frappe.db.get_value("File", new_parent, FILE_CUSTOMER) if new_parent else None
	if (doc.get(FILE_CUSTOMER) or None) != (parent_customer or None):
		frappe.throw(frappe._("Files can only be moved within the same Mandant"), frappe.PermissionError)


def validate_share(doc, method=None):
	if frappe.get_meta("File").has_field(FILE_CUSTOMER) and frappe.db.get_value("File", doc.entity, FILE_CUSTOMER):
		frappe.throw(frappe._("Drive access is managed through Mandant permissions"), frappe.PermissionError)
