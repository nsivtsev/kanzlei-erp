"""Site-scoped S3 storage for Frappe attachments and Suite Drive.

URLs are application identities, never credentials or permission grants. The
mapping is server-owned; every content access still passes File/Mandant guards.
"""

import hashlib
import json
import mimetypes
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import frappe

_configuring = ContextVar("kanzlei_s3_configuration", default=False)
_binding = ContextVar("kanzlei_s3_binding", default=False)
S3_URL_PREFIX = "/api/method/suite.drive.api.s3.fetch?path="
CONFIG_FIELDS = (
	"enabled",
	"bucket",
	"endpoint_url",
	"root_folder",
	"flat",
	"signature_version",
	"kanzlei_s3_region",
	"kanzlei_s3_addressing_style",
	"kanzlei_s3_public_endpoint",
	"kanzlei_s3_prefix",
)


def settings():
	return frappe.get_single("Drive Disk Settings")


def prefix(settings=None):
	doc = settings or globals()["settings"]()
	value = doc.get("kanzlei_s3_prefix") or "sites/" + frappe.local.site
	value = str(value).strip("/")
	if not value or any(part in ("", ".", "..") for part in value.split("/")) or "\\" in value:
		frappe.throw("Invalid S3 site prefix", frappe.ValidationError)
	return value


def managed_key(key, config=None):
	key = str(key)
	if (
		not key.startswith(prefix(config) + "/")
		or any(p in ("", ".", "..") for p in key.split("/"))
		or "\\" in key
	):
		frappe.throw("S3 object is outside this site's storage namespace", frappe.ValidationError)
	return key


def client(public=False, settings=None):
	import boto3
	from botocore.config import Config

	doc = settings or globals()["settings"]()
	endpoint = doc.get("kanzlei_s3_public_endpoint") if public else None
	endpoint = endpoint or doc.get("endpoint_url")
	get_password = getattr(doc, "get_password", None)
	secret = get_password("aws_secret") if callable(get_password) else doc.get("aws_secret")
	return boto3.client(
		"s3",
		endpoint_url=endpoint or None,
		aws_access_key_id=doc.get("aws_key"),
		aws_secret_access_key=secret,
		region_name=doc.get("kanzlei_s3_region") or "us-east-1",
		config=Config(
			signature_version=doc.get("signature_version") or "s3v4",
			s3={"addressing_style": doc.get("kanzlei_s3_addressing_style") or "path"},
			connect_timeout=10,
			read_timeout=120,
			retries={"max_attempts": 3, "mode": "standard"},
		),
	)


@contextmanager
def configuration_change():
	token = _configuring.set(True)
	try:
		yield
	finally:
		_configuring.reset(token)


def configure():
	"""Idempotent schema additions; leave existing backend and credentials alone."""
	if "suite" not in frappe.get_installed_apps():
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	fields = [
		{"fieldname": "kanzlei_s3_region", "fieldtype": "Data", "label": "S3 Region", "default": "us-east-1"},
		{
			"fieldname": "kanzlei_s3_addressing_style",
			"fieldtype": "Select",
			"label": "S3 Addressing Style",
			"options": "path\nvirtual",
			"default": "path",
		},
		{
			"fieldname": "kanzlei_s3_public_endpoint",
			"fieldtype": "Data",
			"label": "Browser S3 Endpoint",
			"description": "Optional browser-accessible endpoint for signed download links.",
		},
		{
			"fieldname": "kanzlei_s3_prefix",
			"fieldtype": "Data",
			"label": "Site S3 Prefix",
			"description": "Dedicated namespace containing Drive, attachments and derived files.",
		},
		{
			"fieldname": "kanzlei_s3_epoch",
			"fieldtype": "Int",
			"label": "Storage Mutation Counter",
			"hidden": 1,
			"read_only": 1,
		},
		{
			"fieldname": "kanzlei_s3_migration_run",
			"fieldtype": "Data",
			"label": "Storage Migration",
			"hidden": 1,
			"read_only": 1,
		},
	]
	for field in fields:
		field.update({"insert_after": "signature_version", "depends_on": "enabled"})
	create_custom_fields({"Drive Disk Settings": fields}, update=True)
	if frappe.db.get_single_value("Drive Disk Settings", "kanzlei_s3_epoch") is None:
		frappe.db.set_single_value("Drive Disk Settings", "kanzlei_s3_epoch", 0)


def validate_settings(doc, method=None):
	if _configuring.get() or not doc.get_doc_before_save():
		return
	# Hidden/read-only form fields are not an authority boundary on REST writes.
	doc.kanzlei_s3_epoch = mutation_epoch()
	doc.kanzlei_s3_migration_run = frappe.db.get_single_value(
		"Drive Disk Settings", "kanzlei_s3_migration_run", cache=False
	)
	old = doc.get_doc_before_save()
	changed = any(str(doc.get(f) or "") != str(old.get(f) or "") for f in CONFIG_FIELDS)
	if (
		changed
		and (doc.enabled or old.enabled)
		and frappe.db.count("File", {"is_folder": 0, "file_url": ["is", "set"]})
	):
		frappe.throw(
			"Use the S3 migration command to change a populated site's storage backend.",
			frappe.ValidationError,
		)
	if doc.enabled:
		prefix(doc)
		if doc.root_folder != prefix(doc) + "/drive":
			frappe.throw(
				"Drive Folder must be the site's S3 prefix followed by /drive", frappe.ValidationError
			)


def mutation_epoch():
	return int(frappe.db.get_single_value("Drive Disk Settings", "kanzlei_s3_epoch", cache=False) or 0)


def mark_mutation(doc=None, method=None):
	if _configuring.get() or not frappe.db.has_column("File", "status"):
		return
	# An atomic counter in the same transaction survives processes and restarts.
	frappe.db.sql(
		"UPDATE tabSingles SET value=CAST(COALESCE(NULLIF(value,''),'0') AS UNSIGNED)+1 "
		"WHERE doctype='Drive Disk Settings' AND field='kanzlei_s3_epoch'"
	)


def canonical_url(url):
	value = str(url or "")
	if value.startswith(("http://", "https://")):
		parsed = urlsplit(value)
		if parsed.netloc != urlsplit(frappe.utils.get_url()).netloc:
			return None
		value = parsed.path
	# Decode only once, consistently with Frappe's File URLs.
	value = unquote(value)
	# Suite also persists site-relative paths without a leading slash.
	if value.startswith(("private/files/", "files/")):
		value = "/" + value
	if not value.startswith(("/files/", "/private/files/")):
		return None
	if value.endswith("/"):
		return None
	if any(p in ("", ".", "..") for p in value.lstrip("/").split("/")) or "\\" in value or "\x00" in value:
		return None
	return value


def get_binding(url):
	url = canonical_url(url)
	if not url or not frappe.db.table_exists("S3 File Object"):
		return None
	if url in getattr(frappe.local, "kanzlei_s3_locked_urls", set()):
		rows = frappe.db.sql(
			"SELECT name,file_url,object_key,file_size,sha256,content_type,source_file,keep_with_source FROM `tabS3 File Object` WHERE name=%s FOR UPDATE",
			(hashlib.sha256(url.encode()).hexdigest(),),
			as_dict=True,
		)
		return rows[0] if rows else None
	return frappe.db.get_value(
		"S3 File Object",
		hashlib.sha256(url.encode()).hexdigest(),
		[
			"name",
			"file_url",
			"object_key",
			"file_size",
			"sha256",
			"content_type",
			"source_file",
			"keep_with_source",
		],
		as_dict=True,
	)


def lock_object(key):
	"""Coordinate alias insertion and reaping until the surrounding transaction ends."""
	name = "kanzlei-s3:" + hashlib.sha256((settings().bucket + "\0" + key).encode()).hexdigest()[:48]
	held = getattr(frappe.local, "kanzlei_s3_locks", None)
	if held is None:
		held = frappe.local.kanzlei_s3_locks = set()
	if name in held:
		return
	if frappe.db.sql("SELECT GET_LOCK(%s,10)", (name,))[0][0] != 1:
		frappe.throw("File storage is busy; retry the operation", frappe.ValidationError)
	held.add(name)

	def release():
		if name in held:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
			held.discard(name)
		frappe.local.kanzlei_s3_locked_urls = set()

	frappe.db.after_commit.add(release)
	frappe.db.after_rollback.add(release)


def lock_binding(url):
	row = get_binding(url)
	if not row:
		return
	lock_object(row.object_key)
	urls = getattr(frappe.local, "kanzlei_s3_locked_urls", None)
	if urls is None:
		urls = frappe.local.kanzlei_s3_locked_urls = set()
	urls.add(canonical_url(url))
	current = get_binding(url)
	if not current:
		frappe.throw("Stored file was deleted; reload before attaching it", frappe.PermissionError)
	if current.object_key != row.object_key:
		lock_object(current.object_key)


def stored(file):
	return not file.is_folder and (
		get_binding(file.file_url) or str(file.file_url or "").startswith(S3_URL_PREFIX)
	)


def file_key(file):
	if row := get_binding(file.file_url):
		return managed_key(row.object_key)
	from suite.drive.utils.files import storage_key

	return managed_key(storage_key(file.file_url))


def resolve_key(file):
	"""Suite extension point; preserves disk behavior when S3 is disabled."""
	from suite.drive.utils.files import storage_key

	if settings().enabled and stored(file):
		return file_key(file)
	return storage_key(file.file_url)


def replace_content(file, key, size, sha256):
	"""Called only by the validated WebDAV transaction, before publishing bytes."""
	from kanzlei_erp.drive_storage import invalidate, require_storage_access

	require_storage_access(file, "write")
	old = resolve_key(file)
	lock_object(old)
	key = managed_key(key)
	lock_object(key)
	token = _binding.set(True)
	try:
		for name in frappe.get_all("S3 File Object", filters={"object_key": old}, pluck="name"):
			doc = frappe.get_doc("S3 File Object", name)
			doc.update({"object_key": key, "file_size": size, "sha256": sha256})
			doc.save(ignore_permissions=True)
	finally:
		_binding.reset(token)
	invalidate()
	mark_mutation()


def protect_binding(doc, method=None):
	if not _binding.get():
		frappe.throw("Storage bindings are managed by the server", frappe.PermissionError)


def bind(url, key, size, sha256, content_type=None, source_file=None, keep_with_source=False):
	url = canonical_url(url)
	if not url:
		frappe.throw("Only local File URLs can have a storage binding", frappe.ValidationError)
	key = managed_key(key)
	lock_object(key)
	name = hashlib.sha256(url.encode()).hexdigest()
	values = {
		"file_url": url,
		"object_key": key,
		"file_size": int(size),
		"sha256": sha256,
		"content_type": content_type or mimetypes.guess_type(url)[0] or "application/octet-stream",
		"source_file": source_file,
		"keep_with_source": int(keep_with_source),
	}
	existing = get_binding(url)
	if existing and (existing.object_key != key or existing.sha256 != sha256):
		frappe.throw("A storage binding cannot be reassigned to different bytes", frappe.ValidationError)
	token = _binding.set(True)
	try:
		if not existing:
			doc = frappe.get_doc({"doctype": "S3 File Object", "name": name, **values})
			doc.insert(ignore_permissions=True, set_name=name)
	finally:
		_binding.reset(token)
	return get_binding(url)


def write_file(file_or_name, content=None, content_type=None, is_private=0):
	"""Both Frappe write_file hook signatures, without bypassing core validation."""
	doc = None if isinstance(file_or_name, str) else file_or_name
	config = settings() if "suite" in frappe.get_installed_apps() else None
	if config is None or not config.enabled:
		if doc is not None:
			return doc.save_file_on_filesystem()
		from frappe.utils.file_manager import save_file_on_filesystem

		return save_file_on_filesystem(file_or_name, content, content_type, is_private)
	name = doc.file_name if doc is not None else file_or_name
	data = doc._content if doc is not None else content
	data = data.encode() if isinstance(data, str) else data
	private = doc.is_private if doc is not None else is_private
	name = re.sub(r"[/\\%?#\x00-\x1f]", "_", name)
	url = ("/private/files/" if private else "/files/") + uuid.uuid4().hex + "_" + name
	key = prefix(config) + "/attachments/" + uuid.uuid4().hex
	sha = hashlib.sha256(data).hexdigest()
	track_write(key)
	client().put_object(
		Bucket=config.bucket,
		Key=key,
		Body=data,
		ContentType=content_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
	)
	bind(url, key, len(data), sha, content_type)
	if doc is not None:
		doc.file_url = url
		from kanzlei_erp.drive_storage import grant_creation

		grant_creation(doc, url)
	# Rollbacks leave no live reference; cleanup is retried by a scheduled sweep.
	return {"file_name": name, "file_url": url}


def track_write(key):
	"""A filesystem intent survives a DB rollback or process termination."""
	key = managed_key(key)
	config = settings()
	directory = Path(frappe.get_site_path("private", "s3-pending"))
	directory.mkdir(parents=True, exist_ok=True, mode=0o700)
	path = directory / (uuid.uuid4().hex + ".json")
	with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
		json.dump(
			{"key": key, "bucket": config.bucket, "endpoint": config.endpoint_url, "created": time.time()},
			handle,
		)
		handle.flush()
		os.fsync(handle.fileno())

	def rolled_back():
		try:
			client(settings=config).delete_object(Bucket=config.bucket, Key=key)
		except Exception:
			return
		path.unlink(missing_ok=True)

	frappe.db.after_rollback.add(rolled_back)

	# A successful DB commit makes the mapping durable. Aborted inserts that
	# callers intentionally catch remain in the ledger for the hourly sweep.
	def committed():
		if has_references(key):
			path.unlink(missing_ok=True)

	frappe.db.after_commit.add(committed)


def open_binding(file, range_header=None):
	row = get_binding(file.get("file_url"))
	if not row:
		return None
	from kanzlei_erp.drive_storage import require_storage_access

	require_storage_access(file)
	args = {"Bucket": settings().bucket, "Key": managed_key(row.object_key)}
	if range_header:
		args["Range"] = range_header
	return client().get_object(**args)["Body"]


def url_content(url):
	"""Resolve retained inline-email aliases through current File permissions."""
	if not (binding := get_binding(url)):
		return None
	from kanzlei_erp.drive_storage import require_storage_access
	from kanzlei_erp.s3_http import authorize

	file = authorize(url, binding.object_key)
	require_storage_access(file)
	with client().get_object(Bucket=settings().bucket, Key=managed_key(binding.object_key))["Body"] as body:
		return body.read()


@contextmanager
def open_file(file):
	"""Stream original bytes, preserving all File/Mandant checks."""
	from kanzlei_erp.drive_storage import require_storage_access

	require_storage_access(file)
	stream = open_binding(file)
	if stream is None:
		if str(file.file_url).startswith(S3_URL_PREFIX):
			from suite.drive.utils.files import FileManager

			stream = FileManager().get_file(file)
		else:
			file.validate_file_path()
			stream = open(file.get_full_path(), "rb")
	try:
		yield stream
	finally:
		stream.close()


@contextmanager
def local_path(file):
	"""An explicitly scoped path for libraries that require a local filename."""
	if not get_binding(file.file_url):
		if not str(file.file_url).startswith(S3_URL_PREFIX):
			yield Path(file.get_full_path())
			return
	with tempfile.NamedTemporaryFile(prefix="kanzlei-s3-", suffix=Path(file.file_name).suffix) as target:
		with open_file(file) as source:
			while block := source.read(1024 * 1024):
				target.write(block)
			target.flush()
		yield Path(target.name)


def materialize(file):
	"""Compatibility for legacy File.get_full_path consumers; request/job scoped."""
	cache = getattr(frappe.local, "kanzlei_s3_temporary", None)
	if cache is None:
		cache = frappe.local.kanzlei_s3_temporary = {}
		frappe.db.after_commit.add(cleanup_temporary_files)
		frappe.db.after_rollback.add(cleanup_temporary_files)
	version = (file.file_url, file_key(file))
	if version not in cache:
		with tempfile.NamedTemporaryFile(
			prefix="kanzlei-s3-", suffix=Path(file.file_name).suffix, delete=False
		) as target:
			try:
				with open_file(file) as source:
					while block := source.read(1024 * 1024):
						target.write(block)
				cache[version] = target.name
			except BaseException:
				Path(target.name).unlink(missing_ok=True)
				raise
	return cache[version]


def cleanup_temporary_files(*args, **kwargs):
	for path in getattr(frappe.local, "kanzlei_s3_temporary", {}).values():
		Path(path).unlink(missing_ok=True)
	frappe.local.kanzlei_s3_temporary = {}


def schedule_delete(file):
	schedule_derivatives(file)
	row = get_binding(file.file_url)
	if not row:
		return False
	# Persist retry intent in the same transaction as the File deletion.
	schedule_key_deletion(row.object_key)
	return True


def schedule_derivatives(file):
	if row := get_binding(file.thumbnail_url):
		schedule_key_deletion(row.object_key)
	for key in frappe.get_all("S3 File Object", filters={"source_file": file.name}, pluck="object_key"):
		schedule_key_deletion(key)


def schedule_key_deletion(key):
	key = managed_key(key)
	lock_object(key)
	if not frappe.db.exists("S3 File Deletion", {"object_key": key}):
		frappe.get_doc({"doctype": "S3 File Deletion", "object_key": key}).insert(ignore_permissions=True)


def has_references(key, current=False):
	from suite.drive.utils.files import get_s3_url

	if current:
		auxiliary = frappe.db.sql(
			"SELECT source_file FROM `tabS3 File Object` WHERE object_key=%s AND keep_with_source=1 FOR UPDATE",
			(key,),
		)
	else:
		auxiliary = [
			(name,)
			for name in frappe.get_all(
				"S3 File Object", filters={"object_key": key, "keep_with_source": 1}, pluck="source_file"
			)
		]
	for (name,) in auxiliary:
		if current:
			if frappe.db.sql(
				"SELECT name FROM tabFile WHERE name=%s AND (status IS NULL OR status!='Removed') FOR UPDATE",
				(name,),
			):
				return True
		elif frappe.db.exists("File", {"name": name, "status": ["!=", "Removed"]}):
			return True
	urls = frappe.get_all("S3 File Object", filters={"object_key": key}, pluck="file_url")
	if current:
		rows = frappe.db.sql(
			"SELECT file_url FROM `tabS3 File Object` WHERE object_key=%s FOR UPDATE", (key,)
		)
		urls = [row[0] for row in rows]
		urls.append(get_s3_url(key))
		placeholders = ",".join(["%s"] * len(urls))
		return bool(
			frappe.db.sql(
				f"SELECT name FROM tabFile WHERE (file_url IN ({placeholders}) OR thumbnail_url IN ({placeholders})) AND (status IS NULL OR status!='Removed') LIMIT 1 FOR UPDATE",
				tuple(urls + urls),
			)
		)
	urls.append(get_s3_url(key))
	return any(
		frappe.db.exists("File", {field: ["in", urls], "status": ["!=", "Removed"]})
		for field in ("file_url", "thumbnail_url")
	)


def retry_deletions():
	if (
		"suite" not in frappe.get_installed_apps()
		or not frappe.db.table_exists("S3 File Deletion")
		or not settings().enabled
	):
		return
	for entry in frappe.get_all("S3 File Deletion", fields=["name", "object_key"], limit_page_length=100):
		lock_object(entry.object_key)
		if has_references(entry.object_key, current=True):
			continue
		try:
			client().delete_object(Bucket=settings().bucket, Key=managed_key(entry.object_key))
		except Exception:
			# Keep intent; do not include credential-bearing SDK errors in UI logs.
			continue
		frappe.db.delete("S3 File Object", {"object_key": entry.object_key})
		frappe.db.delete("S3 File Deletion", entry.name)
	config = settings()
	for path in Path(frappe.get_site_path("private", "s3-pending")).glob("*.json"):
		try:
			entry = json.loads(path.read_text())
			if (
				entry["created"] > time.time() - 3600
				or entry["bucket"] != config.bucket
				or entry["endpoint"] != config.endpoint_url
			):
				continue
			key = managed_key(entry["key"])
			lock_object(key)
			if not has_references(key, current=True):
				client().delete_object(Bucket=config.bucket, Key=key)
				frappe.db.delete("S3 File Object", {"object_key": key})
			path.unlink()
		except Exception:
			continue


def required_keys():
	from suite.drive.utils.files import S3_URL_PREFIX, storage_key

	for row in frappe.get_all(
		"File",
		filters={"is_folder": 0},
		fields=["file_url", "thumbnail_url", "file_type", "content_doctype", "status"],
	):
		if (
			row.status == "Removed"
			or row.file_type == "Link"
			or (row.content_doctype and row.content_doctype != "Writer Document")
		):
			continue
		if str(row.file_url or "").startswith(S3_URL_PREFIX):
			yield managed_key(storage_key(row.file_url))
		elif binding := get_binding(row.file_url):
			yield managed_key(binding.object_key)
		if binding := get_binding(row.thumbnail_url):
			yield managed_key(binding.object_key)
	for binding in frappe.get_all(
		"S3 File Object", filters={"keep_with_source": 1}, fields=["source_file", "object_key"]
	):
		if frappe.db.exists("File", {"name": binding.source_file, "status": ["!=", "Removed"]}):
			yield managed_key(binding.object_key)


def make_thumbnail(file, width=300, height=300, suffix="small", set_as_thumbnail=True, crop=False):
	from PIL import Image, ImageOps

	from kanzlei_erp.drive_storage import require_storage_access

	require_storage_access(file, "write")
	with open_file(file) as source:
		with Image.open(BytesIO(source.read())) as image:
			image = ImageOps.exif_transpose(image).convert("RGB")
			if crop:
				image = ImageOps.fit(image, (width, height))
			else:
				image.thumbnail((width, height))
			body = BytesIO()
			image.save(body, format="WEBP")
	data = body.getvalue()
	key = prefix() + "/derived/" + uuid.uuid4().hex
	url = ("/private/files/" if file.is_private else "/files/") + uuid.uuid4().hex + "_thumbnail.webp"
	track_write(key)
	client().put_object(Bucket=settings().bucket, Key=key, Body=data, ContentType="image/webp")
	bind(
		url,
		key,
		len(data),
		hashlib.sha256(data).hexdigest(),
		"image/webp",
		source_file=file.name,
		keep_with_source=not set_as_thumbnail,
	)
	if set_as_thumbnail:
		old = get_binding(file.thumbnail_url)
		file.db_set("thumbnail_url", url)
		if old:
			schedule_key_deletion(old.object_key)
	return url


@frappe.whitelist()
def check_connection():
	frappe.only_for("System Manager")
	config = settings()
	key = prefix(config) + "/.checks/" + uuid.uuid4().hex
	conn = client()
	try:
		conn.put_object(Bucket=config.bucket, Key=key, Body=b"kanzlei-s3-check")
		with conn.get_object(Bucket=config.bucket, Key=key)["Body"] as body:
			if body.read() != b"kanzlei-s3-check":
				raise ValueError("S3 read verification failed")
	finally:
		conn.delete_object(Bucket=config.bucket, Key=key)
	return {"ok": True, "bucket": config.bucket, "prefix": prefix(config)}
