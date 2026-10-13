"""Transaction callbacks must route notifications using current permissions."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp.tests import test_drive_storage_security as fixtures


class TestDriveRealtime(FrappeTestCase):
	setUp = fixtures.TestDriveStorageSecurity.setUp
	tearDown = fixtures.TestDriveStorageSecurity.tearDown
	make_user = fixtures.TestDriveStorageSecurity.make_user
	root = fixtures.TestDriveStorageSecurity.root
	child = fixtures.TestDriveStorageSecurity.child

	def test_no_global_file_notifications_and_no_paths(self):
		root = self.root()
		with patch("frappe.publish_realtime") as publish:
			self.child(root)
			for call in publish.call_args_list:
				if call.kwargs.get("doctype") != "File" and (len(call.args) < 2 or call.args[1].get("doctype") != "File"):
					continue
				self.assertNotEqual(call.args[0], "list_update")
				self.assertNotEqual(call.kwargs.get("room"), "all")
				self.assertNotEqual(call.args[0], "doc_update")

	def test_folder_signal_uses_current_recipients_after_commit(self):
		from kanzlei_erp.drive_realtime import changed
		root = self.root()
		file = self.child(root)
		# Isolate callbacks from fixture creation, while keeping real permission queries.
		frappe.db.after_commit.reset()
		frappe.db.after_rollback.reset()
		if hasattr(frappe.local, "kanzlei_drive_events"):
			del frappe.local.kanzlei_drive_events
		with patch("frappe.publish_realtime") as publish:
			changed(file)
			changed(file)
			publish.assert_not_called()
			frappe.share.remove("Customer", self.customer.name, self.writer.name)
			frappe.db.after_commit.run()
			signals = [call for call in publish.call_args_list if call.args[0] == "drive-folder-changed"]
			users = [call.kwargs.get("user") for call in signals]
			self.assertIn(self.reader.name, users)
			self.assertNotIn(self.stranger.name, users)
			self.assertNotIn(self.writer.name, users)
			self.assertEqual(len(users), len(set(users)))
			for call in signals:
				self.assertEqual(call.args[1], {"folder": root.name})

	def test_rollback_discards_notifications(self):
		from kanzlei_erp.drive_realtime import changed
		file = self.child(self.root())
		frappe.db.after_commit.reset()
		frappe.db.after_rollback.reset()
		if hasattr(frappe.local, "kanzlei_drive_events"):
			del frappe.local.kanzlei_drive_events
		with patch("frappe.publish_realtime") as publish:
			changed(file)
			frappe.db.after_rollback.run()
			frappe.db.after_commit.run()
			publish.assert_not_called()
