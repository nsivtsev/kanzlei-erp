"""Connector acceptance with real ERP permissions, sessions, Redis and private files."""

import hashlib
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request, Response

from kanzlei_erp.tests import test_mandant_documents as document_fixtures


class TestPermissionContext(FrappeTestCase):
	def test_scope_check_authorizes_current_staff_without_listing_documents(self):
		from kanzlei_erp.ai_connector import policy
		from kanzlei_erp.ai_connector.errors import ConnectorError

		original_user = frappe.session.user
		frappe.set_user("Administrator")
		try:
			with patch("frappe.core.doctype.user.user.throttle_user_creation"):
				staff = frappe.get_doc(
					{
						"doctype": "User",
						"email": f"ai-scope-{frappe.generate_hash(length=8)}@example.invalid",
						"first_name": "AI scope fixture",
						"send_welcome_email": 0,
						"roles": [{"role": "Projects User"}],
					}
				).insert()
			allowed = TestAIConnector.customer_fixture(self, "AI scope A")
			denied = TestAIConnector.customer_fixture(self, "AI scope B")
			frappe.share.add("Customer", allowed.name, staff.name, read=1)
			frappe.set_user(staff.name)
			with patch.object(policy, "get_documents", side_effect=AssertionError):
				self.assertEqual(
					policy.check_scope({"kind": "client", "client_id": allowed.name}),
					{"version": 1, "allowed": True},
				)
				with self.assertRaises(ConnectorError) as result:
					policy.check_scope({"kind": "client", "client_id": denied.name})
				self.assertEqual(result.exception.status, 403)
		finally:
			frappe.set_user(original_user)

	def test_restores_request_locals_when_close_fails(self):
		from kanzlei_erp.ai_connector import inventory

		class Fresh:
			def sql(self, *_args, **_kwargs):
				pass

			def begin(self, **_kwargs):
				pass

			def rollback(self):
				self.rolled_back = True

			def close(self):
				raise RuntimeError("synthetic close failure")

		fresh = Fresh()
		original = {key: getattr(frappe.local, key) for key in ("db", "cache", "request_cache", "role_permissions", "user_perms")}
		try:
			with patch("frappe.database.get_db", return_value=fresh):
				with self.assertRaisesRegex(RuntimeError, "synthetic close failure"):
					with inventory.current_permission_context():
						self.assertIs(frappe.local.db, fresh)
			self.assertTrue(fresh.rolled_back)
			for key, value in original.items():
				self.assertIs(getattr(frappe.local, key), value)
		finally:
			for key, value in original.items():
				setattr(frappe.local, key, value)


class TestAIConnector(FrappeTestCase):
	make_file = document_fixtures.TestMandantDocuments.make_file
	make_drive_root = document_fixtures.TestMandantDocuments.make_drive_root
	make_drive_child = document_fixtures.TestMandantDocuments.make_drive_child

	def setUp(self):
		# Repeated synthetic users can exceed the site's ordinary creation limit.
		self.user_throttle = patch("frappe.core.doctype.user.user.throttle_user_creation")
		self.user_throttle.start()
		self.addCleanup(self.user_throttle.stop)
		frappe.set_user("Administrator")
		self.file_names = set()
		self.reader = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"ai-reader-{frappe.generate_hash(length=8)}@example.invalid",
				"first_name": "AI fixture",
				"send_welcome_email": 0,
				"roles": [{"role": "Projects User"}],
			}
		).insert()
		self.customer = self.customer_fixture("AI client A")
		self.other = self.customer_fixture("AI client B")
		frappe.share.add("Customer", self.customer.name, self.reader.name, read=1)
		self.root = self.make_drive_root()
		from pypdf import PdfWriter

		pdf = BytesIO()
		writer = PdfWriter()
		writer.add_blank_page(width=72, height=72)
		writer.add_metadata({"/Title": "Synthetic " + frappe.generate_hash(length=16)})
		writer.write(pdf)
		# Keep Frappe's text round-trip byte-preserving, as in the document fixture.
		self.original = pdf.getvalue().replace(b"%\xe2\xe3\xcf\xd3", b"%test", 1)
		self.file = self.make_drive_child(self.root, "synthetic.pdf", self.original)
		self.key = "synthetic-service-key-" + frappe.generate_hash(length=32)
		self.config = {
			"enabled": True,
			"connector_id": "fixture-erp",
			"audience": "fixture-ai",
			"service_key_sha256": hashlib.sha256(self.key.encode()).hexdigest(),
			"general_roles": [],
		}
		self.config_patch = patch.dict(frappe.conf, {"kanzlei_ai_connector": self.config})
		self.config_patch.start()
		self.addCleanup(self.config_patch.stop)
		self.config_read_patch = patch(
			"kanzlei_erp.ai_connector.auth.get_site_config", side_effect=lambda **kwargs: frappe.conf
		)
		self.config_read_patch.start()
		self.addCleanup(self.config_read_patch.stop)
		self.sid = "ai-session-" + frappe.generate_hash(length=24)
		frappe.db.sql(
			"INSERT INTO `tabSessions` (sid,user,lastupdate,sessiondata,status) VALUES (%s,%s,%s,%s,'Active')",
			(self.sid, self.reader.name, frappe.utils.now(), json.dumps({"session_expiry": "01:00:00"})),
		)
		frappe.set_user(self.reader.name)
		frappe.session.sid = self.sid

	def tearDown(self):
		frappe.set_user("Administrator")
		if getattr(self, "committed", False):
			frappe.db.rollback()
		for name in self.file_names:
			if frappe.db.exists("File", name):
				Path(frappe.get_doc("File", name).get_full_path()).unlink(missing_ok=True)
		if getattr(self, "committed", False):
			frappe.db.delete("Sessions", {"sid": self.sid})
			frappe.db.delete("File", {"name": ["in", [self.root.name, *self.file_names]]})
			for customer in (self.customer, self.other):
				frappe.delete_doc("Customer", customer.name, ignore_permissions=True, force=True)
			frappe.delete_doc("User", self.reader.name, ignore_permissions=True, force=True)
			frappe.db.commit()
		super().tearDown()

	def customer_fixture(self, title):
		return frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": title + frappe.generate_hash(length=8),
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()

	def api(self):
		self.assertTrue(
			Path(frappe.get_app_path("kanzlei_erp", "ai_connector", "api.py")).exists(),
			"ERP connector API missing",
		)
		return importlib.import_module("kanzlei_erp.ai_connector.api")

	def call(self, method, *, service=True, http_method="POST", **kwargs):
		headers = {"X-Kanzlei-AI-Service-Key": self.key} if service else {}
		request = Request(EnvironBuilder(method=http_method, headers=headers).get_environ())
		with patch.object(frappe.local, "request", request, create=True):
			return getattr(self.api(), method)(**kwargs)

	def grant(self):
		ticket = self.call("issue_ticket", service=False)
		self.assertIsInstance(ticket, dict)
		grant = self.call("exchange_ticket", ticket=ticket["ticket"])
		self.assertIsInstance(grant, dict)
		return grant["access_token"]

	def scope(self, client=None):
		return {"kind": "client", "client_id": client or self.customer.name}

	def documents(self, token, **kwargs):
		return self.call("list_documents", access_token=token, client_id=self.customer.name, **kwargs)

	def test_ticket_requires_active_human_session_and_cannot_select_identity(self):
		self.assertEqual(self.call("issue_ticket", service=False, user="Administrator").status_code, 400)
		frappe.session.sid = "Guest"
		self.assertEqual(self.call("issue_ticket", service=False).status_code, 401)

	def test_one_time_exchange_requires_service_key_and_rejects_replay(self):
		ticket = self.call("issue_ticket", service=False)["ticket"]
		self.assertEqual(self.call("exchange_ticket", ticket=ticket, service=False).status_code, 401)
		result = self.call("exchange_ticket", ticket=ticket)
		self.assertEqual(result["identity"]["subject"], self.reader.name)
		self.assertNotIn(self.sid, json.dumps(result))
		self.assertEqual(self.call("exchange_ticket", ticket=ticket).status_code, 401)

	def test_service_credential_alone_cannot_impersonate_a_user(self):
		result = self.call("identity", access_token="x" * 43, user="Administrator")
		self.assertEqual(result.status_code, 400)
		self.assertNotIn(b"Administrator", result.get_data())
		self.assertEqual(self.call("identity", access_token="x" * 43).status_code, 401)

	def test_identity_logout_and_key_rotation_revoke_access(self):
		token = self.grant()
		self.assertEqual(self.call("identity", access_token=token)["subject"], self.reader.name)
		self.config["service_key_sha256"] = "0" * 64
		self.assertEqual(self.call("identity", access_token=token).status_code, 401)
		self.config["service_key_sha256"] = hashlib.sha256(self.key.encode()).hexdigest()
		frappe.db.delete("Sessions", {"sid": self.sid})
		self.assertEqual(self.call("identity", access_token=token).status_code, 401)

	def test_expired_and_wrong_audience_grants_are_rejected(self):
		token = self.grant()
		with patch("kanzlei_erp.ai_connector.auth.time.time", return_value=9999999999):
			self.assertEqual(self.call("identity", access_token=token).status_code, 401)
		self.config["audience"] = "different-service"
		self.assertEqual(self.call("identity", access_token=token).status_code, 401)

	def test_disabled_user_cannot_keep_access(self):
		token = self.grant()
		frappe.db.set_value("User", self.reader.name, "enabled", 0)
		self.assertEqual(self.call("identity", access_token=token).status_code, 401)

	def test_revoke_is_idempotent_and_post_only(self):
		token = self.grant()
		self.assertEqual(self.call("revoke", access_token=token), {"version": 1, "revoked": True})
		self.assertEqual(self.call("revoke", access_token=token), {"version": 1, "revoked": True})
		self.assertEqual(self.call("identity", access_token=token).status_code, 401)
		self.assertEqual(self.call("identity", access_token=token, http_method="GET").status_code, 405)

	def test_clients_and_documents_follow_current_permissions(self):
		token = self.grant()
		clients = self.call("list_clients", access_token=token)
		self.assertEqual([r["id"] for r in clients["items"]], [self.customer.name])
		page = self.documents(token)
		self.assertEqual([r["source_id"] for r in page["items"]], ["file:" + self.file.name])
		self.assertEqual(page["items"][0]["client_ids"], [self.customer.name])
		self.assertNotIn("file_url", json.dumps(page))
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		self.assertEqual(self.documents(token).status_code, 403)
		self.assertEqual(frappe.session.user, "Administrator")

	def test_read_returns_original_bytes_and_checks_revision_and_scope(self):
		token = self.grant()
		manifest = self.documents(token)["items"][0]
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		response = self.call("read_source", access_token=token, scope=self.scope(), source=ref)
		self.assertEqual(response.get_data(), self.original)
		self.assertEqual(response.headers["X-AI-Content-SHA256"], hashlib.sha256(self.original).hexdigest())
		self.assertEqual(response.headers["Cache-Control"], "private, no-store")
		wrong = self.call("read_source", access_token=token, scope=self.scope(self.other.name), source=ref)
		self.assertEqual(wrong.status_code, 403)
		frappe.db.set_value("File", self.file.name, "file_name", "replacement.pdf")
		self.assertEqual(
			self.call("read_source", access_token=token, scope=self.scope(), source=ref).status_code, 409
		)

	def test_background_read_rejects_local_or_non_drive_source(self):
		"""The worker can read only its configured Drive source and current rights."""
		ingestion_key = "synthetic-ingestion-key-" + frappe.generate_hash(length=32)
		self.config["ingestion"] = {
			"enabled": True,
			"user": self.reader.name,
			"client_ids": [self.customer.name],
			"generation": "fixture-v1",
			"key_sha256": hashlib.sha256(ingestion_key.encode()).hexdigest(),
		}
		manifest = self.documents(self.grant())["items"][0]
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		# background_read_source deliberately uses an isolated connection.
		self.committed = True
		frappe.db.commit()
		request = Request(
			EnvironBuilder(method="POST", headers={"X-Kanzlei-AI-Service-Key": ingestion_key}).get_environ()
		)
		with (
			patch.object(frappe.local, "request", request, create=True),
			patch("kanzlei_erp.object_storage.get_binding", return_value=None),
		):
			response = self.api().background_read_source(source=ref)
		self.assertEqual(response.status_code, 409)
		# The separate private key is required; a browser grant never authorizes this path.
		self.assertEqual(self.call("background_read_source", source=ref).status_code, 401)

	def test_batch_authorization_has_no_denied_metadata_and_rechecks_revocation(self):
		token = self.grant()
		manifest = self.documents(token)["items"][0]
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		fake = {**ref, "source_id": "file:does-not-exist"}
		result = self.call("authorize_sources", access_token=token, scope=self.scope(), sources=[ref, fake])
		self.assertEqual([r["allowed"] for r in result["decisions"]], [True, False])
		self.assertEqual(set(result["decisions"][1]), {"source_id", "revision", "client_id", "allowed"})
		frappe.db.set_value("File", self.root.name, "status", "Trashed")
		result = self.call("authorize_sources", access_token=token, scope=self.scope(), sources=[ref])
		self.assertFalse(result["decisions"][0]["allowed"])

	def test_processing_policy_rechecks_the_current_delegation_and_source_revision(self):
		self.committed = True
		frappe.db.commit()
		token = self.grant()
		manifest = self.documents(token)["items"][0]
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		payload = {"operation": "embed", "access_token": token, "scope": self.scope(), "sources": [ref]}
		self.assertEqual(self.call("check_processing_policy", **payload).status_code, 403)
		self.config["external_processing"] = {self.customer.name: {"embed": True}}
		self.assertEqual(
			self.call("check_processing_policy", **payload), {"version": 1, "allowed": True}
		)
		frappe.db.set_value("File", self.file.name, "file_name", "replacement.pdf")
		frappe.db.commit()
		self.assertEqual(self.call("check_processing_policy", **payload).status_code, 403)
		self.assertEqual(
			self.call("check_processing_policy", **(payload | {"sources": []})),
			{"version": 1, "allowed": True},
		)
		frappe.db.delete("Sessions", {"sid": self.sid})
		frappe.db.commit()
		self.assertEqual(
			self.call("check_processing_policy", **(payload | {"sources": []})).status_code, 401
		)

	def test_processing_policy_checks_the_fixed_ingestion_drive_projection(self):
		ingestion_key = "synthetic-ingestion-key-" + frappe.generate_hash(length=32)
		self.config["ingestion"] = {
			"enabled": True,
			"user": self.reader.name,
			"client_ids": [self.customer.name],
			"generation": "fixture-v1",
			"key_sha256": hashlib.sha256(ingestion_key.encode()).hexdigest(),
		}
		binding = SimpleNamespace(
			object_key="fixture/document", sha256=hashlib.sha256(self.original).hexdigest(),
			file_size=len(self.original),
		)
		self.committed = True
		frappe.db.commit()
		with patch("kanzlei_erp.object_storage.get_binding", return_value=binding):
			manifest = self.documents(self.grant())["items"][0]
			ref = {
				"source_id": manifest["source_id"],
				"revision": manifest["revision"],
				"client_id": self.customer.name,
			}
			payload = {"operation": "ocr", "scope": self.scope(), "sources": [ref]}
			self.assertEqual(self.call("check_processing_policy", **payload).status_code, 401)
			with patch.object(self, "key", ingestion_key):
				self.assertEqual(self.call("check_processing_policy", **payload).status_code, 403)
				self.config["external_processing"] = {self.customer.name: {"ocr": True}}
				self.assertEqual(
					self.call("check_processing_policy", **payload), {"version": 1, "allowed": True}
				)
				frappe.db.set_value("File", self.root.name, "status", "Trashed")
				frappe.db.commit()
				self.assertEqual(self.call("check_processing_policy", **payload).status_code, 403)

	def test_unknown_fields_versions_and_oversized_batches_fail_closed(self):
		token = self.grant()
		for kwargs in ({"version": 2}, {"limit": 101}, {"role": "System Manager"}):
			self.assertEqual(self.call("list_clients", access_token=token, **kwargs).status_code, 400)
		self.assertEqual(
			self.call(
				"authorize_sources", access_token=token, scope=self.scope(), sources=[{}] * 51
			).status_code,
			400,
		)

	def test_general_scope_needs_an_explicit_role(self):
		token = self.grant()
		ref = {"source_id": "file:" + self.file.name, "revision": "0" * 64, "client_id": self.customer.name}
		self.assertFalse(self.call("identity", access_token=token)["general_access"])
		self.assertEqual(
			self.call(
				"authorize_sources", access_token=token, scope={"kind": "general"}, sources=[ref]
			).status_code,
			403,
		)

	def test_general_scope_accepts_configured_frappe_automatic_roles(self):
		self.committed = True
		frappe.db.commit()
		token = self.grant()
		for role in ("All", "Guest", "Desk User"):
			with self.subTest(role=role):
				self.config["general_roles"] = [role]
				self.assertTrue(self.call("identity", access_token=token)["general_access"])
		self.config["general_roles"] = ["projects user", "Administrator"]
		self.assertFalse(self.call("identity", access_token=token)["general_access"])

	def test_committed_general_role_removal_ignores_stale_redis_roles(self):
		self.config["general_roles"] = ["Projects User"]
		self.config["external_processing"] = {self.customer.name: {"generate": True}}
		self.committed = True
		frappe.db.commit()
		token = self.grant()
		manifest = self.documents(token)["items"][0]
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		allowed = {"version": 1, "allowed": True}
		general = {"access_token": token, "scope": {"kind": "general"}}
		self.assertTrue(self.call("identity", access_token=token)["general_access"])
		self.assertEqual(self.call("check_scope", **general), allowed)
		self.assertTrue(self.call("authorize_sources", **general, sources=[ref])["decisions"][0]["allowed"])
		self.assertIn("locator", self.call("resolve_source_locator", **general, source=ref))
		self.assertEqual(self.call("read_source", **general, source=ref).get_data(), self.original)
		self.assertEqual(
			self.call("check_processing_policy", **general, operation="generate", sources=[ref]), allowed
		)

		stale_roles = frappe.get_roles(self.reader.name)
		self.assertIn("Projects User", stale_roles)
		self._commit_general_role_removal()

		for method, kwargs in (
			("identity", {"access_token": token}),
			("check_scope", general),
			("authorize_sources", {**general, "sources": [ref]}),
			("resolve_source_locator", {**general, "source": ref}),
			("read_source", {**general, "source": ref}),
			("check_processing_policy", {**general, "operation": "generate", "sources": [ref]}),
		):
			with self.subTest(method=method), patch.object(frappe.local, "cache", {}):
				# Simulate an older request repopulating the role hash.
				frappe.cache.hset("roles", self.reader.name, stale_roles)
				frappe.local.cache.clear()
				self.assertEqual(frappe.cache.hget("roles", self.reader.name), stale_roles)
				response = self.call(method, **kwargs)
				if method == "identity":
					self.assertFalse(response["general_access"])
				else:
					self.assertIsInstance(response, Response)
					self.assertEqual(response.status_code, 403)

		self.assertEqual(self.call("check_scope", access_token=token, scope=self.scope()), allowed)

	def test_ordinary_general_read_rechecks_role_after_loading_bytes(self):
		from kanzlei_erp.object_storage import open_file

		self.config["general_roles"] = ["Projects User"]
		self.committed = True
		frappe.db.commit()
		token = self.grant()
		manifest = self.documents(token)["items"][0]
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}

		@contextmanager
		def revoking_reader(file):
			with open_file(file) as stream:
				yield stream
			self._commit_general_role_removal()

		with patch("kanzlei_erp.object_storage.open_file", revoking_reader):
			response = self.call("read_source", access_token=token, scope={"kind": "general"}, source=ref)
		self.assertEqual(response.status_code, 403)

	def _commit_general_role_removal(self):
		site, subject = frappe.local.site, self.reader.name

		def remove_role():
			frappe.init(site)
			frappe.connect()
			try:
				frappe.db.delete("Has Role", {"parenttype": "User", "parent": subject, "role": "Projects User"})
				frappe.db.commit()
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=1) as executor:
			executor.submit(remove_role).result(timeout=15)

	def test_signed_pagination_is_bound_to_user_and_client(self):
		frappe.set_user("Administrator")
		self.make_drive_child(self.root, "second.pdf")
		token = self.grant_for_reader()
		page = self.documents(token, limit=1)
		self.assertTrue(page["next_cursor"])
		second = self.documents(token, limit=1, cursor=page["next_cursor"])
		self.assertNotEqual(page["items"][0]["source_id"], second["items"][0]["source_id"])
		self.assertEqual(self.documents(token, cursor=page["next_cursor"] + "tampered").status_code, 400)

	def test_client_page_continues_after_record_level_denials(self):
		frappe.set_user("Administrator")
		extra = self.customer_fixture("AI client C")
		for customer in (self.other, extra):
			frappe.share.add("Customer", customer.name, self.reader.name, read=1)
		names = sorted([self.customer.name, self.other.name, extra.name])
		token = self.grant_for_reader()
		original = frappe.has_permission

		def per_record(doctype=None, ptype="read", doc=None, **kwargs):
			if doctype == "Customer" and isinstance(doc, str) and doc in names[:2]:
				return False
			return original(doctype, ptype, doc=doc, **kwargs)

		with patch("frappe.has_permission", per_record):
			page = self.call("list_clients", access_token=token, limit=1)
		self.assertEqual([row["id"] for row in page["items"]], [names[2]])

	def grant_for_reader(self):
		frappe.set_user(self.reader.name)
		frappe.session.sid = self.sid
		return self.grant()

	def test_feature_disabled_is_safe_and_does_not_read_files(self):
		self.config["enabled"] = False
		response = self.call("issue_ticket", service=False)
		self.assertEqual(response.status_code, 503)
		self.assertNotIn(b"Traceback", response.get_data())

	def test_standard_frappe_rpc_dispatch_preserves_the_contract(self):
		from frappe.handler import execute_cmd

		method = "kanzlei_erp.ai_connector.api.issue_ticket"
		request = Request(EnvironBuilder(method="POST").get_environ())
		with (
			patch.object(frappe.local, "request", request, create=True),
			patch.object(frappe.local, "form_dict", frappe._dict(cmd=method)),
		):
			result = execute_cmd(method)
		self.assertIsInstance(result, dict)
		self.assertIn("ticket", result)

	def test_delegation_preserves_the_original_session_object_and_csrf_data(self):
		token = self.grant()
		original = frappe.local.session
		original.data = frappe._dict(csrf_token="synthetic-csrf", last_updated=frappe.utils.now())
		result = self.call("list_clients", access_token=token)
		self.assertIsInstance(result, dict)
		self.assertIs(frappe.local.session, original)
		self.assertEqual(original.data.csrf_token, "synthetic-csrf")
		self.assertEqual(original.sid, self.sid)

	def test_security_rotation_does_not_trust_the_cached_request_config(self):
		token = self.grant()
		fresh = {**self.config, "service_key_sha256": "0" * 64}
		with patch(
			"kanzlei_erp.ai_connector.auth.get_site_config",
			return_value={"kanzlei_ai_connector": fresh},
			create=True,
		):
			self.assertEqual(self.call("identity", access_token=token).status_code, 401)

	def test_replacement_committed_during_read_is_not_hidden_by_the_old_snapshot(self):
		from kanzlei_erp.object_storage import open_file

		token = self.grant()
		manifest = self.documents(token)["items"][0]
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		self.committed = True
		frappe.db.commit()
		site, file_id = frappe.local.site, self.file.name

		def replace():
			frappe.init(site)
			frappe.connect()
			try:
				frappe.db.set_value("File", file_id, "file_name", "concurrent-replacement.pdf")
				frappe.db.commit()
			finally:
				frappe.destroy()

		@contextmanager
		def replacing_reader(file):
			with open_file(file) as stream:
				yield stream
			with ThreadPoolExecutor(max_workers=1) as executor:
				executor.submit(replace).result(timeout=15)

		with patch("kanzlei_erp.object_storage.open_file", replacing_reader):
			result = self.call("read_source", access_token=token, scope=self.scope(), source=ref)
		self.assertEqual(result.status_code, 409)


class TestAIConnectorS3(FrappeTestCase):
	api = TestAIConnector.api
	call = TestAIConnector.call
	grant = TestAIConnector.grant
	scope = TestAIConnector.scope
	documents = TestAIConnector.documents
	customer_fixture = TestAIConnector.customer_fixture
	make_file = TestAIConnector.make_file
	make_drive_root = TestAIConnector.make_drive_root
	make_drive_child = TestAIConnector.make_drive_child

	def setUp(self):
		from kanzlei_erp.tests.test_object_storage import TestObjectStorage

		TestObjectStorage.setUp(self)
		TestAIConnector.setUp(self)

	def tearDown(self):
		frappe.set_user("Administrator")
		self.storage.cleanup_temporary_files()
		super().tearDown()

	def test_managed_s3_original_passes_the_same_delegated_checks(self):
		token = self.grant()
		manifest = self.documents(token)["items"][0]
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		response = self.call("read_source", access_token=token, scope=self.scope(), source=ref)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.get_data(), self.original)

	def test_background_read_uses_fixed_ingestion_identity_and_current_s3_drive_rights(self):
		ingestion_key = "synthetic-ingestion-key-" + frappe.generate_hash(length=32)
		self.config["ingestion"] = {
			"enabled": True,
			"user": self.reader.name,
			"client_ids": [self.customer.name],
			"generation": "fixture-v1",
			"key_sha256": hashlib.sha256(ingestion_key.encode()).hexdigest(),
		}
		manifest = self.documents(self.grant())["items"][0]
		ref = {"source_id": manifest["source_id"], "revision": manifest["revision"], "client_id": self.customer.name}
		# A production background read is a separate database session. Commit the
		# synthetic fixture so its isolated READ COMMITTED transaction can see it.
		self.committed = True
		frappe.db.commit()
		request = Request(EnvironBuilder(method="POST", headers={"X-Kanzlei-AI-Service-Key": ingestion_key}).get_environ())
		with patch.object(frappe.local, "request", request, create=True):
			response = self.api().background_read_source(source=ref)
		self.assertEqual(response.get_data(), self.original)
		frappe.set_user("Administrator")
		frappe.db.set_value("File", self.root.name, "status", "Trashed")
		frappe.db.commit()
		with patch.object(frappe.local, "request", request, create=True):
			self.assertEqual(self.api().background_read_source(source=ref).status_code, 409)

	def test_background_read_denies_docshare_revoked_after_bytes_and_preserves_request_db(self):
		from kanzlei_erp.object_storage import open_file

		ingestion_key = "synthetic-ingestion-key-" + frappe.generate_hash(length=32)
		self.config["ingestion"] = {
			"enabled": True,
			"user": self.reader.name,
			"client_ids": [self.customer.name],
			"generation": "fixture-v1",
			"key_sha256": hashlib.sha256(ingestion_key.encode()).hexdigest(),
		}
		# This fixture gets Customer access only through the explicit DocShare.
		self.assertTrue(frappe.has_permission("Customer", "read", doc=self.customer.name, user=self.reader.name))
		manifest = self.documents(self.grant())["items"][0]
		ref = {"source_id": manifest["source_id"], "revision": manifest["revision"], "client_id": self.customer.name}
		self.committed = True
		frappe.db.commit()
		site, user, customer = frappe.local.site, self.reader.name, self.customer.name
		original_db, original_connection = frappe.local.db, frappe.local.db._conn

		def revoke_share():
			frappe.init(site)
			frappe.connect()
			try:
				frappe.share.remove("Customer", customer, user)
				frappe.db.commit()
			finally:
				frappe.destroy()

		@contextmanager
		def revoking_reader(file):
			with open_file(file) as stream:
				yield stream
			with ThreadPoolExecutor(max_workers=1) as executor:
				executor.submit(revoke_share).result(timeout=15)

		request = Request(EnvironBuilder(method="POST", headers={"X-Kanzlei-AI-Service-Key": ingestion_key}).get_environ())
		with patch("kanzlei_erp.object_storage.open_file", revoking_reader), patch.object(
			frappe.local, "request", request, create=True
		):
			response = self.api().background_read_source(source=ref)
		self.assertEqual(response.status_code, 403)
		self.assertIs(frappe.local.db, original_db)
		self.assertIs(frappe.local.db._conn, original_connection)
		self.assertEqual(frappe.db.sql("SELECT 1")[0][0], 1)

	def test_s3_binding_replacement_invalidates_revision_without_file_modified(self):
		token = self.grant()
		manifest = self.documents(token)["items"][0]
		ref = {
			"source_id": manifest["source_id"],
			"revision": manifest["revision"],
			"client_id": self.customer.name,
		}
		before = frappe.db.get_value("File", self.file.name, "modified")
		frappe.set_user("Administrator")
		self.s3.objects["unit/replacement"] = b"replacement"
		self.storage.replace_content(
			self.file, "unit/replacement", 11, hashlib.sha256(b"replacement").hexdigest()
		)
		self.assertEqual(frappe.db.get_value("File", self.file.name, "modified"), before)
		response = self.call("read_source", access_token=token, scope=self.scope(), source=ref)
		self.assertEqual(response.status_code, 409)
