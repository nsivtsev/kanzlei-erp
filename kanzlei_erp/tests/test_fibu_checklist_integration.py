import frappe
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp.fibu_checklist import confirm_customer_sources


class TestFiBuChecklist(FrappeTestCase):
	def setUp(self):
		self.customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Checklist {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()
		self.service = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"_Checklist service {frappe.generate_hash(length=8)}",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def add_source(self, category="Bank Account", name="Bank account A", **values):
		self.customer.append(
			"kanzlei_data_sources",
			{"category": category, "source_name": name, **values},
		)
		self.customer.save()
		return self.customer.kanzlei_data_sources[-1]

	def confirm_sources(self):
		return confirm_customer_sources(self.customer.name, str(self.customer.modified))

	def modified(self, doc):
		return str(doc.modified)

	def test_fibu_forms_load_workflow_and_checklist_scripts(self):
		hooks = frappe.get_hooks("doctype_js", app_name="kanzlei_erp")
		for doctype in ("FiBu Package", "FiBu Supplement"):
			paths = hooks.get(doctype, [])
			if not isinstance(paths, list):
				paths = [paths]
			self.assertIn("public/js/fibu_workflow.js", paths)
			self.assertIn("public/js/fibu_checklist.js", paths)

	def make_package(self, **values):
		return frappe.get_doc(
			{
				"doctype": "FiBu Package",
				"customer": self.customer.name,
				"service": self.service.name,
				"period_type": "Monthly",
				"period_year": 2026,
				"period_number": 9,
				"responsible": "Administrator",
				**values,
			}
		).insert()

	def test_profile_snapshot_clips_source_dates_and_excludes_other_service(self):
		bank = self.add_source(valid_from="2026-09-15", note="Primary operating account")
		second_bank = self.add_source("Bank Account", "Bank account B", valid_from="2026-09-01")
		other_service = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"_Other checklist service {frappe.generate_hash(length=8)}",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		self.add_source("Card", "Card for annual service", service=other_service.name)
		self.confirm_sources()

		package = self.make_package()

		entries = {row.source_id: row for row in package.checklist_entries}
		self.assertEqual(set(entries), {bank.source_id, second_bank.source_id})
		self.assertEqual(entries[bank.source_id].expected_from, "2026-09-15")
		self.assertEqual(entries[bank.source_id].expected_through, "2026-09-30")
		self.assertEqual(entries[bank.source_id].source_note, "Primary operating account")
		self.assertEqual(entries[bank.source_id].status, "Expected")
		self.assertNotIn("Cash Register", {row.category for row in package.checklist_entries})

	def test_empty_profile_requires_confirmation_and_is_distinct_from_unconfigured(self):
		unconfigured = self.make_package()
		self.assertFalse(unconfigured.checklist_initialized)
		self.confirm_sources()

		confirmed_empty = self.make_package(period_number=10)

		self.assertTrue(confirmed_empty.checklist_initialized)
		self.assertEqual(confirmed_empty.checklist_entries, [])

	def test_saved_nonempty_profile_is_snapshotted_when_a_period_is_created(self):
		source = self.add_source(valid_from="2026-09-15")
		self.assertFalse(self.customer.reload().kanzlei_sources_reviewed)

		package = self.make_package()

		self.assertTrue(package.checklist_initialized)
		self.assertEqual(package.checklist_entries[0].source_id, source.source_id)
		self.assertEqual(str(package.checklist_entries[0].expected_from), "2026-09-15")

	def test_profile_edits_require_reconfirmation_and_do_not_rewrite_snapshot(self):
		self.add_source()
		self.confirm_sources()
		package = self.make_package()
		self.customer.reload()
		self.customer.kanzlei_data_sources[0].source_name = "Renamed account"
		self.customer.save()

		self.assertFalse(self.customer.kanzlei_sources_reviewed)
		self.assertEqual(package.reload().checklist_entries[0].source_name, "Bank account A")

	def test_source_list_confirmation_is_server_controlled_and_logged_by_customer_history(self):
		self.customer.reload()
		self.customer.kanzlei_sources_reviewed = 1
		with self.assertRaisesRegex(frappe.ValidationError, "confirmation action"):
			self.customer.save()
		self.customer.reload()
		self.confirm_sources()
		self.assertTrue(self.customer.reload().kanzlei_sources_reviewed)

	def test_legacy_period_initialization_uses_only_a_confirmed_current_profile(self):
		package = self.make_package()
		self.assertFalse(package.checklist_initialized)
		self.add_source(valid_from="2026-09-15")
		with self.assertRaisesRegex(frappe.ValidationError, "Confirm the Mandant source list"):
			package.initialize_checklist("Legacy initialization", self.modified(package))
		self.confirm_sources()

		package.reload().initialize_checklist("Legacy source snapshot", self.modified(package))

		self.assertTrue(package.reload().checklist_initialized)
		self.assertEqual(str(package.checklist_entries[0].expected_from), "2026-09-15")
		self.assertEqual(package.checklist_events[-1].reason, "Legacy source snapshot")

	def test_status_requires_full_evidence_and_direct_table_edits_are_rejected(self):
		self.add_source()
		self.confirm_sources()
		package = self.make_package()
		entry = package.checklist_entries[0]
		values = entry.as_dict()
		values["status"] = "Received in Full"
		with self.assertRaisesRegex(frappe.ValidationError, "Evidence"):
			package.update_checklist_entry(entry.source_id, values, {}, "Statement received", self.modified(package))
		package.reload()
		entry = package.checklist_entries[0]
		package.update_checklist_entry(
			entry.source_id,
			{**entry.as_dict(), "status": "Partially Received"},
			{
				"evidence_type": "External URL",
				"external_url": "https://example.invalid/first",
				"covered_from": "2026-09-01",
				"covered_through": "2026-09-14",
			},
			"First half received",
			self.modified(package),
		)
		package.reload()
		entry = package.checklist_entries[0]
		package.update_checklist_entry(
			entry.source_id,
			{**entry.as_dict(), "status": "Received in Full"},
			{
				"evidence_type": "External URL",
				"external_url": "https://example.invalid/second",
				"covered_from": "2026-09-15",
				"covered_through": "2026-09-30",
			},
			"Second half received",
			self.modified(package),
		)
		self.assertEqual(package.reload().checklist_entries[0].status, "Received in Full")
		self.assertEqual(len(package.checklist_events), 3)
		self.assertEqual(package.checklist_events[-1].recorded_by, frappe.session.user)
		self.assertTrue(package.checklist_events[-1].recorded_at)
		package.checklist_entries[0].status = "Expected"
		with self.assertRaisesRegex(frappe.ValidationError, "checklist actions"):
			package.save()
		package.reload()
		package.checklist_entries[0].source_id = "forged-source-id"
		with self.assertRaisesRegex(frappe.ValidationError, "checklist actions"):
			package.save()
		package.reload()
		package.checklist_events[0].reason = "forged history"
		with self.assertRaisesRegex(frappe.ValidationError, "checklist actions"):
			package.save()

	def test_status_rollback_and_stale_form_require_reason_and_reload(self):
		self.add_source()
		self.confirm_sources()
		package = self.make_package()
		entry = package.checklist_entries[0]
		stale_modified = self.modified(package)
		package.update_checklist_entry(
			entry.source_id,
			{**entry.as_dict(), "status": "Not Applicable", "inapplicable_reason": "No cash transactions"},
			{},
			"Confirmed not relevant",
			self.modified(package),
		)
		package.reload()
		entry = package.checklist_entries[0]
		with self.assertRaisesRegex(frappe.ValidationError, "reason"):
			package.update_checklist_entry(
				entry.source_id,
				{**entry.as_dict(), "status": "Expected", "inapplicable_reason": ""},
				{},
				"",
				self.modified(package),
			)
		with self.assertRaisesRegex(frappe.ValidationError, "changed"):
			package.update_checklist_entry(
				entry.source_id,
				{**entry.as_dict(), "status": "Expected", "inapplicable_reason": ""},
				{},
				"Return source to expected",
				stale_modified,
			)

	def test_supplement_starts_empty_and_copies_only_selected_sources(self):
		first = self.add_source(name="Bank account A")
		self.add_source(category="Card", name="Card B")
		self.confirm_sources()
		package = self.make_package()
		package.close("Period complete")
		supplement = frappe.get_doc(
			{"doctype": "FiBu Supplement", "package": package.name, "reason": "Late bank statement"}
		).insert()
		self.assertFalse(supplement.checklist_initialized)

		supplement.initialize_checklist(
			[first.source_id], "Only the late statement is affected", self.modified(supplement)
		)

		self.assertEqual([row.source_id for row in supplement.reload().checklist_entries], [first.source_id])
		self.assertEqual(package.reload().checklist_entries[0].status, "Expected")
		self.assertEqual(len(package.checklist_entries), 2)

	def test_incomplete_checklist_does_not_block_ready_stage_or_change_closed_package(self):
		self.add_source()
		self.confirm_sources()
		package = self.make_package()
		package.change_preparation_stage("Review", expected_modified=self.modified(package))
		package.reload().change_preparation_stage("Ready", expected_modified=self.modified(package))
		self.assertEqual(package.reload().preparation_stage, "Ready")
		package.close("Workflow permits incomplete checklist at this stage")
		original_status = package.checklist_entries[0].status
		with self.assertRaisesRegex(frappe.ValidationError, "closed accounting period checklist"):
			package.update_checklist_entry(
				package.checklist_entries[0].source_id,
				{**package.checklist_entries[0].as_dict(), "status": "Not Applicable", "inapplicable_reason": "No source"},
				{},
				"Mark not applicable",
				self.modified(package),
			)
		self.assertEqual(package.reload().checklist_entries[0].status, original_status)

	def test_browser_serialized_checklist_history_does_not_block_workflow_actions(self):
		self.add_source()
		self.confirm_sources()
		package = self.make_package()
		browser_payload = frappe.parse_json(frappe.as_json(package.as_dict()))
		browser_doc = frappe.get_doc(browser_payload, check_permission=True)
		browser_doc._original_modified = browser_doc.modified
		browser_doc.check_if_latest()

		browser_doc.change_preparation_stage("Review", "", self.modified(browser_doc))
		browser_payload = frappe.parse_json(frappe.as_json(package.reload().as_dict()))
		browser_doc = frappe.get_doc(browser_payload, check_permission=True)
		browser_doc._original_modified = browser_doc.modified
		browser_doc.check_if_latest()
		browser_doc.change_preparation_stage("Ready", "", self.modified(browser_doc))

		self.assertEqual(package.reload().preparation_stage, "Ready")

	def test_private_evidence_must_be_attached_to_the_same_mandant(self):
		from frappe.utils.file_manager import save_file

		self.add_source()
		self.confirm_sources()
		package = self.make_package()
		other = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Other checklist {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()
		file = save_file("other-bank.txt", b"Other client statement", "Customer", other.name, is_private=1)
		entry = package.checklist_entries[0]
		with self.assertRaisesRegex(frappe.ValidationError, "same Mandant"):
			package.update_checklist_entry(
				entry.source_id,
				{**entry.as_dict(), "status": "Partially Received"},
				{
					"evidence_type": "File",
					"file": file.name,
					"covered_from": "2026-09-01",
					"covered_through": "2026-09-30",
				},
				"Wrong client source",
				self.modified(package),
			)
