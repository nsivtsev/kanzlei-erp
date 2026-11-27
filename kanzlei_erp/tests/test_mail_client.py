import frappe
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp.mail_client import (
	assign_mandant,
	get_mailboxes,
	get_message,
	get_messages,
	move_to,
	set_seen,
)


class TestMailClient(FrappeTestCase):
	def setUp(self):
		frappe.db.savepoint("mail_client_test")
		frappe.set_user("Administrator")
		self.account = self.make_account()
		self.other = self.make_account()
		self.admin = frappe.get_doc("User", "Administrator")
		self.previous_emails = [row.email_account for row in self.admin.user_emails]
		self.assign(self.admin, [self.account])
		self.customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Mail {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="mail_client_test")
		frappe.clear_cache()
		super().tearDown()

	def make_account(self):
		return frappe.get_doc(
			{
				"doctype": "Email Account",
				"email_id": f"mail-{frappe.generate_hash(length=8)}@example.invalid",
				"enable_outgoing": 1,
				"always_use_account_email_id_as_sender": 1,
			}
		).insert(ignore_permissions=True)

	def assign(self, user, accounts):
		user.set("user_emails", [{"email_account": account.name} for account in accounts])
		user.save(ignore_permissions=True)

	def mail(self, account=None, **values):
		account = account or self.account
		return frappe.get_doc(
			{
				"doctype": "Communication",
				"communication_type": "Communication",
				"communication_medium": "Email",
				"subject": "Mail client",
				"content": "<p>Hello <b>there</b></p>",
				"sender": "client@example.invalid",
				"recipients": account.email_id,
				"email_account": account.name,
				"sent_or_received": "Received",
				"communication_date": "2098-12-29 09:00:00",
				"email_status": "Open",
				**values,
			}
		).insert(ignore_permissions=True)

	def names(self, **args):
		return [row["name"] for row in get_messages(**args)["rows"]]

	def test_folders_are_limited_to_assigned_mailboxes(self):
		inbox = self.mail()
		sent = self.mail(sent_or_received="Sent", communication_date="2098-12-29 10:00:00")
		spam = self.mail(email_status="Spam")
		trash = self.mail(email_status="Trash")
		foreign = self.mail(self.other)
		self.assertEqual(self.names(folder="inbox"), [inbox.name])
		self.assertEqual(self.names(folder="sent"), [sent.name])
		self.assertEqual(self.names(folder="spam"), [spam.name])
		self.assertEqual(self.names(folder="trash"), [trash.name])
		self.assertNotIn(foreign.name, str(get_messages(folder="inbox")))
		with self.assertRaises(frappe.PermissionError):
			get_message(foreign.name)
		with self.assertRaises(frappe.PermissionError):
			get_messages(folder="inbox", account=self.other.name)
		with self.assertRaises(frappe.ValidationError):
			get_messages(folder="drafts")
		row = get_messages(folder="inbox")["rows"][0]
		self.assertEqual(row["preview"], "Hello there")
		self.assertNotIn("content", row)

	def test_no_account_and_search_filters(self):
		first = self.mail(subject="Belege Oktober", has_attachment=1)
		percent = self.mail(subject="Anything 100%")
		self.assertEqual(self.names(search="oktober"), [first.name])
		self.assertEqual(self.names(search="%"), [percent.name])
		self.assertEqual(self.names(attachments=1), [first.name])
		self.assign(self.admin, [])
		self.assertEqual(get_messages()["state"], "no_account")
		self.assertEqual(get_mailboxes()["state"], "no_account")

	def test_pagination_resumes_after_the_last_shown_row(self):
		mails = [self.mail(communication_date=f"2098-12-29 09:{minute:02d}:00") for minute in range(5)]
		first = get_messages(page_length=2)
		self.assertTrue(first["has_more"])
		second = get_messages(start=first["next_start"], page_length=2)
		third = get_messages(start=second["next_start"], page_length=2)
		shown = [row["name"] for page in (first, second, third) for row in page["rows"]]
		self.assertEqual(shown, [mail.name for mail in reversed(mails)])
		self.assertFalse(third["has_more"])

	def test_unassigned_folder_and_linking_to_a_mandant(self):
		open_mail = self.mail()
		linked = self.mail(reference_doctype="Customer", reference_name=self.customer.name)
		self.assertEqual(self.names(folder="unassigned"), [open_mail.name])
		self.assertEqual(get_mailboxes()["counts"]["unassigned"], 1)
		result = assign_mandant(open_mail.name, self.customer.name)
		self.assertEqual(result["mandants"], [{"name": self.customer.name, "customer_name": self.customer.customer_name}])
		assign_mandant(open_mail.name, self.customer.name)
		links = frappe.get_all(
			"Communication Link", filters={"parent": open_mail.name, "link_doctype": "Customer"}, pluck="link_name"
		)
		self.assertEqual(links, [self.customer.name])
		self.assertEqual(self.names(folder="unassigned"), [])
		self.assertEqual(get_message(linked.name)["mandants"][0]["name"], self.customer.name)

	def test_read_state_and_moving_messages(self):
		mail = self.mail(seen=0)
		self.assertEqual(get_mailboxes()["counts"]["inbox"], 1)
		self.assertFalse(get_message(mail.name)["seen"])
		self.assertEqual(set_seen(frappe.as_json([mail.name]), 1)["changed"], [mail.name])
		self.assertEqual(frappe.db.get_value("Communication", mail.name, "seen"), 1)
		self.assertEqual(set_seen(frappe.as_json([mail.name]), 1)["changed"], [])
		self.assertEqual(get_mailboxes()["counts"]["inbox"], 0)
		move_to(mail.name, "trash")
		self.assertEqual(self.names(folder="trash"), [mail.name])
		move_to(mail.name, "restore")
		self.assertEqual(self.names(folder="inbox"), [mail.name])
		with self.assertRaises(frappe.ValidationError):
			move_to(mail.name, "archive")

	def test_message_details_include_thread_and_attachments(self):
		first = self.mail(sent_or_received="Sent", communication_date="2098-12-28 09:00:00")
		reply = self.mail(in_reply_to=first.name)
		from frappe.utils.file_manager import save_file

		save_file("beleg.txt", b"beleg", "Communication", reply.name, is_private=1)
		message = get_message(reply.name)
		self.assertEqual([row["name"] for row in message["thread"]], [first.name, reply.name])
		self.assertTrue(message["thread"][1]["current"])
		self.assertEqual([row["file_name"] for row in message["attachments"]], ["beleg.txt"])
		self.assertEqual(message["account_email_id"], self.account.email_id)
		self.assertIsNone(message["bcc"])

	def test_inbox_user_without_assignment_cannot_read(self):
		mail = self.mail()
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"mail-reader-{frappe.generate_hash(length=8)}@example.invalid",
				"first_name": "Mail Reader",
				"send_welcome_email": 0,
				"roles": [{"role": "Inbox User"}],
			}
		).insert(ignore_permissions=True)
		frappe.set_user(user.name)
		self.assertEqual(get_messages()["state"], "no_account")
		with self.assertRaises(frappe.PermissionError):
			get_message(mail.name)
		frappe.set_user("Administrator")
		self.assign(user, [self.account])
		frappe.set_user(user.name)
		self.assertEqual(self.names(), [mail.name])
