"""One-time handoff, online ERP session checks and service-bound opaque grants."""

import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import contextmanager
from copy import deepcopy

import frappe
from frappe.config import get_site_config

from kanzlei_erp.ai_connector.errors import fail, text
from kanzlei_erp.posteingang_permissions import staff

TICKET_TTL = 60
GRANT_TTL = 300
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def config():
	# Request config is normally cached for 60 seconds. Integration revocation
	# and key rotation must not inherit that grace period.
	value = get_site_config(cached=False).get("kanzlei_ai_connector") or {}
	if not isinstance(value, dict) or value.get("enabled") is not True:
		fail("CONNECTOR_UNAVAILABLE", 503)
	if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("service_key_sha256", ""))):
		fail("CONNECTOR_UNAVAILABLE", 503)
	for name in ("audience", "connector_id"):
		text(value.get(name), 100)
	for name in ("general_roles", "allowed_users"):
		if value.get(name) is not None and (
			not isinstance(value[name], list)
			or len(value[name]) > 1000
			or not all(isinstance(item, str) and item for item in value[name])
		):
			fail("CONNECTOR_UNAVAILABLE", 503)
	return value


def service():
	settings = config()
	key = frappe.get_request_header("X-Kanzlei-AI-Service-Key") or ""
	if not 32 <= len(key) <= 256 or not hmac.compare_digest(
		hashlib.sha256(key.encode()).hexdigest(), settings["service_key_sha256"]
	):
		fail("UNAUTHENTICATED", 401)
	return settings


def _key(kind, token):
	if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
		fail("UNAUTHENTICATED", 401)
	return frappe.cache.make_key("kanzlei-ai:" + kind + ":" + hashlib.sha256(token.encode()).hexdigest())


def _live_session(subject, sid):
	if not staff(subject) or not sid or sid == "Guest":
		fail("UNAUTHENTICATED", 401)
	settings = config()
	if settings.get("allowed_users") is not None and subject not in settings["allowed_users"]:
		fail("UNAUTHENTICATED", 401)
	row = frappe.db.get_value(
		"Sessions", {"sid": sid, "user": subject}, ["lastupdate", "sessiondata"], as_dict=True, order_by="sid"
	)
	if not row:
		fail("UNAUTHENTICATED", 401)
	from frappe.sessions import get_expiry_in_seconds

	data = frappe.parse_json(row.sessiondata or "{}")
	if frappe.utils.time_diff_in_seconds(frappe.utils.now(), row.lastupdate) > get_expiry_in_seconds(
		data.get("session_expiry")
	):
		fail("UNAUTHENTICATED", 401)
	if data.get("session_end"):
		from datetime import UTC, datetime

		if datetime.now(UTC) >= datetime.fromisoformat(data["session_end"]):
			fail("UNAUTHENTICATED", 401)


def _validate(record, settings):
	if (
		not record
		or record.get("exp", 0) <= time.time()
		or record.get("aud") != settings["audience"]
		or record.get("key") != settings["service_key_sha256"]
	):
		fail("UNAUTHENTICATED", 401)
	_live_session(record["subject"], record["sid"])
	return record


def issue_ticket():
	settings = config()
	_live_session(frappe.session.user, frappe.session.sid)
	now = int(time.time())
	token = secrets.token_urlsafe(32)
	record = {
		"subject": frappe.session.user,
		"sid": frappe.session.sid,
		"aud": settings["audience"],
		"key": settings["service_key_sha256"],
		"exp": now + TICKET_TTL,
	}
	# Raw Redis commands deliberately fail closed; cache helpers can swallow outages.
	frappe.cache.set(_key("ticket", token), json.dumps(record), ex=TICKET_TTL)
	return {"version": 1, "ticket": token, "expires_at": record["exp"]}


def exchange_ticket(ticket):
	settings = service()
	raw = frappe.cache.getdel(_key("ticket", ticket))
	record = _validate(json.loads(raw) if raw else None, settings)
	token = secrets.token_urlsafe(32)
	record["exp"] = int(time.time()) + GRANT_TTL
	frappe.cache.set(_key("grant", token), json.dumps(record), ex=GRANT_TTL)
	return {"version": 1, "access_token": token, "token_type": "bearer", "identity": identity_record(record)}


def resolve(access_token):
	settings = service()
	raw = frappe.cache.get(_key("grant", access_token))
	return _validate(json.loads(raw) if raw else None, settings)


def general_access(subject):
	roles = config().get("general_roles", [])
	return isinstance(roles, list) and bool(set(roles) & set(frappe.get_roles(subject)))


def identity_record(record):
	return {
		"version": 1,
		"subject": record["subject"],
		"connector_id": config()["connector_id"],
		"expires_at": record["exp"],
		"general_access": general_access(record["subject"]),
	}


def revoke(access_token):
	service()
	frappe.cache.delete(_key("grant", access_token))
	return {"version": 1, "revoked": True}


@contextmanager
def principal(access_token):
	record = resolve(access_token)
	original_session = frappe.local.session
	previous_session = deepcopy(original_session)
	previous_form = frappe.local.form_dict
	frappe.set_user(record["subject"])
	try:
		yield record
	finally:
		frappe.set_user(previous_session.user)
		# Session.data can be referenced by Frappe's session_obj. Restore that
		# exact object in place, including SID/CSRF/expiry, not merely the proxy.
		original_session.clear()
		original_session.update(previous_session)
		frappe.local.session = original_session
		frappe.local.form_dict = previous_form
