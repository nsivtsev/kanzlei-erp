"""Packaging/security entry points required before runtime Task tests."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TaskProtectionContract(unittest.TestCase):
	def test_provenance_is_immutable_and_unique_without_reusing_schedule_key(self):
		fields = {
			f["fieldname"]: f
			for f in json.loads((ROOT / "kanzlei_erp/fixtures/custom_field.json").read_text())
			if f["dt"] == "Task"
		}
		for name in ("kanzlei_incoming_letter", "kanzlei_incoming_action_id", "kanzlei_incoming_action_key"):
			self.assertIn(name, fields)
			self.assertEqual(fields[name]["read_only"], 1)
			self.assertEqual(fields[name]["no_copy"], 1)
		self.assertEqual(fields["kanzlei_incoming_action_key"]["unique"], 1)
		self.assertNotEqual(fields["kanzlei_incoming_action_key"]["fieldname"], "kanzlei_occurrence_key")

	def test_patch_has_guards_before_content_copy_and_notification(self):
		patch = (ROOT / "deploy/frappe-mandant-access.patch").read_text()
		for hook in (
			"document_copy_guard",
			"assignment_notification_override",
			"notification_delivery_guard",
		):
			self.assertIn('get_hooks("' + hook + '")', patch)
		self.assertTrue((ROOT / "deploy/frappe-mandant-access-v9.patch").exists())


if __name__ == "__main__":
	unittest.main()
