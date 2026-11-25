"""Structured ERP data for the assistant: fixed views, current permissions, consent."""

from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.ai_connector import api, auth, policy, work_context

CLIENT = {"kind": "client", "client_id": "A"}
GENERAL = {"kind": "general"}


class Row(dict):
	__getattr__ = dict.get


class TestAIWorkContext(TestCase):
	def setUp(self):
		self.config = {}
		self.denied = set()
		self.lists = []
		self.rows = {
			"Task": [
				Row(
					name="T-2",
					subject="Belege\nanfordern",
					status="Open",
					exp_end_date="2026-12-03",
					kanzlei_customer="A",
					kanzlei_work_kind="Question",
				),
				Row(
					name="T-1",
					subject="USt-VA",
					status="Overdue",
					exp_end_date="2026-11-20",
					kanzlei_customer="A",
					kanzlei_work_kind="Task",
				),
				Row(
					name="T-3",
					subject="Fremd",
					status="Open",
					exp_end_date="2026-11-21",
					kanzlei_customer="B",
					kanzlei_work_kind="Task",
				),
			],
			"FiBu Package": [
				Row(
					name="P-1",
					customer="A",
					display_title="FiBu 11/2026",
					preparation_stage="Collection",
					waiting_for_mandant=1,
					next_action="Kontoauszüge",
					document_due_date="2026-12-01",
					internal_due_date="2027-03-01",
					external_due_date=None,
				),
			],
			"Customer": [Row(name="A", customer_name="Mandant A"), Row(name="B", customer_name="Mandant B")],
		}
		request = Request(EnvironBuilder(method="POST").get_environ())
		for target, name, options in (
			(frappe.local, "request", {"create": True, "new": request}),
			(auth, "config", {"side_effect": lambda: self.config}),
			(auth, "principal", {"side_effect": self._delegated}),
			(policy, "scope", {"side_effect": self._scope}),
			(policy, "check_scope", {"return_value": {"version": 1, "allowed": True}}),
			(frappe, "get_list", {"side_effect": self._list}),
			(frappe, "has_permission", {"side_effect": self._permission}),
			(work_context, "nowdate", {"return_value": "2026-11-25"}),
		):
			patcher = patch.object(target, name, **options)
			patcher.start()
			self.addCleanup(patcher.stop)

	@contextmanager
	def _delegated(self, access_token):
		self.assertEqual(access_token, "current-grant")
		yield {"subject": "staff"}

	def _scope(self, value):
		if value not in (CLIENT, GENERAL):
			raise work_context.policy.ConnectorError("FORBIDDEN", 403)
		return value

	def _permission(self, doctype, ptype, doc=None):
		self.assertEqual(ptype, "read")
		return (doctype, doc) not in self.denied

	def _list(self, doctype, filters=None, fields=None, order_by=None, limit=None):
		self.lists.append((doctype, filters))
		rows = self.rows[doctype]
		key = {"Task": "kanzlei_customer", "FiBu Package": "customer", "Customer": "name"}[doctype]
		wanted = filters.get(key)
		if isinstance(wanted, str):
			rows = [row for row in rows if row[key] == wanted]
		elif isinstance(wanted, list) and wanted[0] == "in":
			rows = [row for row in rows if row[key] in wanted[1]]
		if doctype == "Task" and "exp_end_date" in filters:
			rows = [row for row in rows if row.exp_end_date <= filters["exp_end_date"][1]]
		return rows[:limit]

	def call(self, query, scope=CLIENT, external=False):
		return api.read_work_context(
			access_token="current-grant", scope=scope, query=query, external=external
		)

	def test_open_tasks_are_sorted_by_due_date_and_cleaned(self):
		result = self.call({"kind": "tasks"})
		self.assertEqual(result["today"], "2026-11-25")
		self.assertFalse(result["truncated"])
		self.assertEqual(
			[(item["id"], item["type"], item["title"], item["client_label"]) for item in result["items"]],
			[("T-1", "task", "USt-VA", "Mandant A"), ("T-2", "question", "Belege anfordern", "Mandant A")],
		)
		self.assertEqual(self.lists[0][1]["kanzlei_customer"], "A")

	def test_record_and_mandant_permissions_are_rechecked(self):
		self.denied.add(("Task", "T-1"))
		self.assertEqual([item["id"] for item in self.call({"kind": "tasks"})["items"]], ["T-2"])
		self.denied.add(("Customer", "B"))
		general = self.call({"kind": "tasks"}, scope=GENERAL)
		self.assertEqual([item["id"] for item in general["items"]], ["T-2"])

	def test_deadlines_combine_tasks_and_fibu_due_dates_within_the_horizon(self):
		items = self.call({"kind": "deadlines", "days": 10})["items"]
		self.assertEqual(
			[(item["id"], item["due_date"], item["due_kind"]) for item in items],
			[("T-1", "2026-11-20", "task"), ("P-1", "2026-12-01", "document"), ("T-2", "2026-12-03", "task")],
		)
		self.assertEqual(items[1]["note"], "Wartet auf Mandant: Kontoauszüge")

	def test_client_overview_needs_one_permitted_mandant(self):
		(item,) = self.call({"kind": "client"})["items"]
		self.assertEqual((item["type"], item["title"]), ("client", "Mandant A"))
		self.assertIn("Offene Aufgaben: 2", item["note"])
		self.assertEqual(self.call({"kind": "client"}, scope=GENERAL).status_code, 400)
		self.denied.add(("Customer", "B"))
		self.assertEqual(self.call({"kind": "client", "client_id": "B"}, scope=GENERAL).status_code, 403)
		self.assertEqual(self.call({"kind": "tasks", "client_id": "B"}).status_code, 403)

	def test_external_models_need_the_switch_and_per_mandant_consent(self):
		self.assertEqual(self.call({"kind": "tasks"}, external=True).status_code, 403)
		self.config["assistant_generation"] = True
		self.assertEqual(self.call({"kind": "tasks"}, external=True)["items"], [])
		self.config["external_processing"] = {"A": {"generate": True}, "B": {"generate": "true"}}
		general = self.call({"kind": "tasks"}, scope=GENERAL, external=True)
		self.assertEqual({item["client_id"] for item in general["items"]}, {"A"})

	def test_rejects_free_form_queries(self):
		for query in (
			None,
			{"kind": "sql"},
			{"kind": "tasks", "filters": {"status": "Completed"}},
			{"kind": "tasks", "days": 0},
			{"kind": "tasks", "days": 91},
			{"kind": "tasks", "days": True},
		):
			with self.subTest(query=query):
				self.assertEqual(self.call(query).status_code, 400)
		self.assertEqual(self.call({"kind": "tasks"}, external="yes").status_code, 400)
