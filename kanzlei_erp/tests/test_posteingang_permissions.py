"""Real Frappe permission and query paths, without mocked authorization."""

import frappe

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


class TestPosteingangPermissions(PosteingangTestCase):
	def assert_access(self, doc, user, allowed):
		frappe.set_user(user)
		self.assertEqual(bool(frappe.has_permission("Incoming Letter", "read", doc=doc)), allowed)
		try:
			listed = doc.name in frappe.get_list("Incoming Letter", pluck="name", limit=0)
		except frappe.PermissionError:
			listed = False
		self.assertEqual(listed, allowed)
		file = frappe.get_doc("File", doc.original_file)
		self.assertEqual(bool(frappe.has_permission("File", "read", doc=file)), allowed)
		try:
			listed = file.name in frappe.get_list("File", pluck="name", limit=0)
		except frappe.PermissionError:
			listed = False
		self.assertEqual(listed, allowed)
		from kanzlei_erp.object_storage import open_file

		if allowed:
			with open_file(file) as source:
				self.assertTrue(source.read(5).startswith(b"%PDF"))
		else:
			with self.assertRaises(frappe.PermissionError):
				with open_file(file) as source:
					source.read()

	def test_draft_and_confirmed_matrix(self):
		doc = self.letter()
		for user, allowed in (
			(self.operator.name, True),
			(self.reader.name, False),
			(self.stranger.name, False),
			(self.website.name, False),
			("Guest", False),
		):
			with self.subTest(state="draft", user=user):
				self.assert_access(doc, user, allowed)
		doc = self.confirm_fixture(doc)
		for user, allowed in (
			(self.operator.name, False),
			(self.reader.name, True),
			(self.stranger.name, False),
			(self.manager.name, True),
			(self.website.name, False),
			("Guest", False),
		):
			with self.subTest(state="confirmed", user=user):
				# No base role access for Guest/Website, so list must throw rather than leak.
				if user in ("Guest", self.website.name):
					self.assertFalse(frappe.has_permission("Incoming Letter", "read", doc=doc, user=user))
				else:
					self.assert_access(doc, user, allowed)

	def test_quarantine_uses_original_customer(self):
		doc = self.confirm_fixture(self.letter(), quarantine=True)
		self.assert_access(doc, self.reader.name, False)
		self.assert_access(doc, self.operator.name, False)
		self.assert_access(doc, self.manager.name, True)

	def test_owner_and_share_do_not_bypass_revocation(self):
		doc = self.confirm_fixture(self.letter())
		frappe.share.add("File", doc.original_file, self.reader.name, read=1)
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		self.assert_access(doc, self.reader.name, False)
		self.assert_access(doc, self.operator.name, False)

	def test_document_share_cannot_override_customer_policy(self):
		doc = self.confirm_fixture(self.letter())
		frappe.share.add("Incoming Letter", doc.name, self.stranger.name, read=1)
		frappe.set_user(self.stranger.name)
		self.assertFalse(frappe.has_permission("Incoming Letter", "read", doc=doc))

	def test_settings_disabled_does_not_hide_history(self):
		doc = self.confirm_fixture(self.letter())
		frappe.db.set_single_value("Posteingang Settings", "enabled", 0)
		self.assert_access(doc, self.reader.name, True)

	def test_run_event_and_child_access_follow_persisted_parent(self):
		doc = self.confirm_fixture(self.letter())
		from uuid import uuid4

		run = frappe.get_doc(
			{
				"doctype": "Incoming Letter Run",
				"name": frappe.generate_hash(length=10),
				"letter": doc.name,
				"generation": 1,
				"schema_version": "1.0",
				"prompt_version": "fixture",
				"provider": "fake",
				"status": "Succeeded",
				"queued_at": frappe.utils.now_datetime(),
				"finished_at": frappe.utils.now_datetime(),
				"result_json": "{}",
			}
		)
		run.db_insert()
		action = frappe.get_doc(
			{
				"doctype": "Incoming Letter Action",
				"name": frappe.generate_hash(length=10),
				"parent": doc.name,
				"parenttype": "Incoming Letter",
				"parentfield": "actions",
				"idx": 1,
				"action_id": str(uuid4()),
				"title": "Synthetic",
				"description": "Fixture only",
			}
		)
		action.db_insert()
		event = frappe.get_doc("Incoming Letter Event", {"letter": doc.name})
		for user, allowed in ((self.reader.name, True), (self.stranger.name, False)):
			frappe.set_user(user)
			for record in (run, event, action):
				with self.subTest(user=user, doctype=record.doctype):
					self.assertEqual(bool(frappe.has_permission(record.doctype, "read", doc=record)), allowed)
			for doctype in ("Incoming Letter Run", "Incoming Letter Event"):
				self.assertEqual(bool(frappe.get_list(doctype, filters={"letter": doc.name})), allowed)

	def test_legacy_query_engine_cannot_reintroduce_shared_letter(self):
		from frappe.model.db_query import DatabaseQuery

		doc = self.confirm_fixture(self.letter())
		frappe.share.add("Incoming Letter", doc.name, self.stranger.name, read=1)
		frappe.set_user(self.stranger.name)
		rows = DatabaseQuery("Incoming Letter").execute(fields=["name"])
		self.assertNotIn(doc.name, [r.name for r in rows])

	def test_settings_require_manager_even_if_shared(self):
		frappe.set_user("Administrator")
		settings = frappe.get_single("Posteingang Settings")
		# Emulate an old/injected share; Single UI does not offer sharing.
		share = frappe.get_doc(
			{
				"doctype": "DocShare",
				"share_doctype": settings.doctype,
				"share_name": settings.name,
				"user": self.operator.name,
				"read": 1,
			}
		)
		share.db_insert()
		frappe.set_user(self.operator.name)
		self.assertFalse(frappe.has_permission(settings.doctype, "read", doc=settings))

	def test_draft_selected_customer_is_not_generic_list_data(self):
		doc = self.letter()
		frappe.db.set_value("Incoming Letter", doc.name, "selected_customer", self.customer.name)
		frappe.set_user(self.operator.name)
		rows = frappe.get_list("Incoming Letter", fields=["name", "selected_customer", "customer_reason"])
		self.assertNotIn(self.customer.name, frappe.as_json(rows))

	def test_revoking_operator_role_closes_common_inbox(self):
		doc = self.letter()
		frappe.set_user("Administrator")
		user = frappe.get_doc("User", self.operator.name)
		user.set("roles", [r for r in user.roles if r.role != "Posteingang Operator"])
		user.save()
		self.assert_access(doc, self.operator.name, False)

	def test_disabled_system_user_cannot_read_shared_customer_letter(self):
		doc = self.confirm_fixture(self.letter())
		user = frappe.get_doc("User", self.reader.name)
		user.enabled = 0
		user.save()
		self.assert_access(doc, self.reader.name, False)
