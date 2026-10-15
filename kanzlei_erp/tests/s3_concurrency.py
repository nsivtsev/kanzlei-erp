"""Opt-in MariaDB/MinIO race regression, restricted to s3-test.localhost.

The actual cleanup worker starts its repeatable-read snapshot before a concurrent
alias commits. A negative control demonstrates deletion with historical stale
reads. The fixed worker must preserve bytes, including after a snapshot conflict.
"""

import json
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import frappe

from kanzlei_erp import object_storage as storage


def _guard():
	if frappe.local.site != "s3-test.localhost" or not frappe.conf.allow_tests:
		raise ValueError("Concurrency rehearsal requires the disposable S3 test site")
	config = storage.settings()
	if (
		not config.enabled
		or config.bucket != "stb-golke-test"
		or not storage.prefix().startswith("tests/s3-test.localhost/")
	):
		raise ValueError("Concurrency rehearsal requires the isolated MinIO test namespace")
	if frappe.db.count("S3 File Deletion"):
		raise ValueError("Finish existing deletion work before the concurrency rehearsal")
	# The real reaper also sweeps aged intents. Refuse rather than touch another
	# fixture's old intent; this margin exceeds the entire rehearsal deadline.
	for path in Path(frappe.get_site_path("private", "s3-pending")).glob("*.json"):
		if json.loads(path.read_text())["created"] < time.time() - 3500:
			raise ValueError("Resolve old write intents before the concurrency rehearsal")
	frappe.set_user("Administrator")


def _scenario(stale_read_control=False):
	_guard()
	site = frappe.local.site
	sites_path = str(Path(frappe.local.sites_path).resolve())
	token = uuid.uuid4().hex
	body = ("S3 concurrency " + token).encode()
	source = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": "concurrency-" + token + ".txt",
			"is_private": 1,
			"content": body,
		}
	).insert()
	url = source.file_url
	key = storage.get_binding(url).object_key
	# Suite retains Removed rows. The alias is the only live reference that
	# could prevent the pending deletion from removing these fixture bytes.
	frappe.db.set_value("File", source.name, "status", "Removed")
	storage.schedule_delete(source)
	frappe.db.commit()
	frappe.db.rollback()
	snapshot_ready, alias_ready = threading.Event(), threading.Event()
	lock_attempted, commit_alias = threading.Event(), threading.Event()
	worker_done = threading.Event()
	result, failures, threads = {}, [], []
	original_lock, original_refs = storage.lock_object, storage.has_references

	def connected(action):
		try:
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			frappe.set_user("Administrator")
			action()
		except BaseException as error:
			failures.append(error)
		finally:
			try:
				frappe.db.rollback()
			finally:
				frappe.destroy()

	def wait(event, description):
		if not event.wait(15):
			raise AssertionError("Timed out waiting for " + description)

	def alias_writer():
		wait(snapshot_ready, "cleanup snapshot")
		alias = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "concurrency-alias-" + token + ".txt",
				"is_private": 1,
				"file_url": url,
			}
		)
		alias.flags.copy_from_existing_file = True
		alias.insert()
		result["alias"] = alias.name
		alias_ready.set()
		wait(commit_alias, "alias commit permission")
		frappe.db.commit()

	def cleanup_worker():
		# Establish a real consistent snapshot before the alias transaction.
		result["initial_alias_count"] = frappe.db.sql(
			"SELECT COUNT(*) FROM tabFile WHERE file_url=%s AND status!='Removed'", (url,)
		)[0][0]
		snapshot_ready.set()
		wait(alias_ready, "uncommitted alias")
		try:
			storage.retry_deletions()
		except frappe.QueryDeadlockError:
			# MariaDB snapshot isolation may reject the current read. It must do
			# so before deleting anything; retry on a fresh transaction is safe.
			if stale_read_control:
				raise
			frappe.db.rollback()
			response = storage.client().get_object(Bucket=storage.settings().bucket, Key=key)
			with response["Body"] as stream:
				if stream.read() != body:
					raise AssertionError("Snapshot conflict removed stored bytes")
			if not frappe.db.exists("S3 File Deletion", {"object_key": key}):
				raise AssertionError("Snapshot conflict lost the cleanup retry intent")
			result["snapshot_conflict_retried"] = True
			storage.retry_deletions()
		frappe.db.commit()
		worker_done.set()

	def traced_lock(object_key):
		if threading.current_thread().name == "s3-concurrency-reaper" and object_key == key:
			lock_attempted.set()
		return original_lock(object_key)

	def reference_read(object_key, current=False):
		if stale_read_control and threading.current_thread().name == "s3-concurrency-reaper":
			current = False
		return original_refs(object_key, current=current)

	try:
		with (
			patch.object(storage, "lock_object", traced_lock),
			patch.object(storage, "has_references", reference_read),
		):
			for name, action in (
				("s3-concurrency-reaper", cleanup_worker),
				("s3-concurrency-alias", alias_writer),
			):
				thread = threading.Thread(name=name, target=connected, args=(action,), daemon=True)
				threads.append(thread)
				thread.start()
			wait(lock_attempted, "cleanup advisory lock attempt")
			if worker_done.wait(0.2):
				raise AssertionError("Cleanup did not wait for the alias advisory lock")
			commit_alias.set()
			for thread in threads:
				thread.join(20)
			if any(thread.is_alive() for thread in threads):
				raise AssertionError("Concurrency rehearsal exceeded its deadline")
			if failures:
				raise AssertionError(
					"Concurrency worker failed: " + type(failures[0]).__name__
				) from failures[0]
		frappe.db.rollback()
		if result["initial_alias_count"] != 0:
			raise AssertionError("The alias already existed in the cleanup snapshot")
		retained = bool(storage.get_binding(url) and frappe.db.exists("File", result["alias"]))
		if retained:
			response = storage.client().get_object(Bucket=storage.settings().bucket, Key=key)
			with response["Body"] as stream:
				retained = stream.read() == body
		return {
			"retained": retained,
			"snapshot_conflict_retried": result.get("snapshot_conflict_retried", False),
		}
	finally:
		commit_alias.set()
		for thread in threads:
			thread.join(20)
		if any(thread.is_alive() for thread in threads):
			raise AssertionError("Workers are still running; fixture cleanup was intentionally skipped")
		frappe.db.rollback()
		storage.client().delete_object(Bucket=storage.settings().bucket, Key=key)
		# Direct fixture-only cleanup creates no additional deletion intents.
		frappe.db.delete("File", {"name": source.name})
		if result.get("alias"):
			frappe.db.delete("File", {"name": result["alias"]})
		frappe.db.delete("S3 File Object", {"object_key": key})
		frappe.db.delete("S3 File Deletion", {"object_key": key})
		frappe.db.commit()
		storage.cleanup_temporary_files()


def run():
	"""Reproduce the stale-read race, then prove the actual fixed worker is safe."""
	if _scenario(stale_read_control=True)["retained"]:
		raise AssertionError("Negative control did not reproduce the historical cleanup race")
	fixed = _scenario()
	if not fixed["retained"]:
		raise AssertionError("Cleanup deleted a committed alias's stored bytes")
	return {
		"state": "passed",
		"negative_control_detected": True,
		"alias_survives_cleanup": True,
		"independent_connections": 2,
		"snapshot_conflict_retried": fixed["snapshot_conflict_retried"],
	}
