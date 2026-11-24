"""Browser ticket isolation, binding and session checks on a disposable Frappe site."""

import base64
import hashlib
import json
import secrets
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.ai_connector import api, auth
from kanzlei_erp.ai_connector.errors import ConnectorError

ERP_ORIGIN = "https://erp.example.invalid"
CHAT_ORIGIN = "https://chat.example.invalid"


class TestAIBrowserAuth(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.key = secrets.token_urlsafe(32)
		self.sid = secrets.token_urlsafe(32)
		self.csrf = secrets.token_urlsafe(32)
		self.state = secrets.token_urlsafe(32)
		self.verifier = secrets.token_urlsafe(32)
		self.challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode()[
			:-1
		]
		self.config = {
			"enabled": True,
			"connector_id": "browser-fixture",
			"audience": "fixture",
			"service_key_sha256": hashlib.sha256(self.key.encode()).hexdigest(),
			"browser_chat_origin": CHAT_ORIGIN,
			"allowed_users": ["Administrator"],
		}
		config_read = patch(
			"kanzlei_erp.ai_connector.auth.get_site_config",
			return_value={
				"kanzlei_ai_connector": self.config,
			},
		)
		config_read.start()
		self.addCleanup(config_read.stop)
		session = frappe.local.session
		previous_sid, previous_data = session.sid, session.get("data")
		session.sid = self.sid
		session.data = frappe._dict(csrf_token=self.csrf)
		self.addCleanup(session.update, sid=previous_sid, data=previous_data)
		frappe.db.sql(
			"INSERT INTO `tabSessions` (sid,user,lastupdate,sessiondata,status) VALUES (%s,%s,%s,%s,'Active')",
			(self.sid, "Administrator", frappe.utils.now(), json.dumps({"session_expiry": "01:00:00"})),
		)

	def call(self, method, *, service=True, headers=None, verb="POST", base_url=ERP_ORIGIN, **kwargs):
		base_headers = (
			{"X-Kanzlei-AI-Service-Key": self.key}
			if service
			else {"Origin": ERP_ORIGIN, "Cookie": "sid=" + self.sid, "X-Frappe-CSRF-Token": self.csrf}
		)
		builder = EnvironBuilder(method=verb, base_url=base_url, headers={**base_headers, **(headers or {})})
		with patch.object(frappe.local, "request", Request(builder.get_environ()), create=True):
			endpoint = getattr(api, method, None)
			self.assertTrue(callable(endpoint), "Browser RPC is missing")
			return endpoint(**kwargs)

	def issue(self):
		response = self.call(
			"issue_browser_ticket", service=False, state=self.state, challenge=self.challenge
		)
		self.assertTrue(isinstance(response, dict), "Browser ticket issuance failed")
		return response["ticket"]

	def exchange(self, ticket, **kwargs):
		payload = {
			"ticket": ticket,
			"state": self.state,
			"verifier": self.verifier,
			"chat_origin": CHAT_ORIGIN,
		}
		return self.call("exchange_browser_ticket", **{**payload, **kwargs})

	def assert_failure(self, response, status, code):
		self.assertEqual(getattr(response, "status_code", None), status)
		self.assertTrue(json.loads(response.get_data()) == {"code": code}, "Failure exposed request data")
		self.assertEqual(response.headers["Cache-Control"], "private, no-store")

	def test_binding_mismatches_do_not_consume_a_browser_ticket(self):
		ticket = self.issue()
		self.assert_failure(self.exchange(ticket, state=secrets.token_urlsafe(32)), 401, "UNAUTHENTICATED")
		self.assert_failure(self.exchange(ticket, verifier=secrets.token_urlsafe(32)), 401, "UNAUTHENTICATED")
		self.assert_failure(self.exchange(ticket, verifier=None), 400, "INVALID_REQUEST")
		self.assert_failure(
			self.exchange(ticket, chat_origin="https://other.example.invalid"), 401, "UNAUTHENTICATED"
		)
		self.config["browser_chat_origin"] = "https://other.example.invalid"
		self.assert_failure(
			self.exchange(ticket, chat_origin="https://other.example.invalid"), 401, "UNAUTHENTICATED"
		)
		self.config["browser_chat_origin"] = CHAT_ORIGIN
		grant = self.exchange(ticket)
		self.assertTrue(isinstance(grant, dict), "Valid exchange failed after mismatched attempts")
		self.assertTrue(grant["identity"]["subject"] == "Administrator")
		self.assert_failure(self.exchange(ticket), 401, "UNAUTHENTICATED")
		frappe.db.delete("Sessions", {"sid": self.sid})
		self.assert_failure(self.call("identity", access_token=grant["access_token"]), 401, "UNAUTHENTICATED")

	def test_browser_and_legacy_ticket_namespaces_are_disjoint(self):
		browser = self.issue()
		self.assert_failure(self.call("exchange_ticket", ticket=browser), 401, "UNAUTHENTICATED")
		self.assertTrue(
			isinstance(self.exchange(browser), dict), "Legacy exchange consumed the browser ticket"
		)
		legacy = self.call("issue_ticket", service=False)["ticket"]
		self.assert_failure(self.exchange(legacy), 401, "UNAUTHENTICATED")
		self.assertTrue(
			isinstance(self.call("exchange_ticket", ticket=legacy), dict), "Legacy exchange failed"
		)

	def test_browser_issue_requires_post_cookie_same_origin_csrf_and_allowlisting(self):
		payload = {"state": self.state, "challenge": self.challenge}
		for header in ("Origin", "Cookie", "X-Frappe-CSRF-Token"):
			self.assert_failure(
				self.call("issue_browser_ticket", service=False, headers={header: ""}, **payload),
				401,
				"UNAUTHENTICATED",
			)
		self.assert_failure(
			self.call("issue_browser_ticket", service=False, verb="GET", **payload),
			405,
			"METHOD_NOT_ALLOWED",
		)
		self.config["allowed_users"] = []
		self.assert_failure(
			self.call("issue_browser_ticket", service=False, **payload), 401, "UNAUTHENTICATED"
		)

	def test_same_host_chat_uses_public_https_origin_behind_tls_termination(self):
		self.config["browser_chat_origin"] = ERP_ORIGIN
		erp_host = urlsplit(ERP_ORIGIN).netloc
		internal_origin = ERP_ORIGIN.replace("https://", "http://", 1)
		payload = {"state": self.state, "challenge": self.challenge}
		ticket = self.call("issue_browser_ticket", service=False, base_url=internal_origin, **payload)
		self.assertIsInstance(ticket, dict)
		self.assertIsInstance(self.exchange(ticket["ticket"], chat_origin=ERP_ORIGIN), dict)

		for host, origin in (
			(erp_host, internal_origin),
			(erp_host, CHAT_ORIGIN),
			(urlsplit(CHAT_ORIGIN).netloc, ERP_ORIGIN),
			(f"{erp_host}:80", ERP_ORIGIN),
			(f"{erp_host}:8443", ERP_ORIGIN),
		):
			with self.subTest(host=host, origin=origin):
				self.assert_failure(
					self.call(
						"issue_browser_ticket",
						service=False,
						base_url=internal_origin,
						headers={
							"Host": host,
							"Origin": origin,
							"X-Forwarded-Proto": "https",
							"X-Forwarded-Host": erp_host,
						},
						**payload,
					),
					401,
					"UNAUTHENTICATED",
				)

	def test_loopback_http_chat_origin_cannot_override_an_https_request_origin(self):
		self.config["browser_chat_origin"] = "http://chat.localhost"
		self.assert_failure(
			self.call(
				"issue_browser_ticket",
				service=False,
				base_url="https://chat.localhost",
				headers={"Origin": self.config["browser_chat_origin"]},
				state=self.state,
				challenge=self.challenge,
			),
			401,
			"UNAUTHENTICATED",
		)

	def test_browser_ticket_expiry_logout_and_missing_configuration(self):
		ticket = self.issue()
		with patch("kanzlei_erp.ai_connector.auth.time.time", return_value=9_999_999_999):
			self.assert_failure(self.exchange(ticket), 401, "UNAUTHENTICATED")
		self.config["browser_chat_origin"] = "https://*.example.invalid"
		self.assert_failure(self.exchange(ticket), 503, "CONNECTOR_UNAVAILABLE")
		self.config["browser_chat_origin"] = CHAT_ORIGIN
		frappe.db.delete("Sessions", {"sid": self.sid})
		self.assert_failure(self.exchange(ticket), 401, "UNAUTHENTICATED")

	def test_browser_exchange_rechecks_configuration_after_atomic_consumption(self):
		ticket = self.issue()
		getdel = frappe.cache.getdel

		def consume_and_revoke(key):
			value = getdel(key)
			self.config["audience"] = "different-service"
			return value

		with patch.object(frappe.cache, "getdel", side_effect=consume_and_revoke):
			self.assert_failure(self.exchange(ticket), 401, "UNAUTHENTICATED")
		self.config["audience"] = "fixture"
		self.assert_failure(self.exchange(ticket), 401, "UNAUTHENTICATED")

	def test_browser_origin_requires_a_canonical_http_origin(self):
		for invalid in (
			None,
			"*",
			"https://chat.example.invalid/",
			"https://chat.example.invalid:443",
			"https://chat.example.invalid?",
			"https://chat.example.invalid#",
			"https://chat.example.invalid%2f",
			"https://staff@chat.example.invalid",
			"https://chat.example.invalid:+9000",
			"http://chat.example.invalid",
			"https://chat.example.invalid\n",
			"https://0x7f000001",
			"https://[fe80::1%eth0]",
		):
			self.config["browser_chat_origin"] = invalid
			origin_check = getattr(auth, "browser_chat_origin", None)
			self.assertTrue(callable(origin_check), "Browser origin validation is missing")
			with self.assertRaises(ConnectorError):
				origin_check(self.config)
		for valid in (
			CHAT_ORIGIN,
			"http://chat.localhost:5173",
			"http://127.0.0.1:5173",
			"http://[::1]:5173",
		):
			self.config["browser_chat_origin"] = valid
			self.assertTrue(auth.browser_chat_origin(self.config) == valid)

	def test_guest_login_uses_the_fixed_popup_return_path(self):
		from kanzlei_erp.www import kanzlei_ai_connect

		state, challenge = self.state, self.challenge
		builder = EnvironBuilder(
			path="/kanzlei-ai-connect",
			base_url=ERP_ORIGIN,
			query_string={
				"state": state,
				"challenge": challenge,
			},
		)
		with (
			patch.object(frappe.local, "request", Request(builder.get_environ()), create=True),
			patch.object(frappe.local, "session", frappe._dict(user="Guest")),
			patch.object(frappe, "redirect") as redirect,
		):
			kanzlei_ai_connect.get_context(frappe._dict())
		destination = urlsplit(redirect.call_args.args[0])
		self.assertEqual(destination.path, "/login")
		return_path = urlsplit(parse_qs(destination.query)["redirect-to"][0])
		self.assertEqual(return_path.path, "/kanzlei-ai-connect")
		self.assertTrue(parse_qs(return_path.query) == {"state": [state], "challenge": [challenge]})

	def test_malformed_or_disabled_popup_get_does_not_redirect(self):
		from kanzlei_erp.www import kanzlei_ai_connect

		valid = {"state": self.state, "challenge": self.challenge}
		for args, origin, status in (
			({**valid, "redirect-to": "https://other.example.invalid"}, CHAT_ORIGIN, 400),
			({**valid, "state": [self.state, self.state]}, CHAT_ORIGIN, 400),
			(valid, None, 503),
		):
			self.config["browser_chat_origin"] = origin
			builder = EnvironBuilder(path="/kanzlei-ai-connect", base_url=ERP_ORIGIN, query_string=args)
			context = frappe._dict()
			with (
				patch.object(frappe.local, "request", Request(builder.get_environ()), create=True),
				patch.object(frappe.local, "session", frappe._dict(user="Guest")),
			):
				kanzlei_ai_connect.get_context(context)
			self.assertEqual(context.http_status_code, status)
			self.assertIsNone(context.handoff)
