"""Working-date rule and company holiday lookup for the one-click Posteingang."""

import unittest
from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp.posteingang_simple import company_holidays, working_date
from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestWorkingDate(unittest.TestCase):
	def test_five_working_days_before_deadline(self):
		self.assertEqual(working_date(date(2026, 12, 15), date(2026, 11, 26), frozenset()), date(2026, 12, 8))

	def test_skips_weekend_and_holidays(self):
		self.assertEqual(working_date(date(2027, 1, 15), date(2026, 11, 26), frozenset()), date(2027, 1, 8))
		self.assertEqual(
			working_date(date(2027, 1, 15), date(2026, 11, 26), frozenset({date(2027, 1, 12)})),
			date(2027, 1, 7),
		)

	def test_never_before_today(self):
		self.assertEqual(working_date(date(2026, 11, 30), date(2026, 11, 26), frozenset()), date(2026, 11, 26))
		self.assertEqual(working_date(date(2026, 11, 1), date(2026, 11, 26), frozenset()), date(2026, 11, 26))

	def test_no_deadline_is_one_week(self):
		self.assertEqual(working_date(None, date(2026, 11, 26), frozenset()), date(2026, 12, 3))


class TestCompanyHolidays(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.previous_default_company = frappe.db.get_single_value("Global Defaults", "default_company")

	def tearDown(self):
		frappe.db.set_single_value("Global Defaults", "default_company", self.previous_default_company)
		frappe.db.rollback()

	def test_reads_holidays_of_default_company(self):
		holiday_list = frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": "Posteingang Simple Test",
				"from_date": "2027-01-01",
				"to_date": "2027-12-31",
				"holidays": [{"holiday_date": "2027-01-12", "description": "Test"}],
			}
		).insert()
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "Posteingang Simple Test GmbH",
				"abbr": "PST",
				"default_currency": "EUR",
				"country": "Germany",
				"default_holiday_list": holiday_list.name,
			}
		)
		# Company.insert() builds warehouses and accounts a fresh site lacks; only the link is needed.
		company.db_insert()
		frappe.db.set_single_value("Global Defaults", "default_company", company.name)
		self.assertIn(date(2027, 1, 12), company_holidays())

	def test_empty_without_default_company(self):
		frappe.db.set_single_value("Global Defaults", "default_company", None)
		self.assertEqual(company_holidays(), frozenset())


class SimpleCase(PosteingangTestCase):
	"""Shared setup and cleanup for one-click tests; holds no tests itself."""

	def setUp(self):
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		from kanzlei_erp.posteingang_setup import DEFAULTS

		self.settings = {key: frappe.db.get_single_value("Posteingang Settings", key) for key in DEFAULTS}
		super().setUp()
		from kanzlei_erp import posteingang_confirmation, posteingang_jobs, posteingang_workspace

		self.review, self.jobs, self.workspace = posteingang_confirmation, posteingang_jobs, posteingang_workspace
		self.committed, self.customers = [], [self.customer.name]
		self.customer.append("kanzlei_identifiers", SYN_0007)
		self.customer.save()
		self.share_and_verify(self.customer)

	def share_and_verify(self, customer, write=1):
		from kanzlei_erp.mandant_identifiers import verify_identifier

		frappe.share.add("Customer", customer.name, self.operator.name, read=1, write=write)
		frappe.share.add("Customer", customer.name, self.manager.name, read=1, write=1)
		frappe.set_user(self.manager.name)
		customer.reload()
		verify_identifier(customer.name, customer.kanzlei_identifiers[0].name, str(customer.modified), "Synthetic one-click source")
		frappe.set_user("Administrator")

	def successful(self, case="AP01-01"):
		from kanzlei_erp.tests.test_posteingang_matching import TestPosteingangMatching

		doc = TestPosteingangMatching.successful_letter(self, case)
		self.committed.append(doc.name)
		return doc

	def tearDown(self):
		# process_run commits everything setUp created; remove it so verified SYN-0007
		# Customers and open Letters do not pile up for later runs and other modules.
		frappe.set_user("Administrator")
		frappe.db.rollback()
		if self.committed:
			for name in self.committed:
				frappe.db.delete("File", {"attached_to_doctype": "Incoming Letter", "attached_to_name": name})
				for kind in ("Incoming Letter Intake Receipt", "Incoming Letter Event", "Incoming Letter Run"):
					frappe.db.delete(kind, {"letter": name})
				frappe.db.delete("Incoming Letter Action", {"parent": name})
				frappe.db.delete("Incoming Letter", {"name": name})
			for name in self.customers:
				frappe.db.delete("DocShare", {"share_doctype": "Customer", "share_name": name})
				frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
			for user in (self.operator, self.reader, self.stranger, self.manager, self.website):
				frappe.delete_doc("User", user.name, force=True, ignore_permissions=True)
			frappe.db.set_single_value("Posteingang Settings", self.settings)
			frappe.db.commit()
		super().tearDown()

	def card(self, doc, user=None):
		frappe.set_user(user or self.operator.name)
		return self.workspace.get_letter_workspace(doc.name)["card"]

	def status(self, doc, status):
		frappe.set_user("Administrator")
		frappe.db.set_value("Incoming Letter", doc.name, "status", status, update_modified=False)

	def manual_review(self, doc):
		frappe.set_user(self.operator.name)
		self.jobs.manual_review(doc.name, doc.revision, frappe.generate_hash())

	def add_action(self, doc):
		frappe.set_user(self.operator.name)
		revision = frappe.db.get_value("Incoming Letter", doc.name, "revision")
		self.review.add_manual_action(doc.name, "Synthetic work", "Synthetic description", revision, frappe.generate_hash())


class TestSimpleCard(SimpleCase):
	def test_queued_and_processing_are_analyzing(self):
		doc = self.letter()
		for status in ("Queued", "Processing"):
			self.status(doc, status)
			card = self.card(doc)
			self.assertEqual(card["state"], "analyzing")
			self.assertEqual(card["tasks"], [])
			self.assertEqual(card["customer"], {"suggested": None, "candidates": [], "ambiguity": None})
			self.assertIsNone(card["undo_until"])

	def test_error_is_failed(self):
		doc = self.letter()
		self.status(doc, "Error")
		self.assertEqual(self.card(doc)["state"], "failed")

	def test_unreadable_extraction_is_failed(self):
		doc = self.successful("AP01-11")
		self.assertIn(doc.status, ("Needs Review", "Needs Clarification"))
		card = self.card(doc)
		self.assertEqual(card["state"], "failed")
		self.assertEqual(card["tasks"], [])

	def test_received_and_review_without_run_are_not_started(self):
		doc = self.letter()
		self.assertEqual(self.card(doc)["state"], "not_started")
		self.manual_review(doc)
		self.assertEqual(frappe.db.get_value("Incoming Letter", doc.name, "status"), "Needs Review")
		card = self.card(doc)
		self.assertEqual(card["state"], "not_started")
		self.assertIsNone(card["run"])
		self.assertEqual(card["tasks"], [])

	def test_stored_actions_are_a_draft_even_with_a_run(self):
		doc = self.successful()
		self.add_action(doc)
		card = self.card(doc)
		self.assertEqual(card["state"], "draft")
		self.assertTrue(card["run"])
		self.assertEqual([task["title"] for task in card["tasks"]], ["Synthetic work"])
		task = card["tasks"][0]
		self.assertIsNone(task["source_index"])
		self.assertEqual(task["description"], "Synthetic description")
		self.assertEqual(task["deadline"], {"kind": "not_found", "date": None, "wording": None, "page": None, "quote": None})
		self.assertEqual(task["internal_date"], str(working_date(None, frappe.utils.getdate(), company_holidays())))
		self.assertEqual(task["assigned_to"], self.operator.name)

	def test_needs_clarification_with_actions_is_a_draft(self):
		doc = self.letter()
		self.manual_review(doc)
		self.add_action(doc)
		self.status(doc, "Needs Clarification")
		self.assertEqual(self.card(doc)["state"], "draft")

	def test_successful_run_without_actions_is_a_proposal(self):
		doc = self.successful()
		card = self.card(doc)
		self.assertEqual(card["state"], "proposal")
		self.assertEqual(card["run"], frappe.db.get_value("Incoming Letter Run", {"letter": doc.name, "status": "Succeeded"}, "name"))
		self.assertEqual(card["customer"]["ambiguity"], "none")
		self.assertEqual(card["customer"]["suggested"], {"customer": self.customer.name, "customer_name": self.customer.customer_name,
			"why": "Referenz „Mandantenreferenz: SYN-0007“ im Schreiben", "page": 1})
		self.assertEqual(card["customer"]["candidates"], [{"customer": self.customer.name, "customer_name": self.customer.customer_name,
			"why": "Referenz „Mandantenreferenz: SYN-0007“ im Schreiben", "can_select": True}])
		self.assertEqual(card["tasks"], [{"source_index": 0, "title": "Unterlagen einreichen", "description": "Bitte die angeforderten Unterlagen einreichen.",
			"deadline": {"kind": "explicit_date", "date": "2026-11-20", "wording": "Bitte bis zum 20.11.2026 einreichen.", "page": 1, "quote": "Bitte bis zum 20.11.2026 einreichen."},
			"internal_date": str(working_date(date(2026, 11, 20), frappe.utils.getdate(), company_holidays())),
			"assigned_to": self.operator.name, "assigned_to_name": self.operator.full_name}])
		self.assertIsNone(card["no_task_reason"])
		self.assertIsNone(card["undo_until"])

	def test_completed_is_done(self):
		doc = self.confirm_fixture(self.letter())
		card = self.card(doc)
		self.assertEqual(card["state"], "done")
		self.assertEqual(card["tasks"], [])

	def test_multiple_matches_suggest_nothing(self):
		frappe.set_user("Administrator")
		other = frappe.copy_doc(self.customer)
		other.customer_name += " other"
		other.insert()
		self.customers.append(other.name)
		self.share_and_verify(other)
		card = self.card(self.successful())
		self.assertEqual(card["customer"]["ambiguity"], "multiple_matches")
		self.assertIsNone(card["customer"]["suggested"])
		self.assertEqual({c["customer"] for c in card["customer"]["candidates"]}, {self.customer.name, other.name})

	def test_read_only_candidate_is_not_suggested(self):
		frappe.share.remove("Customer", self.customer.name, self.operator.name)
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1)
		card = self.card(self.successful())
		self.assertEqual(card["customer"]["ambiguity"], "none")
		self.assertIsNone(card["customer"]["suggested"])
		self.assertFalse(card["customer"]["candidates"][0]["can_select"])

	def test_relative_deadline_has_no_internal_date(self):
		task = self.card(self.successful("AP01-07"))["tasks"][0]
		self.assertIsNone(task["internal_date"])
		self.assertEqual(task["deadline"], {"kind": "relative", "date": None, "wording": "Bitte innerhalb eines Monats nach Zustellung einreichen.",
			"page": 1, "quote": "Bitte innerhalb eines Monats nach Zustellung einreichen."})

	def test_no_actions_found_gives_a_reason(self):
		card = self.card(self.successful("AP01-03"))
		self.assertEqual(card["state"], "proposal")
		self.assertEqual(card["tasks"], [])
		self.assertEqual(card["no_task_reason"], "Reine Information ohne Handlungsbedarf.")

	def test_inactive_uploader_falls_back_to_current_user(self):
		doc = self.successful()
		frappe.db.set_value("User", self.operator.name, "enabled", 0)
		task = self.card(doc, self.manager.name)["tasks"][0]
		self.assertEqual(task["assigned_to"], self.manager.name)
		self.assertEqual(task["assigned_to_name"], self.manager.full_name)

	def apply(self, doc):
		frappe.set_user(self.operator.name)
		state = self.review.get_review_state(doc.name)
		self.review.apply_result(doc.name, state["proposal"]["run"], state["revision"], frappe.generate_hash(), False)

	def test_applied_run_keeps_proposed_deadlines_in_the_draft(self):
		doc = self.successful("AP01-40")
		self.apply(doc)
		card = self.card(doc)
		self.assertEqual(card["state"], "draft")
		explicit, relative = card["tasks"]
		self.assertEqual(explicit["source_index"], 0)
		self.assertEqual(explicit["deadline"], {"kind": "explicit_date", "date": "2026-12-22", "wording": "Spätester Eingang: 22.12.2026.",
			"page": 2, "quote": "Spätester Eingang: 22.12.2026."})
		self.assertEqual(explicit["internal_date"], str(working_date(date(2026, 12, 22), frappe.utils.getdate(), company_holidays())))
		self.assertEqual(relative["deadline"]["kind"], "relative")
		self.assertEqual(relative["deadline"]["wording"], "Antwort innerhalb von sieben Tagen nach der Musterfreigabe.")
		self.assertIsNone(relative["deadline"]["date"])
		self.assertIsNone(relative["internal_date"])

	def test_draft_keeps_human_decisions_and_drops_rejected_rows(self):
		doc = self.successful("AP01-40")
		self.apply(doc)
		frappe.set_user("Administrator")
		first, second = self.review._actions(doc.name)
		# Test-side state: decisions a human saved earlier in the multi-step review.
		frappe.db.set_value("Incoming Letter Action", first.name, {"scheduling_decision": "unscheduled", "assignment_decision": "unassigned"})
		frappe.db.set_value("Incoming Letter Action", second.name, "decision", "reject")
		card = self.card(doc)
		self.assertEqual([task["title"] for task in card["tasks"]], [first.title])
		task = card["tasks"][0]
		self.assertIsNone(task["internal_date"])
		self.assertIsNone(task["assigned_to"])
		self.assertIsNone(task["assigned_to_name"])

	def test_uploader_without_mandant_access_falls_back_to_current_user(self):
		doc = self.successful()
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.operator.name)
		card = self.card(doc, self.manager.name)
		self.assertEqual(card["customer"]["suggested"]["customer"], self.customer.name)
		self.assertEqual(card["tasks"][0]["assigned_to"], self.manager.name)

	def test_cancelled_is_done(self):
		doc = self.letter()
		self.status(doc, "Cancelled")
		card = self.card(doc)
		self.assertEqual(card["state"], "done")
		self.assertEqual(card["tasks"], [])


SYN_0007 = {"identifier_type": "other", "raw_value": "SYN-0007", "jurisdiction": "SYNTHETIC",
	"authority": "Fiktive Beispielstelle", "other_type": "synthetic_client_reference"}


class TestConfirmSimple(SimpleCase):
	def run_confirm(self, doc, tasks, origin="proposal", customer=None, key=None, revision=None, user=None):
		frappe.set_user(user or self.operator.name)
		revision = revision or frappe.db.get_value("Incoming Letter", doc.name, "revision")
		return self.review.confirm_simple(doc.name, revision, key or frappe.generate_hash(), origin, customer or self.customer.name, tasks)

	def proposal_tasks(self, doc, indices=None, **changes):
		tasks = []
		for task in self.card(doc)["tasks"]:
			if indices is None or task["source_index"] in indices:
				explicit = task["deadline"]["kind"] == "explicit_date"
				tasks.append({"source_index": task["source_index"], "title": task["title"], "description": task["description"],
					"external_date": task["deadline"]["date"] if explicit else None, "internal_date": None,
					"assigned_to": task["assigned_to"], **changes})
		return tasks

	def manual_task(self, **changes):
		return {"source_index": None, "title": "Synthetic manual work", "description": "", "external_date": None,
			"internal_date": None, "assigned_to": self.operator.name, **changes}

	def written(self, doc):
		frappe.set_user("Administrator")
		row = frappe.db.get_value("Incoming Letter", doc.name, ["revision", "status", "selected_customer", "customer_reason", "applied_run"], as_dict=True)
		return (dict(row), len(self.review._actions(doc.name)), frappe.db.count("Task", {"kanzlei_incoming_letter": doc.name}))

	def letter_row(self, doc):
		return frappe.db.get_value("Incoming Letter", doc.name, "*", as_dict=True)

	def event(self, doc):
		return frappe.db.get_value("Incoming Letter Event", {"letter": doc.name, "event_type": "confirm_simple"}, "*", as_dict=True)

	def working(self, deadline):
		return str(working_date(frappe.utils.getdate(deadline) if deadline else None, frappe.utils.getdate(), company_holidays()))

	def test_proposal_creates_one_task_with_working_date(self):
		import json

		doc = self.successful()
		card = self.card(doc)
		tasks = self.proposal_tasks(doc)
		result = self.run_confirm(doc, tasks)
		self.assertEqual(result["status"], "Completed")
		self.assertFalse(result["replayed"])
		self.assertEqual(len(result["actions"]), 1)
		task = frappe.get_doc("Task", result["actions"][0]["task"])
		self.assertEqual(task.subject, "Unterlagen einreichen")
		self.assertEqual(task.kanzlei_customer, card["customer"]["suggested"]["customer"])
		self.assertEqual(str(frappe.utils.getdate(task.exp_end_date)), self.working("2026-11-20"))
		self.assertEqual(result["tasks_modified"], {task.name: str(task.modified)})
		self.assertEqual(frappe.db.count("ToDo", {"reference_type": "Task", "reference_name": task.name, "allocated_to": self.operator.name}), 1)
		letter = self.letter_row(doc)
		self.assertEqual((letter.status, letter.customer_reason, letter.confirmed_customer), ("Completed", "Vorschlag übernommen", self.customer.name))
		self.assertEqual(letter.applied_run, card["run"])
		self.assertTrue(letter.sender)
		(row,) = self.review._actions(doc.name)
		self.assertEqual((row.decision, row.external_date_basis, row.source_run), ("accept", "Vorschlag übernommen (Fundstelle S. 1)", card["run"]))
		self.assertEqual(str(row.external_date), "2026-11-20")
		event = self.event(doc)
		payload = json.loads(event.payload_json)
		self.assertEqual(payload["origin"], "proposal")
		self.assertEqual(payload["customer"], self.customer.name)
		self.assertEqual(payload["tasks"], tasks)
		self.assertIs(payload["review"]["human_verified"], True)
		self.assertEqual(payload["before"]["review"]["actions"], [])

	def test_unselected_proposal_action_is_rejected(self):
		doc = self.successful("AP01-40")
		result = self.run_confirm(doc, self.proposal_tasks(doc, {0}))
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": doc.name}), 1)
		self.assertEqual(len(result["actions"]), 1)
		first, second = self.review._actions(doc.name)
		self.assertEqual((first.source_index, first.decision, first.created_task), (0, "accept", result["actions"][0]["task"]))
		self.assertEqual((second.source_index, second.decision, second.rejection_reason), (1, "reject", "Nicht ausgewählt"))
		self.assertFalse(second.created_task)

	def test_archive_without_tasks_states_why(self):
		doc = self.successful("AP01-03")
		self.run_confirm(doc, [])
		letter = self.letter_row(doc)
		self.assertEqual((letter.status, letter.review_outcome), ("Completed", "no_actions"))
		self.assertEqual(letter.no_actions_reason, "Vom Assistenten: keine Handlung erforderlich, übernommen")
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": doc.name}), 0)

	def test_archive_with_proposed_tasks_rejects_them(self):
		doc = self.successful()
		self.run_confirm(doc, [])
		self.assertEqual(self.letter_row(doc).no_actions_reason, "Ohne Aufgabe abgelegt")
		(row,) = self.review._actions(doc.name)
		self.assertEqual((row.decision, row.rejection_reason), ("reject", "Nicht ausgewählt"))
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": doc.name}), 0)

	def test_relative_deadline_requires_a_human_date(self):
		doc = self.successful("AP01-07")
		before = self.written(doc)
		with self.assertRaisesRegex(frappe.ValidationError, "DEADLINE_REQUIRED"):
			self.run_confirm(doc, self.proposal_tasks(doc))
		self.assertEqual(self.written(doc), before)
		result = self.run_confirm(doc, self.proposal_tasks(doc, external_date="2026-12-10"))
		(row,) = self.review._actions(doc.name)
		self.assertEqual(row.external_date_basis, "Vom Benutzer eingetragen: „Bitte innerhalb eines Monats nach Zustellung einreichen.“")
		self.assertTrue(row.external_date_basis.startswith("Vom Benutzer eingetragen"))
		task = frappe.get_doc("Task", result["actions"][0]["task"])
		self.assertEqual(str(frappe.utils.getdate(task.exp_end_date)), self.working("2026-12-10"))

	def test_multiple_addressees_with_explicit_customer(self):
		doc = self.successful("AP01-06")
		self.assertEqual(self.card(doc)["customer"]["ambiguity"], "multiple_addressees")
		result = self.run_confirm(doc, self.proposal_tasks(doc))
		self.assertEqual(result["status"], "Completed")
		self.assertEqual(self.letter_row(doc).customer_reason, "Mehrere Empfänger, Mandant manuell gewählt")

	def test_error_letter_only_confirms_manually(self):
		doc = self.letter()
		self.status(doc, "Error")
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_TRANSITION"):
			self.run_confirm(doc, [self.manual_task()])
		for tasks in (None, [self.manual_task(assigned_to="")], [self.manual_task(source_index=0)], [self.manual_task(external_date="10.12.2026")]):
			with self.assertRaisesRegex(frappe.ValidationError, "INVALID_REQUEST"):
				self.run_confirm(doc, tasks, "manual")
		result = self.run_confirm(doc, [self.manual_task()], "manual")
		self.assertEqual(result["status"], "Completed")
		task = frappe.get_doc("Task", result["actions"][0]["task"])
		self.assertEqual((task.subject, task.description), ("Synthetic manual work", "Synthetic manual work"))
		self.assertEqual(str(frappe.utils.getdate(task.exp_end_date)), self.working(None))
		letter = self.letter_row(doc)
		self.assertEqual((letter.status, letter.customer_reason), ("Completed", "Manuell gewählt"))
		self.assertIsNone(self.review._actions(doc.name)[0].external_date_basis)

	def test_manual_dates_are_marked_as_entered(self):
		doc = self.letter()
		self.manual_review(doc)
		result = self.run_confirm(doc, [self.manual_task(external_date="2027-01-15", internal_date="2027-01-04")], "manual")
		(row,) = self.review._actions(doc.name)
		self.assertEqual((row.external_date_basis, str(row.internal_date)), ("Manuell eingetragen", "2027-01-04"))
		self.assertEqual(str(frappe.utils.getdate(frappe.db.get_value("Task", result["actions"][0]["task"], "exp_end_date"))), "2027-01-04")

	def test_proposal_refuses_a_letter_with_a_draft(self):
		doc = self.successful()
		tasks = self.proposal_tasks(doc)
		self.add_action(doc)
		before = self.written(doc)
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_REQUEST"):
			self.run_confirm(doc, tasks)
		self.assertEqual(self.written(doc), before)

	def test_unknown_source_index_is_refused(self):
		doc = self.successful()
		for index in (1, None):
			with self.assertRaisesRegex(frappe.ValidationError, "INVALID_REQUEST"):
				self.run_confirm(doc, self.proposal_tasks(doc, source_index=index))

	def test_same_request_key_replays(self):
		doc = self.successful()
		tasks = self.proposal_tasks(doc)
		revision = frappe.db.get_value("Incoming Letter", doc.name, "revision")
		first = self.run_confirm(doc, tasks, key="double-click", revision=revision)
		second = self.run_confirm(doc, tasks, key="double-click", revision=revision)
		self.assertTrue(second["replayed"])
		self.assertEqual(second["actions"], first["actions"])
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": doc.name}), 1)

	def test_stale_revision_creates_nothing(self):
		doc = self.successful()
		revision = frappe.db.get_value("Incoming Letter", doc.name, "revision")
		self.assertGreater(revision, 1)
		with self.assertRaisesRegex(frappe.ValidationError, "REVISION_CONFLICT"):
			self.run_confirm(doc, self.proposal_tasks(doc), revision=revision - 1)
		self.assertEqual(frappe.db.count("Task", {"kanzlei_incoming_letter": doc.name}), 0)

	def test_assignee_without_customer_read_writes_nothing(self):
		doc = self.successful()
		tasks = self.proposal_tasks(doc, assigned_to=self.stranger.name)
		before = self.written(doc)
		with self.assertRaisesRegex(frappe.ValidationError, "ASSIGNEE_NOT_ALLOWED"):
			self.run_confirm(doc, tasks)
		self.assertEqual(self.written(doc), before)

	def test_read_only_customer_writes_nothing(self):
		doc = self.successful()
		tasks = self.proposal_tasks(doc)
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.operator.name)
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1)
		before = self.written(doc)
		with self.assertRaises(frappe.PermissionError):
			self.run_confirm(doc, tasks)
		self.assertEqual(self.written(doc), before)


class TestReceiptProjection(unittest.TestCase):
	def test_hides_tasks_the_reader_cannot_read(self):
		from kanzlei_erp.posteingang_confirmation import _receipt_projection

		result = {"actions": [{"action_id": "a", "task": "TASK-OPEN"}, {"action_id": "b", "task": "TASK-HIDDEN"}],
			"tasks_modified": {"TASK-OPEN": "2026-11-26 10:00:00", "TASK-HIDDEN": "2026-11-26 10:00:01"}}
		with patch("frappe.has_permission", side_effect=lambda doctype, ptype, doc=None, **kw: doc != "TASK-HIDDEN"):
			projected = _receipt_projection(result)
		self.assertEqual([row["task"] for row in projected["actions"]], ["TASK-OPEN", None])
		self.assertEqual(projected["tasks_modified"], {"TASK-OPEN": "2026-11-26 10:00:00"})
		self.assertEqual(len(result["tasks_modified"]), 2)
