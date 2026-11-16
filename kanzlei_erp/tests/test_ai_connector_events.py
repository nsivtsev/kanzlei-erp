"""Real database acceptance for the unattended Drive source feed."""

import frappe
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp.tests import test_ai_connector as connector_fixtures


class TestAIConnectorEvents(FrappeTestCase):
	make_file = connector_fixtures.TestAIConnector.make_file
	make_drive_root = connector_fixtures.TestAIConnector.make_drive_root
	make_drive_child = connector_fixtures.TestAIConnector.make_drive_child
	customer_fixture = connector_fixtures.TestAIConnector.customer_fixture
	api = connector_fixtures.TestAIConnector.api
	call = connector_fixtures.TestAIConnector.call

	STORAGE_FIELDS = ("enabled", "flat", "bucket", "root_folder", "kanzlei_s3_prefix", "endpoint_url")

	@classmethod
	def setUpClass(cls):
		import json
		import os
		import tempfile

		from kanzlei_erp import object_storage

		super().setUpClass()
		frappe.db.rollback()
		settings = object_storage.settings()
		cls.original_storage = {field: settings.get(field) for field in cls.STORAGE_FIELDS}
		fd, cls.class_recovery_path = tempfile.mkstemp(
			prefix="kanzlei-ai-feed-class-settings-", suffix=".json"
		)
		with os.fdopen(fd, "w") as recovery:
			json.dump(cls.original_storage, recovery)
			recovery.flush()
			os.fsync(recovery.fileno())
		cls.addClassCleanup(cls.verify_class_settings)

	@classmethod
	def verify_class_settings(cls):
		from pathlib import Path

		frappe.db.rollback()
		for field, expected in cls.original_storage.items():
			actual = frappe.db.get_single_value("Drive Disk Settings", field, cache=False)
			if str(actual or "") != str(expected or ""):
				raise AssertionError("Storage settings need recovery from " + cls.class_recovery_path)
		Path(cls.class_recovery_path).unlink()

	def setUp(self):
		import hashlib
		import json
		import os
		import tempfile

		from kanzlei_erp import object_storage
		from kanzlei_erp.tests.test_object_storage import TestObjectStorage

		# CompatFrappeTestCase only rolls back after the entire class.
		frappe.db.rollback()
		settings = object_storage.settings()
		self.previous_storage = {field: settings.get(field) for field in self.STORAGE_FIELDS}
		for field in self.STORAGE_FIELDS:
			self.assertEqual(str(self.previous_storage[field] or ""), str(self.original_storage[field] or ""))
		fd, self.recovery_path = tempfile.mkstemp(prefix="kanzlei-ai-feed-settings-", suffix=".json")
		with os.fdopen(fd, "w") as recovery:
			json.dump(self.previous_storage, recovery)
			recovery.flush()
			os.fsync(recovery.fileno())
		TestObjectStorage.setUp(self)
		connector_fixtures.TestAIConnector.setUp(self)
		self.binding_names = {self.storage.get_binding(self.file.file_url).name}
		self.key = "fixture-ingestion-key-" + frappe.generate_hash(length=32)
		self.config["ingestion"] = {
			"enabled": True,
			"user": self.reader.name,
			"client_ids": [self.customer.name],
			"generation": "fixture-1",
			"key_sha256": hashlib.sha256(self.key.encode()).hexdigest(),
		}

	def tearDown(self):
		from pathlib import Path

		frappe.set_user("Administrator")
		if getattr(self, "committed", False):
			frappe.db.rollback()
		try:
			for name in self.file_names:
				if frappe.db.exists("File", name):
					Path(frappe.get_doc("File", name).get_full_path()).unlink(missing_ok=True)
			if getattr(self, "committed", False):
				frappe.db.delete("Sessions", {"sid": self.sid})
				frappe.db.delete("AI Source Event", {"client_id": self.customer.name})
				frappe.db.delete("S3 File Object", {"name": ["in", list(self.binding_names)]})
				frappe.db.delete("S3 File Object", {"source_file": ["in", list(self.file_names)]})
				frappe.db.delete("File", {"name": ["in", [self.root.name, *self.file_names]]})
				for customer in (self.customer, self.other):
					frappe.delete_doc("Customer", customer.name, ignore_permissions=True, force=True)
				frappe.delete_doc("User", self.reader.name, ignore_permissions=True, force=True)
		except Exception:
			frappe.db.rollback()
			raise
		finally:
			if getattr(self, "committed", False):
				with self.storage.configuration_change():
					settings = self.storage.settings()
					for field in self.STORAGE_FIELDS:
						settings.set(field, self.original_storage[field])
					settings.save(ignore_permissions=True)
				frappe.db.commit()
			else:
				frappe.db.rollback()
			for field, expected in self.original_storage.items():
				actual = frappe.db.get_single_value("Drive Disk Settings", field, cache=False)
				self.assertEqual(str(actual or ""), str(expected or ""), self.recovery_path)
			self.storage.cleanup_temporary_files()
		super().tearDown()
		Path(self.recovery_path).unlink()

	def test_inventory_and_changes_capture_mutation(self):
		page = self.call("inventory", limit=10)
		self.assertIsInstance(page, dict)
		self.assertEqual([item["source_id"] for item in page["items"]], ["file:" + self.file.name])
		frappe.set_user("Administrator")
		self.file.db_set("file_name", "changed.pdf")
		changes = self.call("changes", cursor=page["changes_cursor"], limit=10)
		self.assertIn("file:" + self.file.name, [item["source_id"] for item in changes["items"]])

	def test_rollback_duplicates_trash_restore_and_delete(self):
		page = self.call("inventory")
		cursor = page["changes_cursor"]
		frappe.set_user("Administrator")
		frappe.db.savepoint("feed_test")
		self.file.db_set("file_name", "rolled-back.pdf")
		frappe.db.rollback(save_point="feed_test")
		self.assertEqual(self.call("changes", cursor=cursor)["items"], [])
		self.file.db_set("file_name", "renamed.pdf")
		self.file.db_set("file_name", "renamed-again.pdf")
		items = self.call("changes", cursor=cursor)["items"]
		self.assertGreaterEqual(len(items), 2)
		self.assertTrue(all(item["operation"] == "upsert" for item in items))
		cursor = self.call("changes", cursor=cursor)["next_cursor"]
		self.root.db_set("status", "Trashed")
		items = self.call("changes", cursor=cursor)["items"]
		self.assertTrue(
			any(
				item["source_id"] == "file:" + self.file.name and item["operation"] == "delete"
				for item in items
			)
		)
		cursor = self.call("changes", cursor=cursor)["next_cursor"]
		self.root.db_set("status", "Active")
		items = self.call("changes", cursor=cursor)["items"]
		self.assertTrue(
			any(
				item["source_id"] == "file:" + self.file.name and item["operation"] == "upsert"
				for item in items
			)
		)
		cursor = self.call("changes", cursor=cursor)["next_cursor"]
		self.committed = True
		frappe.db.commit()
		for attempt in range(3):
			try:
				frappe.delete_doc("File", self.file.name, ignore_permissions=True, force=True)
				break
			except frappe.QueryDeadlockError:
				frappe.db.rollback()
				if attempt == 2:
					raise
		items = self.call("changes", cursor=cursor)["items"]
		self.assertTrue(
			any(
				item["source_id"] == "file:" + self.file.name and item["operation"] == "delete"
				for item in items
			)
		)

	def test_ingestion_requires_private_identity_and_binds_cursor(self):
		page = self.call("inventory")
		self.assertEqual(self.call("inventory", service=False).status_code, 401)
		self.assertEqual(self.call("inventory", user="Administrator").status_code, 400)
		self.config["ingestion"]["generation"] = "fixture-2"
		self.assertEqual(self.call("changes", cursor=page["changes_cursor"]).status_code, 400)
		self.config["ingestion"]["enabled"] = False
		self.assertEqual(self.call("inventory").status_code, 503)

	def test_inventory_watermark_uses_existing_snapshot(self):
		from concurrent.futures import ThreadPoolExecutor

		from kanzlei_erp.ai_connector import events

		self.committed = True
		frappe.db.commit()
		page = self.call("inventory")
		site, name = frappe.local.site, self.file.name

		def change():
			frappe.init(site)
			frappe.connect()
			try:
				events.record_ids([name])
				frappe.db.commit()
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=1) as pool:
			pool.submit(change).result(timeout=15)
		old_snapshot = self.call("inventory")
		self.assertEqual(old_snapshot["changes_cursor"], page["changes_cursor"])
		frappe.db.commit()
		self.assertTrue(self.call("changes", cursor=old_snapshot["changes_cursor"])["items"])

	def test_storage_binding_replacement_invalidates_source(self):
		import hashlib

		page = self.call("inventory")
		old_revision = page["items"][0]["revision"]
		frappe.set_user("Administrator")
		self.s3.objects["unit/replacement-feed"] = b"replacement"
		self.storage.replace_content(
			self.file, "unit/replacement-feed", 11, hashlib.sha256(b"replacement").hexdigest()
		)
		items = self.call("changes", cursor=page["changes_cursor"])["items"]
		self.assertTrue(
			any(
				item["operation"] == "upsert" and item["manifest"]["revision"] != old_revision
				for item in items
			)
		)

	def test_legacy_local_file_is_rejected_and_client_revocation_is_forbidden(self):
		page = self.call("inventory")
		frappe.set_user("Administrator")
		binding = self.storage.get_binding(self.file.file_url)
		frappe.db.delete("S3 File Object", {"name": binding.name})
		self.assertEqual(self.call("inventory").status_code, 409)
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		self.assertEqual(self.call("changes", cursor=page["changes_cursor"]).status_code, 403)

	def test_reversed_mutation_completion_keeps_both_events_after_watermark(self):
		from concurrent.futures import ThreadPoolExecutor
		from threading import Event

		from kanzlei_erp.ai_connector import events

		self.committed = True
		frappe.db.commit()
		cursor = self.call("inventory")["changes_cursor"]
		frappe.db.commit()
		site, client, name = frappe.local.site, self.customer.name, self.file.name
		first_changed, second_committed = Event(), Event()

		def slow():
			frappe.init(site)
			frappe.connect()
			try:
				# This transaction starts first, but allocates its sequence only after
				# its mutation, and after the other transaction has committed.
				frappe.db.sql("UPDATE `tabCustomer` SET customer_name=customer_name WHERE name=%s", client)
				first_changed.set()
				if not second_committed.wait(10):
					raise TimeoutError("second transaction did not commit")
				events.record_ids([name])
				frappe.db.commit()
			finally:
				frappe.destroy()

		def fast():
			if not first_changed.wait(10):
				raise TimeoutError("first transaction did not start")
			frappe.init(site)
			frappe.connect()
			try:
				events.record_ids([name])
				frappe.db.commit()
			finally:
				second_committed.set()
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=2) as pool:
			first, second = pool.submit(slow), pool.submit(fast)
			first.result(timeout=20)
			second.result(timeout=20)
		items = self.call("changes", cursor=cursor)["items"]
		self.assertEqual([item["source_id"] for item in items], ["file:" + name, "file:" + name])

	def test_allocated_sequence_blocks_later_commit_until_rollback(self):
		from concurrent.futures import ThreadPoolExecutor
		from threading import Event

		from kanzlei_erp.ai_connector import events

		self.committed = True
		frappe.db.commit()
		cursor = self.call("inventory")["changes_cursor"]
		frappe.db.commit()
		site, name = frappe.local.site, self.file.name
		allocated, release, attempting, committed = Event(), Event(), Event(), Event()

		def first():
			frappe.init(site)
			frappe.connect()
			try:
				events.record_ids([name])
				allocated.set()
				if not release.wait(10):
					raise TimeoutError("release not signaled")
				frappe.db.rollback()
			finally:
				frappe.destroy()

		def second():
			if not allocated.wait(10):
				raise TimeoutError("first allocation missing")
			frappe.init(site)
			frappe.connect()
			try:
				attempting.set()
				events.record_ids([name])
				frappe.db.commit()
				committed.set()
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=2) as pool:
			one, two = pool.submit(first), pool.submit(second)
			try:
				self.assertTrue(attempting.wait(10))
				self.assertFalse(committed.wait(0.3))
				self.assertEqual(self.call("changes", cursor=cursor)["items"], [])
			finally:
				release.set()
			one.result(timeout=15)
			two.result(timeout=15)
		frappe.db.commit()
		self.assertEqual(len(self.call("changes", cursor=cursor)["items"]), 1)

	def test_pagination_replays_earlier_insert_and_eligibility_loss(self):
		import hashlib

		from kanzlei_erp.ai_connector import events

		page = self.call("inventory", limit=1)
		watermark = page["changes_cursor"]
		while not page["items"]:
			self.assertTrue(page["next_cursor"])
			page = self.call("inventory", cursor=page["next_cursor"], limit=1)
		continuation = page["next_cursor"]
		self.assertTrue(continuation)
		self.committed = True
		frappe.db.commit()
		frappe.set_user("Administrator")
		name = "0000000000-" + frappe.generate_hash(length=16)
		self.assertLess(name, self.file.name)
		self.assertFalse(frappe.db.exists("File", name))
		url = "/private/files/feed-" + frappe.generate_hash(length=12) + ".pdf"
		key = "unit/insert-" + frappe.generate_hash(length=12)
		self.s3.objects[key] = b"inserted"
		binding = self.storage.bind(url, key, 8, hashlib.sha256(b"inserted").hexdigest())
		self.binding_names.add(binding.name)
		file = frappe.get_doc(
			{
				"doctype": "File",
				"name": name,
				"file_name": "inserted.pdf",
				"file_url": url,
				"folder": self.root.name,
				"is_private": 1,
				"is_folder": 0,
				"file_size": 8,
				"file_type": "PDF",
				"status": "Active",
				"kanzlei_drive_customer": self.customer.name,
			}
		)
		file.db_insert()
		self.file_names.add(name)
		self.file.db_set("status", "Trashed")
		frappe.db.commit()
		page = self.call("inventory", cursor=continuation, limit=1)
		self.assertNotIn("file:" + name, [item["source_id"] for item in page["items"]])
		items = []
		cursor = watermark
		while True:
			page = self.call("changes", cursor=cursor, limit=1)
			items.extend(page["items"])
			if page["next_cursor"] == cursor:
				break
			cursor = page["next_cursor"]
		self.assertTrue(any(i["source_id"] == "file:" + name and i["operation"] == "upsert" for i in items))
		self.assertTrue(
			any(i["source_id"] == "file:" + self.file.name and i["operation"] == "delete" for i in items)
		)

	def test_capture_has_no_storage_or_manifest_calls(self):
		from unittest.mock import patch

		from kanzlei_erp.ai_connector import events

		with (
			patch("kanzlei_erp.object_storage.client", side_effect=AssertionError("network")),
			patch("kanzlei_erp.ai_connector.policy.manifest", side_effect=AssertionError("manifest")),
			patch("kanzlei_erp.object_storage.open_file", side_effect=AssertionError("materialize")),
		):
			events.record_ids([self.file.name])
			self.file.db_set("file_name", "capture-without-network.pdf")

	def test_cursor_tampering_and_rpc_dispatch(self):
		from unittest.mock import patch

		from frappe.handler import execute_cmd
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		page = self.call("inventory", limit=1)
		self.assertEqual(self.call("changes", cursor=page["changes_cursor"] + "tampered").status_code, 400)
		method = "kanzlei_erp.ai_connector.api.inventory"
		request = Request(
			EnvironBuilder(method="POST", headers={"X-Kanzlei-AI-Service-Key": self.key}).get_environ()
		)
		with (
			patch.object(frappe.local, "request", request, create=True),
			patch.object(frappe.local, "form_dict", frappe._dict(cmd=method)),
		):
			result = execute_cmd(method)
		self.assertIsInstance(result, dict)
		self.assertIn("changes_cursor", result)

	def test_db_set_commit_keeps_file_and_customer_events_atomic(self):
		self.committed = True
		frappe.db.commit()
		cursor = self.call("inventory")["changes_cursor"]
		frappe.set_user("Administrator")
		self.file.db_set("file_name", "committed.pdf", True, False, True)
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("File", self.file.name, "file_name"), "committed.pdf")
		changes = self.call("changes", cursor=cursor)
		self.assertTrue(any(item["source_id"] == "file:" + self.file.name for item in changes["items"]))
		cursor = changes["next_cursor"]
		self.customer.db_set("kanzlei_drive_folder", None, True, False, True)
		frappe.db.rollback()
		self.assertIsNone(frappe.db.get_value("Customer", self.customer.name, "kanzlei_drive_folder"))
		changes = self.call("changes", cursor=cursor)
		self.assertTrue(
			any(
				item["source_id"] == "file:" + self.file.name and item["operation"] == "delete"
				for item in changes["items"]
			)
		)

	def test_db_set_commit_rolls_back_when_capture_fails(self):
		from unittest.mock import patch

		self.committed = True
		frappe.db.commit()
		original = frappe.db.get_value("File", self.file.name, "file_name")
		root = self.root.name
		frappe.set_user("Administrator")
		with patch(
			"kanzlei_erp.ai_connector.events.file_changed", side_effect=RuntimeError("capture failed")
		):
			with self.assertRaises(RuntimeError):
				self.file.db_set("file_name", "must-rollback.pdf", commit=True)
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value("File", self.file.name, "file_name"), original)
		with patch("kanzlei_erp.ai_connector.events.record_ids", side_effect=RuntimeError("capture failed")):
			with self.assertRaises(RuntimeError):
				self.customer.db_set("kanzlei_drive_folder", None, commit=True)
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value("Customer", self.customer.name, "kanzlei_drive_folder"), root)

	def test_file_db_set_without_suite_customer_column(self):
		from unittest.mock import patch

		get_value = frappe.db.get_value

		def guarded(doctype, name, fieldname="name", *args, **kwargs):
			if doctype == "File" and fieldname == "kanzlei_drive_customer":
				raise AssertionError("missing field selected")
			return get_value(doctype, name, fieldname, *args, **kwargs)

		with (
			patch.object(frappe.db, "has_column", return_value=False),
			patch("frappe.get_installed_apps", return_value=["frappe", "erpnext", "kanzlei_erp"]),
			patch.object(frappe.db, "get_value", side_effect=guarded),
		):
			self.file.db_set("file_name", "ordinary-file.pdf")
		self.assertEqual(frappe.db.get_value("File", self.file.name, "file_name"), "ordinary-file.pdf")

	def test_encoded_and_absolute_binding_aliases_emit_invalidation(self):
		import hashlib
		from unittest.mock import patch

		frappe.set_user("Administrator")
		original_url = self.file.file_url
		alias = original_url.replace("synthetic", "%73ynthetic")
		if alias == original_url:
			alias = original_url.replace("/private/", "/%70rivate/")
		self.assertNotEqual(alias, original_url)
		with patch("frappe.utils.get_url", return_value="http://fixture.invalid"):
			for value in (alias, "http://fixture.invalid" + alias):
				frappe.db.set_value("File", self.file.name, "file_url", value, update_modified=False)
				file = frappe.get_doc("File", self.file.name)
				page = self.call("inventory")
				before = page["items"][0]["revision"]
				key = "unit/replacement-" + frappe.generate_hash(length=16)
				self.s3.objects[key] = b"replacement"
				self.storage.replace_content(file, key, 11, hashlib.sha256(b"replacement").hexdigest())
				changes = self.call("changes", cursor=page["changes_cursor"])["items"]
				self.assertTrue(
					any(
						item["source_id"] == "file:" + self.file.name
						and item["operation"] == "upsert"
						and item["manifest"]["revision"] != before
						for item in changes
					)
				)

	def test_parent_capture_includes_child_moved_after_old_snapshot(self):
		from concurrent.futures import ThreadPoolExecutor

		from kanzlei_erp.ai_connector import events
		from kanzlei_erp.object_storage import mark_mutation

		frappe.set_user("Administrator")
		child = self.make_drive_child(self.root, "late-child.pdf", b"late child")
		self.binding_names.add(self.storage.get_binding(child.file_url).name)
		frappe.db.set_value("File", child.name, "folder", "outside-fixture-root")
		self.committed = True
		frappe.db.commit()
		try:
			previous_isolation = frappe.db.sql("SELECT @@SESSION.innodb_snapshot_isolation")[0][0]
			frappe.db.sql("SET SESSION innodb_snapshot_isolation=OFF")
		except Exception:
			self.skipTest("MariaDB does not expose innodb_snapshot_isolation")
		try:
			frappe.db.sql("SELECT name FROM `tabFile` WHERE name=%s", self.root.name)
			before = frappe.db.sql("SELECT `sequence` FROM `tabAI Source State` WHERE name='drive'")[0][0]
			site, child_name, parent_name = frappe.local.site, child.name, self.root.name

			def move():
				frappe.init(site)
				frappe.connect()
				try:
					frappe.db.sql("UPDATE `tabFile` SET folder=%s WHERE name=%s", (parent_name, child_name))
					mark_mutation()
					events.record_ids([child_name])
					frappe.db.commit()
				finally:
					frappe.destroy()

			with ThreadPoolExecutor(max_workers=1) as pool:
				pool.submit(move).result(timeout=15)
			try:
				self.root.db_set("status", "Trashed")
				rows = frappe.db.sql(
					"SELECT source_id FROM `tabAI Source Event` WHERE source_id=%s AND client_id=%s AND `sequence` > %s",
					("file:" + child_name, self.customer.name, before),
				)
				self.assertTrue(rows, "parent change omitted moved descendant")
				frappe.db.commit()
			except frappe.QueryDeadlockError:
				frappe.db.rollback()  # Retrying the full transaction is safe.
		finally:
			frappe.db.rollback()
			frappe.db.sql("SET SESSION innodb_snapshot_isolation=%s", previous_isolation)

	def test_00_fixture_uncommitted_settings(self):
		self.assertEqual(self.storage.settings().bucket, "unit-test")

	def test_01_fixture_prior_noncommit_does_not_leak_into_commit(self):
		for field in self.STORAGE_FIELDS:
			self.assertEqual(str(self.previous_storage[field] or ""), str(self.original_storage[field] or ""))
		self.committed = True
		frappe.db.commit()
