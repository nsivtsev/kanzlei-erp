"""Resumable, administrator-only local-disk to S3 migration.

The pure inventory/copy routines deliberately do not import Frappe. Bench entry
points below require a real Administrator/System Manager and a maintenance fence.
No operation removes original bytes. Journals contain paths and metadata, never keys.
"""

import fcntl
import hashlib
import json
import mimetypes
import os
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

S3_URL_PREFIX = "/api/method/suite.drive.api.s3.fetch?path="
CONFIG_FIELDS = (
	"enabled",
	"flat",
	"root_folder",
	"thumbnail_prefix",
	"bucket",
	"endpoint_url",
	"signature_version",
	"kanzlei_s3_region",
	"kanzlei_s3_addressing_style",
	"kanzlei_s3_public_endpoint",
	"kanzlei_s3_prefix",
	"aws_key",
)
LAYOUT_FIELDS = ("enabled", "flat", "root_folder", "thumbnail_prefix")


def _now():
	return datetime.now(UTC).isoformat()


def digest_stream(stream):
	digest = hashlib.sha256()
	size = 0
	while block := stream.read(4 * 1024 * 1024):
		digest.update(block)
		size += len(block)
	return size, digest.hexdigest()


def digest_file(path):
	with Path(path).open("rb") as stream:
		return digest_stream(stream)


def guarded_path(site, url):
	"""Only local public/private file trees; reject lexical and symlink escapes."""
	value = str(url or "").lstrip("/")
	if "\\" in value or "\x00" in value or ".." in PurePosixPath(value).parts:
		raise ValueError("Unsafe file path")
	if value.startswith("files/"):
		value = "public/" + value
	if not value.startswith(("private/files/", "public/files/")) and value not in (
		"private/files",
		"public/files",
	):
		raise ValueError("Path is outside site file storage")
	site = Path(site).resolve()
	path = (site / value).resolve()
	roots = (site / "private/files", site / "public/files")
	if not any(path == root or root in path.parents for root in roots):
		raise ValueError("Symlink escapes site file storage")
	return path


def _safe_component(value):
	value = str(value)
	if not value or value in (".", "..") or any(c in value for c in ("/", "\\", "\x00")):
		raise ValueError("Unsafe File identifier")
	return value


def _drive_ancestor(row, by_name):
	seen = set()
	while row and row.get("name") not in seen:
		name = row.get("name")
		if name in ("Drive", "Users"):
			return True
		seen.add(name)
		row = by_name.get(row.get("folder"))
	return False


def _source_path(row, by_name, site, root, flat):
	original = guarded_path(site, row.get("file_url"))
	if flat:
		return original
	# A trashed parent moves the complete physical tree. Descendants retain the
	# original logical URLs and may themselves have status Trashed.
	candidates = []
	cursor = row
	seen = set()
	while cursor and cursor.get("name") not in seen:
		seen.add(cursor.get("name"))
		if cursor.get("status") in ("Trashed", "Removed"):
			ancestor_path = guarded_path(site, cursor.get("file_url"))
			try:
				relative = original.relative_to(ancestor_path)
			except ValueError:
				pass
			else:
				candidates.append(
					guarded_path(
						site,
						str(
							root.relative_to(Path(site).resolve())
							/ ".trash"
							/ _safe_component(cursor["name"])
							/ relative
						),
					)
				)
		cursor = by_name.get(cursor.get("folder"))
	# Prefer the nearest actual trash location; never reuse a new Active file
	# at the remembered original URL when this row lives in the trash.
	for candidate in candidates:
		if candidate.is_file():
			return candidate
	if candidates:
		return candidates[0]
	return original


def build_inventory(rows, site, prefix, thumbnail_prefix=".thumbnails", flat=False, root_url=None):
	"""Return a stable manifest from DB rows and the two local file trees."""
	site = Path(site).resolve()
	prefix = str(prefix).strip("/")
	if not prefix or any(part in ("", ".", "..") for part in prefix.split("/")) or "\\" in prefix:
		raise ValueError("A safe, site-specific object prefix is required")
	rows = sorted((dict(row) for row in rows), key=lambda row: row["name"])
	by_name = {row["name"]: row for row in rows}
	root = guarded_path(site, root_url or by_name.get("Drive", {}).get("file_url") or "/private/files")
	_safe_component(thumbnail_prefix)
	report = {
		"entries": [],
		"missing": [],
		"conflicts": [],
		"skipped": [],
		"unregistered": [],
		"folders": [],
		"rows": rows,
		"prefix": prefix,
		"root_source": str(root),
	}
	grouped = {}
	referenced = set()
	url_sources = {}
	physical_owners = {}

	def entry(source, key, row, kind, urls):
		referenced.add(str(source))
		previous_owner = physical_owners.get(str(source))
		if previous_owner and previous_owner[0] != row["name"] and "drive" in (kind, previous_owner[1]):
			report["conflicts"].append(
				{"file": row["name"], "reason": "Native Drive object has multiple owners"}
			)
		physical_owners[str(source)] = (row["name"], kind)
		if not source.is_file():
			report["missing"].append({"file": row["name"], "source": str(source), "kind": kind})
			return
		size, sha = digest_file(source)
		for url in urls:
			previous = url_sources.get(url)
			if previous and previous != sha:
				report["conflicts"].append(
					{"file": row["name"], "url": url, "reason": "Ambiguous legacy URL"}
				)
			url_sources[url] = sha
		if key in grouped:
			item = grouped[key]
			if item["sha256"] != sha or item["source"] != str(source):
				report["conflicts"].append(
					{"file": row["name"], "key": key, "reason": "Object key collision"}
				)
			else:
				item["files"].append(row["name"])
				item["urls"] = sorted(set(item["urls"] + urls))
			return
		item = {
			"source": str(source),
			"key": key,
			"files": [row["name"]],
			"urls": urls,
			"size": size,
			"sha256": sha,
			"kind": kind,
			"state": "pending",
			"content_type": row.get("mime_type")
			or mimetypes.guess_type(row.get("file_name") or str(source))[0]
			or "application/octet-stream",
		}
		if kind == "thumbnail":
			item["content_type"] = "image/webp"
		grouped[key] = item
		report["entries"].append(item)

	for row in rows:
		url = str(row.get("file_url") or "")
		native = _drive_ancestor(row, by_name)
		if row.get("is_folder"):
			if native:
				report["folders"].append(row["name"])
			continue
		if (
			not url
			or url.startswith(("http://", "https://"))
			or row.get("file_type") == "Link"
			or row.get("content_doctype")
			or row.get("status") == "Removed"
		):
			report["skipped"].append(
				{"file": row["name"], "reason": "external, database-backed or permanently removed content"}
			)
			continue
		try:
			if url.startswith(S3_URL_PREFIX):
				raise ValueError("Source already references S3")
			source = _source_path(row, by_name, site, root, flat) if native else guarded_path(site, url)
			# Attached uploads retain their framework URL, even after Drive adopts
			# them into its logical tree. Native objects receive a flat ID key.
			framework = not native or bool(row.get("attached_to_doctype"))
			if framework:
				key = (
					prefix
					+ "/attachments/"
					+ hashlib.sha256(str(source.relative_to(site)).encode()).hexdigest()
				)
			else:
				key = prefix + "/drive/" + _safe_component(row["name"])
			# Old trash URLs describe a future restore target, not current bytes.
			urls = [url] if framework or row.get("status") not in ("Trashed", "Removed") else []
			entry(source, key, row, "attachment" if framework else "drive", urls)
			preview = guarded_path(
				site,
				str(
					root.relative_to(site) / thumbnail_prefix / (_safe_component(row["name"]) + ".thumbnail")
				),
			)
			if native and preview.is_file():
				entry(
					preview,
					prefix + "/drive/" + thumbnail_prefix + "/" + row["name"] + ".thumbnail",
					row,
					"thumbnail",
					[],
				)
		except (ValueError, OSError) as error:
			report["conflicts"].append({"file": row["name"], "reason": str(error)})
	# Framework previews are separate URLs, not File rows. Preserve their URLs
	# and bind access to the original File instead of publishing the preview.
	# Run after Suite sidecars so the same physical preview is never copied twice.
	for row in rows:
		url = str(row.get("thumbnail_url") or "")
		if not url or row.get("status") == "Removed" or url.startswith(("http://", "https://")):
			continue
		if not url.lstrip("/").startswith(("files/", "private/files/", "public/files/")):
			continue
		try:
			source = guarded_path(site, url)
			existing = next((item for item in report["entries"] if item["source"] == str(source)), None)
			if existing:
				if existing["kind"] in ("thumbnail", "derivative"):
					existing.setdefault("source_file", row["name"])
					existing["files"] = sorted(set(existing["files"] + [row["name"]]))
				existing["urls"] = sorted(set(existing["urls"] + [url]))
				continue
			key = prefix + "/derived/" + hashlib.sha256(str(source.relative_to(site)).encode()).hexdigest()
			entry(source, key, row, "derivative", [url])
			if key in grouped:
				grouped[key]["source_file"] = row["name"]
				grouped[key]["content_type"] = mimetypes.guess_type(str(source))[0] or "application/octet-stream"
		except (ValueError, OSError) as error:
			report["conflicts"].append({"file": row["name"], "url": url, "reason": str(error)})
	for folder in (site / "private/files", site / "public/files"):
		if not folder.exists():
			continue
		for path in sorted(folder.rglob("*")):
			if path.is_symlink():
				# Do not traverse symlinks, including links to other in-site blobs.
				report["conflicts"].append(
					{"source": str(path.relative_to(site)), "reason": "Unregistered symlink"}
				)
			elif path.is_file() and str(path.resolve()) not in referenced:
				report["unregistered"].append(str(path.relative_to(site)))
	return report


def _check_source(entry):
	if Path(entry["source"]).resolve() != Path(entry["source"]):
		raise ValueError("Migration source path changed: " + entry["source"])
	if digest_file(entry["source"]) != (entry["size"], entry["sha256"]):
		raise ValueError("Migration source changed: " + entry["source"])


def _remote_digest(client, bucket, key):
	body = client.get_object(Bucket=bucket, Key=key)["Body"]
	try:
		return digest_stream(body)
	finally:
		body.close()


def _remote_exists(client, bucket, key):
	try:
		client.head_object(Bucket=bucket, Key=key)
		return True
	except Exception as error:
		code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
		if code in ("404", "NoSuchKey", "NotFound"):
			return False
		raise


def copy_entries(report, client, bucket, save):
	if report["conflicts"] or report["missing"]:
		raise ValueError("Resolve missing files and conflicts before copying")
	for entry in report["entries"]:
		_check_source(entry)
		if _remote_exists(client, bucket, entry["key"]):
			if _remote_digest(client, bucket, entry["key"]) != (entry["size"], entry["sha256"]):
				raise ValueError("S3 object collision at " + entry["key"])
		else:
			client.upload_file(
				entry["source"], bucket, entry["key"], ExtraArgs={"ContentType": entry["content_type"]}
			)
			_check_source(entry)
		entry["state"] = "copied"
		save()


def verify_entries(report, client, bucket, save):
	if report["conflicts"] or report["missing"]:
		raise ValueError("Resolve missing files and conflicts before verification")
	for entry in report["entries"]:
		_check_source(entry)
		if _remote_digest(client, bucket, entry["key"]) != (entry["size"], entry["sha256"]):
			entry["state"] = "failed"
			save()
			raise ValueError("S3 content does not match SHA-256: " + entry["key"])
		entry["state"] = "verified"
		save()


def validate_run_id(run_id):
	if not isinstance(run_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run_id):
		raise ValueError("Invalid migration run ID")
	return run_id


def write_journal(path, data):
	"""Replace atomically; chmod before any bytes and fsync file and directory."""
	path = Path(path)
	temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
	fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
	try:
		with os.fdopen(fd, "w") as stream:
			json.dump(data, stream, ensure_ascii=False, sort_keys=True, default=str)
			stream.flush()
			os.fsync(stream.fileno())
		os.replace(temporary, path)
		directory = os.open(path.parent, os.O_RDONLY)
		try:
			os.fsync(directory)
		finally:
			os.close(directory)
	finally:
		temporary.unlink(missing_ok=True)


def _admin():
	import frappe

	frappe.only_for("System Manager")
	return frappe


def _rows(frappe):
	# Include *all* persistent fields, so activation/rollback cannot miss changed
	# attachment relations, permissions markers, status or folder metadata.
	# MariaDB's case-insensitive collation sorts names differently from Python;
	# the manifest builder uses a deterministic Python ordering as well.
	return sorted(
		(dict(row) for row in frappe.get_all("File", fields=["*"], order_by="name")),
		key=lambda row: row["name"],
	)


def _canonical(value):
	return json.loads(json.dumps(value, sort_keys=True, default=str))


def _settings_snapshot(settings):
	return {field: settings.get(field) for field in CONFIG_FIELDS}


def _require_maintenance(frappe):
	if not frappe.conf.get("maintenance_mode"):
		raise ValueError("Enable maintenance mode and stop scheduler/workers before activation or rollback")


@contextmanager
def _journal(run_id=None):
	frappe = _admin()
	directory = Path(frappe.get_site_path("private", "s3-migrations")).resolve()
	directory.mkdir(mode=0o700, parents=True, exist_ok=True)
	os.chmod(directory, 0o700)
	# One migration writer per site, not just per run.
	fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
	try:
		fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		path = directory / (validate_run_id(run_id) + ".json") if run_id else None
		data = json.loads(path.read_text()) if path else None
		if data and data["site"] != frappe.local.site:
			raise ValueError("Journal belongs to another site")
		yield frappe, directory, path, data
	finally:
		os.close(fd)


def _summary(data):
	return {
		"run_id": data["run_id"],
		"state": data["state"],
		"objects": len(data["entries"]),
		"bytes": sum(entry["size"] for entry in data["entries"]),
		"missing": data["missing"],
		"conflicts": data["conflicts"],
		"unregistered": data["unregistered"],
		"skipped": data["skipped"],
	}


def inventory():
	"""bench --site SITE execute kanzlei_erp.s3_migration.inventory"""
	from kanzlei_erp import object_storage as storage

	with _journal() as (frappe, directory, _, __):
		settings = storage.settings()
		if settings.enabled:
			raise ValueError("Inventory requires the existing disk backend")
		rows = _rows(frappe)
		data = build_inventory(
			rows,
			frappe.get_site_path(),
			storage.prefix(settings),
			settings.thumbnail_prefix or ".thumbnails",
			flat=bool(settings.flat),
			root_url=frappe.db.get_value("File", "Drive", "file_url")
			or "/private/files/" + (settings.root_folder or ""),
		)
		data.update(
			version=1,
			site=frappe.local.site,
			run_id=uuid.uuid4().hex,
			state="inventory",
			created=_now(),
			settings=_settings_snapshot(settings),
			epoch=storage.mutation_epoch(),
		)
		write_journal(directory / (data["run_id"] + ".json"), data)
		return _summary(data)


def _validate_target(data, settings):
	if _canonical(data["settings"]) != _canonical(_settings_snapshot(settings)):
		raise ValueError("Storage configuration changed since inventory; create a new inventory")


def copy(run_id):
	"""Upload without changing live File rows; safe to run again after interruption."""
	from kanzlei_erp import object_storage as storage

	with _journal(run_id) as (_, __, path, data):
		settings = storage.settings()
		_validate_target(data, settings)
		if data["state"] not in ("inventory", "copying", "copied", "verifying", "verified"):
			raise ValueError("Cannot copy this migration state")
		data["state"] = "copying"
		write_journal(path, data)
		copy_entries(
			data, storage.client(settings=settings), settings.bucket, lambda: write_journal(path, data)
		)
		data["state"] = "copied"
		write_journal(path, data)
		return _summary(data)


def verify(run_id):
	from kanzlei_erp import object_storage as storage

	with _journal(run_id) as (_, __, path, data):
		settings = storage.settings()
		_validate_target(data, settings)
		if data["state"] not in ("copied", "verifying", "verified"):
			raise ValueError("Complete copy before verification")
		data["state"] = "verifying"
		write_journal(path, data)
		verify_entries(
			data, storage.client(settings=settings), settings.bucket, lambda: write_journal(path, data)
		)
		data["state"] = "verified"
		write_journal(path, data)
		return _summary(data)


def _bindings_for(data):
	# A reused URL can point at several equal immutable originals; expose one
	# canonical target. Different bytes were rejected during inventory.
	bindings = {}
	for entry in data["entries"]:
		for url in entry["urls"]:
			bindings.setdefault(url, entry)
	return bindings


def _activated_urls(data):
	urls = {}
	for name in data["folders"]:
		key = (
			data["prefix"]
			+ "/drive"
			+ ("/Users" if name == "Users" else "" if name == "Drive" else "/" + _safe_component(name))
		)
		urls[name] = S3_URL_PREFIX + quote(key)
	for entry in data["entries"]:
		if entry["kind"] == "drive":
			for name in entry["files"]:
				urls[name] = S3_URL_PREFIX + quote(entry["key"])
	return urls


def activate(run_id):
	"""Atomic metadata activation. Requires a stopped-writer maintenance window."""
	from kanzlei_erp import object_storage as storage

	with _journal(run_id) as (frappe, _, path, data):
		_require_maintenance(frappe)
		settings = storage.settings()
		_validate_target(data, settings)
		if data["state"] != "verified" or any(entry["state"] != "verified" for entry in data["entries"]):
			raise ValueError("Fully verify migration before activation")
		if _canonical(_rows(frappe)) != _canonical(data["rows"]) or storage.mutation_epoch() != data["epoch"]:
			raise ValueError("File metadata changed since inventory; create a new inventory")
		# Re-read every source and remote object immediately before switching.
		verify_entries(
			data, storage.client(settings=settings), settings.bucket, lambda: write_journal(path, data)
		)
		bindings = _bindings_for(data)
		data["previous_bindings"] = {}
		for url, entry in bindings.items():
			previous = storage.get_binding(url)
			if previous and previous.get("object_key") != entry["key"]:
				raise ValueError("Existing S3 binding collision: " + url)
			data["previous_bindings"][url] = dict(previous) if previous else None
		data["target_urls"] = _activated_urls(data)
		data["state"] = "activating"
		write_journal(path, data)
		try:
			with storage.configuration_change():
				for url, entry in bindings.items():
					storage.bind(url, entry["key"], entry["size"], entry["sha256"], entry["content_type"],
						source_file=entry.get("source_file"))
				for name, url in data["target_urls"].items():
					frappe.db.set_value("File", name, "file_url", url, update_modified=False)
				settings.enabled = 1
				settings.flat = 1
				settings.root_folder = data["prefix"] + "/drive"
				settings.save(ignore_permissions=True)
				# Persist the recovery marker in the SAME DB transaction; a process
				# crash after commit cannot leave an undecidable journal state.
				frappe.db.set_single_value("Drive Disk Settings", "kanzlei_s3_migration_run", run_id)
				data["activation_epoch"] = storage.mutation_epoch()
				data["activated_rows"] = _rows(frappe)
				data["activated_settings"] = _settings_snapshot(settings)
				data["state"] = "activation_prepared"
				write_journal(path, data)
				frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			raise
		data["state"] = "active"
		data["activated"] = _now()
		write_journal(path, data)
		frappe.clear_cache()
		return _summary(data)


def recover(run_id):
	"""Resolve a crash around the DB commit using its atomic activation marker."""
	from kanzlei_erp import object_storage as storage

	with _journal(run_id) as (frappe, _, path, data):
		_require_maintenance(frappe)
		if data["state"] not in ("activating", "activation_prepared", "rolling_back"):
			return _summary(data)
		marker = frappe.db.get_single_value("Drive Disk Settings", "kanzlei_s3_migration_run", cache=False)
		if marker == run_id and data["state"] in ("activation_prepared", "rolling_back"):
			data["state"] = "active"
		elif not marker and data["state"] == "rolling_back":
			data["state"] = "rolled_back"
		elif (
			data["state"] in ("activating", "activation_prepared")
			and not marker
			and not storage.settings().enabled
		):
			data["state"] = "verified"
		else:
			raise ValueError("Migration recovery marker is inconsistent; inspect before proceeding")
		write_journal(path, data)
		return _summary(data)


def rollback(run_id):
	"""Only an unchanged activation can return to retained original disk bytes."""
	from kanzlei_erp import object_storage as storage

	with _journal(run_id) as (frappe, _, path, data):
		_require_maintenance(frappe)
		if data["state"] != "active":
			raise ValueError(
				"Only an active migration can be rolled back; recover interrupted operations first"
			)
		if (
			frappe.db.get_single_value("Drive Disk Settings", "kanzlei_s3_migration_run", cache=False)
			!= run_id
		):
			raise ValueError("Activation marker does not match this migration")
		settings = storage.settings()
		if storage.mutation_epoch() != data["activation_epoch"] or _canonical(_rows(frappe)) != _canonical(
			data["activated_rows"]
		):
			raise ValueError("Files changed after activation; use a verified reverse migration")
		if _canonical(_settings_snapshot(settings)) != _canonical(data["activated_settings"]):
			raise ValueError("Storage configuration changed after activation")
		for entry in data["entries"]:
			_check_source(entry)
		for url, entry in _bindings_for(data).items():
			current = storage.get_binding(url)
			if (
				not current
				or current.get("object_key") != entry["key"]
				or current.get("sha256") != entry["sha256"]
				or (current.get("source_file") or None) != entry.get("source_file")
			):
				raise ValueError("Object bindings changed after activation")
		data["state"] = "rolling_back"
		write_journal(path, data)
		try:
			with storage.configuration_change():
				for row in data["rows"]:
					if row["name"] in data["target_urls"]:
						frappe.db.set_value(
							"File", row["name"], "file_url", row.get("file_url"), update_modified=False
						)
				for url, previous in data["previous_bindings"].items():
					if previous is None:
						current = storage.get_binding(url)
						if current:
							frappe.db.delete("S3 File Object", {"name": current["name"]})
				for field in LAYOUT_FIELDS:
					settings.set(field, data["settings"].get(field))
				settings.save(ignore_permissions=True)
				frappe.db.set_single_value("Drive Disk Settings", "kanzlei_s3_migration_run", "")
				frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			raise
		data["state"] = "rolled_back"
		data["rolled_back"] = _now()
		write_journal(path, data)
		frappe.clear_cache()
		return _summary(data)
