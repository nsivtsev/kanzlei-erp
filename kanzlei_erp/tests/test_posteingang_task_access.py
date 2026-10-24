"""AP-07 real permission/assignment/copy tests; synthetic data only."""

import multiprocessing
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestGuardFastPaths(TestCase):
	def test_unrelated_query_never_scans_tasks(self):
		from kanzlei_erp import posteingang_task_access as access

		with (
			patch.object(access, "ready", return_value=True),
			patch.object(access, "_hidden_tasks", side_effect=AssertionError("unrelated Task scan")),
		):
			self.assertIsNone(access.required_query("Customer", "synthetic@example.invalid"))

	def test_stale_source_lock_has_safe_permission_error(self):
		from kanzlei_erp import posteingang_task_access as access

		with patch.object(
			frappe.db, "sql", side_effect=RuntimeError(1020, "synthetic MariaDB record changed")
		):
			with self.assertRaises(frappe.PermissionError):
				access._lock_sources(["synthetic-letter"])


def _notification_worker(site, sites_path, queue_name):
	frappe.init(site, sites_path=sites_path, force=True)
	frappe.connect()
	try:
		from frappe.utils.background_jobs import generate_qname, get_redis_conn
		from rq import Queue, Worker

		queue = Queue(generate_qname(queue_name), connection=get_redis_conn())
		# Only external email delivery is intercepted. RQ, worker isolation,
		# DB commits, Notification Log and current permission checks are real.
		with patch("frappe.sendmail"):
			Worker([queue], connection=queue.connection).work(
				burst=True, with_scheduler=False, logging_level="WARNING"
			)
	finally:
		frappe.destroy()


def _quarantine_letter(site, sites_path, name):
	frappe.init(site, sites_path=sites_path, force=True)
	frappe.connect()
	try:
		# Test-side fixture mutation: root's confirmation concurrency suite tests
		# the complete correction service. This isolates Task's old-snapshot guard.
		frappe.db.set_value("Incoming Letter", name, {"status": "Cancelled", "correction_quarantined": 1})
		frappe.db.commit()
	finally:
		frappe.destroy()


class TestPosteingangTaskAccess(PosteingangTestCase):
	def setUp(self):
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		from kanzlei_erp import posteingang_task_access as access

		self.access = access
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		self.doc = self.confirm_fixture(self.letter())
		self.action_id = str(uuid4())
		from kanzlei_erp.posteingang_jobs import _insert

		_insert(
			"Incoming Letter Action",
			{
				"parent": self.doc.name,
				"parenttype": "Incoming Letter",
				"parentfield": "actions",
				"action_id": self.action_id,
				"title": "Synthetic secret title",
				"description": "Synthetic secret description",
				"decision": "accept",
			},
		)
		frappe.set_user(self.operator.name)
		with self.access.creating_task(self.doc.name, self.action_id):
			self.task = frappe.get_doc(
				{
					"doctype": "Task",
					"subject": "Synthetic secret title",
					"description": "Synthetic secret description",
					"kanzlei_customer": self.customer.name,
					"kanzlei_incoming_letter": self.doc.name,
					"kanzlei_incoming_action_id": self.action_id,
					"kanzlei_incoming_action_key": self.access.action_key(self.doc.name, self.action_id),
				}
			).insert()

	def test_provenance_insert_requires_narrow_server_context(self):
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": "Task",
					"subject": "forged",
					"kanzlei_customer": self.customer.name,
					"kanzlei_incoming_letter": self.doc.name,
					"kanzlei_incoming_action_id": str(uuid4()),
					"kanzlei_incoming_action_key": "a" * 64,
				}
			).insert()

	def test_source_task_cannot_be_used_as_template_or_template_copy(self):
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{"doctype": "Task", "subject": "copied confidential title", "template_task": self.task.name}
			).insert()
		with self.assertRaises(frappe.PermissionError):
			self.task.db_set("is_template", 1)
		self.task.is_template = 1
		with self.assertRaises(frappe.PermissionError):
			self.task.save()

	def test_loaded_task_snapshot_cannot_write_after_concurrent_quarantine(self):
		frappe.db.commit()
		stale = frappe.get_doc("Task", self.task.name)
		context = multiprocessing.get_context("spawn")
		process = context.Process(
			target=_quarantine_letter,
			args=(frappe.local.site, str(Path(frappe.local.sites_path).resolve()), self.doc.name),
		)
		process.start()
		process.join(timeout=20)
		if process.is_alive():
			process.kill()
			process.join(timeout=5)
		self.assertEqual(process.exitcode, 0)
		stale.subject = "Late update must be discarded"
		with self.assertRaises(frappe.PermissionError):
			stale.save()
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Task", stale.name, "subject"), "Synthetic secret title")

	def test_child_export_hook_uses_framework_signature(self):
		self.assertIsNone(
			self.access.child_export_permission(
				doctype="Mandant Identifier",
				parent_doctype="Customer",
				parent=self.customer.name,
				parentfield="kanzlei_identifiers",
				user=self.operator.name,
			)
		)
		self.assertTrue(
			self.access.child_export_permission(
				doctype="Task Depends On",
				parent_doctype="Task",
				parent=self.task.name,
				parentfield="depends_on",
				user=self.operator.name,
			)
		)

	def test_provenance_and_customer_cannot_be_removed_or_db_set(self):
		for field in (*self.access.PROVENANCE, "kanzlei_customer"):
			with self.subTest(field=field):
				self.task.reload()
				with self.assertRaises(frappe.PermissionError):
					self.task.db_set(field, None)
				self.task.set(field, None)
				with self.assertRaises(frappe.PermissionError):
					self.task.save()

	def test_standard_copy_and_delete_are_blocked(self):
		with self.assertRaises(frappe.PermissionError):
			frappe.copy_doc(self.task)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Task", self.task.name)
		ordinary = frappe.get_doc({"doctype": "Task", "subject": "ordinary"}).insert()
		self.assertTrue(frappe.copy_doc(ordinary).insert().name)

	def test_task_share_does_not_bypass_customer_and_queries(self):
		frappe.set_user("Administrator")
		frappe.share.add("Task", self.task.name, self.stranger.name, read=1)
		frappe.set_user(self.stranger.name)
		self.assertFalse(frappe.has_permission("Task", "read", doc=self.task.name))
		self.assertNotIn(self.task.name, frappe.get_list("Task", pluck="name"))
		frappe.set_user(self.reader.name)
		self.assertTrue(frappe.has_permission("Task", "read", doc=self.task.name))
		self.assertIn(self.task.name, frappe.get_list("Task", pluck="name"))

	def test_assignment_is_neutral_and_invalid_assignee_is_rejected(self):
		from frappe.desk.form import assign_to

		with patch(
			"frappe.desk.doctype.notification_log.notification_log.enqueue_create_notification"
		) as notify:
			assign_to.add(
				{
					"doctype": "Task",
					"name": self.task.name,
					"assign_to": [self.reader.name],
					"description": "Synthetic secret title",
					"date": None,
				}
			)
			self.assertEqual(notify.call_count, 1)
			payload = notify.call_args.args[1]
			self.assertNotIn("Synthetic secret", str(payload))
		todo = frappe.get_doc(
			"ToDo",
			{"reference_type": "Task", "reference_name": self.task.name, "allocated_to": self.reader.name},
		)
		self.assertNotIn("Synthetic secret", todo.description)
		self.assertIsNone(todo.date)
		with self.assertRaises(frappe.PermissionError):
			assign_to.add({"doctype": "Task", "name": self.task.name, "assign_to": [self.stranger.name]})

	def test_todo_and_notification_are_hidden_after_revocation(self):
		from frappe.desk.form import assign_to

		with patch("frappe.desk.doctype.notification_log.notification_log.enqueue_create_notification"):
			assign_to.add(
				{"doctype": "Task", "name": self.task.name, "assign_to": [self.reader.name], "date": None}
			)
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user(self.reader.name)
		self.assertEqual(frappe.get_list("ToDo", filters={"reference_name": self.task.name}), [])
		self.assertFalse(
			self.access.notification_delivery(
				{"document_type": "Task", "document_name": self.task.name}, self.reader.name
			)
		)
		with self.assertRaises(frappe.PermissionError):
			assign_to.get({"doctype": "Task", "name": self.task.name})

	def test_quarantine_is_mandatory_for_old_assignee_and_manager_can_inspect(self):
		self.confirm_fixture(self.doc, quarantine=True)
		frappe.share.add("Task", self.task.name, self.manager.name, read=1)
		frappe.set_user(self.reader.name)
		self.assertFalse(frappe.has_permission("Task", "read", doc=self.task.name))
		frappe.set_user(self.manager.name)
		self.assertTrue(frappe.has_permission("Task", "read", doc=self.task.name))

	def test_editing_work_fields_preserves_confirmed_action_snapshot(self):
		self.task.description = "Updated working note"
		self.task.save()
		self.assertEqual(
			frappe.db.get_value("Incoming Letter Action", {"action_id": self.action_id}, "description"),
			"Synthetic secret description",
		)

	def test_task_file_cannot_be_published_or_detached(self):
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "AP07-task.txt",
				"content": b"synthetic task attachment",
				"is_private": 1,
				"attached_to_doctype": "Task",
				"attached_to_name": self.task.name,
			}
		).insert()
		self.paths.append(Path(file.get_full_path()))
		with self.assertRaises(frappe.PermissionError):
			file.is_private = 0
			file.save()
		file.reload()
		with self.assertRaises(frappe.PermissionError):
			file.db_set({"attached_to_doctype": None, "attached_to_name": None})

	def test_related_comments_and_versions_follow_task_access(self):
		comment = self.task.add_comment("Comment", "Synthetic confidential comment")
		frappe.set_user(self.stranger.name)
		self.assertFalse(frappe.has_permission("Comment", "read", doc=comment.name))
		with self.assertRaises(frappe.PermissionError):
			frappe.get_list("Comment", filters={"name": comment.name})

	def test_todo_does_not_grant_base_task_read(self):
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"reference_type": "Task",
				"reference_name": self.task.name,
				"allocated_to": self.manager.name,
				"assigned_by": self.operator.name,
				"description": "neutral",
			}
		).insert(ignore_permissions=True)
		frappe.set_user(self.manager.name)
		self.assertFalse(frappe.has_permission("Task", "read", doc=self.task.name))
		self.assertFalse(frappe.has_permission("ToDo", "read", doc=todo.name))
		self.assertEqual(frappe.get_list("ToDo", filters={"name": todo.name}), [])

	def test_todo_reference_cannot_be_detached_even_before_revocation(self):
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"reference_type": "Task",
				"reference_name": self.task.name,
				"allocated_to": self.reader.name,
				"assigned_by": self.operator.name,
				"description": "neutral",
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			todo.db_set({"reference_type": None, "reference_name": None})
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "reference_name"), self.task.name)

	def test_email_is_neutral_and_rechecks_current_recipient_access(self):
		from frappe.desk.doctype.notification_log.notification_log import send_notification_email

		with patch(
			"frappe.desk.doctype.notification_log.notification_log.enqueue_create_notification"
		) as enqueue:
			with patch(
				"frappe.desk.form.assign_to.get_title",
				side_effect=AssertionError("must not read confidential title"),
			):
				from frappe.desk.form.assign_to import notify_assignment

				notify_assignment(
					self.operator.name,
					self.reader.name,
					"Task",
					self.task.name,
					action="ASSIGN",
					description="Synthetic secret description",
				)
		payload = enqueue.call_args.args[1]
		notification = frappe.get_doc(
			{"doctype": "Notification Log", **payload, "for_user": self.reader.name}
		)
		with patch("frappe.sendmail") as send:
			send_notification_email(notification)
			self.assertEqual(send.call_count, 1)
			self.assertNotIn("Synthetic secret", str(send.call_args))
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		with patch("frappe.sendmail") as send:
			send_notification_email(notification)
			send.assert_not_called()

	def test_real_rq_assignment_commit_rollback_and_delivery_revocation(self):
		from frappe.desk.form import assign_to
		from frappe.utils.background_jobs import generate_qname, get_redis_conn
		from rq import Queue

		frappe.db.commit()
		queue_name = "ap07-notify-" + frappe.generate_hash(length=12)
		queue = Queue(generate_qname(queue_name), connection=get_redis_conn())
		context = multiprocessing.get_context("spawn")

		def consume():
			process = context.Process(
				target=_notification_worker,
				args=(frappe.local.site, str(Path(frappe.local.sites_path).resolve()), queue_name),
			)
			process.start()
			process.join(timeout=45)
			if process.is_alive():
				process.kill()
				process.join(timeout=5)
			self.assertEqual(process.exitcode, 0)

		try:
			with (
				patch.object(frappe, "in_test", False),
				patch("frappe.utils.background_jobs.get_queue", return_value=queue),
			):
				args = {
					"doctype": "Task",
					"name": self.task.name,
					"assign_to": [self.reader.name],
					"date": None,
				}
				assign_to.add(args)
				self.assertEqual(queue.count, 0)
				frappe.db.rollback()
				self.assertEqual(queue.count, 0)
				assign_to.add(args)
				self.assertEqual(queue.count, 0)
				frappe.db.commit()
				self.assertEqual(queue.count, 1)
				self.assertNotIn("Synthetic secret", str(queue.jobs[0].kwargs))
			consume()
			frappe.db.rollback()
			self.assertEqual(
				frappe.db.count(
					"Notification Log",
					{"document_type": "Task", "document_name": self.task.name, "for_user": self.reader.name},
				),
				1,
			)
			with (
				patch.object(frappe, "in_test", False),
				patch("frappe.utils.background_jobs.get_queue", return_value=queue),
			):
				assign_to.remove("Task", self.task.name, self.reader.name)
				frappe.db.commit()
			self.assertEqual(queue.count, 1)
			frappe.set_user("Administrator")
			frappe.share.remove("Customer", self.customer.name, self.reader.name)
			frappe.db.commit()
			consume()
			frappe.db.rollback()
			self.assertEqual(
				frappe.db.count(
					"Notification Log",
					{"document_type": "Task", "document_name": self.task.name, "for_user": self.reader.name},
				),
				1,
			)
		finally:
			queue.delete(delete_jobs=True)
