"""Consistent S3 payload snapshots used while application writers are stopped.

The database/site configuration backup contains credentials; the S3 manifest never
contains them. Extra destination objects are retained for recovery, never deleted.
"""

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path

CHUNK = 1024 * 1024
TARGET_FIELDS = (
	"bucket",
	"aws_key",
	"aws_secret",
	"endpoint_url",
	"signature_version",
	"kanzlei_s3_region",
	"kanzlei_s3_addressing_style",
	"kanzlei_s3_public_endpoint",
	"kanzlei_s3_prefix",
)
OBJECT_HEADERS = (
	"ContentType",
	"ContentDisposition",
	"ContentEncoding",
	"ContentLanguage",
	"CacheControl",
	"Metadata",
)


def _storage():
	from kanzlei_erp import object_storage

	return object_storage


def _namespace(value):
	if not isinstance(value, str) or not value.strip("/") or value.startswith("/"):
		raise ValueError("S3 snapshot requires a nonempty managed prefix")
	if any(part in (".", "..", "") for part in value.rstrip("/").split("/")):
		raise ValueError("Invalid S3 snapshot prefix")
	return value.rstrip("/") + "/"


def _objects(connection, bucket, prefix):
	for page in connection.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
		for item in page.get("Contents", []):
			key = item["Key"]
			if not isinstance(key, str) or not key.startswith(prefix):
				raise ValueError("Object outside managed S3 prefix")
			yield key


def _copy_and_hash(source, destination=None):
	digest, size = hashlib.sha256(), 0
	for chunk in iter(lambda: source.read(CHUNK), b""):
		digest.update(chunk)
		size += len(chunk)
		if destination is not None:
			destination.write(chunk)
	return size, digest.hexdigest()


def _write_manifest(path, manifest):
	temporary = path / ".manifest.tmp"
	with temporary.open("x", encoding="utf-8") as handle:
		json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
		handle.write("\n")
		handle.flush()
		os.fsync(handle.fileno())
	temporary.replace(path / "manifest.json")


def export_snapshot(path):
	"""Export the entire managed namespace, failing on any missing required object."""
	storage = _storage()
	settings = storage.settings()
	path = Path(path)
	path.mkdir(parents=True, exist_ok=False, mode=0o700)
	manifest = {
		"version": 1,
		"enabled": bool(settings.enabled),
		"bucket": settings.bucket,
		"prefix": None,
		"objects": [],
	}
	if not settings.enabled:
		_write_manifest(path, manifest)
		return manifest
	prefix = manifest["prefix"] = _namespace(storage.prefix(settings=settings))
	connection = storage.client(settings=settings)
	keys = sorted(set(_objects(connection, settings.bucket, prefix)))
	required = set(storage.required_keys())
	if any(not isinstance(key, str) or not key.startswith(prefix) for key in required):
		raise ValueError("Required object outside managed S3 prefix")
	if required - set(keys):
		raise ValueError("S3 snapshot missing required objects: " + str(len(required - set(keys))))
	(path / "objects").mkdir(mode=0o700)
	for index, key in enumerate(keys):
		payload = f"objects/{index:012d}"
		response = connection.get_object(Bucket=settings.bucket, Key=key)
		with contextlib.closing(response["Body"]) as source, (path / payload).open("xb") as destination:
			size, digest = _copy_and_hash(source, destination)
		os.chmod(path / payload, 0o600)
		manifest["objects"].append(
			{
				"key": key[len(prefix) :],
				"payload": payload,
				"size": size,
				"sha256": digest,
				"headers": {field: response[field] for field in OBJECT_HEADERS if field in response},
			}
		)
	# Detect namespace changes rather than silently produce a mixed snapshot.
	if set(keys) != set(_objects(connection, settings.bucket, prefix)):
		raise ValueError("S3 namespace changed during backup; stop application writers")
	_write_manifest(path, manifest)
	return manifest


def verify_snapshot(path):
	"""Validate all local payloads before database/object restore changes anything."""
	path = Path(path).resolve()
	manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
	if manifest.get("version") != 1 or not isinstance(manifest.get("objects"), list):
		raise ValueError("Unsupported S3 snapshot manifest")
	if not manifest.get("enabled"):
		if manifest["objects"]:
			raise ValueError("Disabled S3 snapshot cannot contain objects")
		return manifest
	_namespace(manifest.get("prefix"))
	seen_keys, seen_payloads = set(), set()
	for item in manifest["objects"]:
		key, payload = item.get("key"), item.get("payload")
		if not isinstance(key, str) or not key or key.startswith("/") or key in seen_keys:
			raise ValueError("Invalid or duplicate S3 snapshot object key")
		if (
			not isinstance(payload, str)
			or not re.fullmatch(r"objects/[0-9]{12}", payload)
			or payload in seen_payloads
		):
			raise ValueError("Invalid S3 snapshot payload path")
		file = path / payload
		if file.is_symlink() or not file.resolve().is_relative_to(path) or not file.is_file():
			raise ValueError("Invalid S3 snapshot payload file")
		with file.open("rb") as source:
			size, digest = _copy_and_hash(source)
		if size != item.get("size") or digest != item.get("sha256"):
			raise ValueError("S3 snapshot checksum mismatch: " + payload)
		if not isinstance(item.get("headers", {}), dict) or set(item.get("headers", {})) - set(
			OBJECT_HEADERS
		):
			raise ValueError("Invalid S3 snapshot headers")
		seen_keys.add(key)
		seen_payloads.add(payload)
	return manifest


def _target_settings(target, manifest):
	if isinstance(target, str):
		file = Path(target)
		if file.stat().st_mode & 0o077:
			raise ValueError("S3 restore target file must have permissions 600")
		target = json.loads(file.read_text(encoding="utf-8"))
	if (
		not isinstance(target, dict)
		or set(target) != set(TARGET_FIELDS)
		or any(not target.get(field) for field in TARGET_FIELDS)
	):
		raise ValueError("Explicit S3 restore target must contain every connection field")
	if _namespace(target["kanzlei_s3_prefix"]) != manifest["prefix"]:
		raise ValueError("S3 restore target prefix must match database object bindings")
	# A separate detached document prevents reading production secrets for a target.
	from frappe import _dict

	return _dict({**target, "enabled": 1})


def restore_snapshot(path, target=None):
	"""Restore and reread objects before enabling the recovered application.

	An explicit target replaces every connection field; its prefix must match the
	recovered DB's keys. Extra objects are deliberately retained for later recovery.
	"""
	manifest = verify_snapshot(path)
	if not manifest["enabled"]:
		return manifest
	storage = _storage()
	current = storage.settings()
	settings = _target_settings(target, manifest) if target is not None else current
	if _namespace(storage.prefix(settings=settings)) != manifest["prefix"]:
		raise ValueError("Recovered S3 prefix differs from snapshot")
	if target is None and settings.bucket != manifest["bucket"]:
		raise ValueError("Recovered S3 bucket differs from snapshot")
	required = set(storage.required_keys())
	if required - {manifest["prefix"] + item["key"] for item in manifest["objects"]}:
		raise ValueError("S3 snapshot missing objects required by recovered database")
	connection = storage.client(settings=settings)
	for item in manifest["objects"]:
		key = manifest["prefix"] + item["key"]
		with (Path(path) / item["payload"]).open("rb") as source:
			connection.upload_fileobj(source, settings.bucket, key, ExtraArgs=item.get("headers", {}))
		response = connection.get_object(Bucket=settings.bucket, Key=key)
		with contextlib.closing(response["Body"]) as source:
			size, digest = _copy_and_hash(source)
		if size != item["size"] or digest != item["sha256"]:
			raise ValueError("Restored S3 object checksum mismatch")
	if target is not None:
		import frappe

		with storage.configuration_change():
			current.update({field: settings.get(field) for field in TARGET_FIELDS})
			current.enabled = 1
			current.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()
	return manifest
