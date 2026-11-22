"""Current-session delivery of a cited Drive DOCX revision."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.ai_connector import api, policy
from kanzlei_erp.mandant_documents import download_document
from kanzlei_erp.object_storage import open_file
from kanzlei_erp.tests import test_ai_connector_email as email_fixtures
from kanzlei_erp.tests import test_mandant_documents as drive_fixtures


class TestDriveDocxOriginal(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.customers = []
		self.customer = email_fixtures.TestAIConnectorEmail.make_customer(self)
		self.root = drive_fixtures.TestMandantDocuments.make_drive_root(self)
		with patch("frappe.core.doctype.user.user.throttle_user_creation"):
			self.reader = frappe.get_doc(
				{
					"doctype": "User",
					"email": f"drive-docx-{frappe.generate_hash(length=8)}@example.invalid",
					"first_name": "Synthetic DOCX reader",
					"send_welcome_email": 0,
					"roles": [{"role": "Projects User"}],
				}
			).insert()
		frappe.share.add("Customer", self.customer.name, self.reader.name, read=1)
		self.content = email_fixtures.synthetic_docx()
		self.file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": frappe.generate_hash(length=8) + ".docx",
				"is_private": 1,
				"content": self.content,
			}
		).insert()
		frappe.db.set_value(
			"File",
			self.file.name,
			{
				"folder": self.root.name,
				"status": "Active",
				"kanzlei_drive_customer": self.customer.name,
			},
		)
		self.sid = "synthetic-docx-session-" + frappe.generate_hash(length=20)
		frappe.db.sql(
			"INSERT INTO `tabSessions` (sid,user,lastupdate,sessiondata,status) VALUES (%s,%s,%s,%s,'Active')",
			(self.sid, self.reader.name, frappe.utils.now(), json.dumps({"session_expiry": "01:00:00"})),
		)
		frappe.db.commit()
		self.site = frappe.local.site
		config = {
			"enabled": True,
			"connector_id": "fixture",
			"audience": "fixture",
			"service_key_sha256": "a" * 64,
		}
		patcher = patch.object(api.auth, "get_site_config", return_value={"kanzlei_ai_connector": config})
		patcher.start()
		self.addCleanup(patcher.stop)
		frappe.set_user(self.reader.name)
		frappe.session.sid = self.sid

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.delete("Sessions", {"sid": self.sid})
		frappe.delete_doc("File", self.file.name, ignore_permissions=True, force=True)
		frappe.db.set_value("Customer", self.customer.name, "kanzlei_drive_folder", None)
		frappe.db.delete("File", {"name": self.root.name})
		frappe.delete_doc("User", self.reader.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Customer", self.customer.name, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDown()

	def arguments(self):
		customer = self.customer.name
		scope = {"kind": "client", "client_id": customer}
		file = frappe.get_doc("File", self.file.name)
		item = policy.manifest(file, customer)
		ref = {"source_id": item["source_id"], "revision": item["revision"], "client_id": customer}
		locator = urlsplit(policy.resolve_source_locator(scope, ref)["locator"])
		self.assertEqual(locator.path, "/api/method/kanzlei_erp.ai_connector.api.download_drive_docx")
		self.assertEqual(
			parse_qs(locator.query),
			{"customer": [customer], "file_id": [file.name], "revision": [item["revision"]]},
		)
		return {key: values[0] for key, values in parse_qs(locator.query).items()}

	def download(self, arguments):
		request = Request(EnvironBuilder(method="GET").get_environ())
		with patch.object(frappe.local, "request", request, create=True):
			return api.download_drive_docx(**arguments)

	@contextmanager
	def mutate_during_read(self, mutate):
		def commit():
			frappe.init(self.site)
			frappe.connect()
			try:
				mutate()
				frappe.db.commit()
			finally:
				frappe.destroy()

		@contextmanager
		def open_and_commit(file):
			with open_file(file) as stream:
				yield stream
			with ThreadPoolExecutor(max_workers=1) as worker:
				worker.submit(commit).result(timeout=10)

		with patch("kanzlei_erp.object_storage.open_file", open_and_commit):
			yield

	def test_drive_docx_revision_is_checked_after_read_and_ordinary_download_still_works(self):
		arguments = self.arguments()
		initial = self.download(arguments)
		self.assertEqual(initial.get_data(), self.content)
		self.assertEqual(initial.mimetype, email_fixtures.DOCX_MIME)
		self.assertTrue(initial.headers["Content-Disposition"].startswith("attachment;"))
		frappe.session.sid = "Guest"
		self.assertEqual(self.download(arguments).status_code, 401)
		frappe.session.sid = self.sid
		with self.mutate_during_read(
			lambda: frappe.db.set_value("File", self.file.name, "file_name", "Revision changed.docx")
		):
			self.assertEqual(self.download(arguments).status_code, 409)
		self.assertEqual(self.download(arguments).status_code, 409)
		frappe.db.rollback()
		download_document(self.customer.name, self.file.name)
		self.assertEqual(frappe.local.response.filecontent, self.content)

	def test_drive_docx_customer_permission_is_checked_after_read(self):
		arguments = self.arguments()
		with self.mutate_during_read(
			lambda: frappe.share.remove("Customer", self.customer.name, self.reader.name)
		):
			self.assertEqual(self.download(arguments).status_code, 403)

	def test_drive_docx_file_membership_is_checked_after_read(self):
		arguments = self.arguments()
		with self.mutate_during_read(lambda: frappe.db.set_value("File", self.file.name, "folder", "Home")):
			self.assertEqual(self.download(arguments).status_code, 403)
