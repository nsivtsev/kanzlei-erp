from datetime import date, timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kanzlei_erp.overview import (
	get_deadline_agenda,
	get_incoming_mail,
	get_task_overview,
)


class TestOverview(FrappeTestCase):
	def setUp(self):
		frappe.db.savepoint("overview_test")
		frappe.set_user("Administrator")
		self.clock = patch("kanzlei_erp.overview.nowdate", return_value="2098-12-29")
		self.now = self.clock.start()
		self.today = date(2098, 12, 29)
		self.customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Overview {frappe.generate_hash(length=8)}",
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()

	def tearDown(self):
		self.clock.stop()
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="overview_test")
		frappe.clear_cache()
		super().tearDown()

	def task(self, offset=None, **values):
		return frappe.get_doc(
			{
				"doctype": "Task",
				"subject": f"_Overview {frappe.generate_hash(length=8)}",
				"kanzlei_customer": self.customer.name,
				"exp_end_date": self.today + timedelta(days=offset) if offset is not None else None,
				**values,
			}
		).insert()

	def user(self, roles=("Projects User",), **values):
		return frappe.get_doc(
			{
				"doctype": "User",
				"email": f"overview-{frappe.generate_hash(length=8)}@example.invalid",
				"first_name": "Overview Reader",
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
				**values,
			}
		).insert(ignore_permissions=True)

	def account(self):
		return frappe.get_doc(
			{
				"doctype": "Email Account",
				"email_id": f"overview-{frappe.generate_hash(length=8)}@example.invalid",
				"enable_outgoing": 1,
				"always_use_account_email_id_as_sender": 1,
			}
		).insert(ignore_permissions=True)

	def assign_accounts(self, user, accounts):
		user.set("user_emails", [{"email_account": account.name} for account in accounts])
		user.save(ignore_permissions=True)

	def mail(self, account, **values):
		return frappe.get_doc(
			{
				"doctype": "Communication",
				"communication_type": "Communication",
				"communication_medium": "Email",
				"subject": "Overview mail",
				"content": "<p>Test message</p>",
				"sender": "sender@example.invalid",
				"recipients": account.email_id,
				"email_account": account.name,
				"sent_or_received": "Received",
				"communication_date": "2098-12-29 09:00:00",
				"status": "Open",
				"email_status": "Open",
				**values,
			}
		).insert(ignore_permissions=True)

	def test_deadlines_are_disjoint_and_cross_year_boundary(self):
		before = get_task_overview()["summary"]
		self.task(-1, status="Open")
		self.task(0)
		first, last = self.task(1), self.task(7)
		self.task(8)
		self.task()
		result = get_task_overview()
		self.assertEqual(
			result["summary"],
			{key: before[key] + value for key, value in {"overdue": 1, "today": 1, "upcoming": 2}.items()},
		)
		self.assertEqual(result["range_start"], "2098-12-30")
		self.assertEqual(result["range_end"], "2099-01-05")
		agenda = get_deadline_agenda()
		self.assertEqual([row["name"] for row in agenda["rows"]], [first.name, last.name])
		self.assertEqual(agenda["total"], result["summary"]["upcoming"])

	def test_datetime_deadlines_cover_full_site_days_and_month_boundary(self):
		self.now.return_value = "2098-12-31"
		self.today = date(2098, 12, 31)
		today = self.task(exp_end_date="2098-12-31 23:59:59")
		last = self.task(exp_end_date="2099-01-07 23:59:59")
		self.task(exp_end_date="2099-01-08 00:00:00")
		self.assertEqual([row["name"] for row in get_task_overview("today")["rows"]], [today.name])
		self.assertEqual([row["name"] for row in get_deadline_agenda()["rows"]], [last.name])

	def test_excludes_closed_questions_groups_and_templates_but_includes_legacy(self):
		before = get_task_overview()["summary"]["today"]
		for values in (
			{"status": "Completed"},
			{"status": "Cancelled"},
			{"status": "Template"},
			{"is_template": 1},
			{"is_group": 1},
		):
			doc = self.task(0, **values)
			if values.get("status") == "Template":
				frappe.db.set_value("Task", doc.name, "status", "Template")
		question, legacy = self.task(0), self.task(0)
		frappe.db.set_value("Task", question.name, "kanzlei_work_kind", "Question", update_modified=False)
		frappe.db.set_value("Task", legacy.name, "kanzlei_work_kind", None, update_modified=False)
		self.assertEqual(get_task_overview()["summary"]["today"], before + 1)

	def test_summary_and_pagination_do_not_duplicate_assignments(self):
		from frappe.desk.form.assign_to import add

		other = self.user()
		tasks = [self.task(1) for _ in range(11)]
		add({"doctype": "Task", "name": tasks[0].name, "assign_to": ["Administrator", other.name]})
		first = get_task_overview("upcoming", page_length=8)
		second = get_task_overview("upcoming", start=8, page_length=8)
		self.assertEqual(first["summary"]["upcoming"], 11)
		self.assertTrue(first["has_more"])
		self.assertFalse(second["has_more"])
		self.assertEqual(
			[row["name"] for row in first["rows"] + second["rows"]], sorted(task.name for task in tasks)
		)
		self.assertEqual(get_deadline_agenda(start=8)["rows"], second["rows"])
		row = next(row for row in first["rows"] + second["rows"] if row["name"] == tasks[0].name)
		self.assertEqual({user["name"] for user in row["assignees"]}, {"Administrator", other.name})
		self.assertEqual(row["customer_name"], self.customer.customer_name)

	def test_linked_tasks_follow_customer_permissions_including_counts(self):
		other_customer = frappe.copy_doc(self.customer)
		other_customer.customer_name = f"_Hidden overview {frappe.generate_hash(length=8)}"
		other_customer.insert()

		def package(customer):
			doc = frappe.get_doc(
				{
					"doctype": "FiBu Package",
					"name": f"_overview-{frappe.generate_hash(length=8)}",
					"customer": customer.name,
					"status": "Open",
				}
			)
			doc.db_insert()
			return doc

		allowed = self.task(1, kanzlei_fibu_package=package(self.customer).name)
		denied = self.task(
			1, kanzlei_customer=other_customer.name, kanzlei_fibu_package=package(other_customer).name
		)
		reader = self.user()
		frappe.share.add("Customer", self.customer.name, reader.name, read=1)
		frappe.set_user(reader.name)
		result = get_task_overview("upcoming")
		self.assertEqual({row["name"] for row in result["rows"]}, {allowed.name})
		self.assertNotIn(denied.name, str(result))
		self.assertEqual(result["summary"]["upcoming"], 1)
		self.assertEqual(get_deadline_agenda()["total"], 1)
		frappe.set_user("Administrator")
		self.assertEqual(get_task_overview()["summary"]["upcoming"], 2)

	def test_task_does_not_leak_unreadable_customer_or_use_creator_as_assignee(self):
		task, reader = self.task(1), self.user()
		frappe.share.add("Task", task.name, reader.name, read=1)
		frappe.set_user(reader.name)
		row = next(row for row in get_deadline_agenda()["rows"] if row["name"] == task.name)
		self.assertIsNone(row["customer"])
		self.assertIsNone(row["customer_name"])
		self.assertTrue(row["customer_restricted"])
		self.assertEqual(row["assignees"], [])

	def test_mail_is_restricted_to_assignments_even_for_administrator(self):
		account, other = self.account(), self.account()
		reader = self.user(("Inbox User",))
		self.assign_accounts(reader, [account])
		allowed, denied = self.mail(account), self.mail(other)
		for values in (
			{"sent_or_received": "Sent"},
			{"email_status": "Spam"},
			{"email_status": "Trash"},
			{"status": "Closed"},
			{"status": "Replied"},
		):
			mail = self.mail(account, **values)
			if "status" in values:
				frappe.db.set_value("Communication", mail.name, "status", values["status"])
		frappe.set_user(reader.name)
		result = get_incoming_mail()
		self.assertEqual([row["name"] for row in result["rows"]], [allowed.name])
		self.assertNotIn(denied.name, str(result))
		self.assertEqual(get_task_overview()["state"], "no_access")
		self.assertEqual(get_deadline_agenda()["state"], "no_access")
		frappe.set_user("Administrator")
		admin = frappe.get_doc("User", "Administrator")
		self.assign_accounts(admin, [])
		self.assertEqual(get_incoming_mail()["state"], "no_account")
		self.assign_accounts(admin, [account])
		self.assertEqual([row["name"] for row in get_incoming_mail()["rows"]], [allowed.name])

	def test_shared_task_is_readable_without_global_task_role(self):
		task, reader = self.task(1), self.user(("Inbox User",))
		frappe.share.add("Task", task.name, reader.name, read=1)
		frappe.set_user(reader.name)
		self.assertTrue(frappe.has_permission("Task", "read", doc=task.name))
		result = get_task_overview("upcoming")
		self.assertEqual(result["summary"]["upcoming"], 1)
		self.assertEqual(result["rows"][0]["subject"], task.subject)
		self.assertEqual(result["rows"][0]["exp_end_date"], "2098-12-30")

	def test_multiple_mailboxes_safe_preview_and_no_read_side_effects(self):
		accounts, reader = [self.account(), self.account()], self.user(("Inbox User",))
		self.assign_accounts(reader, accounts)
		mail = self.mail(
			accounts[0],
			content='<script>secret()</script><style>hidden</style><p>Hello <b>world</b></p><img src="https://example.invalid/track">'
			+ "word " * 60,
		)
		other = self.mail(accounts[1], subject="Other", communication_date="2098-12-29 10:00:00")
		before = frappe.db.get_value(
			"Communication", mail.name, ["seen", "_seen", "status", "modified"], as_dict=True
		)
		queue_before = frappe.db.count("Email Flag Queue", {"communication": mail.name})
		frappe.set_user(reader.name)
		result = get_incoming_mail()
		self.assertEqual([row["name"] for row in result["rows"]], [other.name, mail.name])
		self.assertEqual(
			{account["name"] for account in result["accounts"]}, {account.name for account in accounts}
		)
		row = result["rows"][1]
		self.assertLessEqual(len(row["preview"]), 180)
		self.assertTrue(row["preview"].startswith("Hello world"))
		self.assertNotIn("secret", row["preview"])
		self.assertNotIn("<", row["preview"])
		self.assertNotIn("content", row)
		self.assertNotIn("file_url", row)
		self.assertEqual(
			frappe.db.get_value(
				"Communication", mail.name, ["seen", "_seen", "status", "modified"], as_dict=True
			),
			before,
		)
		self.assertEqual(frappe.db.count("Email Flag Queue", {"communication": mail.name}), queue_before)

	def test_mail_refills_after_record_permission_denials(self):
		account, reader = self.account(), self.user(("Inbox User",))
		self.assign_accounts(reader, [account])
		mails = [self.mail(account, communication_date=f"2098-12-29 09:{i:02d}:00") for i in range(9)]
		original = frappe.has_permission

		def restricted(doctype, *args, **kwargs):
			if doctype == "Communication" and kwargs.get("doc") in {mail.name for mail in mails[-4:]}:
				return False
			return original(doctype, *args, **kwargs)

		frappe.set_user(reader.name)
		with (
			patch("kanzlei_erp.overview.frappe.has_permission", side_effect=restricted),
			patch("kanzlei_erp.overview.MAIL_BATCH_SIZE", 3),
		):
			self.assertEqual(
				[row["name"] for row in get_incoming_mail()["rows"]],
				[mail.name for mail in reversed(mails[:5])],
			)

	def test_empty_mail_and_attachment_flag(self):
		account, reader = self.account(), self.user(("Inbox User",))
		self.assign_accounts(reader, [account])
		mail = self.mail(account)
		frappe.db.set_value("Communication", mail.name, {"subject": "", "content": "", "has_attachment": 1})
		frappe.set_user(reader.name)
		row = get_incoming_mail()["rows"][0]
		self.assertEqual(row["subject"], "")
		self.assertEqual(row["preview"], "")
		self.assertTrue(row["has_attachment"])

	def test_different_users_never_share_overview_results(self):
		accounts = [self.account(), self.account()]
		readers = [self.user(("Inbox User",)), self.user(("Inbox User",))]
		mails = [self.mail(account) for account in accounts]
		for reader, account in zip(readers, accounts, strict=True):
			self.assign_accounts(reader, [account])
		for index in (0, 1, 0):
			frappe.set_user(readers[index].name)
			self.assertEqual([row["name"] for row in get_incoming_mail()["rows"]], [mails[index].name])

	def test_invalid_arguments_and_non_desk_users_are_rejected(self):
		for kwargs in (
			{"bucket": "bad"},
			{"bucket": []},
			{"start": -1},
			{"start": "1.5"},
			{"page_length": 0},
			{"page_length": 21},
			{"page_length": True},
		):
			with self.assertRaises(frappe.ValidationError):
				get_task_overview(**kwargs)
		with self.assertRaises(frappe.ValidationError):
			get_incoming_mail(page_length=6)
		with self.assertRaises(frappe.ValidationError):
			get_deadline_agenda(start=-1)
		website = self.user((), user_type="Website User")
		for user in ("Guest", website.name):
			frappe.set_user(user)
			for method in (get_task_overview, get_deadline_agenda, get_incoming_mail):
				with self.assertRaises(frappe.PermissionError):
					method()
