import hashlib
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp.email_archive import (
	_batches,
	classify_message_direction,
	import_archive,
	message_dedupe_key,
	parse_imap_list,
	should_import_mailbox,
)


class TestEmailArchiveHelpers(FrappeTestCase):
	def test_parse_imap_list_preserves_folder_names_and_flags(self):
		folders = parse_imap_list(
			[
				b'(\\HasNoChildren) "/" "INBOX"',
				b'(\\HasNoChildren \\Sent) "/" "Sent Items"',
				b'(\\HasChildren) "/" "Archiv 2024"',
				b'(\\HasNoChildren) "/" "Mandanten &APw-"',
			]
		)
		self.assertEqual(
			folders,
			[
				{"name": "INBOX", "server_name": "INBOX", "flags": {"\\hasnochildren"}},
				{"name": "Sent Items", "server_name": "Sent Items", "flags": {"\\hasnochildren", "\\sent"}},
				{"name": "Archiv 2024", "server_name": "Archiv 2024", "flags": {"\\haschildren"}},
				{
					"name": "Mandanten ü",
					"server_name": "Mandanten &APw-",
					"flags": {"\\hasnochildren"},
				},
			],
		)

	def test_import_excludes_system_junk_trash_and_draft_folders(self):
		for name, flags in (
			("[Gmail]/Spam", {"\\junk"}),
			("Papierkorb", set()),
			("Drafts", {"\\drafts"}),
		):
			with self.subTest(name=name):
				self.assertFalse(should_import_mailbox(name, flags))
		self.assertTrue(should_import_mailbox("Mandant Drafts", set()))
		self.assertTrue(should_import_mailbox("INBOX", {"\\inbox"}))

	def test_message_id_is_the_stable_deduplication_key(self):
		self.assertEqual(
			message_dedupe_key(" <AbC-123@example.invalid> ", b"different raw bytes"),
			"message-id:abc-123@example.invalid",
		)

	def test_messages_without_an_id_use_a_stable_content_hash(self):
		raw = b"From: sender@example.invalid\r\nSubject: old mail\r\n\r\nBody"
		self.assertEqual(message_dedupe_key(None, raw), "sha256:" + hashlib.sha256(raw).hexdigest())

	def test_direction_uses_sender_addresses_and_aliases(self):
		self.assertEqual(
			classify_message_direction(
				"Kanzlei <archive@kanzlei.example>",
				"post@kanzlei.example",
				["archive@kanzlei.example"],
			),
			"Sent",
		)
		self.assertEqual(
			classify_message_direction("Client <client@example.invalid>", "post@kanzlei.example", []),
			"Received",
		)

	def test_mailbox_import_is_split_into_batches_of_at_most_100(self):
		batches = list(_batches(list(range(501))))
		self.assertEqual([len(batch) for batch in batches], [100, 100, 100, 100, 100, 1])
		self.assertEqual([item for batch in batches for item in batch], list(range(501)))


class FakeIMAP:
	def __init__(self, messages_by_folder, missing_uids=None):
		self.messages_by_folder = messages_by_folder
		self.missing_uids = missing_uids or {}
		self.uid_validity = "100"
		self.current_folder = None

	def list(self):
		return (
			"OK",
			[
				b'(\\HasNoChildren \\Inbox) "/" "INBOX"',
				b'(\\HasNoChildren \\Sent) "/" "Sent Items"',
			],
		)

	def select(self, folder, readonly=False):
		self.current_folder = folder.strip('"')
		return ("OK", [b"2"])

	def response(self, name):
		return ("UIDVALIDITY", [self.uid_validity.encode()])

	def uid(self, operation, *args):
		messages = self.messages_by_folder[self.current_folder]
		if operation == "SEARCH":
			uids = sorted(set(messages) | set(self.missing_uids.get(self.current_folder, [])))
			return "OK", [" ".join(str(uid) for uid in uids).encode()]
		if operation == "FETCH":
			uids = [int(value) for value in args[0].split(",")]
			return "OK", [
				(
					f"1 (UID {uid} FLAGS () BODY[] {{{len(messages[uid])}}}".encode(),
					messages[uid],
				)
				for uid in uids
				if uid in messages
			]
		raise AssertionError(f"Unexpected IMAP UID operation: {operation}")

	def logout(self):
		return "OK", [b"LOGOUT"]


class TestEmailArchiveImport(FrappeTestCase):
	def test_archive_import_is_idempotent_and_links_later_created_contacts(self):
		account_email = f"archive-{frappe.generate_hash(length=8)}@example.invalid"
		account = frappe.get_doc(
			{
				"doctype": "Email Account",
				"email_id": account_email,
				"email_account_name": f"Archive Test {frappe.generate_hash(length=8)}",
				"enable_incoming": 0,
				"enable_outgoing": 0,
				"use_imap": 1,
				"use_ssl": 1,
				"create_contact": 0,
			}
		).insert(ignore_permissions=True)
		parent = EmailMessage()
		parent["From"] = "client@example.invalid"
		parent["To"] = account_email
		parent["Subject"] = "Document request"
		parent["Message-ID"] = f"<parent-{frappe.generate_hash(length=12)}@example.invalid>"
		parent["Date"] = "Tue, 02 Jun 2026 09:00:00 +0000"
		parent.set_content("Please send me the document.")
		message = EmailMessage()
		message["From"] = account_email
		message["To"] = "client@example.invalid"
		message["Subject"] = "Re: Document request"
		message["Message-ID"] = f"<reply-{frappe.generate_hash(length=12)}@example.invalid>"
		message["In-Reply-To"] = parent["Message-ID"]
		message["Date"] = "Tue, 02 Jun 2026 10:00:00 +0000"
		message.set_content("Please find the document attached.")
		message.add_attachment(b"private test document", maintype="text", subtype="plain", filename="document.txt")
		parent_raw = parent.as_bytes()
		reply_raw = message.as_bytes()
		followup = EmailMessage()
		followup["From"] = "client@example.invalid"
		followup["To"] = account_email
		followup["Subject"] = "Re: Re: Document request"
		followup["Message-ID"] = f"<followup-{frappe.generate_hash(length=12)}@example.invalid>"
		followup["In-Reply-To"] = message["Message-ID"]
		followup["Date"] = "Tue, 02 Jun 2026 11:00:00 +0000"
		followup.set_content("Thank you, I received it.")
		followup_raw = followup.as_bytes()
		imap = FakeIMAP(
			{"INBOX": {1: parent_raw, 2: reply_raw}, "Sent Items": {1: reply_raw}}, {"INBOX": [3]}
		)
		server = frappe._dict(settings=frappe._dict(use_imap=1), imap=imap, logout=imap.logout)
		contact = None
		readers = []
		communication_name = None
		parent_communication_name = None
		followup_communication_name = None
		try:
			with TemporaryDirectory() as temp_dir:
				archive_path = Path(temp_dir) / "archive.sqlite"
				with (
					patch("kanzlei_erp.email_archive._connect", return_value=server),
					patch("kanzlei_erp.email_archive._site_archive_dir", return_value=archive_path),
					patch("frappe.sendmail") as sendmail,
					patch("frappe.log_error") as log_error,
				):
					first = import_archive(account.name)
					log_error.assert_called_once()
					communication_name = frappe.db.get_value(
						"Communication",
						{"email_account": account.name, "message_id": message["Message-ID"].strip("<>")},
						"name",
					)
					self.assertTrue(communication_name)
					communication = frappe.get_doc("Communication", communication_name)
					parent_communication_name = frappe.db.get_value(
						"Communication",
						{"email_account": account.name, "message_id": parent["Message-ID"].strip("<>")},
						"name",
					)
					parent_communication = frappe.get_doc("Communication", parent_communication_name)
					self.assertEqual(first["imported"], 2)
					self.assertEqual(first["existing"], 1)
					self.assertEqual(first["unlinked"], 2)
					self.assertFalse(first["complete"])
					self.assertEqual(len(first["failures"]), 1)
					self.assertEqual(communication.sent_or_received, "Sent")
					self.assertEqual(communication.uid, 2)
					self.assertEqual(communication.delivery_status, "Sent")
					self.assertEqual(communication.in_reply_to, parent_communication.name)
					self.assertTrue(
						frappe.db.exists(
							"File",
						{
							"attached_to_doctype": "Communication",
							"attached_to_name": communication.name,
							"is_private": 1,
							"file_name": "document.txt",
						},
						)
					)
					sendmail.assert_not_called()
					unlinked_reader = frappe.get_doc(
						{
							"doctype": "User",
							"email": f"unlinked-{frappe.generate_hash(length=8)}@example.invalid",
							"first_name": "Unlinked Mail Reader",
							"send_welcome_email": 0,
							"roles": [{"role": "Inbox User"}],
						}
					).insert(ignore_permissions=True)
					readers.append(unlinked_reader)
					file_name = frappe.db.get_value(
						"File",
						{
							"attached_to_doctype": "Communication",
							"attached_to_name": communication.name,
						},
						"name",
					)
					self.assertFalse(
						frappe.has_permission("Communication", ptype="read", doc=communication.name, user=unlinked_reader.name)
					)
					self.assertFalse(
						frappe.has_permission("File", ptype="read", doc=file_name, user=unlinked_reader.name)
					)
					customer_names = frappe.get_all("Customer", pluck="name", limit=2)
					self.assertEqual(len(customer_names), 2, "ERPNext test records should include two Mandanten")
					contact = frappe.get_doc(
						{
							"doctype": "Contact",
							"first_name": "Archive test client",
							"email_ids": [{"email_id": "client@example.invalid", "is_primary": 1}],
							"links": [
								{"link_doctype": "Customer", "link_name": customer_name}
								for customer_name in customer_names
							],
						}
					).insert(ignore_permissions=True)
					imap.messages_by_folder["INBOX"][3] = followup_raw
					imap.missing_uids["INBOX"] = []
					mandant_reader = frappe.get_doc(
						{
							"doctype": "User",
							"email": f"mandant-{frappe.generate_hash(length=8)}@example.invalid",
							"first_name": "Mandant Mail Reader",
							"send_welcome_email": 0,
							"roles": [{"role": "Inbox User"}, {"role": "Accounts User"}],
						}
					).insert(ignore_permissions=True)
					readers.append(mandant_reader)
					second = import_archive(account.name)
					communication.reload()
					parent_communication.reload()
					self.assertEqual(second["imported"], 1)
					self.assertEqual(second["existing"], 3)
					self.assertGreaterEqual(second["new_contact_links"], 6)
					self.assertEqual(second["unlinked"], 0)
					self.assertTrue(second["complete"])
					self.assertEqual(second["failures"], [])
					sendmail.assert_not_called()
					self.assertTrue(
						frappe.has_permission(
							"Communication", ptype="read", doc=communication.name, user=mandant_reader.name
						)
					)
					self.assertTrue(frappe.has_permission("File", ptype="read", doc=file_name, user=mandant_reader.name))
					followup_communication_name = frappe.db.get_value(
						"Communication",
						{"email_account": account.name, "message_id": followup["Message-ID"].strip("<>")},
						"name",
					)
					followup_communication = frappe.get_doc("Communication", followup_communication_name)
					self.assertEqual(followup_communication.in_reply_to, communication.name)
					for comm in (communication, parent_communication, followup_communication):
						for customer_name in customer_names:
							self.assertTrue(
								frappe.db.exists(
									"Communication Link",
									{
										"parent": comm.name,
										"link_doctype": "Customer",
										"link_name": customer_name,
									},
									)
							)
					imap.uid_validity = "101"
					third = import_archive(account.name)
					self.assertEqual(third["imported"], 0)
					self.assertEqual(third["existing"], 4)
					self.assertTrue(third["complete"])
					self.assertTrue(all(row["uid_validity"] == "101" for row in third["progress"]))
		finally:
			communication_names = set(
				frappe.get_all(
					"Communication", filters={"email_account": account.name}, pluck="name"
				)
			)
			if communication_name:
				communication_names.add(communication_name)
			if parent_communication_name:
				communication_names.add(parent_communication_name)
			if followup_communication_name:
				communication_names.add(followup_communication_name)
			for communication_name in communication_names:
				for file_name in frappe.get_all(
					"File",
					filters={"attached_to_doctype": "Communication", "attached_to_name": communication_name},
					pluck="name",
				):
					frappe.delete_doc("File", file_name, ignore_permissions=True, force=True)
				frappe.delete_doc("Communication", communication_name, ignore_permissions=True, force=True)
			if contact and frappe.db.exists("Contact", contact.name):
				frappe.delete_doc("Contact", contact.name, ignore_permissions=True, force=True)
			for reader in readers:
				if frappe.db.exists("User", reader.name):
					frappe.delete_doc("User", reader.name, ignore_permissions=True, force=True)
			if frappe.db.exists("Email Account", account.name):
				frappe.delete_doc("Email Account", account.name, ignore_permissions=True, force=True)
			frappe.db.commit()
