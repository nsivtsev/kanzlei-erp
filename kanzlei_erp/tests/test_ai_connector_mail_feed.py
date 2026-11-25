"""Exercise mail feed candidates and event hooks with real database transactions."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.model.base_document import get_controller
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp import ai_connector, email_history
from kanzlei_erp.ai_connector import inventory


class TestAIConnectorFeedControllers(FrappeTestCase):
	def test_feed_doctypes_resolve_their_controllers(self):
		for doctype, classname in (
			("AI Source Event", "AISourceEvent"),
			("AI Source State", "AISourceState"),
		):
			with self.subTest(doctype=doctype):
				self.assertEqual(get_controller(doctype).__name__, classname)


class TestAIConnectorMailFeed(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.client = self.customer()
		self.other = self.customer()
		self.clients = {self.client, self.other}
		self.active = True
		self.project = Mock(side_effect=self.project_source)
		self.reader = Mock(return_value=b"fixture")
		stub = SimpleNamespace(enabled=lambda: self.active, project=self.project, read_current=self.reader)
		self.enterContext(patch.object(ai_connector, "mail", stub, create=True))
		self.enterContext(
			patch.object(
				inventory,
				"settings",
				return_value=(
					"Administrator",
					self.clients,
					"synthetic-binding",
				),
			)
		)

	def project_source(self, source_id, client_id):
		if source_id.startswith("email:"):
			name = source_id[6:]
		else:
			name = frappe.db.get_value("File", source_id[5:], "attached_to_name")
		if not name or not frappe.db.exists("Communication", name):
			return None
		doc = frappe.get_doc("Communication", name)
		if (doc.reference_doctype, doc.reference_name) == ("Customer", client_id) or any(
			(link.link_doctype, link.link_name) == ("Customer", client_id) for link in doc.timeline_links
		):
			return {"source_id": source_id, "client_id": client_id}
		return None

	def customer(self):
		return (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "_Mail Feed " + frappe.generate_hash(),
					"customer_type": "Company",
					"customer_group": "Commercial",
					"territory": "All Territories",
				}
			)
			.insert()
			.name
		)

	def contact(self):
		return frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": "_Mail Feed " + frappe.generate_hash(),
			}
		).insert()

	def message(self, client=None, **values):
		if client:
			values.update(reference_doctype="Customer", reference_name=client)
		return frappe.get_doc(
			{
				"doctype": "Communication",
				"communication_type": "Communication",
				"communication_medium": "Email",
				"sent_or_received": "Received",
				"subject": "Fixture only",
				"content": "Original body",
				"sender": "feed@example.invalid",
				"recipients": "office@example.invalid",
				**values,
			}
		).insert()

	def attachment(self, mail):
		# The feed delegates byte/storage eligibility. Only the persisted binding is needed here.
		file = frappe.get_doc(
			{
				"doctype": "File",
				"name": frappe.generate_hash(),
				"file_name": "fixture.pdf",
				"file_url": "/private/files/" + frappe.generate_hash() + ".pdf",
				"is_private": 1,
				"attached_to_doctype": "Communication",
				"attached_to_name": mail.name,
			}
		)
		file.db_insert()
		return file

	def test_inventory_pages_through_scoped_unique_source_client_pairs(self):
		mail = self.message(
			self.client,
			timeline_links=[
				{"link_doctype": "Customer", "link_name": self.client},
				{"link_doctype": "Customer", "link_name": self.other},
			],
		)
		outgoing = self.message(
			sent_or_received="Sent",
			timeline_links=[
				{"link_doctype": "Customer", "link_name": self.client},
			],
		)
		self.message(self.customer())
		self.message(timeline_links=[{"link_doctype": "Contact", "link_name": self.contact().name}])
		file = self.attachment(mail)
		cursor = None
		items = []
		watermarks = set()
		for _ in range(14):
			page = inventory.inventory(cursor, limit=1)
			watermarks.add(page["changes_cursor"])
			items.extend((item["source_id"], item["client_id"]) for item in page["items"])
			cursor = page["next_cursor"]
			if cursor is None:
				break
		self.assertIsNone(cursor)
		self.assertEqual(len(watermarks), 1)
		self.assertCountEqual(
			items,
			[
				("email:" + mail.name, self.client),
				("email:" + mail.name, self.other),
				("email:" + outgoing.name, self.client),
				("file:" + file.name, self.client),
				("file:" + file.name, self.other),
			],
		)
		self.assertEqual(self.project.call_count, len(items))

	def test_flag_changes_cursor_binding_and_preserves_disabled_drive_binding(self):
		self.active = False
		legacy = inventory.inventory()
		legacy_page = inventory.encode(
			{"kind": "inventory", "watermark": 0, "after": ""}, "synthetic-binding"
		)
		self.assertIsNone(inventory.inventory(legacy_page)["next_cursor"])
		self.active = True
		with self.assertRaises(inventory.ConnectorError):
			inventory.changes(legacy["changes_cursor"])
		with self.assertRaises(inventory.ConnectorError):
			inventory.inventory(legacy_page)
		enabled = inventory.inventory()
		self.active = False
		with self.assertRaises(inventory.ConnectorError):
			inventory.changes(enabled["changes_cursor"])

	def changes(self, cursor):
		return {
			(item["source_id"], item["client_id"], item["operation"])
			for item in inventory.changes(cursor, limit=100)["items"]
		}

	def test_link_update_invalidates_previous_and_current_clients_and_attachments(self):
		mail = self.message(self.client)
		file = self.attachment(mail)
		cursor = inventory.inventory()["changes_cursor"]
		frappe.db.savepoint("mail_feed_link")
		mail.reference_name = self.other
		mail.save()
		self.assertEqual(
			self.changes(cursor),
			{
				(prefix + name, client, operation)
				for prefix, name in (("email:", mail.name), ("file:", file.name))
				for client, operation in ((self.client, "delete"), (self.other, "upsert"))
			},
		)
		frappe.db.rollback(save_point="mail_feed_link")
		self.assertEqual(self.changes(cursor), set())

	def test_attachment_move_and_delete_invalidate_old_and_new_parents(self):
		old, new = self.message(self.client), self.message(self.other)
		file = self.attachment(old)
		cursor = inventory.inventory()["changes_cursor"]
		file.db_set("attached_to_name", new.name)
		self.assertEqual(
			self.changes(cursor),
			{
				("file:" + file.name, self.client, "delete"),
				("file:" + file.name, self.other, "upsert"),
			},
		)
		cursor = inventory.inventory()["changes_cursor"]
		frappe.delete_doc("File", file.name, force=True)
		self.assertEqual(self.changes(cursor), {("file:" + file.name, self.other, "delete")})

	def undo_send(self, mail):
		from frappe.handler import execute_cmd
		from frappe.utils import get_datetime
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		route = "frappe.core.doctype.communication.email.undo_email_send"
		request = Request(EnvironBuilder(method="POST").get_environ())
		with (
			patch.object(frappe.local, "request", request, create=True),
			patch.object(frappe.local, "form_dict", frappe._dict(cmd=route, communication_name=mail.name)),
			patch(
				"frappe.core.doctype.communication.email.now_datetime",
				return_value=get_datetime(mail.creation),
			),
		):
			return execute_cmd(route)

	def test_undo_send_route_invalidates_detached_pdf_for_all_former_customers(self):
		mail = self.message(self.client, sent_or_received="Sent")
		mail.add_link("Customer", self.other, autosave=True)
		file = self.attachment(mail)
		queue = frappe.get_doc(
			{
				"doctype": "Email Queue",
				"sender": "feed@example.invalid",
				"message": "Fixture only",
				"status": "Not Sent",
				"priority": 1,
				"communication": mail.name,
				"recipients": [{"recipient": "office@example.invalid", "status": "Not Sent"}],
			}
		).insert()
		cursor = inventory.inventory()["changes_cursor"]
		frappe.db.savepoint("undo_fixture")

		result = self.undo_send(mail)

		self.assertEqual(result["attachments"], [file.name])
		self.assertEqual(result["subject"], mail.subject)
		self.assertFalse(frappe.db.exists("Communication", mail.name))
		self.assertFalse(frappe.db.exists("Email Queue", queue.name))
		file.reload()
		self.assertFalse(file.attached_to_name)
		self.assertFalse(file.attached_to_doctype)
		self.assertEqual(
			self.changes(cursor),
			{
				(source_id, customer, "delete")
				for source_id in ("email:" + mail.name, "file:" + file.name)
				for customer in (self.client, self.other)
			},
		)

		frappe.db.rollback(save_point="undo_fixture")
		self.assertTrue(frappe.db.exists("Communication", mail.name))
		self.assertTrue(frappe.db.exists("Email Queue", queue.name))
		self.assertEqual(frappe.db.get_value("File", file.name, "attached_to_name"), mail.name)
		self.assertEqual(self.changes(cursor), set())

	def test_undo_send_route_preserves_owner_check(self):
		mail = self.message(self.client, sent_or_received="Sent")
		file = self.attachment(mail)
		frappe.db.set_value("Communication", mail.name, "owner", "Guest")
		cursor = inventory.inventory()["changes_cursor"]

		with self.assertRaises(frappe.ValidationError):
			self.undo_send(mail)

		self.assertTrue(frappe.db.exists("Communication", mail.name))
		self.assertEqual(frappe.db.get_value("File", file.name, "attached_to_name"), mail.name)
		self.assertEqual(self.changes(cursor), set())

	def test_undo_send_route_still_detaches_when_mail_feed_is_disabled(self):
		mail = self.message(self.client, sent_or_received="Sent")
		file = self.attachment(mail)
		self.active = False
		cursor = inventory.inventory()["changes_cursor"]

		self.assertEqual(self.undo_send(mail)["attachments"], [file.name])
		self.assertFalse(frappe.db.exists("Communication", mail.name))
		self.assertFalse(frappe.db.get_value("File", file.name, "attached_to_name"))
		self.assertEqual(self.changes(cursor), set())

	def test_attachment_save_invalidates_the_previous_customer(self):
		from io import BytesIO
		from pathlib import Path

		from frappe.utils.file_manager import save_file
		from pypdf import PdfWriter

		old, new = self.message(self.client), self.message(self.other)
		pdf = BytesIO()
		writer = PdfWriter()
		writer.add_blank_page(width=72, height=72)
		writer.write(pdf)
		file = save_file(
			frappe.generate_hash(length=16) + ".pdf",
			pdf.getvalue().replace(b"%\xe2\xe3\xcf\xd3", b"%test", 1),
			"Communication",
			old.name,
			is_private=1,
		)
		self.addCleanup(Path(file.get_full_path()).unlink, missing_ok=True)
		cursor = inventory.inventory()["changes_cursor"]
		file.attached_to_name = new.name
		file.save()
		self.assertEqual(
			self.changes(cursor),
			{
				("file:" + file.name, self.client, "delete"),
				("file:" + file.name, self.other, "upsert"),
			},
		)

	def test_disabled_feed_hides_mail_and_attachment_invalidations_from_old_drive_cursor(self):
		self.active = False
		cursor = inventory.inventory()["changes_cursor"]
		self.active = True
		mail = self.message(self.client)
		self.attachment(mail)
		self.active = False
		self.assertEqual(self.changes(cursor), set())

	def test_explicit_customer_link_removal_and_parent_delete_invalidate_attachments(self):
		mail = self.message(self.client)
		mail.add_link("Customer", self.other, autosave=True)
		file = self.attachment(mail)
		cursor = inventory.inventory()["changes_cursor"]
		mail.remove_link("Customer", self.other, autosave=True)
		self.assertEqual(
			self.changes(cursor),
			{
				("email:" + mail.name, self.client, "upsert"),
				("email:" + mail.name, self.other, "delete"),
				("file:" + file.name, self.client, "upsert"),
				("file:" + file.name, self.other, "delete"),
			},
		)
		cursor = inventory.inventory()["changes_cursor"]
		frappe.delete_doc("Communication", mail.name, force=True)
		self.assertEqual(
			self.changes(cursor),
			{
				("email:" + mail.name, self.client, "delete"),
				("file:" + file.name, self.client, "delete"),
			},
		)

	def test_archive_reply_update_invalidates_message_without_modifying_timestamp(self):
		from kanzlei_erp.email_archive import _link_pending_replies

		parent = self.message(self.other)
		reply = self.message(self.client)
		parent.db_set({"email_account": "_fixture", "message_id": "parent@example.invalid"})
		cursor = inventory.inventory()["changes_cursor"]
		journal = SimpleNamespace(
			thread_records=lambda: [(reply.name, "reply@example.invalid", "<parent@example.invalid>")]
		)
		self.assertEqual(_link_pending_replies(journal, "_fixture"), 1)
		modified = reply.modified
		reply.reload()
		self.assertEqual(str(reply.modified), str(modified))
		self.assertEqual(reply.in_reply_to, parent.name)
		self.assertEqual(self.changes(cursor), {("email:" + reply.name, self.client, "upsert")})

	def test_history_insert_invalidates_explicit_customer_and_ignores_contact_only_change(self):
		mail = self.message()
		file = self.attachment(mail)
		cursor = inventory.inventory()["changes_cursor"]
		with patch.object(
			email_history,
			"_matching_contacts",
			return_value=[
				frappe._dict(customer=self.client, contact=self.contact().name),
			],
		):
			self.assertEqual(email_history.link_communication(mail.name), ("linked", 2))
		self.assertEqual(
			self.changes(cursor),
			{
				("email:" + mail.name, self.client, "upsert"),
				("file:" + file.name, self.client, "upsert"),
			},
		)
		cursor = inventory.inventory()["changes_cursor"]
		mail.reload()
		contact = self.contact()
		mail.add_link("Contact", contact.name, autosave=True)
		mail.db_set({"reference_doctype": "Contact", "reference_name": contact.name})
		self.assertEqual(self.changes(cursor), set())

	def test_background_read_delegates_attachment_and_preserves_final_projection_callback(self):
		file = self.attachment(self.message(self.client))
		source = {"source_id": "file:" + file.name, "client_id": self.client, "revision": "a" * 64}
		final = Mock()
		self.assertEqual(
			inventory.read_current(source, self.clients, before_final_projection=final), b"fixture"
		)
		self.reader.assert_called_once_with(source, before_final_projection=final)
