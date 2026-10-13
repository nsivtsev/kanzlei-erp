"""Bind permissions to stored objects as well as File rows.

No client flags grant authority here. A managed blob (including its sidecars)
can only be used by its persisted File, under current Customer permissions.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import unquote, urlsplit

import frappe

MARKER = "kanzlei_drive_customer"
FIELDS = ["name", "file_url", "folder", "is_folder", "file_type", "status", MARKER]
_bindings = ContextVar("kanzlei_storage_bindings", default=())
_deleted = ContextVar("kanzlei_deleted_storage", default=None)
_reader = ContextVar("kanzlei_storage_reader", default=None)
_creations = ContextVar("kanzlei_storage_creation", default=())
_io_owner = ContextVar("kanzlei_storage_io_owner", default=None)


def _deny():
	frappe.throw(frappe._("This File cannot access the stored object."), frappe.PermissionError)


def identity(url, settings=None):
	"""Match backend semantics; never normalize S3 keys as filesystem paths."""
	from suite.drive.utils.files import S3_URL_PREFIX, storage_key

	value = str(url or "")
	if not value:
		return None
	if value.startswith(S3_URL_PREFIX):
		key = storage_key(value)
		if not key.startswith("private/files/"):
			return ("s3", key)
		value = key
	elif value.startswith(("http://", "https://")):
		parsed = urlsplit(value)
		if parsed.netloc != urlsplit(frappe.utils.get_url()).netloc:
			return None
		value = unquote(parsed.path)
		if not value.startswith(("/private/files/", "/files/")):
			return None
	settings = settings or frappe.get_single("Drive Disk Settings")
	key = storage_key(value)
	if settings.enabled and not key.startswith(("private/files/", "files/", "public/files/")):
		return ("s3", key)
	if key.startswith("files/"):
		key = "public/" + key
	return ("disk", str((Path(frappe.get_site_path()).resolve() / key).resolve()))


def _under(child, parent):
	return child and parent and child[0] == parent[0] and (
		child[1] == parent[1] or child[1].startswith(parent[1].rstrip("/") + "/")
	)


def _managed():
	if "suite" not in frappe.get_installed_apps() or not frappe.get_meta("File").has_field(MARKER):
		return []
	return frappe.get_all("File", filters={MARKER: ["is", "set"]}, fields=FIELDS)


def _sidecar_paths(row, manager):
	from suite.drive.utils.files import get_s3_key, get_s3_url, storage_key
	# The resolver must not call Suite's lazy get_root_folder(), which writes
	# Files and re-enters this guard on fresh installations.
	root_url = frappe.db.get_value("File", "Drive", "file_url")
	if not root_url:
		prefix = manager.settings.root_folder or ""
		root_url = get_s3_url(prefix) if manager.s3_enabled else "/private/files/" + prefix
	root_key = storage_key(get_s3_key(root_url) if manager.s3_enabled else root_url)
	return (Path(root_key) / ".trash" / row.name,
		Path(root_key) / manager.settings.thumbnail_prefix / (row.name + ".thumbnail"))


def _index(rows=None):
	if rows is None and hasattr(frappe.local, "kanzlei_storage_index"):
		return frappe.local.kanzlei_storage_index
	from suite.drive.utils.files import FileManager
	manager = FileManager()
	cache = rows is None
	rows = rows if rows is not None else _managed()
	objects = []
	for row in rows:
		trash, thumbnail = _sidecar_paths(row, manager)
		physical = trash if row.status == "Trashed" and not manager.flat else row.file_url
		objects.append((row, identity(physical, manager.settings),
			[identity(trash, manager.settings), identity(thumbnail, manager.settings)]))
	index = (manager, objects)
	if cache:
		frappe.local.kanzlei_storage_index = index
		frappe.db.after_rollback.add(invalidate)
	return index


def invalidate(doc=None, method=None):
	for key in ("kanzlei_storage_index", "kanzlei_storage_aliases", "kanzlei_storage_sources"):
		if hasattr(frappe.local, key):
			delattr(frappe.local, key)


def conflicts(file, rows=None, target=None, recursive=False, index=None):
	"""Only same-customer directory containment is legal; exact aliases never are."""
	url = file.get("file_url") if target is None else target
	manager, objects = index or _index(rows)
	name = file.get("name")
	if callable(getattr(file, "is_new", None)) and file.is_new():
		name = None  # insert has not assigned its server ID yet
	key = identity(url, manager.settings)
	# A trashed hierarchical File remembers its former URL for restore. Those
	# bytes now belong to the trash object; a new Active sibling may reuse the URL.
	if target is None:
		for row, physical, _ in objects:
			if row.name == name and row.status == "Trashed" and identity(url, manager.settings) == identity(row.file_url, manager.settings):
				key = physical
	if not key:
		return []
	persisted_customer = next((row.get(MARKER) for row, _, _ in objects if row.name == name), None)
	deleted = _deleted.get()
	if deleted and deleted.name == name and identity(url) in deleted.storage_keys:
		persisted_customer = deleted.customer
	result = []
	for row, row_key, sidecars in objects:
		if row.name == name:
			continue
		exact = key == row_key or key in sidecars
		contained = row.is_folder and not manager.flat and _under(key, row_key)
		contains_protected = recursive and persisted_customer != row.get(MARKER) and any(
			_under(other, key) for other in [row_key, *sidecars])
		if exact or contains_protected or (contained and persisted_customer != row.get(MARKER)):
			result.append(row)
	return result


def is_alias(file, rows=None, index=None):
	if "suite" not in frappe.get_installed_apps():
		return False
	return bool(conflicts(file, rows=rows, index=index))


def validate_file_binding(doc, previous=None):
	if "suite" not in frappe.get_installed_apps():
		return
	if is_alias(doc):
		_deny()
	previous = previous or (doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None)
	if previous and previous.file_url and identity(previous.file_url) != identity(doc.file_url):
		if not any(name == doc.name and identity(url) == identity(doc.file_url) for name, url in _bindings.get()):
			_deny()
	# Compare canonical identities, not only text URLs. Client flags cannot mint
	# a creation capability or claim an unregistered object in another folder.
	if doc.file_url and (doc.is_new() or not previous or not previous.file_url):
		key = identity(doc.file_url)
		if key is None:
			return
		if not hasattr(frappe.local, "kanzlei_storage_sources"):
			settings = _index()[0].settings
			frappe.local.kanzlei_storage_sources = [(row.name, identity(row.file_url, settings))
				for row in frappe.get_all("File", filters={"file_url": ["is", "set"]}, fields=["name", "file_url"])]
		sources = [name for name, source in frappe.local.kanzlei_storage_sources if source == key and name != doc.name]
		if sources:
			if not any(frappe.has_permission("File", "read", doc=name) for name in sources):
				_deny()
		elif not any(entity_id == id(doc) and object_key == key for entity_id, object_key in _creations.get()):
			_deny()


def validate_binding_event(doc, method=None):
	# before_validate runs even when an incoming Document requests ignore_validate.
	invalidate()
	validate_file_binding(doc)


def require_storage_access(file, operation="read", user=None, target=None):
	if "suite" not in frappe.get_installed_apps():
		return
	if isinstance(file, str):
		file = frappe.get_doc("File", file)
	# Persisted markers only; a caller may legitimately supply a sidecar URL.
	if conflicts(file, recursive=bool(file.get("is_folder")) and operation != "read"):
		_deny()
	name = file.get("name")
	if callable(getattr(file, "is_new", None)) and file.is_new():
		name = None
	row = frappe.db.get_value("File", name, ["name", "file_url", MARKER], as_dict=True) if name else None
	customer = row.get(MARKER) if row else None
	deleted = _deleted.get()
	if deleted and deleted.name == name and identity(file.get("file_url")) in deleted.storage_keys:
		customer = deleted.customer
	if row and row.file_url and file.get("file_url") and identity(row.file_url) != identity(file.file_url):
		manager = _index()[0]
		allowed = [identity(path, manager.settings) for path in _sidecar_paths(row, manager)]
		allowed.extend(identity(url) for entity, url in _bindings.get() if entity == name)
		if identity(file.file_url) not in allowed:
			_deny()
	if customer:
		from kanzlei_erp.mandant_drive import _customer_access
		permission = "read" if operation == "read" else "write"
		reader = _reader.get()
		principal = reader[1] if operation == "read" and reader and reader[0] == name else user or frappe.session.user
		if not _customer_access(customer, principal).get(permission):
			_deny()
	if target is not None and conflicts(file, target=target, recursive=bool(file.get("is_folder"))):
		_deny()


@contextmanager
def binding_change(doc, target=None):
	"""Authorize only this operation's File IDs and their computed destination keys."""
	from suite.drive.utils.files import FileManager, storage_key

	manager = FileManager()
	base = str(target if target is not None else manager.get_disk_path(doc))
	entries = [(doc.name, base), (doc.name, doc.file_url)]
	parents = [doc.name] if doc.is_folder else []
	seen = set()
	while parents:
		seen.update(parents)
		children = frappe.get_all("File", filters={"folder": ["in", parents]}, fields=FIELDS)
		parents = []
		for child in children:
			old_key = str(storage_key(doc.file_url)).rstrip("/")
			child_key = str(storage_key(child.file_url))
			if child_key.startswith(old_key + "/"):
				entries.append((child.name, base.rstrip("/") + child_key[len(old_key):]))
			if child.is_folder and child.name not in seen:
				parents.append(child.name)
	token = _bindings.set((*_bindings.get(), *entries))
	try:
		yield
	finally:
		_bindings.reset(token)


def audit_storage_aliases():
	frappe.only_for("System Manager")
	rows = _managed()
	return [{"file": row.name, "conflicts": [other.name for other in conflicts(row, rows=rows,
		recursive=bool(row.is_folder))]} for row in frappe.get_all("File", fields=FIELDS)
		if is_alias(row, rows=rows)]


def alias_names():
	if hasattr(frappe.local, "kanzlei_storage_aliases"):
		return frappe.local.kanzlei_storage_aliases
	index = _index()
	if not index[1]:
		return []
	names = [row.name for row in frappe.get_all("File", filters={"file_url": ["is", "set"]}, fields=FIELDS)
		if is_alias(row, index=index)]
	frappe.local.kanzlei_storage_aliases = names
	return names


def suite_storage_guard(entity, operation="read", target=None):
	return require_storage_access(entity, operation, target=target)


@contextmanager
def deleted_storage(doc):
	"""Server-only cleanup scope retains identity after a validated row deletion."""
	from suite.drive.utils.files import FileManager
	manager = FileManager()
	state = frappe._dict(name=doc.name, customer=doc.get(MARKER), storage_keys=[identity(doc.file_url),
		*(identity(path, manager.settings) for path in _sidecar_paths(doc, manager))])
	token = _deleted.set(state)
	try:
		yield
	finally:
		_deleted.reset(token)


@contextmanager
def authorized_read(file, user):
	"""Only the token-bound File uses the issuer; session identity is unchanged."""
	token = _reader.set((file, user))
	try:
		yield
	finally:
		_reader.reset(token)


@contextmanager
def creation_binding(doc, url):
	"""Minted by framework/Suite Python flows, never by Document.flags."""
	if "suite" not in frappe.get_installed_apps():
		yield
		return
	token = _creations.set((*_creations.get(), (id(doc), identity(url))))
	try:
		yield
	finally:
		_creations.reset(token)


@contextmanager
def creation_flow():
	token = _creations.set(list(_creations.get()))
	try:
		yield
	finally:
		_creations.reset(token)


def grant_creation(doc, url):
	# Only called from the server's before_insert inside creation_flow.
	bindings = _creations.get()
	if not isinstance(bindings, list):
		_deny()
	bindings.append((id(doc), identity(url)))


@contextmanager
def exact_io(doc):
	token = _io_owner.set(doc)
	try:
		yield
	finally:
		_io_owner.reset(token)


def guard_privacy_change(doc, source, target):
	if conflicts(doc, target=source) or conflicts(doc, target=target):
		_deny()
	with exact_io(doc):
		require_path_access(source, "write")
		require_path_access(target, "write")
	if frappe.get_all("File", filters={"content_hash": doc.content_hash, MARKER: ["is", "set"]}, limit=1):
		_deny()


def require_path_access(path, operation="read"):
	"""Guard direct framework/DAV IO using the actual resolved storage path."""
	if "suite" not in frappe.get_installed_apps():
		return
	from kanzlei_erp.mandant_drive import _customer_access

	key = identity(path)
	for row, physical, sidecars in _index()[1]:
		objects = [physical, *sidecars]
		if any(key == other or (row.is_folder and _under(key, other)) or
			(operation != "read" and _under(other, key)) for other in objects):
			if operation != "read":
				owner = _io_owner.get()
				if owner is None or conflicts(owner, target=path, recursive=True):
					_deny()
			permission = "read" if operation == "read" else "write"
			if not _customer_access(row.get(MARKER), frappe.session.user)[permission]:
				_deny()
