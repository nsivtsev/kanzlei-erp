from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
from unittest.mock import patch
from zipfile import ZipFile

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.file_manager import save_file

from kanzlei_erp import email_history
from kanzlei_erp.mandant_documents import (
	download_document,
	download_documents_zip,
	get_documents,
)


class TestEmailHistory(FrappeTestCase):
	def setUp(self):
		self.enqueued = patch("frappe.enqueue").start()
		self.addCleanup(patch.stopall)
		self.customers = []
		self.contacts = []
		self.mails = []
		self.users = []
		self.files = []
		self.customer = self.make_customer()
		self.address = f"history-{frappe.generate_hash(length=12)}@example.invalid"

	def tearDown(self):
		self.enqueued.side_effect = None
		frappe.set_user("Administrator")
		if getattr(self, "committed", False):
			frappe.db.rollback()
		for doctype, names in (("File", self.files), ("Communication", self.mails),
			("Contact", self.contacts), ("Customer", self.customers), ("User", self.users)):
			for name in names:
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		if getattr(self, "committed", False):
			frappe.db.commit()
		super().tearDown()

	def make_customer(self):
		doc = frappe.get_doc({"doctype": "Customer", "customer_name": "_History " + frappe.generate_hash(),
			"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories"}).insert()
		self.customers.append(doc.name)
		return doc

	def make_contact(self, address=None, customers=None):
		doc = frappe.get_doc({"doctype": "Contact", "first_name": "_History " + frappe.generate_hash(),
			"email_ids": [{"email_id": address or self.address}],
			"links": [{"link_doctype": "Customer", "link_name": customer.name}
				for customer in (customers if customers is not None else [self.customer])]}).insert()
		self.contacts.append(doc.name)
		return doc

	def make_mail(self, **values):
		doc = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
			"communication_medium": "Email", "sent_or_received": "Received",
			"subject": "History test", "content": "Original body", "sender": self.address,
			"recipients": "office@example.invalid", **values}).insert(ignore_permissions=True)
		self.mails.append(doc.name)
		return doc

	def customer_links(self, mail):
		return frappe.get_all("Communication Link", filters={"parent": mail.name,
			"parenttype": "Communication", "link_doctype": "Customer"}, pluck="link_name")

	def run_history(self, contact, actor="Administrator"):
		# Exercise worker batching without committing the surrounding test transaction.
		with patch("frappe.db.commit"):
			return email_history.link_contact_history(contact.name, actor=actor)

	def test_old_mail_and_original_attachment_appear_once_without_mutating_mail(self):
		mail = self.make_mail()
		file = save_file("history-original.txt", b"original attachment bytes", "Communication", mail.name, is_private=1)
		self.files.append(file.name)
		before = frappe.db.get_value("Communication", mail.name,
			["content", "modified", "status", "communication_date", "_seen", "read_by_recipient"], as_dict=True)
		contact = self.make_contact()
		result = self.run_history(contact)
		self.assertEqual(result, {"linked": 1, "ambiguous": 0})
		self.assertEqual(self.customer_links(mail), [self.customer.name])
		self.assertEqual(self.run_history(contact), {"linked": 0, "ambiguous": 0})
		self.assertEqual(frappe.db.get_value("Communication", mail.name, list(before), as_dict=True), before)
		self.assertEqual(frappe.db.get_value("File", file.name, "file_url"), file.file_url)
		row = next(row for row in get_documents(self.customer.name)["items"] if row["file_id"] == file.name)
		self.assertEqual({ctx["source"] for ctx in row["contexts"]}, {"email"})
		download_document(self.customer.name, file.name)
		self.assertEqual(frappe.local.response.filecontent, b"original attachment bytes")
		download_documents_zip(self.customer.name, [file.name])
		with ZipFile(BytesIO(frappe.local.response.filecontent)) as archive:
			self.assertEqual(archive.read(archive.namelist()[0]), b"original attachment bytes")

	def test_addresses_are_exact_case_insensitive_and_include_sent_cc_bcc(self):
		mails = [self.make_mail(**{field: f'Client <{self.address.upper()}>',
			**({"sender": "office@example.invalid", "sent_or_received": "Sent"} if field != "sender" else {})})
			for field in ("sender", "recipients", "cc", "bcc")]
		near = self.make_mail(sender="prefix-" + self.address)
		contact = self.make_contact()
		self.assertEqual(self.run_history(contact)["linked"], 4)
		for mail in mails:
			self.assertEqual(self.customer_links(mail), [self.customer.name])
		self.assertEqual(self.customer_links(near), [])

	def test_same_mandant_is_unique_but_all_contacts_and_participants_can_make_it_ambiguous(self):
		mail = self.make_mail()
		contact = self.make_contact()
		self.make_contact()
		other = self.make_customer()
		self.make_contact(customers=[other])
		self.assertEqual(self.run_history(contact), {"linked": 0, "ambiguous": 1})
		self.assertEqual(self.customer_links(mail), [])
		frappe.db.delete("Dynamic Link", {"parent": self.contacts[-1], "parenttype": "Contact"})
		self.assertEqual(self.run_history(contact)["linked"], 1)
		mail2 = self.make_mail(sender="new@example.invalid", bcc=self.address)
		frappe.db.delete("Communication Link", {"parent": mail2.name, "link_doctype": "Customer"})
		self.make_contact("new@example.invalid", [other])
		self.assertEqual(self.run_history(contact)["ambiguous"], 1)
		self.assertEqual(self.customer_links(mail2), [])

	def test_existing_references_and_explicit_fibu_links_are_preserved(self):
		other = self.make_customer()
		direct = self.make_mail(reference_doctype="Customer", reference_name=other.name)
		timeline = self.make_mail()
		timeline.add_link("Customer", other.name, autosave=True)
		evidence = self.make_mail()
		frappe.get_doc({"doctype": "FiBu Message Link", "parent": "history-package",
			"parenttype": "FiBu Package", "parentfield": "messages", "communication": evidence.name}).db_insert()
		contact = self.make_contact()
		self.assertEqual(self.run_history(contact)["linked"], 0)
		self.assertNotIn(self.customer.name, self.customer_links(direct))
		self.assertEqual(self.customer_links(timeline), [other.name])
		self.assertEqual(self.customer_links(evidence), [])
		frappe.db.delete("FiBu Message Link", {"parent": "history-package"})

	def test_contact_changes_are_reloaded_and_irrelevant_saves_do_not_enqueue(self):
		mail = self.make_mail()
		contact = self.make_contact()
		with patch("kanzlei_erp.email_history.queue_history") as queue:
			contact.first_name += " edited"
			contact.save()
			queue.assert_not_called()
			contact.add_email("another@example.invalid")
			contact.save()
			queue.assert_called_once()
		frappe.db.delete("Dynamic Link", {"parent": contact.name, "parenttype": "Contact"})
		self.assertEqual(self.run_history(contact)["linked"], 0)
		self.assertEqual(self.customer_links(mail), [])

	def test_api_queues_after_commit_with_distinct_jobs_and_current_actor(self):
		contact = self.make_contact()
		with patch("frappe.db.after_commit") as manager:
			callbacks = manager.add
			one = email_history.enqueue_contact_history(contact.name)
			two = email_history.enqueue_contact_history(contact.name)
			self.assertNotEqual(one["job_id"], two["job_id"])
			self.assertEqual(callbacks.call_count, 2)
			self.enqueued.reset_mock()
			callbacks.call_args_list[0].args[0]()
			self.assertEqual(self.enqueued.call_args.kwargs["actor"], "Administrator")

	def test_permission_revocation_and_nonstaff_never_link_or_disclose_counts(self):
		mail = self.make_mail()
		contact = self.make_contact()
		user = frappe.get_doc({"doctype": "User", "email": "history-user-" + frappe.generate_hash() + "@example.invalid",
			"first_name": "History reader", "send_welcome_email": 0,
			"roles": [{"role": "Projects User"}, {"role": "Inbox User"}]}).insert(ignore_permissions=True)
		self.users.append(user.name)
		for doctype, name in (("Contact", contact.name), ("Customer", self.customer.name), ("Communication", mail.name)):
			frappe.share.add(doctype, name, user.name, read=1, write=1)
		self.assertEqual(self.run_history(contact, user.name)["linked"], 1)
		frappe.db.delete("Communication Link", {"parent": mail.name})
		frappe.share.remove("Communication", mail.name, user.name)
		self.assertEqual(self.run_history(contact, user.name), {"linked": 0, "ambiguous": 0})
		frappe.share.remove("Contact", contact.name, user.name)
		with self.assertRaises(frappe.PermissionError):
			self.run_history(contact, user.name)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			email_history.enqueue_contact_history(contact.name)
		frappe.set_user("Administrator")
		frappe.db.set_value("User", user.name, "user_type", "Website User")
		with self.assertRaises(frappe.PermissionError):
			self.run_history(contact, user.name)

	def test_failed_batch_can_be_retried_without_duplicate_links(self):
		mails = [self.make_mail(subject=str(i)) for i in range(3)]
		contact = self.make_contact()
		original = email_history.link_communication
		calls = 0

		def fail_second(*args, **kwargs):
			nonlocal calls
			calls += 1
			if calls == 2:
				raise RuntimeError("simulated worker interruption")
			return original(*args, **kwargs)

		self.committed = True
		with patch("kanzlei_erp.email_history.BATCH_SIZE", 1), patch(
			"kanzlei_erp.email_history.link_communication", side_effect=fail_second):
			with self.assertRaises(RuntimeError):
				email_history.link_contact_history(contact.name, actor="Administrator")
		self.assertEqual(sum(bool(self.customer_links(mail)) for mail in mails), 1)
		self.assertEqual(self.run_history(contact)["linked"], 2)
		for mail in mails:
			self.assertEqual(self.customer_links(mail), [self.customer.name])

	def test_parallel_transactions_create_one_customer_link(self):
		mail = self.make_mail()
		contact = self.make_contact()
		self.committed = True
		frappe.db.commit()
		site = frappe.local.site
		sites_path = frappe.local.sites_path
		barrier = Barrier(2)

		def link_in_connection():
			frappe.init(site, sites_path=sites_path)
			frappe.connect()
			frappe.set_user("Administrator")
			try:
				barrier.wait(timeout=10)
				result = email_history.link_contact_history(contact.name, actor="Administrator")
				return result["linked"]
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=2) as pool:
			results = list(pool.map(lambda _: link_in_connection(), range(2)))
		frappe.db.rollback()
		self.assertEqual(sorted(results), [0, 1])
		self.assertEqual(self.customer_links(mail), [self.customer.name])

	def test_creation_and_new_customer_link_trigger_but_empty_contact_does_not(self):
		with patch("kanzlei_erp.email_history.queue_history") as queue:
			self.make_contact(customers=[])
			queue.assert_not_called()
			contact = self.make_contact()
			queue.assert_called_once()
			queue.reset_mock()
			other = self.make_customer()
			contact.append("links", {"link_doctype": "Customer", "link_name": other.name})
			contact.save()
			queue.assert_called_once()

	def test_archive_relink_skips_ambiguous_and_assigned_messages(self):
		from kanzlei_erp.email_archive import _add_contact_links

		mail = self.make_mail()
		other = self.make_customer()
		contact = self.make_contact(customers=[self.customer, other])
		self.assertEqual(_add_contact_links(mail), 0)
		self.assertEqual(self.customer_links(mail), [])
		contact.set("links", [{"link_doctype": "Customer", "link_name": self.customer.name}])
		contact.save()
		self.assertEqual(_add_contact_links(mail), 2)
		self.assertEqual(_add_contact_links(mail), 0)
		self.assertEqual(self.customer_links(mail), [self.customer.name])

	def test_queue_failure_keeps_saved_contact_and_notifies_only_initiator(self):
		contact = self.make_contact()
		self.enqueued.side_effect = ConnectionError("queue unavailable")
		with patch("frappe.log_error") as log, patch("frappe.publish_realtime") as notify:
			email_history._enqueue(contact.name, "Administrator", "queue-failure-test")
			self.assertTrue(frappe.db.exists("Contact", contact.name))
			self.assertTrue(log.call_args.kwargs.get("defer_insert"))
			self.assertEqual(notify.call_args.kwargs["user"], "Administrator")
			self.assertEqual(notify.call_args.args[1]["indicator"], "red")

	def test_redis_outage_does_not_escape_the_after_commit_callback(self):
		contact = self.make_contact()
		self.enqueued.side_effect = ConnectionError("queue unavailable")
		with patch("frappe.log_error", side_effect=ConnectionError("deferred log unavailable")), patch(
			"frappe.publish_realtime", side_effect=ConnectionError("socket unavailable")), patch("frappe.logger"):
			email_history._enqueue(contact.name, "Administrator", "redis-failure-test")
		self.assertTrue(frappe.db.exists("Contact", contact.name))
