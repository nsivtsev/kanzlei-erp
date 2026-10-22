"""AP-06 real Customer permissions and synthetic extraction only."""

import copy
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestIdentifierRules(TestCase):
	def test_identifier_table_is_on_the_visible_tax_tab(self):
		fields = frappe.get_meta("Customer").fields
		active_tab = None
		for field in fields:
			if field.fieldtype == "Tab Break":
				active_tab = field
			if field.fieldname == "kanzlei_identifiers":
				self.assertEqual(active_tab.fieldname, "tax_tab")
				self.assertFalse(active_tab.hidden)
				break
		else:
			self.fail("Customer identifier table missing")

	def test_vat_evidence_does_not_join_surrounding_words(self):
		from kanzlei_erp.posteingang_matching import _evidence

		proof = [{"page": 1, "quote": "USt ID DE 00123", "verification": "matched"}]
		self.assertEqual(_evidence("DE00123", proof, "de_vat_id"), proof)
	def test_identifier_evidence_keeps_other_internal_spacing(self):
		from kanzlei_erp.posteingang_matching import _evidence

		proof = [{"page": 1, "quote": "Reference: A  001", "verification": "matched"}]
		self.assertEqual(_evidence("A  001", proof, "other"), proof)
		self.assertEqual(_evidence("A 001", proof, "other"), [])
		self.assertEqual(_evidence("001", [{**proof[0], "quote": "Reference: 0012"}], "de_tax_id"), [])
	def test_type_specific_normalization_and_context(self):
		self.assertTrue(Path(frappe.get_app_path("kanzlei_erp", "mandant_identifiers.py")).exists(), "AP-06 identifier rules missing")
		from kanzlei_erp.mandant_identifiers import identifier_key, normalize_value

		for kind, raw, expected in (("de_steuernummer", " \uff10\uff11\uff12 / 034  ", "012034"), ("de_tax_id", "00 123", "00123"), ("de_vat_id", " de 00123 ", "DE00123"), ("other", " Ab- 00 ", "Ab- 00")):
			self.assertEqual(normalize_value(kind, raw), expected)
		self.assertIsNone(identifier_key("de_steuernummer", "012/34"))
		self.assertNotEqual(identifier_key("de_steuernummer", "012/34", "DE-BE", "A"), identifier_key("de_steuernummer", "012/34", "DE-BY", "A"))
		self.assertNotEqual(identifier_key("other", "Ab", None, "A", "ref"), identifier_key("other", "ab", None, "A", "ref"))
		for kind, raw in (("de_tax_id", "01-23"), ("de_vat_id", "DE/123"), ("de_steuernummer", "12A"), ("unknown", "12"), ("other", "bad\ud800")):
			with self.assertRaises(frappe.ValidationError):
				normalize_value(kind, raw)


class TestPosteingangMatching(PosteingangTestCase):
	def setUp(self):
		delivery = patch("kanzlei_erp.drive_realtime._deliver")
		delivery.start()
		self.addCleanup(delivery.stop)
		super().setUp()
		self.assertTrue(Path(frappe.get_app_path("kanzlei_erp", "mandant_identifiers.py")).exists(), "AP-06 registry missing")
		from kanzlei_erp import mandant_identifiers, posteingang_matching

		self.ids, self.matching = mandant_identifiers, posteingang_matching
		self.customer.append("kanzlei_identifiers", {"identifier_type": "other", "raw_value": "SYN-0007", "jurisdiction": "SYNTHETIC", "authority": "Fiktive Beispielstelle", "other_type": "synthetic_client_reference"})
		self.customer.save()
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1, write=1)
		frappe.share.add("Customer", self.customer.name, self.manager.name, read=1, write=1)
		self.row = self.customer.kanzlei_identifiers[0]

	def verify(self):
		frappe.set_user(self.manager.name)
		self.customer.reload()
		return self.ids.verify_identifier(self.customer.name, self.row.name, str(self.customer.modified), "Synthetic AP-06 source")

	def test_saved_row_is_unverified_and_cannot_forge_stamps(self):
		self.assertFalse(self.row.verified_by)
		self.assertEqual(self.row.normalized_value, "SYN-0007")
		self.row.update({"verified_by": self.manager.name, "verified_at": "2026-10-22 12:00:00", "verification_source": "forged", "normalized_value": "forged"})
		self.customer.save()
		self.customer.reload()
		self.assertFalse(self.customer.kanzlei_identifiers[0].verified_by)
		self.assertEqual(self.customer.kanzlei_identifiers[0].normalized_value, "SYN-0007")

	def test_verification_is_explicit_and_edit_resets_it(self):
		self.verify()
		self.customer.reload()
		row = self.customer.kanzlei_identifiers[0]
		self.assertEqual(row.verified_by, self.manager.name)
		self.assertTrue(row.verified_at)
		self.assertEqual(row.verification_source, "Synthetic AP-06 source")
		row.raw_value = "SYN-0008"
		self.customer.save()
		self.assertFalse(self.customer.kanzlei_identifiers[0].verified_by)
		self.assertFalse(self.customer.kanzlei_identifiers[0].verification_source)

	def test_stale_and_read_only_verification_are_rejected(self):
		frappe.set_user(self.reader.name)
		with self.assertRaises(frappe.PermissionError):
			self.ids.verify_identifier(self.customer.name, self.row.name, str(self.customer.modified), "source")
		frappe.set_user(self.manager.name)
		with self.assertRaisesRegex(frappe.ValidationError, "CUSTOMER_CHANGED"):
			self.ids.verify_identifier(self.customer.name, self.row.name, "2000-01-01 00:00:00", "source")

	def test_copied_customer_does_not_inherit_verification(self):
		self.verify()
		frappe.set_user("Administrator")
		self.customer.reload()
		copy = frappe.copy_doc(self.customer)
		copy.customer_name += " copy"
		copy.insert()
		self.assertFalse(copy.kanzlei_identifiers[0].verified_by)

	def test_foreign_row_identity_and_direct_write_are_rejected(self):
		other = frappe.copy_doc(self.customer)
		other.customer_name += " other"
		other.insert()
		other.kanzlei_identifiers[0].name = self.row.name
		with self.assertRaises(frappe.PermissionError):
			other.save()
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("Mandant Identifier", self.row.name).db_set("verified_by", self.manager.name)

	def test_manual_selection_is_idempotent_and_does_not_confirm(self):
		doc = self.letter()
		result = self.matching.select_customer(doc.name, self.customer.name, "Reviewed original", 1, "selection")
		self.assertEqual(result["revision"], 2)
		self.assertTrue(self.matching.select_customer(doc.name, self.customer.name, "Reviewed original", 1, "selection")["replayed"])
		with self.assertRaisesRegex(frappe.ValidationError, "IDEMPOTENCY_CONFLICT"):
			self.matching.select_customer(doc.name, self.customer.name, "Other reason", 1, "selection")
		doc.reload()
		self.assertEqual(doc.selected_customer, self.customer.name)
		self.assertEqual((doc.status, doc.analysis_generation, doc.confirmed_customer), ("Received", 0, None))
		self.assertEqual(frappe.db.count("Incoming Letter Run", {"letter": doc.name}), 0)
		self.assertEqual(frappe.db.count("Incoming Letter Event", {"letter": doc.name}), 2)

	def test_read_only_candidate_and_manual_search_do_not_grant_write(self):
		doc = self.letter()
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.operator.name)
		frappe.share.add("Customer", self.customer.name, self.operator.name, read=1)
		frappe.set_user(self.operator.name)
		result = self.matching.search_customers(doc.name, self.customer.customer_name)
		self.assertEqual(result["rows"][0]["customer"], self.customer.name)
		self.assertFalse(result["rows"][0]["can_select"])
		with self.assertRaises(frappe.PermissionError):
			self.matching.select_customer(doc.name, self.customer.name, "reason", 1, "read-only")

	def successful_letter(self, case="AP01-01"):
		from kanzlei_erp import posteingang_jobs as jobs
		from kanzlei_erp.tests.test_posteingang_ai import TestPosteingangAi

		doc = TestPosteingangAi.fixture_letter(self, case)
		with patch.object(jobs, "_dispatch"):
			queued = jobs.queue_analysis(doc.name, 1, frappe.generate_hash())
			frappe.db.commit()
		self.assertEqual(jobs.process_run(queued["run"]), {"code": "SUCCEEDED"})
		doc.reload()
		return doc

	def test_all_sixteen_fixture_candidate_sets(self):
		from kanzlei_erp.tests.test_posteingang_ai import FIXTURES

		catalog = json.loads((FIXTURES / "catalog.json").read_text())
		customers = {}
		for source in catalog["customers"]:
			frappe.set_user("Administrator")
			doc = frappe.copy_doc(self.customer)
			doc.customer_name = source["name"]
			doc.set("kanzlei_identifiers", [{"identifier_type": i["type"], "raw_value": i["value"], "jurisdiction": i["jurisdiction"], "authority": i["authority"], "other_type": i["other_type"]} for i in source["identifiers"]])
			doc.insert()
			self.ids.verify_identifier(doc.name, doc.kanzlei_identifiers[0].name, str(doc.modified), "Synthetic catalog")
			customers[source["customer"]] = doc.name
		for case in json.loads((FIXTURES / "manifest.json").read_text())["cases"]:
			with self.subTest(case=case["case_id"]):
				frappe.set_user("Administrator")
				for key, name in customers.items():
					frappe.share.remove("Customer", name, self.operator.name)
					if key in case["allowed_customers"]:
						frappe.share.add("Customer", name, self.operator.name, read=1, write=1)
				doc = self.successful_letter(case["case_id"])
				before = (doc.revision, frappe.db.count("Task"), frappe.db.count("Incoming Letter Event"))
				state = self.matching.get_matching_state(doc.name)
				actual = {c["customer"] for c in state["matching"]["candidates"]}
				self.assertEqual(actual, {customers[k] for k in case["expected"]["candidate_customers"]})
				expected_ambiguity = "multiple_addressees" if case["case_id"] == "AP01-06" else "multiple_matches" if len(actual) > 1 else "none" if actual else "not_found"
				self.assertEqual(state["matching"]["ambiguity"], expected_ambiguity)
				self.assertEqual((frappe.db.get_value("Incoming Letter", doc.name, "revision"), frappe.db.count("Task"), frappe.db.count("Incoming Letter Event")), before)
				if case["case_id"] == "AP01-06":
					self.assertEqual(state["matching"]["ambiguity"], "multiple_addressees")
					with self.assertRaisesRegex(frappe.ValidationError, "MULTIPLE_ADDRESSEES"):
						self.matching.select_customer(doc.name, customers["SYN-CUSTOMER-A"], "reason", doc.revision, "multi")

	def test_structured_address_ranks_names_and_conflicts_remain_explicit(self):
		self.verify()
		doc = self.successful_letter()
		source = self.matching._source(doc)
		frappe.set_user("Administrator")
		other = frappe.copy_doc(self.customer)
		other.customer_name = self.customer.customer_name
		other.set("kanzlei_identifiers", [])
		other.insert()
		frappe.share.add("Customer", other.name, self.operator.name, read=1, write=1)
		if not frappe.db.exists("Address Template", "Germany"):
			frappe.get_doc({"doctype": "Address Template", "country": "Germany", "template": "{{ address_line1 }} {{ pincode }} {{ city }}"}).insert()
		address = frappe.get_doc({"doctype": "Address", "address_title": "AP06 synthetic", "address_type": "Billing", "address_line1": "Synthetic Lane 7", "pincode": "00123", "city": "Teststadt", "country": "Germany", "links": [{"link_doctype": "Customer", "link_name": other.name}]}).insert()
		frappe.share.add("Address", address.name, self.operator.name, read=1)
		frappe.set_user(self.operator.name)
		name_proof = [{"page": 1, "quote": self.customer.customer_name, "verification": "matched"}]
		source["recipients"][0]["name"] = {"value": self.customer.customer_name, "evidence": name_proof}
		source["recipients"][0]["identifiers"] = []
		source["recipients"][0]["address"] = {"value": "Synthetic Lane 7, 00123 Teststadt", "evidence": [{"page": 1, "quote": "Synthetic Lane 7, 00123 Teststadt", "verification": "matched"}]}
		result = self.matching._match(source)
		self.assertEqual(result["candidates"][0]["customer"], other.name)
		self.assertEqual(result["ambiguity"], "multiple_matches")
		permission = frappe.has_permission
		with patch("frappe.has_permission", side_effect=lambda doctype, *args, **kwargs: False if doctype == "Address" else permission(doctype, *args, **kwargs)):
			ordered = self.matching._match(source)["candidates"]
			self.assertEqual([c["customer"] for c in ordered], sorted([self.customer.name, other.name]))
		# An exact identifier wins the pool, but contradictory facts stay visible.
		source = self.matching._source(doc)
		identifier = copy.deepcopy(source["recipients"][0]["identifiers"][0])
		identifier["value"] = "SYN-9999"
		identifier["evidence"] = [{"page": 1, "quote": "SYN-9999", "verification": "matched"}]
		source["recipients"][0]["identifiers"].append(identifier)
		candidate = self.matching._match(source)["candidates"][0]
		self.assertEqual(candidate["customer"], self.customer.name)
		self.assertEqual({c["code"] for c in candidate["conflicts"]}, {"identifier_conflict", "name_mismatch"})

	def test_hidden_duplicate_does_not_change_matching_or_ambiguity(self):
		self.verify()
		doc = self.successful_letter()
		before = self.matching.get_matching_state(doc.name)
		frappe.set_user("Administrator")
		hidden = frappe.copy_doc(self.customer)
		hidden.customer_name += " hidden"
		hidden.insert()
		self.ids.verify_identifier(hidden.name, hidden.kanzlei_identifiers[0].name, str(hidden.modified), "source")
		frappe.set_user(self.operator.name)
		self.assertEqual(self.matching.get_matching_state(doc.name), before)

	def test_unverified_quote_sender_and_tax_id_are_not_exact_signals(self):
		self.verify()
		doc = self.successful_letter()
		run = frappe.get_doc("Incoming Letter Run", doc.current_run)
		original = json.loads(run.result_json)
		for mode in ("sender", "unverified", "not_in_quote"):
			value = copy.deepcopy(original)
			item = value["recipients"][0]["identifiers"][0]
			if mode == "sender":
				item["holder_role"] = "sender"
			elif mode == "unverified":
				item["evidence"][0]["quote"] = "Absent quote"
			else:
				item["evidence"] = value["sender"]["evidence"]
			frappe.db.set_value("Incoming Letter Run", run.name, "result_json", json.dumps(value))
			self.assertEqual(self.matching.get_matching_state(doc.name)["matching"]["candidates"], [])

	def test_selection_replay_rechecks_access_and_projection_hides_revoked_choice(self):
		doc = self.letter()
		self.matching.select_customer(doc.name, self.customer.name, "reason", 1, "selected")
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.operator.name)
		frappe.set_user(self.operator.name)
		state = self.matching.get_matching_state(doc.name)
		self.assertIsNone(state["selected_customer"])
		self.assertIsNone(state["customer_reason"])
		with self.assertRaises(frappe.PermissionError):
			self.matching.select_customer(doc.name, self.customer.name, "reason", 1, "selected")

	def test_generic_child_reads_follow_customer_and_unknown_parameters_fail(self):
		from frappe.model.db_query import DatabaseQuery

		frappe.set_user(self.stranger.name)
		self.assertFalse(frappe.has_permission("Mandant Identifier", "read", doc=self.row.name))
		self.assertEqual(frappe.get_list("Mandant Identifier", parent_doctype="Customer", fields=["name"]), [])
		frappe.set_user(self.operator.name)
		self.assertEqual([r.name for r in DatabaseQuery("Mandant Identifier").execute(fields=["name"], parent_doctype="Customer")], [self.row.name])
		doc = self.letter()
		for call in (self.matching.get_matching_state, self.matching.search_customers):
			with self.assertRaisesRegex(frappe.ValidationError, "INVALID_REQUEST"):
				call(doc.name, actor="Administrator")

	def test_customer_join_does_not_expose_child_values_to_select_only_user(self):
		from frappe.model.db_query import DatabaseQuery

		frappe.set_user(self.stranger.name)
		for query in (lambda: frappe.get_list("Customer", fields=["name", "kanzlei_identifiers.raw_value"]), lambda: DatabaseQuery("Customer").execute(fields=["name", "`tabMandant Identifier`.raw_value"])):
			try:
				rows = query()
			except frappe.PermissionError:
				continue
			self.assertNotIn("SYN-0007", frappe.as_json(rows))

	def test_customer_export_does_not_bypass_child_read_permission(self):
		from frappe.core.doctype.data_export.exporter import DataExporter

		hidden = frappe.copy_doc(self.customer)
		hidden.customer_name += " hidden export"
		hidden.kanzlei_identifiers[0].raw_value = "SYN-HIDDEN-EXPORT"
		hidden.insert()
		frappe.get_doc("User", self.manager.name).add_roles("Projects User")
		frappe.set_user(self.manager.name)
		self.assertFalse(frappe.has_permission("Customer", "read", doc=hidden.name))
		exporter = DataExporter(doctype="Customer", all_doctypes=True, with_data=True)
		exporter.build_response()
		data = frappe.response["result"]
		self.assertIn("SYN-0007", data)
		self.assertFalse("SYN-HIDDEN-EXPORT" in data, "Export exposed identifiers of an unreadable Customer")

	def test_verification_requires_context_but_not_operator_role(self):
		self.row.update({"identifier_type": "de_steuernummer", "raw_value": "001/23", "jurisdiction": None, "authority": None, "other_type": None})
		self.customer.save()
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_REQUEST"):
			self.verify()
		frappe.set_user("Administrator")
		self.customer.reload()
		self.customer.kanzlei_identifiers[0].update({"jurisdiction": "DE-BE", "authority": "FA Synthetic"})
		self.customer.save()
		frappe.share.add("Customer", self.customer.name, self.reader.name, read=1, write=1)
		frappe.set_user(self.reader.name)
		result = self.ids.verify_identifier(self.customer.name, self.row.name, str(self.customer.modified), "source")
		self.assertEqual(result["verified_by"], self.reader.name)

	def test_top_three_does_not_hide_ambiguity_and_duplicates_are_deduplicated(self):
		self.verify()
		doc = self.successful_letter()
		for i in range(4):
			frappe.set_user("Administrator")
			other = frappe.copy_doc(self.customer)
			other.customer_name = "AP06 duplicate " + str(i) + frappe.generate_hash(length=5)
			other.insert()
			self.ids.verify_identifier(other.name, other.kanzlei_identifiers[0].name, str(other.modified), "source")
			frappe.share.add("Customer", other.name, self.operator.name, read=1)
		frappe.set_user(self.operator.name)
		state = self.matching.get_matching_state(doc.name)
		self.assertEqual(len(state["matching"]["candidates"]), 3)
		self.assertEqual(state["matching"]["ambiguity"], "multiple_matches")
		extraction = self.matching._source(doc)
		extraction["recipients"].append(copy.deepcopy(extraction["recipients"][0]))
		self.assertEqual(self.matching._match(extraction)["ambiguity"], "multiple_matches")

	def test_historical_result_uses_hard_caps_and_invalid_result_is_not_accepted(self):
		self.verify()
		doc = self.successful_letter("AP01-02")
		frappe.db.set_single_value("Posteingang Settings", "max_actions", 1)
		self.assertEqual(len(self.matching.get_matching_state(doc.name)["matching"]["candidates"]), 1)
		run = frappe.get_doc("Incoming Letter Run", doc.current_run)
		value = json.loads(run.result_json)
		value["document_date"]["value"] = "2026-02-30"
		frappe.db.set_value("Incoming Letter Run", run.name, "result_json", json.dumps(value))
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_RESULT"):
			self.matching.get_matching_state(doc.name)

	def test_disabled_feature_allows_reads_and_replay_but_not_new_selection(self):
		doc = self.letter()
		result = self.matching.select_customer(doc.name, self.customer.name, "reason", 1, "key")
		frappe.db.set_single_value("Posteingang Settings", "enabled", 0)
		self.assertEqual(self.matching.get_matching_state(doc.name)["allowed_operations"], [])
		self.assertTrue(self.matching.select_customer(doc.name, self.customer.name, "reason", 1, "key")["replayed"])
		with self.assertRaisesRegex(frappe.ValidationError, "INVALID_TRANSITION"):
			self.matching.select_customer(doc.name, None, "clear", result["revision"], "new")

	def test_disabled_and_portal_users_cannot_search_or_read_matching(self):
		doc = self.letter()
		for actor in ("Guest", self.website.name):
			frappe.set_user(actor)
			with self.assertRaises(frappe.PermissionError):
				self.matching.search_customers(doc.name, "")
		frappe.set_user("Administrator")
		frappe.db.set_value("User", self.operator.name, "enabled", 0)
		frappe.set_user(self.operator.name)
		with self.assertRaises(frappe.PermissionError):
			self.matching.get_matching_state(doc.name)

	def test_duplicate_facts_merge_evidence_without_collapsing_unknown_identifiers(self):
		self.verify()
		doc = self.successful_letter()
		extraction = self.matching._source(doc)
		duplicate = copy.deepcopy(extraction["recipients"][0])
		for fact in (extraction["recipients"][0]["name"], extraction["recipients"][0]["identifiers"][0]):
			fact["evidence"] = []
		extraction["recipients"].append(duplicate)
		result = self.matching._match(extraction)
		self.assertEqual([c["customer"] for c in result["candidates"]], [self.customer.name])
		self.assertEqual(result["ambiguity"], "none")
		for index, recipient in enumerate(extraction["recipients"]):
			recipient["identifiers"][0].update({"type": "de_steuernummer", "value": "001/" + str(index), "jurisdiction": None, "authority": None, "other_type": None})
		self.assertEqual(self.matching._match(extraction)["ambiguity"], "multiple_addressees")

	def test_missing_customer_targets_have_safe_errors(self):
		doc = self.letter()
		with self.assertRaises(frappe.PermissionError):
			self.matching.select_customer(doc.name, "missing-ap06-customer", "reason", 1, "missing")
		with self.assertRaises(frappe.PermissionError):
			self.ids.verify_identifier("missing-ap06-customer", self.row.name, "2026-01-01", "source")

	def test_multiple_addressees_allow_clearing_existing_choice(self):
		doc = self.successful_letter("AP01-06")
		frappe.db.set_value("Incoming Letter", doc.name, {"selected_customer": self.customer.name, "customer_reason": "Previous analysis"})
		self.assertIn("clear_customer", self.matching.get_matching_state(doc.name)["allowed_operations"])
		self.matching.select_customer(doc.name, None, "Several addressees", doc.revision, "clear")
		self.assertFalse(frappe.db.get_value("Incoming Letter", doc.name, "selected_customer"))
