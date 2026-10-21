"""Bind Posteingang authorization to persisted originals and their byte paths."""

from pathlib import Path
from urllib.parse import unquote, urlsplit

import frappe
from werkzeug.exceptions import HTTPException, RequestedRangeNotSatisfiable
from werkzeug.utils import send_file
from werkzeug.wrappers import Response


def _identity(url):
	# Core File IO also accepts site-qualified filesystem paths. Resolve those
	# before interpreting a relative URL as a Suite storage key.
	value = str(url or "")
	if not value:
		return None
	root = Path(frappe.get_site_path()).resolve()
	physical = Path(value).resolve()
	if physical.is_relative_to(root):
		parts = physical.relative_to(root).parts
		if parts and parts[0] in ("private", "public"):
			return ("disk", str(physical))
	if "suite" in frappe.get_installed_apps():
		from kanzlei_erp.drive_storage import identity

		return identity(url)
	from kanzlei_erp.object_storage import get_binding

	value = str(url or "")
	if not value:
		return None
	if binding := get_binding(value):
		return ("s3", binding.object_key)
	if value.startswith("s3://"):
		return ("s3", value.removeprefix("s3://"))
	if value.startswith(("http://", "https://")):
		parts = urlsplit(value)
		if parts.netloc != urlsplit(frappe.utils.get_url()).netloc:
			return None
		value = unquote(parts.path)
	if value.startswith("/files/"):
		value = "/public" + value
	root = Path(frappe.get_site_path()).resolve()
	path = Path(value)
	if not path.is_relative_to(root):
		path = root / value.lstrip("/")
	return ("disk", str(path.resolve()))


def _sources():
	if not frappe.db.table_exists("Incoming Letter"):
		return []
	result = []
	for letter in frappe.get_all(
		"Incoming Letter",
		fields=["name", "original_file", "confirmed_customer", "correction_quarantined"],
		limit=0,
	):
		file = frappe.db.get_value(
			"File",
			letter.original_file,
			["name", "file_url", "thumbnail_url", "attached_to_doctype", "attached_to_name", "is_private"],
			as_dict=True,
		)
		if file:
			paths = [file.file_url, file.thumbnail_url]
			if frappe.db.table_exists("S3 File Object"):
				paths += frappe.get_all(
					"S3 File Object", filters={"source_file": file.name}, pluck="file_url"
				)
			result.append((letter, file, {_identity(p) for p in paths if p}))
	return result


def protected_letters(doc):
	"""None: ordinary. []: malformed binding/alias. [Letter]: owned material."""
	if isinstance(doc, str):
		doc = frappe.get_doc("File", doc)
	row = (
		frappe.db.get_value(
			"File",
			doc.get("name"),
			["name", "file_url", "thumbnail_url", "attached_to_doctype", "attached_to_name"],
			as_dict=True,
		)
		if doc.get("name")
		else None
	)
	candidate = row or doc
	owned = []
	for letter, source, paths in _sources():
		if candidate.get("name") == source.name:
			if (
				source.attached_to_doctype != "Incoming Letter"
				or source.attached_to_name != letter.name
				or not source.is_private
			):
				return []
			owned.append(letter)
		elif any(_identity(candidate.get(field)) in paths for field in ("file_url", "thumbnail_url")):
			# Independent aliases cannot broaden the original's access.
			return []
	if owned:
		return owned
	if candidate.get("attached_to_doctype") == "Incoming Letter":
		return []
	return None


def require_access(doc, operation="read", user=None, target=None):
	from kanzlei_erp.posteingang_model import creating, deny
	from kanzlei_erp.posteingang_permissions import letter_access

	if isinstance(doc, str):
		doc = frappe.get_doc("File", doc)
	if creating(file=doc.get("name")) and creating(letter=doc.get("attached_to_name")):
		return
	if operation != "read" and doc.get("is_folder"):
		pending, seen = [doc.name], set()
		while pending:
			folder = pending.pop()
			if folder in seen:
				continue
			seen.add(folder)
			for child in frappe.get_all(
				"File", filters={"folder": folder}, fields=["name", "is_folder", "file_url"]
			):
				if protected_letters(child) is not None:
					deny()
				if child.is_folder:
					pending.append(child.name)
	if target is not None:
		path_guard(target, "write")
	letters = protected_letters(doc)
	if letters is None:
		probe = frappe._dict(file_url=doc.get("file_url"), thumbnail_url=doc.get("thumbnail_url"))
		if protected_letters(probe) is not None:
			deny()
	if letters is not None:
		if operation != "read" or not letters or not all(letter_access(d, user) for d in letters):
			deny()
		# A caller cannot redirect an authorized persisted File to another URL.
		stored = frappe.db.get_value("File", doc.name, "file_url")
		if doc.get("file_url") and _identity(doc.file_url) != _identity(stored):
			from kanzlei_erp.object_storage import get_binding

			binding = get_binding(doc.file_url)
			if not binding or binding.source_file != doc.name:
				deny()


def validate_binding(doc, method=None):
	from kanzlei_erp.posteingang_model import creating, deny

	if creating(file=doc.get("name")):
		return
	if protected_letters(doc) is not None:
		deny()
	# Check proposed URL independently of persisted name.
	probe = frappe._dict(
		file_url=doc.get("file_url"),
		thumbnail_url=doc.get("thumbnail_url"),
		attached_to_doctype=doc.get("attached_to_doctype"),
	)
	if protected_letters(probe) is not None:
		deny()


def path_guard(path, operation="read"):
	from kanzlei_erp.posteingang_model import creating, deny
	from kanzlei_erp.posteingang_permissions import letter_access

	identity = _identity(path)
	for letter, file, paths in _sources():
		contains = (
			operation != "read"
			and identity
			and any(
				p and p[0] == identity[0] and p[1].startswith(identity[1].rstrip("/") + "/") for p in paths
			)
		)
		if identity in paths or contains:
			if creating(letter=letter.name, file=file.name):
				continue
			if operation != "read" or not letter_access(letter):
				deny()


def response(file):
	"""Authorized GET/HEAD/Range through the app; never mints a bearer URL."""
	from kanzlei_erp.object_storage import open_file

	require_access(file)
	with open_file(file) as source:
		# Bounded by v1's 20 MiB. BytesIO lets Werkzeug handle Range and HEAD.
		from io import BytesIO

		stream = BytesIO(source.read(20971521))
	if stream.getbuffer().nbytes > 20971520:
		stream.close()
		frappe.throw("Posteingang limit exceeded", frappe.ValidationError)
	from kanzlei_erp.object_storage import get_binding

	binding = get_binding(file.file_url)
	content_type = binding.content_type if binding and binding.content_type else "application/pdf"
	try:
		res = send_file(
			stream,
			frappe.local.request.environ,
			mimetype=content_type,
			download_name=file.file_name,
			conditional=True,
			max_age=0,
			etag=False,
		)
	except RequestedRangeNotSatisfiable:
		stream.close()
		res = Response(status=416)
	res.headers["Cache-Control"] = "private, no-store"
	res.headers["X-Content-Type-Options"] = "nosniff"
	res.headers["Accept-Ranges"] = "bytes"
	return res


def reject_archive(entities):
	from kanzlei_erp.posteingang_model import deny

	pending, seen = list(entities), set()
	while pending:
		name = pending.pop()
		if name in seen:
			continue
		seen.add(name)
		doc = frappe.get_doc("File", name)
		if protected_letters(doc) is not None:
			deny()
		if doc.is_folder:
			pending.extend(frappe.get_all("File", filters={"folder": name}, pluck="name"))


def before_request():
	"""Intercept Suite byte/token/archive endpoints before bearer issuance."""
	if not frappe.db.table_exists("Incoming Letter"):
		return
	from frappe.auth import validate_auth

	request = frappe.local.request
	method = frappe.form_dict.get("cmd") or request.path.removeprefix("/api/").removeprefix(
		"v1/"
	).removeprefix("v2/").removeprefix("method/")
	if method.startswith("suite.drive.api.files."):
		validate_auth()
		action = method.rsplit(".", 1)[-1]
		if action in ("get_file_content", "stream_file_content", "create_auth_token"):
			name = frappe.form_dict.get("entity_name")
			if name and frappe.db.exists("File", name):
				file = frappe.get_doc("File", name)
				if protected_letters(file) is not None:
					if action == "create_auth_token" or frappe.form_dict.get("token"):
						from kanzlei_erp.posteingang_model import deny

						deny()
					raise HTTPException(response=response(file))
		if action == "download_folder":
			entities = frappe.form_dict.get("entities") or []
			reject_archive(frappe.parse_json(entities) if isinstance(entities, str) else entities)
		if action in ("download_status", "download_archive"):
			from suite.drive.api.files import _get_download_entry

			entry = _get_download_entry(frappe.form_dict.get("token"))
			reject_archive(entry.get("files", []) + entry.get("entities", []))
	if request.method in ("GET", "HEAD") and request.path.startswith(("/private/files/", "/files/")):
		validate_auth()
		path_guard(request.path)
		identity = _identity(request.path)
		for _letter, file, paths in _sources():
			if identity in paths:
				# Derivative route must not return original bytes.
				doc = frappe.get_doc("File", file.name)
				if identity != _identity(doc.file_url):
					doc.file_url = request.path
				raise HTTPException(response=response(doc))


def presign_guard(key):
	from kanzlei_erp.posteingang_model import deny

	for _letter, _file, paths in _sources():
		if ("s3", str(key)) in paths:
			deny()


def suite_response(file):
	if protected_letters(file) is not None:
		return response(frappe.get_doc("File", file.name))
	return None


def token_guard(entity_name):
	from kanzlei_erp.posteingang_model import deny

	if protected_letters(frappe.get_doc("File", entity_name)) is not None:
		deny()
