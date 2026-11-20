"""Typed mail projection through actual ERP records and connector permissions."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.file_manager import save_file
from pypdf import PdfWriter
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.ai_connector import api, external_processing, policy
from kanzlei_erp.ai_connector.errors import ConnectorError
from kanzlei_erp.tests import test_email_history as fixtures
from kanzlei_erp.tests import test_mandant_documents as drive_fixtures


class TestAIConnectorEmail(FrappeTestCase):
	make_customer = fixtures.TestEmailHistory.make_customer
	make_contact = fixtures.TestEmailHistory.make_contact
	make_mail = fixtures.TestEmailHistory.make_mail

	def setUp(self):
		frappe.set_user("Administrator")
		fixtures.TestEmailHistory.setUp(self)
		self.other = self.make_customer()
		self.config = {
			"email_enabled": True,
			"enabled": True,
			"audience": "synthetic-ai",
			"connector_id": "synthetic-erp",
			"service_key_sha256": "b" * 64,
		}
		config_patch = patch.object(
			api.auth, "get_site_config", return_value={"kanzlei_ai_connector": self.config}
		)
		config_patch.start()
		self.addCleanup(config_patch.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		if getattr(self, "committed", False):
			frappe.db.rollback()
			frappe.db.delete("Sessions", {"sid": self.sid})
		for doctype, names in (
			("File", self.files),
			("Communication", self.mails),
			("Contact", self.contacts),
			("Customer", self.customers),
			("User", self.users),
		):
			for name in names:
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		if getattr(self, "committed", False):
			frappe.db.commit()
		super().tearDown()

	def linked(self, **fields):
		return self.make_mail(reference_doctype="Customer", reference_name=self.customer.name, **fields)

	def add_link(self, message, client):
		message.add_link("Customer", client.name)
		message.timeline_links[-1].db_insert()

	def reference(self, manifest):
		return {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}

	def attachment(self, message, name="synthetic.pdf"):
		pdf = BytesIO()
		writer = PdfWriter()
		writer.add_blank_page(width=72, height=72)
		writer.add_metadata({"/Title": "Synthetic " + frappe.generate_hash(length=16)})
		writer.write(pdf)
		self.pdf_content = pdf.getvalue().replace(b"%\xe2\xe3\xcf\xd3", b"%test", 1)
		file = save_file(
			name,
			self.pdf_content,
			"Communication" if message else None,
			message.name if message else None,
			is_private=1,
		)
		self.files.append(file.name)
		return file

	def test_flag_is_literal_and_opt_in(self):
		from kanzlei_erp.ai_connector import mail

		message = self.linked()
		source_id = "email:" + message.name
		for value in (None, False, "true", 1):
			self.config["email_enabled"] = value
			self.assertFalse(mail.enabled())
			self.assertIsNone(mail.project(source_id, self.customer.name))
		self.config["email_enabled"] = True
		self.assertTrue(mail.enabled())

	def test_incoming_and_outgoing_messages_have_separate_bounded_json_bytes(self):
		from kanzlei_erp.ai_connector import mail

		incoming = self.linked(content="<p>Unterlagen für März</p>", recipients="office@example.invalid")
		outgoing = self.linked(sent_or_received="Sent", content="<p>Получено</p>", recipients=self.address)
		frappe.db.set_value("Communication", outgoing.name, "in_reply_to", incoming.name)
		for message, direction, body, recipients, reply in (
			(incoming, "incoming", "<p>Unterlagen für März</p>", ["office@example.invalid"], None),
			(outgoing, "outgoing", "<p>Получено</p>", [self.address], "email:" + incoming.name),
		):
			manifest = mail.project("email:" + message.name, self.customer.name)
			self.assertEqual(manifest["source_type"], "email")
			self.assertEqual(manifest["mime_type"], "application/vnd.kanzlei.email+json")
			self.assertEqual(manifest["locator"], "/desk/communication/" + message.name)
			content = mail.read_current(self.reference(manifest))
			self.assertEqual(manifest["byte_size"], len(content))
			payload = json.loads(content.decode("utf-8"))
			self.assertEqual(payload.pop("subject"), message.subject)
			self.assertEqual(payload.pop("sender"), self.address)
			self.assertIn("T", payload.pop("sent_at"))
			self.assertEqual(
				payload,
				{
					"version": 1,
					"direction": direction,
					"body": body,
					"body_format": "html",
					"recipients": recipients,
					"reply_to": reply,
				},
			)
		with patch.object(mail, "MAX_BYTES", 25):
			self.assertIsNone(mail.project("email:" + incoming.name, self.customer.name))

	def test_indirect_unlinked_and_ambiguous_matches_have_no_source(self):
		from kanzlei_erp.ai_connector import mail

		contact = self.make_contact(customers=[self.customer, self.other])
		for message in (
			self.make_mail(),
			self.make_mail(reference_doctype="Contact", reference_name=contact.name),
		):
			# Frappe also copies Dynamic Links during insert. Remove these copies
			# to exercise records whose only persisted relationship is Contact.
			frappe.db.delete("Communication Link", {"parent": message.name, "link_doctype": "Customer"})
			self.assertIsNone(mail.project("email:" + message.name, self.customer.name))
		message = self.make_mail()
		self.add_link(message, self.customer)
		self.assertIsNotNone(mail.project("email:" + message.name, self.customer.name))

	def test_reader_can_project_only_their_direct_customer_context(self):
		from kanzlei_erp.ai_connector import mail

		with patch("frappe.core.doctype.user.user.throttle_user_creation"):
			reader = frappe.get_doc(
				{
					"doctype": "User",
					"email": f"ai-mail-{frappe.generate_hash()}@example.invalid",
					"first_name": "synthetic mail reader",
					"send_welcome_email": 0,
					"roles": [{"role": "Projects User"}],
				}
			).insert()
		self.users.append(reader.name)
		message = self.make_mail()
		self.add_link(message, self.customer)
		self.add_link(message, self.other)
		frappe.share.add("Customer", self.customer.name, reader.name, read=1)
		frappe.share.add("Communication", message.name, reader.name, read=1)
		frappe.set_user(reader.name)
		source_id = "email:" + message.name
		self.assertIsNone(mail.project(source_id, self.other.name))
		self.assertEqual(mail.project(source_id, self.customer.name)["client_ids"], [self.customer.name])
		self.assertEqual(mail.external_clients(source_id), {self.customer.name, self.other.name})

	def test_private_attachment_is_separately_checked_and_requires_actual_binding(self):
		from kanzlei_erp.ai_connector import mail

		message = self.linked()
		file = self.attachment(message)
		source_id = "file:" + file.name
		manifest = mail.project(source_id, self.customer.name)
		self.assertEqual(manifest["parent_message_id"], "email:" + message.name)
		self.assertEqual(manifest["mime_type"], "application/pdf")
		self.assertEqual(mail.read_current(self.reference(manifest)), self.pdf_content)
		has_permission = frappe.has_permission

		def permit(doctype=None, ptype="read", doc=None, **kwargs):
			if doctype == "File":
				return False
			return has_permission(doctype, ptype, doc=doc, **kwargs)

		with patch("frappe.has_permission", side_effect=permit):
			self.assertIsNone(mail.project(source_id, self.customer.name))
			with self.assertRaises(ConnectorError):
				mail.read_current(self.reference(manifest))
		frappe.db.set_value("File", file.name, "attached_to_name", self.make_mail().name)
		self.assertIsNone(mail.project(source_id, self.customer.name))

	def test_remote_attachment_is_rejected_before_opening_storage(self):
		from kanzlei_erp.ai_connector import mail

		file = self.attachment(self.linked())
		frappe.db.set_value("File", file.name, "file_url", "https://synthetic.invalid/test.pdf")
		with patch("kanzlei_erp.object_storage.open_file", side_effect=AssertionError):
			self.assertIsNone(mail.project("file:" + file.name, self.customer.name))

	def test_read_revalidates_revision_and_permissions_after_bytes(self):
		from kanzlei_erp.ai_connector import mail

		message = self.linked()
		value = self.reference(mail.project("email:" + message.name, self.customer.name))

		def replace():
			frappe.db.set_value("Communication", message.name, "content", "New body", update_modified=False)

		with self.assertRaises(ConnectorError) as stale:
			mail.read_current(value, before_final_projection=replace)
		self.assertEqual(stale.exception.status, 409)
		value = self.reference(mail.project(value["source_id"], self.customer.name))

		def revoke():
			frappe.db.set_value("Communication", message.name, "reference_name", self.other.name)

		with self.assertRaises(ConnectorError) as denied:
			mail.read_current(value, before_final_projection=revoke)
		self.assertEqual(denied.exception.status, 403)

	def test_delegated_catalogue_authorize_read_and_resolve_agree(self):
		message = self.linked()
		file = self.attachment(message)
		scope = {"kind": "client", "client_id": self.customer.name}
		ids = {"email:" + message.name, "file:" + file.name}
		items, cursor = {}, None
		for _ in range(10):
			page = policy.documents(self.customer.name, page_limit=1, cursor=cursor)
			for item in page["items"]:
				self.assertNotIn(item["source_id"], items)
				items[item["source_id"]] = item
			cursor = page["next_cursor"]
			if cursor is None:
				break
		self.assertIsNone(cursor)
		self.assertTrue(ids.issubset(items))
		values = [self.reference(items[source_id]) for source_id in ids]
		self.assertTrue(all(row["allowed"] for row in policy.authorize_sources(scope, values)["decisions"]))
		for value in values:
			item = items[value["source_id"]]
			self.assertEqual(
				policy.resolve_source_locator(scope, value), {"version": 1, "locator": item["locator"]}
			)
			self.assertEqual(len(policy.read_source(scope, value)), item["byte_size"])
		self.config["email_enabled"] = False
		self.assertFalse(any(row["allowed"] for row in policy.authorize_sources(scope, values)["decisions"]))

	def test_enabling_mail_preserves_a_managed_drive_pdf_manifest_and_bytes(self):
		root = drive_fixtures.TestMandantDocuments.make_drive_root(self)
		try:
			file = self.attachment(None, name="drive.pdf")
			frappe.db.set_value(
				"File",
				file.name,
				{"folder": root.name, "status": "Active", "kanzlei_drive_customer": self.customer.name},
			)
			self.config["email_enabled"] = False
			manifest = policy.documents(self.customer.name)["items"][0]
			value = self.reference(manifest)
			self.assertEqual(value["source_id"], "file:" + file.name)
			self.assertNotIn("parent_message_id", manifest)
			self.linked()
			self.config["email_enabled"] = True
			scope = {"kind": "client", "client_id": self.customer.name}
			page = policy.documents(self.customer.name)
			self.assertEqual(
				policy.documents(self.customer.name, cursor=page["next_cursor"])["items"], [manifest]
			)
			self.assertTrue(policy.authorize_sources(scope, [value])["decisions"][0]["allowed"])
			self.assertEqual(policy.read_source(scope, value), self.pdf_content)
		finally:
			frappe.db.set_value("Customer", self.customer.name, "kanzlei_drive_folder", None)
			frappe.db.delete("File", {"name": root.name})

	def test_external_processing_requires_literal_grant_for_every_direct_customer(self):
		from kanzlei_erp.ai_connector import mail

		message = self.linked()
		self.add_link(message, self.other)
		file = self.attachment(message)
		scope = {"kind": "client", "client_id": self.customer.name}
		self.config["external_processing"] = {self.customer.name: {"embed": True}}
		for source_id in ("email:" + message.name, "file:" + file.name):
			value = self.reference(mail.project(source_id, self.customer.name))
			for grant in (None, 1, True):
				self.config["external_processing"][self.other.name] = {"embed": grant}
				if grant is True:
					external_processing._check_current(
						"embed", scope, [value], background_clients={self.customer.name}
					)
				else:
					with self.assertRaises(ConnectorError) as denied:
						external_processing._check_current(
							"embed", scope, [value], background_clients={self.customer.name}
						)
					self.assertEqual(denied.exception.status, 403)

	def test_processing_grants_and_requested_revision_share_one_direct_customer_projection(self):
		from kanzlei_erp.ai_connector import mail

		message = self.linked()
		self.add_link(message, self.other)
		source_id = "email:" + message.name
		value = self.reference(mail.project(source_id, self.customer.name))
		frappe.db.delete("Communication Link", {"parent": message.name, "link_name": self.other.name})
		self.config["external_processing"] = {self.customer.name: {"embed": True}}
		external_clients = mail.external_clients

		def relink_after_grant_projection(*args, **kwargs):
			clients = external_clients(*args, **kwargs)
			self.add_link(message, self.other)
			return clients

		scope = {"kind": "client", "client_id": self.customer.name}
		with (
			patch.object(mail, "external_clients", side_effect=relink_after_grant_projection),
			self.assertRaises(ConnectorError) as denied,
		):
			external_processing._check_current("embed", scope, [value])
		self.assertEqual(denied.exception.status, 403)

	def test_session_attachment_original_rechecks_binding_after_a_concurrent_commit(self):
		from kanzlei_erp.ai_connector import mail
		from kanzlei_erp.object_storage import open_file

		message = self.linked()
		file = self.attachment(message)
		item = mail.project("file:" + file.name, self.customer.name)
		message_item = mail.project("email:" + message.name, self.customer.name)
		locator = urlsplit(item["locator"])
		self.assertEqual(locator.path, "/api/method/kanzlei_erp.ai_connector.api.download_mail_attachment")
		arguments = {key: values[0] for key, values in parse_qs(locator.query).items()}
		self.assertEqual(set(arguments), {"customer", "file_id", "revision"})
		site, original_user, original_sid = frappe.local.site, frappe.session.user, frappe.session.sid
		self.sid = "ai-mail-session-" + frappe.generate_hash(length=24)
		frappe.db.sql(
			"INSERT INTO `tabSessions` (sid,user,lastupdate,sessiondata,status) VALUES (%s,%s,%s,%s,'Active')",
			(self.sid, original_user, frappe.utils.now(), json.dumps({"session_expiry": "01:00:00"})),
		)
		self.committed = True
		frappe.db.commit()
		key = "synthetic-ai9-service-" + frappe.generate_hash(length=32)
		self.config["service_key_sha256"] = hashlib.sha256(key.encode()).hexdigest()

		def remove_link():
			frappe.init(site)
			frappe.connect()
			try:
				frappe.db.set_value("Communication", message.name, "reference_name", self.other.name)
				frappe.db.commit()
			finally:
				frappe.destroy()

		@contextmanager
		def stream_with_revocation(file):
			with open_file(file) as stream:
				yield stream
			with ThreadPoolExecutor(max_workers=1) as executor:
				executor.submit(remove_link).result(timeout=15)

		request = Request(EnvironBuilder(method="GET").get_environ())
		delegated = Request(
			EnvironBuilder(method="POST", headers={"X-Kanzlei-AI-Service-Key": key}).get_environ()
		)
		original_db = frappe.local.db
		try:
			frappe.session.sid = self.sid
			with patch.object(frappe.local, "request", delegated, create=True):
				ticket = api.issue_ticket()["ticket"]
				token = api.exchange_ticket(ticket=ticket)["access_token"]
				scope = {"kind": "client", "client_id": self.customer.name}
				arguments_for_mail = {
					"access_token": token,
					"scope": scope,
					"source": self.reference(message_item),
				}
				response = api.read_source(**arguments_for_mail)
				self.assertEqual(response.mimetype, mail.MIME_TYPE)
				self.assertEqual(response.headers["X-AI-Revision"], message_item["revision"])
				self.assertEqual(
					api.resolve_source_locator(**arguments_for_mail)["locator"], message_item["locator"]
				)
			with patch.object(frappe.local, "request", request, create=True):
				frappe.session.sid = "Guest"
				self.assertEqual(api.download_mail_attachment(**arguments).status_code, 401)
				frappe.session.sid = self.sid
				response = api.download_mail_attachment(**arguments)
				self.assertEqual(response.mimetype, "application/pdf")
				self.assertEqual(response.get_data(), self.pdf_content)
				with patch("kanzlei_erp.object_storage.open_file", stream_with_revocation):
					self.assertEqual(api.download_mail_attachment(**arguments).status_code, 403)
				self.assertIs(frappe.local.db, original_db)
		finally:
			frappe.session.sid = original_sid
