"""Bounded, permission-checked structured ERP data for the AI assistant.

The assistant may ask for a few fixed views (open tasks, upcoming deadlines,
a Mandant overview). There is no free-form filter and no SQL. Every row is
read through Frappe's permission-aware list API and then rechecked against
the record and its Mandant. Rows for a Mandant without external processing
consent are left out when the result is sent to an external model.
"""

from datetime import date

import frappe
from frappe.utils import add_days, getdate, nowdate

from kanzlei_erp.ai_connector import auth, policy
from kanzlei_erp.ai_connector.errors import fail, text

KINDS = ("tasks", "deadlines", "client")
MAX_ITEMS = 25
SCAN_LIMIT = 200
DEFAULT_DAYS = 30
MAX_DAYS = 90
CLOSED_TASK_STATUSES = ["Completed", "Cancelled", "Template"]
TITLE_LIMIT = 300
NOTE_LIMIT = 500


def _clean(value, maximum):
	if value is None:
		return None
	value = " ".join("".join(" " if ord(char) < 32 else char for char in str(value)).split())
	return value[:maximum] or None


def _query(value):
	if not isinstance(value, dict) or set(value) - {"kind", "client_id", "days"}:
		fail()
	if value.get("kind") not in KINDS:
		fail()
	days = value.get("days", DEFAULT_DAYS)
	if type(days) is not int or not 1 <= days <= MAX_DAYS:
		fail()
	client = value.get("client_id")
	if client is not None:
		text(client)
	return value["kind"], client, days


def _clients(checked_scope, requested):
	"""The one Mandant the request is limited to, or None for all permitted ones."""
	if checked_scope["kind"] == "client":
		if requested is not None and requested != checked_scope["client_id"]:
			fail("FORBIDDEN", 403)
		return checked_scope["client_id"]
	if requested is not None and not frappe.has_permission("Customer", "read", doc=requested):
		fail("FORBIDDEN", 403)
	return requested


def _consent(external):
	if not external:
		return lambda _client: True
	settings = auth.config()
	if settings.get("assistant_generation") is not True:
		fail("FORBIDDEN", 403)
	grants = settings.get("external_processing")

	def allowed(client):
		operations = grants.get(client) if isinstance(grants, dict) else None
		return isinstance(operations, dict) and operations.get("generate") is True

	return allowed


class _Collector:
	def __init__(self, consent):
		self.consent = consent
		self.items = []
		self.truncated = False
		self._customers = {}

	def customer(self, name):
		if not name:
			return False
		if name not in self._customers:
			self._customers[name] = bool(
				frappe.has_permission("Customer", "read", doc=name) and self.consent(name)
			)
		return self._customers[name]

	def add(self, item):
		self.items.append(item)

	def limit(self):
		if len(self.items) > MAX_ITEMS:
			self.truncated = True
			del self.items[MAX_ITEMS:]

	def labels(self):
		names = sorted({item["client_id"] for item in self.items})
		rows = (
			frappe.get_list(
				"Customer",
				filters={"name": ["in", names]},
				fields=["name", "customer_name"],
				limit=len(names),
			)
			if names
			else []
		)
		label = {row.name: _clean(row.customer_name, 500) or row.name for row in rows}
		for item in self.items:
			item["client_label"] = label.get(item["client_id"], item["client_id"])


def _iso(value):
	return getdate(value).isoformat() if value else None


def _due_order(value):
	return getdate(value) if value else date.max


def _tasks(collector, client, horizon=None):
	filters = {"kanzlei_customer": client or ["is", "set"], "status": ["not in", CLOSED_TASK_STATUSES]}
	if horizon is not None:
		filters["exp_end_date"] = ["<=", horizon]
	rows = frappe.get_list(
		"Task",
		filters=filters,
		fields=["name", "subject", "status", "exp_end_date", "kanzlei_customer", "kanzlei_work_kind"],
		order_by="modified desc",
		limit=SCAN_LIMIT,
	)
	if len(rows) >= SCAN_LIMIT:
		collector.truncated = True
	for row in sorted(rows, key=lambda row: (_due_order(row.exp_end_date), row.name)):
		if not collector.customer(row.kanzlei_customer) or not frappe.has_permission(
			"Task", "read", doc=row.name
		):
			continue
		collector.add(
			{
				"type": "question" if row.kanzlei_work_kind == "Question" else "task",
				"id": row.name,
				"client_id": row.kanzlei_customer,
				"title": _clean(row.subject, TITLE_LIMIT) or row.name,
				"status": _clean(row.status, 100),
				"due_date": _iso(row.exp_end_date),
				"due_kind": "task" if row.exp_end_date else None,
				"note": None,
			}
		)


_PACKAGE_DUES = (
	("document_due_date", "document"),
	("internal_due_date", "internal"),
	("external_due_date", "external"),
)


def _packages(collector, client, horizon):
	rows = frappe.get_list(
		"FiBu Package",
		filters={"customer": client or ["is", "set"], "status": "Open"},
		fields=[
			"name",
			"customer",
			"display_title",
			"preparation_stage",
			"waiting_for_mandant",
			"next_action",
			*(field for field, _ in _PACKAGE_DUES),
		],
		order_by="modified desc",
		limit=SCAN_LIMIT,
	)
	if len(rows) >= SCAN_LIMIT:
		collector.truncated = True
	dues = []
	for row in rows:
		for field, kind in _PACKAGE_DUES:
			due = row.get(field)
			if due and getdate(due) <= getdate(horizon):
				dues.append((getdate(due), row.name, kind, row))
	for due, name, kind, row in sorted(dues, key=lambda entry: entry[:3]):
		if not collector.customer(row.customer) or not frappe.has_permission(
			"FiBu Package", "read", doc=name
		):
			continue
		note = _clean(row.next_action, NOTE_LIMIT)
		if row.waiting_for_mandant:
			note = "Wartet auf Mandant" + (": " + note if note else "")
		collector.add(
			{
				"type": "fibu_package",
				"id": name,
				"client_id": row.customer,
				"title": _clean(row.display_title, TITLE_LIMIT) or name,
				"status": _clean(row.preparation_stage, 100),
				"due_date": due.isoformat(),
				"due_kind": kind,
				"note": note,
			}
		)


def _client(collector, client):
	if client is None:
		# An overview needs one Mandant; general scope must name it.
		fail()
	if not collector.customer(client):
		fail("FORBIDDEN", 403)
	tasks = frappe.get_list(
		"Task",
		filters={"kanzlei_customer": client, "status": ["not in", CLOSED_TASK_STATUSES]},
		fields=["name"],
		limit=SCAN_LIMIT,
	)
	packages = frappe.get_list(
		"FiBu Package",
		filters={"customer": client, "status": "Open"},
		fields=["name", "waiting_for_mandant"],
		limit=SCAN_LIMIT,
	)
	readable_tasks = [row for row in tasks if frappe.has_permission("Task", "read", doc=row.name)]
	readable_packages = [
		row for row in packages if frappe.has_permission("FiBu Package", "read", doc=row.name)
	]
	waiting = sum(1 for row in readable_packages if row.waiting_for_mandant)
	collector.add(
		{
			"type": "client",
			"id": client,
			"client_id": client,
			"title": client,
			"status": None,
			"due_date": None,
			"due_kind": None,
			"note": (
				f"Offene Aufgaben: {len(readable_tasks)}; offene FiBu-Pakete: {len(readable_packages)}"
				f"; davon wartend auf Mandant: {waiting}"
			),
		}
	)


def read(scope, query, external):
	"""Run one fixed view for the current principal. Call inside ``auth.principal``."""
	if type(external) is not bool:
		fail()
	kind, requested, days = _query(query)
	checked_scope = policy.scope(scope)
	if checked_scope["kind"] == "client":
		policy.check_scope(checked_scope)
	client = _clients(checked_scope, requested)
	collector = _Collector(_consent(external))
	if kind == "tasks":
		_tasks(collector, client)
	elif kind == "deadlines":
		horizon = getdate(add_days(nowdate(), days)).isoformat()
		_tasks(collector, client, horizon)
		_packages(collector, client, horizon)
		collector.items.sort(key=lambda item: (item["due_date"] or "9999-12-31", item["type"], item["id"]))
	else:
		_client(collector, client)
	collector.limit()
	collector.labels()
	for item in collector.items:
		if item["type"] == "client":
			item["title"] = item["client_label"]
	return {
		"version": 1,
		"today": getdate(nowdate()).isoformat(),
		"items": collector.items,
		"truncated": collector.truncated,
	}
