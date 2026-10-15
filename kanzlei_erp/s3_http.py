"""Serve stable File URLs through current permissions, independent of backend."""

import mimetypes
from pathlib import Path

import frappe
from werkzeug.exceptions import Forbidden, HTTPException, NotFound
from werkzeug.utils import send_file
from werkzeug.wrappers import Response


def before_request():
	request = frappe.local.request
	if request.method not in ("GET", "HEAD") or not request.path.startswith(("/files/", "/private/files/")):
		return
	from frappe.auth import validate_auth

	from kanzlei_erp import object_storage as storage

	validate_auth()
	path = storage.canonical_url(request.path)
	if not path:
		raise NotFound()
	row = storage.get_binding(path)
	if not row:
		# This also replaces the development static-file bypass. Private legacy
		# files retain Frappe's current authorization and download implementation.
		if path.startswith("/private/files/"):
			return
		if storage.settings().enabled:
			raise NotFound()
		root = Path(frappe.get_site_path("public", "files")).resolve()
		local = (root / path.removeprefix("/files/")).resolve()
		if not local.is_relative_to(root) or not local.is_file():
			raise NotFound()
		response = send_file(local, request.environ, conditional=True)
		response.headers["X-Content-Type-Options"] = "nosniff"
		from frappe.utils.response import FORCE_DOWNLOAD_EXTENSIONS

		if local.suffix.lower() in FORCE_DOWNLOAD_EXTENSIONS:
			response.headers.set("Content-Disposition", "attachment", filename=local.name)
		raise HTTPException(response=response)
	file = authorize(path, row.object_key)
	from kanzlei_erp.drive_storage import require_storage_access

	require_storage_access(file)
	if request.method == "HEAD":
		meta = storage.client().head_object(
			Bucket=storage.settings().bucket, Key=storage.managed_key(row.object_key)
		)
		response = Response(
			status=200,
			headers={
				"Content-Length": str(meta["ContentLength"]),
				"Content-Type": row.content_type or "application/octet-stream",
				"Accept-Ranges": "bytes",
				"Cache-Control": "private, no-store",
				"X-Content-Type-Options": "nosniff",
			},
		)
		raise HTTPException(response=response)
	params = {
		"Bucket": storage.settings().bucket,
		"Key": storage.managed_key(row.object_key),
		"ResponseContentType": row.content_type
		or mimetypes.guess_type(file.file_name)[0]
		or "application/octet-stream",
	}
	from frappe.utils.response import FORCE_DOWNLOAD_EXTENSIONS
	from suite.drive.utils.files import content_disposition

	inline = Path(file.file_name).suffix.lower() not in FORCE_DOWNLOAD_EXTENSIONS
	params["ResponseContentDisposition"] = (
		content_disposition(file.file_name).replace("attachment;", "inline;", 1)
		if inline
		else content_disposition(file.file_name)
	)
	url = storage.client(public=True).generate_presigned_url("get_object", Params=params, ExpiresIn=300)
	response = Response(
		status=302,
		headers={"Location": url, "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
	)
	raise HTTPException(response=response)


def authorize(path, key):
	"""Find a live authorized File, including aliases retained during migration."""
	from suite.drive.utils.files import get_s3_url

	urls = frappe.get_all("S3 File Object", filters={"object_key": key}, pluck="file_url")
	urls.append(get_s3_url(key))
	fid = frappe.form_dict.get("fid")
	filters = {"file_url": ["in", urls], "is_folder": 0}
	if fid:
		filters["name"] = fid
	names = frappe.get_all("File", filters=filters, pluck="name")
	thumbnail_filters = {"thumbnail_url": ["in", urls], "is_folder": 0}
	if fid:
		thumbnail_filters["name"] = fid
	names.extend(frappe.get_all("File", filters=thumbnail_filters, pluck="name"))
	for owner in frappe.get_all(
		"S3 File Object", filters={"object_key": key, "keep_with_source": 1}, pluck="source_file"
	):
		if owner and (not fid or fid == owner) and frappe.db.exists("File", owner):
			names.append(owner)
	for name in set(names):
		file = frappe.get_doc("File", name)
		if file.get("status") in ("Trashed", "Removed"):
			continue
		if path.startswith("/private/") and frappe.session.user == "Guest":
			continue
		if not file.is_private or frappe.has_permission("File", "read", doc=file):
			return file
	raise Forbidden("You do not have permission to access this file")
