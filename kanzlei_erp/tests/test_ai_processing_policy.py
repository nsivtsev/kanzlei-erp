"""External processing tests at the real ERP RPC and policy boundary."""

from contextlib import contextmanager, nullcontext
from unittest import TestCase
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.ai_connector import api, auth, inventory, policy

CLIENT = {"kind": "client", "client_id": "A"}
GENERAL = {"kind": "general"}
SOURCE_A = {"client_id": "A", "source_id": "file:fixture-a", "revision": "a" * 64}
SOURCE_B = {"client_id": "B", "source_id": "file:fixture-b", "revision": "b" * 64}


class TestAIProcessingPolicy(TestCase):
	def setUp(self):
		self.config = {}
		self.events = []
		self.scope_allowed = True
		self.permission_version = 1
		self.decisions = None
		request = Request(EnvironBuilder(method="POST").get_environ())
		for target, name, options in (
			(frappe.local, "request", {"create": True, "new": request}),
			(auth, "config", {"side_effect": lambda: self.config}),
			(auth, "principal", {"side_effect": self._delegated}),
			(inventory, "settings", {"return_value": ("worker", {"A"}, "binding")}),
			(inventory, "principal", {"side_effect": self._background}),
			(inventory, "current_permission_context", {"side_effect": self._current}),
			(inventory, "refresh_principal_permissions", {"side_effect": self.events.append}),
			(policy, "check_scope", {"side_effect": self._scope}),
			(policy, "_mail_ids", {"return_value": set()}),
			(policy, "authorize_sources", {"side_effect": self._authorize}),
		):
			patcher = patch.object(target, name, **options)
			patcher.start()
			self.addCleanup(patcher.stop)

	@contextmanager
	def _current(self):
		self.events.append("current")
		yield

	@contextmanager
	def _delegated(self, access_token):
		self.events.append(("grant", access_token))
		yield {"subject": "staff"}

	def _background(self, user):
		self.events.append(("ingestion", user))
		return nullcontext()

	def _scope(self, scope):
		self.events.append(("scope", scope))
		return {"version": 1, "allowed": self.scope_allowed}

	def _authorize(self, scope, sources, *, expected_mail_ids):
		self.assertEqual(expected_mail_ids, set())
		self.events.append(("refs", scope, sources))
		decisions = self.decisions
		if decisions is None:
			decisions = [{**source, "allowed": True} for source in sources]
		return {"version": self.permission_version, "decisions": decisions}

	def call(self, operation, *, scope=CLIENT, sources=None, delegated=True, **kwargs):
		payload = {"access_token": "current-grant"} if delegated else {}
		if scope is not None:
			payload["scope"] = scope
		return api.check_processing_policy(operation=operation, sources=sources, **payload, **kwargs)

	def test_assistant_question_requires_the_kanzlei_switch_and_a_delegated_identity(self):
		self.assertEqual(self.call("assist", scope=None, sources=[]).status_code, 403)
		self.config["assistant_generation"] = "true"
		self.assertEqual(self.call("assist", scope=None, sources=[]).status_code, 403)
		self.config["assistant_generation"] = True
		self.assertEqual(self.call("assist", scope=None, sources=[]), {"version": 1, "allowed": True})
		self.assertEqual(self.events[-3:], ["current", ("grant", "current-grant"), "staff"])
		self.assertNotIn(("scope", None), self.events)

	def test_assistant_question_rejects_scope_sources_and_background_identity(self):
		self.config["assistant_generation"] = True
		for kwargs in (
			{"scope": CLIENT, "sources": []},
			{"scope": None, "sources": [SOURCE_A]},
			{"scope": None, "sources": [], "delegated": False},
		):
			with self.subTest(**{key: str(value) for key, value in kwargs.items()}):
				self.assertEqual(self.call("assist", **kwargs).status_code, 400)

	def test_missing_setting_and_non_boolean_grants_are_denied(self):
		for value in ({}, {"A": {"embed": 1}}, {"A": {"embed": "true"}}, {"A": {"ocr": True}}):
			with self.subTest(value=value):
				self.config["external_processing"] = value
				self.assertEqual(self.call("embed", sources=[]).status_code, 403)

	def test_question_embedding_checks_current_client_scope_under_the_delegated_identity(self):
		self.config["external_processing"] = {"A": {"embed": True}}
		self.assertEqual(self.call("embed", sources=[]), {"version": 1, "allowed": True})
		self.assertEqual(self.events, ["current", ("grant", "current-grant"), "staff", ("scope", CLIENT)])
		self.scope_allowed = False
		self.assertEqual(self.call("embed", sources=[]).status_code, 403)

	def test_only_delegated_client_embedding_accepts_an_empty_source_list(self):
		self.config["external_processing"] = {"A": {"embed": True, "ocr": True, "generate": True}}
		for operation, scope, delegated in (
			("embed", GENERAL, True),
			("embed", CLIENT, False),
			("generate", CLIENT, True),
			("ocr", CLIENT, False),
		):
			with self.subTest(operation=operation, scope=scope, delegated=delegated):
				self.assertEqual(
					self.call(operation, scope=scope, sources=[], delegated=delegated).status_code, 403
				)
		self.assertEqual(self.call("embed").status_code, 400)

	def test_every_mandant_in_general_source_batch_must_allow_the_operation(self):
		self.config["external_processing"] = {"A": {"generate": True}, "B": {"embed": True}}
		self.assertEqual(self.call("generate", scope=GENERAL, sources=[SOURCE_A, SOURCE_B]).status_code, 403)
		self.config["external_processing"]["B"]["generate"] = True
		self.assertEqual(
			self.call("generate", scope=GENERAL, sources=[SOURCE_A, SOURCE_B]),
			{"version": 1, "allowed": True},
		)
		self.assertIn(("refs", GENERAL, [SOURCE_A, SOURCE_B]), self.events)

	def test_partial_denied_or_unexpected_source_permissions_deny_the_entire_batch(self):
		self.config["external_processing"] = {
			"A": {"generate": True},
			"B": {"generate": True},
		}
		a, b = {**SOURCE_A, "allowed": True}, {**SOURCE_B, "allowed": True}
		for decisions in ([a], [a, {**b, "allowed": False}], [b, a], [a, {**b, "allowed": 1}], [a, b, a]):
			with self.subTest(decisions=decisions):
				self.decisions = decisions
				self.assertEqual(
					self.call("generate", scope=GENERAL, sources=[SOURCE_A, SOURCE_B]).status_code, 403
				)

	def test_background_ingestion_checks_fixed_identity_allowlist_and_current_projection(self):
		self.config["external_processing"] = {"A": {"ocr": True}, "B": {"ocr": True}}
		with patch.object(inventory, "project", return_value={"revision": SOURCE_A["revision"]}) as project:
			self.assertEqual(
				self.call("ocr", sources=[SOURCE_A], delegated=False), {"version": 1, "allowed": True}
			)
			self.assertEqual(self.events[:3], ["current", "worker", ("ingestion", "worker")])
			project.assert_called_once_with("fixture-a", "A", {"A"})
			self.assertEqual(
				self.call(
					"ocr", scope={"kind": "client", "client_id": "B"}, sources=[SOURCE_B], delegated=False
				).status_code,
				403,
			)
			project.return_value = {"revision": "c" * 64}
			self.assertEqual(self.call("ocr", sources=[SOURCE_A], delegated=False).status_code, 403)

	def test_rejects_source_outside_client_scope_even_with_mandant_grant(self):
		self.config["external_processing"] = {"B": {"generate": True}}
		self.assertEqual(self.call("generate", sources=[SOURCE_B]).status_code, 403)

	def test_unexpected_permission_version_denies_the_batch(self):
		self.config["external_processing"] = {"A": {"embed": True}}
		self.permission_version = True
		self.assertEqual(self.call("embed", sources=[SOURCE_A]).status_code, 403)

	def test_rejects_unknown_operation_and_identity_override(self):
		self.assertEqual(self.call("other", sources=[]).status_code, 400)
		self.assertEqual(self.call("embed", sources=[], user="Administrator").status_code, 400)
