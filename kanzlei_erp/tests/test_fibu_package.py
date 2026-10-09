from datetime import date

import frappe
from frappe.tests.utils import FrappeTestCase


class TestFiBuPackage(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def setUp(self):
		self.customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_FiBu Test {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()
		self.service = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"_FiBu Service {frappe.generate_hash(length=8)}",
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

	def test_september_period_is_independent_of_october_work(self):
		package = self.make_package(internal_due_date=date(2026, 10, 15))
		self.assertEqual(package.period_start, "2026-09-01")
		self.assertEqual(package.period_end, "2026-09-30")
		self.assertEqual(package.internal_due_date, date(2026, 10, 15))

	def test_forms_expose_work_materials_and_supplements(self):
		from frappe.desk.form.meta import FormMeta

		package_meta = frappe.get_meta("FiBu Package")
		supplement_meta = frappe.get_meta("FiBu Supplement")
		self.assertIsNotNone(package_meta.get_field("work_html"))
		self.assertIsNotNone(package_meta.get_field("supplements_html"))
		self.assertEqual(package_meta.get_field("has_open_supplements").fieldtype, "Int")
		self.assertIsNotNone(supplement_meta.get_field("work_html"))
		self.assertIn("Upload document", FormMeta("FiBu Package").get("__js"))
		self.assertIn("New Question", FormMeta("FiBu Supplement").get("__js"))

	def test_duplicate_period_and_invalid_service_are_rejected(self):
		first = self.make_package()
		with self.assertRaisesRegex(frappe.ValidationError, "already exists"):
			self.make_package()
		with self.assertRaisesRegex(frappe.ValidationError, "already exists"):
			frappe.copy_doc(first).insert()
		self.assertTrue(frappe.db.exists("FiBu Package", first.name))
		self.make_package(period_number=10)
		other_customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Other Period {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()
		self.make_package(customer=other_customer.name)
		other_service = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"_Other Service {frappe.generate_hash(length=8)}",
				"item_group": self.service.item_group,
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		self.make_package(service=other_service.name)
		self.service.is_stock_item = 1
		self.service.save()
		with self.assertRaisesRegex(frappe.ValidationError, "service"):
			self.make_package(period_number=10)

	def test_database_prevents_parallel_duplicate_packages(self):
		indexes = frappe.db.sql(
			"SHOW INDEX FROM `tabFiBu Package` WHERE Key_name='unique_fibu_work_period'", as_dict=True
		)
		self.assertEqual(len(indexes), 4)
		self.assertEqual({row.Non_unique for row in indexes}, {0})
		self.assertEqual(
			[row.Column_name for row in sorted(indexes, key=lambda row: row.Seq_in_index)],
			["customer", "service", "period_start", "period_end"],
		)

	def test_identity_deadline_and_closure_are_server_controlled(self):
		package = self.make_package()
		package.period_number = 10
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be changed"):
			package.save()
		package.reload()
		package.external_due_date = date(2026, 10, 20)
		with self.assertRaisesRegex(frappe.ValidationError, "source"):
			package.save()
		package.reload()
		package.status = "Closed"
		with self.assertRaisesRegex(frappe.ValidationError, "Close"):
			package.save()
		package.reload()
		package.close("All work complete")
		self.assertEqual(package.status, "Closed")
		self.assertEqual(package.closed_by, "Administrator")
		self.assertTrue(package.closed_at)
		package.internal_due_date = date(2026, 11, 1)
		with self.assertRaisesRegex(frappe.ValidationError, "closed"):
			package.save()

	def test_closure_metadata_and_summary_cannot_be_forged(self):
		package = self.make_package()
		package.closed_by = "Administrator"
		with self.assertRaisesRegex(frappe.ValidationError, "Close"):
			package.save()
		package.reload()
		package.has_open_supplements = 1
		with self.assertRaisesRegex(frappe.ValidationError, "supplement"):
			package.save()

	def test_only_empty_open_package_can_be_deleted(self):
		empty = self.make_package()
		empty.delete()
		self.assertFalse(frappe.db.exists("FiBu Package", empty.name))
		package = self.make_package()
		task = frappe.get_doc(
			{"doctype": "Task", "subject": "Review", "kanzlei_fibu_package": package.name}
		).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be deleted"):
			package.delete()
		with_history = self.make_package(period_number=10)
		with_history.internal_due_date = date(2026, 11, 1)
		with_history.save()
		frappe.get_doc(
			{"doctype": "Version", "ref_doctype": "FiBu Package", "docname": with_history.name, "data": "{}"}
		).insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be deleted"):
			with_history.delete()
		task.status = "Cancelled"
		task.save()
		package.close("All work complete")
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be deleted"):
			package.delete()

	def test_late_work_uses_numbered_supplements_without_reopening_package(self):
		package = self.make_package()
		package.close("Initial work complete")
		original_closed_at = package.closed_at
		original_modified = package.modified
		first = frappe.get_doc(
			{"doctype": "FiBu Supplement", "package": package.name, "reason": "Late receipt"}
		).insert()
		package.reload()
		self.assertEqual(first.sequence, 1)
		self.assertEqual(first.responsible, package.responsible)
		self.assertEqual(package.has_open_supplements, 1)
		self.assertNotEqual(package.modified, original_modified)
		self.assertEqual(package.closed_at, original_closed_at)
		first.close("Added to subsequent transfer")
		second = frappe.get_doc(
			{"doctype": "FiBu Supplement", "package": package.name, "reason": "Correction"}
		).insert()
		self.assertEqual(second.sequence, 2)
		self.assertEqual(package.reload().status, "Closed")
		self.assertEqual(package.closure_note, "Initial work complete")
		self.assertEqual(frappe.db.get_value("FiBu Supplement", first.name, "status"), "Closed")

	def test_supplement_closure_metadata_is_server_controlled(self):
		package = self.make_package()
		package.close("Initial work complete")
		with self.assertRaisesRegex(frappe.ValidationError, "Close"):
			frappe.get_doc(
				{"doctype": "FiBu Supplement", "package": package.name, "reason": "Late file", "closed_by": "Administrator"}
			).insert()
		supplement = frappe.get_doc(
			{"doctype": "FiBu Supplement", "package": package.name, "reason": "Late file"}
		).insert()
		supplement.closed_by = "Administrator"
		with self.assertRaisesRegex(frappe.ValidationError, "Close"):
			supplement.save()

	def test_tasks_and_questions_block_closure_and_keep_mandant(self):
		package = self.make_package()
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"subject": "Ask for missing bank statement",
				"kanzlei_fibu_package": package.name,
				"kanzlei_work_kind": "Question",
			}
		).insert()
		self.assertEqual(task.kanzlei_customer, package.customer)
		with self.assertRaisesRegex(frappe.ValidationError, "unfinished"):
			package.close("Too early")
		package.reload()
		from kanzlei_erp.fibu_questions import update_question
		update_question(task.name, "cancel", {"reason": "Late issue resolved"}, str(task.modified))
		task.reload()
		package.close("Question resolved outside the package")
		self.assertEqual(package.status, "Closed")
		with self.assertRaisesRegex(frappe.ValidationError, "closed"):
			task.status = "Open"
			task.save()
		task.reload()
		with self.assertRaisesRegex(frappe.ValidationError, "closed"):
			task.kanzlei_fibu_package = None
			task.save()
		with self.assertRaisesRegex(frappe.ValidationError, "closed"):
			frappe.delete_doc("Task", task.name)

	def test_supplement_closure_requires_completed_work(self):
		package = self.make_package()
		package.close("Initial work complete")
		supplement = frappe.get_doc(
			{"doctype": "FiBu Supplement", "package": package.name, "reason": "Late question"}
		).insert()
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"subject": "Ask about late receipt",
				"kanzlei_fibu_supplement": supplement.name,
				"kanzlei_work_kind": "Question",
			}
		).insert()
		self.assertEqual(task.kanzlei_fibu_package, package.name)
		with self.assertRaisesRegex(frappe.ValidationError, "unfinished"):
			supplement.close("Too early")
		from kanzlei_erp.fibu_questions import update_question
		update_question(task.name, "cancel", {"reason": "Late issue resolved"}, str(task.modified))
		task.reload()
		supplement.reload().close("Late issue resolved")
		self.assertEqual(frappe.db.get_value("FiBu Package", package.name, "has_open_supplements"), 0)
		with self.assertRaisesRegex(frappe.ValidationError, "closed"):
			task.reload().kanzlei_fibu_supplement = None
			task.save()

	def test_task_cannot_link_to_wrong_mandant_or_closed_base(self):
		package = self.make_package()
		other_customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Other {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "Mandant"):
			frappe.get_doc(
				{
					"doctype": "Task",
					"subject": "Wrong client",
					"kanzlei_customer": other_customer.name,
					"kanzlei_fibu_package": package.name,
				}
			).insert()
		package.close("Done")
		with self.assertRaisesRegex(frappe.ValidationError, "closed"):
			frappe.get_doc(
				{"doctype": "Task", "subject": "Late item", "kanzlei_fibu_package": package.name}
			).insert()

	def test_materials_and_transfer_history_stay_with_closed_period(self):
		from frappe.utils.file_manager import save_file

		package = self.make_package()
		file = save_file("bank-statement.txt", b"test statement", "FiBu Package", package.name, is_private=1)
		package.append("files", {"file": file.name})
		package.append("transfers", {"event_date": date(2026, 10, 15), "note": "Sent manually"})
		package.save()
		self.assertEqual(package.files[0].file, file.name)
		self.assertEqual(package.transfers[0].recorded_by, "Administrator")
		self.assertTrue(package.transfers[0].recorded_at)
		package.transfers[0].note = "Altered"
		with self.assertRaisesRegex(frappe.ValidationError, "Transfer history"):
			package.save()
		package.reload()
		package.close("Original package complete")
		supplement = frappe.get_doc(
			{"doctype": "FiBu Supplement", "package": package.name, "reason": "Late bank statement"}
		).insert()
		supplement.append("files", {"file": file.name})
		supplement.save()
		self.assertEqual(len(package.reload().files), 1)
		self.assertEqual(package.transfers[0].note, "Sent manually")
		self.assertEqual(supplement.files[0].file, file.name)

	def test_browser_round_trip_preserves_transfer_history_on_close(self):
		from frappe.utils import get_datetime

		package = self.make_package()
		package.append("transfers", {"event_date": date(2026, 10, 4), "note": "Sent manually"})
		package.save()
		package.reload()
		row = package.transfers[0]
		row.evidence_file = ""
		row.evidence_communication = ""
		row.recorded_at = get_datetime(row.recorded_at).replace(microsecond=0).isoformat(" ")
		package.close("Initial transfer complete")
		self.assertEqual(package.status, "Closed")

	def test_files_attach_only_to_open_package_or_supplement(self):
		from frappe.utils.file_manager import save_file

		package = self.make_package()
		package.close("Initial work complete")
		with self.assertRaisesRegex(frappe.ValidationError, "closed"):
			save_file("late.txt", b"late document", "FiBu Package", package.name, is_private=1)
		supplement = frappe.get_doc(
			{"doctype": "FiBu Supplement", "package": package.name, "reason": "Late document"}
		).insert()
		supplement.close("Late work complete")
		with self.assertRaisesRegex(frappe.ValidationError, "closed"):
			save_file("later.txt", b"even later", "FiBu Supplement", supplement.name, is_private=1)

	def test_mandant_permissions_cover_packages_supplements_and_linked_tasks(self):
		from frappe.utils.file_manager import save_file

		staff = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"fibu-{frappe.generate_hash(length=8)}@example.invalid",
				"first_name": "FiBu Staff",
				"send_welcome_email": 0,
				"roles": [{"role": "Projects User"}, {"role": "Sales User"}],
			}
		).insert()
		frappe.get_doc(
			{"doctype": "User Permission", "user": staff.name, "allow": "Customer", "for_value": self.customer.name}
		).insert(ignore_permissions=True)
		visible = self.make_package(responsible=staff.name)
		foreign_customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Foreign {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()
		foreign = self.make_package(customer=foreign_customer.name, responsible=staff.name, deputy=staff.name)
		foreign_task = frappe.get_doc(
			{"doctype": "Task", "subject": "Foreign question", "kanzlei_fibu_package": foreign.name}
		).insert()
		foreign_task.status = "Cancelled"
		foreign_task.save()
		foreign.close("Initial work complete")
		foreign_supplement = frappe.get_doc(
			{"doctype": "FiBu Supplement", "package": foreign.name, "reason": "Late item"}
		).insert()
		foreign_file = save_file("foreign.txt", b"private", "FiBu Supplement", foreign_supplement.name, is_private=1)
		frappe.set_user(staff.name)
		self.assertTrue(frappe.has_permission("FiBu Package", "read", visible.name))
		self.assertFalse(frappe.has_permission("FiBu Package", "read", foreign.name))
		self.assertNotIn(foreign.name, frappe.get_list("FiBu Package", pluck="name"))
		self.assertFalse(frappe.has_permission("FiBu Supplement", "read", foreign_supplement.name))
		self.assertNotIn(foreign_supplement.name, frappe.get_list("FiBu Supplement", pluck="name"))
		self.assertFalse(frappe.has_permission("Task", "read", foreign_task.name))
		self.assertFalse(frappe.has_permission("File", "read", foreign_file.name))

	def test_authorized_staff_can_create_work_in_open_supplement(self):
		from frappe.utils.file_manager import save_file

		staff = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"fibu-deputy-{frappe.generate_hash(length=8)}@example.invalid",
				"first_name": "FiBu Deputy",
				"send_welcome_email": 0,
				"roles": [{"role": "Projects User"}, {"role": "Sales User"}],
			}
		).insert()
		frappe.get_doc(
			{"doctype": "User Permission", "user": staff.name, "allow": "Customer", "for_value": self.customer.name}
		).insert(ignore_permissions=True)
		package = self.make_package(deputy=staff.name)
		package.close("Initial work complete")
		supplement = frappe.get_doc(
			{"doctype": "FiBu Supplement", "package": package.name, "reason": "Late receipt"}
		).insert()
		file = save_file("authorized.txt", b"private", "FiBu Supplement", supplement.name, is_private=1)
		frappe.set_user(staff.name)
		self.assertTrue(frappe.has_permission("FiBu Supplement", "read", supplement.name))
		self.assertTrue(frappe.has_permission("File", "read", file.name))
		task = frappe.get_doc(
			{"doctype": "Task", "subject": "Check late receipt", "kanzlei_fibu_supplement": supplement.name}
		).insert()
		self.assertEqual(task.kanzlei_customer, self.customer.name)
		self.assertEqual(task.kanzlei_fibu_package, package.name)
