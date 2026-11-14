import multiprocessing
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from kanzlei_erp.tests.test_email_archive import ArchiveMailFixture


def _start_with_stale_snapshot(site, sites_path, account, barrier, results):
	frappe.init(site, sites_path=sites_path, force=True)
	frappe.connect()
	frappe.set_user("Administrator")
	try:
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		from kanzlei_erp import email_archive_jobs as jobs

		frappe.db.sql("SELECT COUNT(*) FROM `tabEmail Archive Import`")
		barrier.wait(timeout=20)
		frappe.local.request = Request(EnvironBuilder(method="POST").get_environ())
		with patch.object(jobs, "_enqueue"):
			result = jobs.start_import(account)
			frappe.db.commit()
		results.put(result["name"])
	except Exception as error:
		frappe.db.rollback()
		results.put(str(error))
	finally:
		frappe.destroy()


class TestEmailArchiveJobs(ArchiveMailFixture):
	def jobs(self):
		from kanzlei_erp import email_archive_jobs

		return email_archive_jobs

	def tearDown(self):
		frappe.set_user("Administrator")
		if frappe.db.exists("DocType", "Email Archive Import"):
			for name in frappe.get_all("Email Archive Import", filters={"email_account": self.account.name}, pluck="name"):
				self.jobs()._release_run_lock(name, self.account.name)
				frappe.delete_doc("Email Archive Import", name, ignore_permissions=True, force=True)
		super().tearDown()

	def start(self):
		with patch("kanzlei_erp.email_archive_jobs._enqueue"):
			result = self.jobs().start_import(self.account.name)
			frappe.db.commit()
		return result

	def test_start_reuses_active_run_and_is_account_specific(self):
		first = self.start()
		self.assertEqual(first["status"], "Queued")
		self.assertEqual(first["email_account"], self.account.name)
		self.assertEqual(self.start()["name"], first["name"])
		self.assertEqual(self.jobs().get_import_state(self.account.name)["name"], first["name"])

	def test_worker_commits_progress_and_rerun_does_not_duplicate(self):
		self.message()
		first = self.start()
		self.jobs().run_import(first["name"])
		result = self.jobs().get_import_state(self.account.name)
		self.assertEqual(result["status"], "Completed")
		self.assertEqual((result["total"], result["processed"], result["imported"]), (1, 1, 1))
		self.assertEqual(len(result["folders"]), 2)
		self.assertEqual(result["folders"][0]["name"], "INBOX")
		second = self.start()
		self.assertNotEqual(first["name"], second["name"])
		self.jobs().run_import(second["name"])
		result = self.jobs().get_import_state(self.account.name)
		self.assertEqual((result["imported"], result["existing"]), (0, 1))
		self.assertEqual(frappe.db.count("Communication", {"email_account": self.account.name}), 1)

	def test_failed_worker_releases_receiver_without_changing_configuration(self):
		first = self.start()
		with patch("kanzlei_erp.email_archive_jobs.import_archive", side_effect=RuntimeError("private connection detail")):
			self.jobs().run_import(first["name"])
		result = self.jobs().get_import_state(self.account.name)
		self.assertEqual(result["status"], "Failed")
		self.assertNotIn("private connection detail", frappe.as_json(result))
		lock = self.jobs().receiver_lock(self.account.name)
		self.assertTrue(lock.acquire(blocking=False))
		lock.release()
		self.assertEqual(frappe.db.get_value("Email Account", self.account.name, "enable_incoming"), 0)

	def test_running_import_excludes_receiver_and_recovers_only_its_own_lock(self):
		first = self.start()
		lock = self.jobs().receiver_lock(self.account.name)
		self.assertTrue(lock.acquire(blocking=False, token=first["name"]))
		frappe.db.set_value("Email Archive Import", first["name"], {
			"status": "Running", "started_at": add_to_date(now_datetime(), minutes=-10),
		})
		frappe.db.commit()
		with patch("frappe.email.doctype.email_account.email_account.EmailAccount.receive") as receive:
			self.account.receive()
			receive.assert_not_called()
		with patch("kanzlei_erp.email_archive_jobs.get_job_status", return_value="failed"):
			self.jobs().recover_imports()
		self.assertEqual(self.jobs().get_import_state(self.account.name)["status"], "Interrupted")
		self.assertFalse(lock.locked())
		other = self.jobs().receiver_lock(self.account.name)
		self.assertTrue(other.acquire(blocking=False, token="another-receiver"))
		self.jobs()._release_run_lock(first["name"], self.account.name)
		self.assertTrue(other.owned())
		other.release()

	def test_unauthorized_user_cannot_start_or_read_progress(self):
		self.start()
		frappe.set_user("Guest")
		for operation in (self.jobs().start_import, self.jobs().get_import_state):
			with self.assertRaises(frappe.PermissionError):
				operation(self.account.name)

	def test_unsafe_configuration_is_rejected_before_queueing(self):
		frappe.db.set_value("Email Account", self.account.name, "create_contact", 1)
		with self.assertRaises(frappe.ValidationError):
			self.start()
		self.assertFalse(frappe.db.exists("Email Archive Import", {"email_account": self.account.name}))

	def test_enabled_incoming_import_keeps_settings_and_resumes_normal_receiver(self):
		frappe.db.set_value("Email Account", self.account.name, "enable_incoming", 1)
		self.message()
		first = self.start()
		self.jobs().run_import(first["name"])
		self.assertEqual(self.jobs().get_import_state(self.account.name)["status"], "Completed")
		self.assertEqual(frappe.db.get_value("Email Account", self.account.name, "enable_incoming"), 1)
		with patch("frappe.email.doctype.email_account.email_account.EmailAccount.receive") as receive:
			self.account.receive()
			receive.assert_called_once()

	def test_mailbox_batch_failure_advances_checked_count_without_claiming_success(self):
		self.message()
		first = self.start()
		with patch("kanzlei_erp.email_archive._fetch_message_batch", side_effect=RuntimeError("private test error")):
			self.jobs().run_import(first["name"])
		result = self.jobs().get_import_state(self.account.name)
		self.assertEqual(result["status"], "Completed with errors")
		self.assertEqual((result["total"], result["processed"], result["failed"]), (1, 1, 1))
		self.assertEqual(result["folders"][0]["failed"], 1)
		self.assertNotIn("private test error", frappe.as_json(result))
		self.error_log.assert_called()

	def test_simultaneous_stale_transactions_create_only_one_active_run(self):
		frappe.db.commit()
		context = multiprocessing.get_context("spawn")
		barrier, results = context.Barrier(2), context.Queue()
		args = (frappe.local.site, str(Path(frappe.local.sites_path).resolve()), self.account.name, barrier, results)
		processes = [context.Process(target=_start_with_stale_snapshot, args=args) for _ in range(2)]
		try:
			for process in processes:
				process.start()
			names = [results.get(timeout=40) for _ in processes]
			for process in processes:
				process.join(timeout=10)
				self.assertEqual(process.exitcode, 0)
			frappe.db.rollback()
			self.assertEqual(frappe.db.count("Email Archive Import", {"email_account": self.account.name}), 1, names)
			self.assertEqual(names[0], names[1])
		finally:
			for process in processes:
				if process.is_alive():
					process.kill()
				process.join(timeout=5)

	def test_timeout_aborts_instead_of_becoming_a_recoverable_message_error(self):
		from rq.timeouts import JobTimeoutException

		self.message()
		for operation in ("_fetch_message_batch", "_ArchiveMail"):
			with self.subTest(operation=operation):
				first = self.start()
				with patch(f"kanzlei_erp.email_archive.{operation}", side_effect=JobTimeoutException("test timeout")):
					self.jobs().run_import(first["name"])
				self.assertEqual(self.jobs().get_import_state(self.account.name)["status"], "Interrupted")

	def test_receiver_release_outage_keeps_the_run_recoverable(self):
		first = self.start()
		lock = self.jobs().receiver_lock(self.account.name)
		with patch.object(type(lock), "release", side_effect=ConnectionError("synthetic Redis outage")):
			with self.assertRaises(ConnectionError):
				self.jobs().run_import(first["name"])
		self.assertEqual(self.jobs().get_import_state(self.account.name)["status"], "Running")
		frappe.db.set_value("Email Archive Import", first["name"], "started_at", add_to_date(now_datetime(), minutes=-10))
		frappe.db.commit()
		with patch("kanzlei_erp.email_archive_jobs.get_job_status", return_value="failed"):
			self.jobs().recover_imports()
		self.assertFalse(lock.locked())
