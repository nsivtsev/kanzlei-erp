"""Acceptance scenarios for Mandant sources and period completeness."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.file_manager import save_file

from kanzlei_erp.tests import test_fibu_package


class TestFiBuChecklist(FrappeTestCase):
	def setUp(self):
		if self._testMethodName != "test_schema_exposes_customer_sources_and_shared_checklists":
			test_fibu_package.TestFiBuPackage.setUp(self)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def make_package(self, **values):
		return test_fibu_package.TestFiBuPackage.make_package(self, **values)

	def sources(self):
		self.customer.append("kanzlei_data_sources", {"category": "Bank Account", "source_title": "First account"})
		self.customer.append("kanzlei_data_sources", {"category": "Bank Account", "source_title": "Second account", "valid_from": "2026-09-10"})
		self.customer.append("kanzlei_data_sources", {"category": "Card", "source_title": "Old card", "valid_to": "2026-08-31"})
		self.customer.save()
		return self.customer.kanzlei_data_sources

	def evidence(self, start="2026-09-01", end="2026-09-30", **values):
		return {"covered_from": start, "covered_to": end, "external_url": "https://example.invalid/statement", **values}

	def update(self, package, entry=None, status="Received", evidence=None, **values):
		package.update_checklist_entry(entry_key=(entry or package.checklist[0]).entry_key, status=status,
			evidence=evidence if evidence is not None else [self.evidence()], expected_modified=str(package.modified), **values)
		return package.reload()

	def test_schema_exposes_customer_sources_and_shared_checklists(self):
		from kanzlei_erp.kanzlei_erp.doctype.fibu_package.fibu_package import (
			FiBuPackage,
		)
		self.assertTrue(callable(getattr(FiBuPackage, "initialize_checklist", None)))
		self.assertIsNotNone(frappe.get_meta("Customer").get_field("kanzlei_data_sources"))
		for doctype in ("FiBu Package", "FiBu Supplement"):
			self.assertIsNotNone(frappe.get_meta(doctype).get_field("checklist"))
			self.assertIsNotNone(frappe.get_meta(doctype).get_field("checklist_evidence"))

	def test_sources_snapshot_and_date_intersection(self):
		sources = self.sources()
		self.assertTrue(all(row.source_key for row in sources))
		self.assertEqual(len({row.source_key for row in sources}), 3)
		package = self.make_package()
		self.assertEqual(len(package.checklist), 2)
		self.assertEqual(str(package.checklist[1].expected_from), "2026-09-10")
		self.assertEqual(str(package.checklist[1].expected_to), "2026-09-30")
		self.assertEqual(package.checklist[0].status, "Expected")
		self.customer.kanzlei_data_sources[0].source_title = "Renamed account"
		self.customer.save()
		self.assertEqual(package.reload().checklist[0].source_title, "First account")
		self.assertEqual(self.make_package(period_number=10).checklist[0].source_title, "Renamed account")

	def test_invalid_source_dates_and_duplicate_identity(self):
		self.sources()
		self.customer.kanzlei_data_sources[0].valid_from = "2026-10-01"
		self.customer.kanzlei_data_sources[0].valid_to = "2026-09-01"
		with self.assertRaises(frappe.ValidationError):
			self.customer.save()
		self.customer.reload()
		self.customer.kanzlei_data_sources[1].source_key = self.customer.kanzlei_data_sources[0].source_key
		with self.assertRaises(frappe.ValidationError):
			self.customer.save()

	def test_partial_coverage_and_gap_do_not_allow_received(self):
		self.sources()
		package = self.make_package()
		self.update(package, status="Partially Received", evidence=[self.evidence(end="2026-09-15")])
		self.assertEqual(package.checklist[0].status, "Partially Received")
		with self.assertRaises(frappe.ValidationError):
			self.update(package, evidence=[self.evidence(end="2026-09-15"), self.evidence(start="2026-09-17")])
		package.reload()
		self.update(package, evidence=[self.evidence(end="2026-09-15"), self.evidence(start="2026-09-16")])
		self.assertEqual(package.checklist[0].status, "Received")
		self.assertFalse(package.checklist[0].checked_by)

	def test_checked_metadata_is_server_authored_and_edit_resets_check(self):
		self.sources()
		package = self.make_package()
		self.update(package, status="Checked")
		self.assertEqual(package.checklist[0].checked_by, "Administrator")
		self.assertTrue(package.checklist[0].checked_at)
		self.update(package, status="Checked", evidence=[self.evidence(note="Corrected statement")])
		self.assertEqual(package.checklist[0].status, "Received")
		self.assertFalse(package.checklist[0].checked_by)
		self.update(package, status="Checked", evidence=[self.evidence(note="Corrected statement")])
		self.assertEqual(package.checklist[0].status, "Checked")

	def test_not_applicable_requires_reason_and_partial_requires_evidence(self):
		self.sources()
		package = self.make_package()
		with self.assertRaises(frappe.ValidationError):
			self.update(package, status="Not Applicable", evidence=[])
		package.reload()
		with self.assertRaises(frappe.ValidationError):
			self.update(package, status="Partially Received", evidence=[])
		package.reload()
		self.update(package, status="Not Applicable", evidence=[], reason="Account unused this month")
		self.assertEqual(package.checklist[0].status, "Not Applicable")

	def test_direct_changes_deletion_and_stale_actions_rejected(self):
		self.sources()
		package = self.make_package()
		package.checklist[0].status = "Checked"
		package.checklist[0].checked_by = "Administrator"
		with self.assertRaises(frappe.ValidationError):
			package.save()
		package.reload()
		package.checklist.pop()
		with self.assertRaises(frappe.ValidationError):
			package.save()
		package.reload()
		old_modified = str(package.modified)
		self.update(package)
		with self.assertRaises(frappe.ValidationError):
			package.update_checklist_entry(entry_key=package.checklist[0].entry_key, status="Checked", evidence=[self.evidence()], expected_modified=old_modified)

	def test_public_files_and_unsafe_links_are_rejected(self):
		self.sources()
		package = self.make_package()
		public = save_file("public-statement.txt", b"public", "Customer", self.customer.name, is_private=0)
		with self.assertRaises(frappe.ValidationError):
			self.update(package, evidence=[self.evidence(external_url="", file=public.name)])
		package.reload()
		with self.assertRaises(frappe.ValidationError):
			self.update(package, evidence=[self.evidence(external_url="javascript:alert(1)")])

	def test_incomplete_checklist_does_not_block_ready_or_closure(self):
		self.sources()
		package = self.make_package()
		package.change_preparation_stage("Review", expected_modified=str(package.modified))
		package.reload().change_preparation_stage("Ready", expected_modified=str(package.modified))
		package.reload().close("Warnings acknowledged")
		self.assertEqual(package.reload().checklist[0].status, "Expected")
		with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
			self.update(package)

	def test_supplement_selects_sources_without_copying_verification(self):
		self.sources()
		package = self.make_package()
		self.update(package, status="Checked")
		package.close("Original completed")
		supplement = frappe.get_doc({"doctype": "FiBu Supplement", "package": package.name, "reason": "Late statement"}).insert()
		self.assertEqual(len(supplement.checklist), 0)
		supplement.add_checklist_source(parent_entry_key=package.checklist[0].entry_key, expected_modified=str(supplement.modified))
		supplement.reload()
		self.assertEqual(len(supplement.checklist), 1)
		self.assertEqual(supplement.checklist[0].status, "Expected")
		self.assertFalse(supplement.checklist[0].checked_by)
		self.update(supplement)
		self.assertEqual(package.reload().checklist[0].status, "Checked")

	def test_copy_uses_fresh_profile_without_verification(self):
		self.sources()
		package = self.make_package()
		self.update(package, status="Checked")
		copy = frappe.copy_doc(package)
		copy.period_number = 10
		copy.insert()
		self.assertEqual(len(copy.checklist), 2)
		self.assertEqual(copy.checklist[0].status, "Expected")
		self.assertEqual(len(copy.checklist_evidence), 0)
		self.assertFalse(copy.checklist[0].checked_by)

	def test_checklist_evidence_appears_in_mandant_documents(self):
		from kanzlei_erp.mandant_documents import get_documents
		self.sources()
		package = self.make_package()
		file = save_file("evidence-only.txt", b"evidence", "Customer", self.customer.name, is_private=1)
		self.update(package, evidence=[self.evidence(external_url="", file=file.name)])
		result = get_documents(self.customer.name, filters={"source": "fibu", "package": package.name})
		self.assertIn(file.name, [row["file_id"] for row in result["items"]])

	def test_existing_open_period_initializes_once_and_closed_history_stays_untracked(self):
		from kanzlei_erp.fibu_checklist import checklist_summary
		package = self.make_package()
		frappe.db.set_value("FiBu Package", package.name, "checklist_initialized", 0)
		package.reload()
		self.sources()
		package.initialize_checklist(expected_modified=str(package.modified))
		self.assertEqual(len(package.reload().checklist), 2)
		modified = package.modified
		package.initialize_checklist(expected_modified=str(modified))
		self.assertEqual(len(package.reload().checklist), 2)
		self.assertEqual(package.modified, modified)
		self.customer.set("kanzlei_data_sources", [])
		self.customer.save()
		old = self.make_package(period_number=10)
		old.close("Historical closure")
		frappe.db.set_value("FiBu Package", old.name, "checklist_initialized", 0)
		old.reload()
		self.assertFalse(checklist_summary(old)["initialized"])
		with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
			old.initialize_checklist(expected_modified=str(old.modified))

	def test_empty_checklist_is_not_complete_and_manual_source_remains_visible(self):
		package = self.make_package()
		self.assertFalse(package.get_checklist_summary()["complete"])
		package.add_checklist_source(category="Contracts", source_title="Lease", expected_from="2026-09-01", expected_to="2026-09-30", expected_modified=str(package.modified))
		self.assertEqual(package.reload().checklist[0].source_title, "Lease")
		self.update(package, status="Not Applicable", evidence=[], reason="Lease starts next month")
		self.assertTrue(package.get_checklist_summary()["complete"])
		self.assertEqual(len(package.checklist), 1)

	def test_quarter_year_and_multiple_cards_preserve_individual_sources(self):
		self.sources()
		self.customer.append("kanzlei_data_sources", {"category": "Card", "source_title": "Card A", "valid_from": "2026-09-15"})
		self.customer.append("kanzlei_data_sources", {"category": "Card", "source_title": "Card B", "valid_from": "2026-09-20"})
		self.customer.save()
		quarter = self.make_package(period_type="Quarterly", period_number=3)
		self.assertEqual(len(quarter.checklist), 5)
		self.assertEqual(str(quarter.checklist[2].expected_to), "2026-08-31")
		annual = self.make_package(period_type="Yearly", period_number=None)
		self.assertEqual(str(annual.checklist[0].expected_from), "2026-01-01")
		self.assertEqual(str(annual.checklist[0].expected_to), "2026-12-31")
		self.assertNotEqual(quarter.checklist[3].entry_key, quarter.checklist[4].entry_key)

	def test_history_cannot_be_rewritten_and_out_of_range_evidence_is_rejected(self):
		self.sources()
		package = self.make_package()
		self.update(package)
		package.checklist_events[0].recorded_by = "Guest"
		with self.assertRaises(frappe.ValidationError):
			package.save()
		package.reload()
		with self.assertRaises(frappe.ValidationError):
			self.update(package, evidence=[self.evidence(start="2026-08-31")])
		package.reload()
		with self.assertRaises(frappe.ValidationError):
			package.add_checklist_source(source_key=self.customer.kanzlei_data_sources[0].source_key, expected_modified=str(package.modified))

	def test_restricted_staff_can_work_but_cannot_read_foreign_evidence_or_forge_context(self):
		self.sources()
		package = self.make_package()
		foreign_customer = frappe.get_doc({"doctype": "Customer", "customer_name": "_Checklist Foreign " + frappe.generate_hash(length=8), "customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories"}).insert()
		foreign = self.make_package(customer=foreign_customer.name)
		foreign.add_checklist_source(category="Bank Account", source_title="Foreign account", expected_from="2026-09-01", expected_to="2026-09-30", expected_modified=str(foreign.modified))
		file = save_file("foreign-checklist.txt", b"foreign evidence", "FiBu Package", foreign.name, is_private=1)
		communication = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication", "communication_medium": "Email", "sent_or_received": "Received", "subject": "Foreign statement", "sender": "sender@example.invalid", "content": "Test only", "reference_doctype": "Customer", "reference_name": foreign_customer.name}).insert()
		staff = frappe.get_doc({"doctype": "User", "email": "checklist-" + frappe.generate_hash(length=8) + "@example.invalid", "first_name": "Checklist Staff", "send_welcome_email": 0, "roles": [{"role": "Projects User"}, {"role": "Sales User"}, {"role": "Inbox User"}]}).insert()
		frappe.get_doc({"doctype": "User Permission", "user": staff.name, "allow": "Customer", "for_value": self.customer.name}).insert(ignore_permissions=True)
		frappe.set_user(staff.name)
		self.update(package, status="Checked")
		self.assertEqual(package.checklist[0].checked_by, staff.name)
		with self.assertRaises(frappe.PermissionError):
			self.update(package, evidence=[self.evidence(external_url="", file=file.name)])
		package.reload()
		with self.assertRaises(frappe.PermissionError):
			self.update(package, evidence=[self.evidence(external_url="", communication=communication.name)])
		foreign.customer = self.customer.name
		with self.assertRaises(frappe.PermissionError):
			foreign.update_checklist_entry(entry_key=foreign.checklist[0].entry_key, status="Received", evidence=[self.evidence()], expected_modified=str(foreign.modified))
		foreign.reload()
		foreign.customer = self.customer.name
		with self.assertRaises(frappe.PermissionError):
			foreign.get_checklist_summary()

	def test_external_evidence_appears_once_in_document_browser_without_download(self):
		from kanzlei_erp.mandant_documents import get_documents
		self.sources()
		package = self.make_package()
		self.update(package, evidence=[self.evidence(end="2026-09-15"), self.evidence(start="2026-09-16")])
		result = get_documents(self.customer.name, filters={"source": "fibu", "package": package.name})
		links = [row for row in result["items"] if row["external_url"] == "https://example.invalid/statement"]
		self.assertEqual(len(links), 1)
		self.assertFalse(links[0]["can_download"])
		self.assertFalse(links[0]["can_select_for_zip"])

	def test_communication_evidence_without_attachment_has_readable_reference(self):
		from kanzlei_erp.mandant_documents import get_documents
		self.sources()
		package = self.make_package()
		communication = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication", "communication_medium": "Email", "sent_or_received": "Received", "subject": "No transactions confirmed", "sender": "sender@example.invalid", "content": "Fictitious confirmation", "reference_doctype": "Customer", "reference_name": self.customer.name}).insert()
		self.update(package, evidence=[self.evidence(external_url="", communication=communication.name)])
		items = get_documents(self.customer.name, filters={"source": "fibu", "package": package.name})["items"]
		self.assertEqual(len(items), 1)
		self.assertIn(communication.name, items[0]["external_url"])
		self.assertEqual(items[0]["file_name"], communication.subject)

	def test_completeness_translations_distinguish_receipt_from_email(self):
		from frappe.translate import get_translations_from_apps
		for language, received in (("ru", "Получено"), ("de", "Erhalten")):
			terms = get_translations_from_apps(language)
			self.assertEqual(terms["Received in full"], received)
			for key in ("Accounting Sources", "Completeness", "Partially Received", "Checked", "Not Applicable", "Sources are not configured"):
				self.assertTrue(terms.get(key))

	def test_browser_round_trip_preserves_workflow_history_and_allows_next_transition(self):
		import json
		self.sources()
		package = self.make_package()
		self.update(package, status="Checked")
		package.change_preparation_stage("Review", expected_modified=str(package.modified))
		browser_doc = frappe.get_doc(json.loads(package.as_json()))
		browser_doc.change_preparation_stage("Ready", expected_modified=str(browser_doc.modified))
		self.assertEqual(browser_doc.preparation_stage, "Ready")
		round_trip = frappe.get_doc(json.loads(browser_doc.as_json()))
		round_trip.internal_due_date = "2026-10-15"
		round_trip.save()
		self.assertEqual(len(round_trip.workflow_events), 2)

	def test_normal_duplicate_of_closed_package_starts_new_work_without_history(self):
		self.sources()
		package = self.make_package()
		self.update(package, status="Checked")
		package.change_preparation_stage("Review", expected_modified=str(package.modified))
		package.reload().change_preparation_stage("Ready", expected_modified=str(package.modified))
		package.reload().close("Original closed")
		copy = frappe.copy_doc(package, ignore_no_copy=False)
		copy.period_number = 10
		copy.insert()
		self.assertEqual(copy.status, "Open")
		self.assertEqual(copy.preparation_stage, "Collection")
		self.assertFalse(copy.workflow_events)
		self.assertFalse(copy.closed_by)
		self.assertFalse(copy.checklist_evidence)
		self.assertEqual(copy.checklist[0].status, "Expected")

	def test_verification_timestamps_cannot_be_changed_even_within_same_second(self):
		from datetime import timedelta

		from frappe.utils import get_datetime

		self.sources()
		package = self.make_package()
		self.update(package, status="Checked")
		package.checklist[0].checked_at = get_datetime(package.checklist[0].checked_at) + timedelta(microseconds=1)
		with self.assertRaises(frappe.ValidationError):
			package.save()

	def test_manual_original_source_cannot_be_selected_twice_in_supplement(self):
		package = self.make_package()
		package.add_checklist_source(category="Contracts", source_title="Lease", expected_from="2026-09-01", expected_to="2026-09-30", expected_modified=str(package.modified))
		package.reload().close("Original closed")
		supplement = frappe.get_doc({"doctype": "FiBu Supplement", "package": package.name, "reason": "Lease correction"}).insert()
		supplement.add_checklist_source(parent_entry_key=package.checklist[0].entry_key, expected_modified=str(supplement.modified))
		with self.assertRaises(frappe.ValidationError):
			supplement.reload().add_checklist_source(parent_entry_key=package.checklist[0].entry_key, expected_modified=str(supplement.modified))
