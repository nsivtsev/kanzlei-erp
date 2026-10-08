import frappe
from frappe.tests.utils import FrappeTestCase


class TestFiBuWorkflowIntegration(FrappeTestCase):
	def setUp(self):
		self.customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Workflow Test {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()
		self.service = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"_Workflow Service {frappe.generate_hash(length=8)}",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()

	def make_package(self, **overrides):
		values = {
			"doctype": "FiBu Package",
			"customer": self.customer.name,
			"service": self.service.name,
			"period_type": "Monthly",
			"period_year": 2026,
			"period_number": 9,
			"responsible": "Administrator",
		}
		values.update(overrides)
		return frappe.get_doc(values).insert()

	def test_new_package_starts_with_an_action_assigned_to_its_responsible(self):
		package = self.make_package()
		self.assertEqual(package.preparation_stage, "Collection")
		self.assertEqual(package.next_action, "Review the current period status")
		self.assertEqual(package.next_action_assignee, package.responsible)

	def test_package_and_supplement_expose_read_only_workflow_fields(self):
		for doctype in ("FiBu Package", "FiBu Supplement"):
			meta = frappe.get_meta(doctype)
			self.assertEqual(meta.get_field("preparation_stage").fieldtype, "Select")
			self.assertTrue(meta.get_field("preparation_stage").read_only)
			self.assertTrue(meta.get_field("workflow_events").read_only)
			self.assertEqual(meta.get_field("workflow_events").options, "FiBu Workflow Event")
			self.assertEqual(meta.get_field("workflow_untracked_html").fieldtype, "HTML")
		self.assertIn("/assets/kanzlei_erp/js/fibu_workflow.js", frappe.get_hooks("app_include_js"))
		self.assertIn("/assets/kanzlei_erp/js/fibu_checklist.js", frappe.get_hooks("app_include_js"))

	def test_backfill_initializes_open_work_and_leaves_closed_stage_unknown(self):
		from kanzlei_erp.patches.backfill_fibu_workflow import execute

		open_package = self.make_package()
		closed_package = self.make_package(period_number=10)
		closed_package.close("Closed before stage tracking")
		frappe.db.set_value(
			"FiBu Package",
			open_package.name,
			{"preparation_stage": "", "next_action": "", "next_action_assignee": ""},
		)
		execute()
		open_package.reload()
		closed_package.reload()
		self.assertEqual(open_package.preparation_stage, "Collection")
		self.assertEqual(open_package.next_action, "Review the current period status")
		self.assertEqual(open_package.next_action_assignee, open_package.responsible)
		self.assertFalse(closed_package.preparation_stage)

	def test_stage_change_is_audited_and_does_not_close_the_package(self):
		package = self.make_package()
		package.change_preparation_stage("Review", "", str(package.modified))
		updated = frappe.get_doc("FiBu Package", package.name)
		self.assertEqual(updated.preparation_stage, "Review")
		self.assertEqual(updated.status, "Open")
		self.assertEqual(len(updated.workflow_events), 1)
		self.assertEqual(updated.workflow_events[0].from_stage, "Collection")
		self.assertEqual(updated.workflow_events[0].to_stage, "Review")
		self.assertEqual(updated.workflow_events[0].recorded_by, "Administrator")
		self.assertTrue(updated.workflow_events[0].recorded_at)

	def test_stage_transition_rules_and_optimistic_lock(self):
		package = self.make_package()
		original_modified = str(package.modified)
		with self.assertRaisesRegex(frappe.ValidationError, "transition"):
			package.change_preparation_stage("Ready", "", original_modified)
		package.reload()
		package.change_preparation_stage("Review", "", original_modified)
		with self.assertRaisesRegex(frappe.ValidationError, "changed"):
			package.change_preparation_stage("Ready", "", original_modified)

	def test_waiting_and_blocking_context_are_independent_and_audited(self):
		package = self.make_package()
		package.update_work_context(
			1,
			"Waiting for the second bank statement",
			"The second account is not reconciled",
			"Review the existing account",
			"Administrator",
			"",
			str(package.modified),
			"Client request sent",
		)
		updated = frappe.get_doc("FiBu Package", package.name)
		self.assertEqual(updated.preparation_stage, "Collection")
		self.assertEqual(updated.status, "Open")
		self.assertEqual(updated.waiting_for_mandant, 1)
		self.assertEqual(updated.workflow_events[0].event_type, "Waiting Changed")
		with self.assertRaisesRegex(frappe.ValidationError, "waiting reason"):
			updated.update_work_context(1, "", "", "Review documents", "Administrator", "", str(updated.modified))

	def test_direct_save_cannot_forge_workflow_state(self):
		package = self.make_package()
		package.preparation_stage = "Review"
		with self.assertRaisesRegex(frappe.ValidationError, "workflow actions"):
			package.save()

	def test_supplement_workflow_is_independent_of_closed_package(self):
		package = self.make_package()
		package.close("Initial work complete")
		supplement = frappe.get_doc(
			{
				"doctype": "FiBu Supplement",
				"package": package.name,
				"reason": "Late bank statement",
			}
		).insert()
		supplement.change_preparation_stage("Review", "", str(supplement.modified))
		self.assertEqual(frappe.db.get_value("FiBu Package", package.name, "status"), "Closed")
		self.assertEqual(frappe.db.get_value("FiBu Package", package.name, "preparation_stage"), "Collection")
		self.assertEqual(frappe.db.get_value("FiBu Supplement", supplement.name, "preparation_stage"), "Review")

	def test_next_action_task_must_belong_to_the_same_open_context(self):
		package = self.make_package()
		other_package = self.make_package(period_number=10)
		other_task = frappe.get_doc(
			{"doctype": "Task", "subject": "Other period", "kanzlei_fibu_package": other_package.name}
		).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "belong to this FiBu work context"):
			package.update_work_context(
				0, "", "", "Review the bank statement", "Administrator", other_task.name, str(package.modified)
			)
		task = frappe.get_doc(
			{"doctype": "Task", "subject": "Review the bank statement", "kanzlei_fibu_package": package.name}
		).insert()
		package.update_work_context(
			0, "", "", "Review the bank statement", "Administrator", task.name, str(package.modified)
		)
		self.assertEqual(frappe.db.get_value("FiBu Package", package.name, "next_action_task"), task.name)

	def test_receipt_confirmation_requires_a_note_and_clears_the_finished_action(self):
		package = self.make_package()
		package.change_preparation_stage("Review", "", str(package.modified))
		package.change_preparation_stage("Ready", "", str(package.modified))
		package.change_preparation_stage("Transfer", "", str(package.modified))
		with self.assertRaisesRegex(frappe.ValidationError, "note is required"):
			package.change_preparation_stage("Receipt Confirmed", "", str(package.modified))
		package.change_preparation_stage("Receipt Confirmed", "Receipt acknowledged", str(package.modified))
		updated = frappe.get_doc("FiBu Package", package.name)
		self.assertEqual(updated.preparation_stage, "Receipt Confirmed")
		self.assertEqual(updated.next_action, "")
		self.assertEqual(updated.next_action_assignee, "")
		self.assertEqual(updated.workflow_events[-1].note, "Receipt acknowledged")
