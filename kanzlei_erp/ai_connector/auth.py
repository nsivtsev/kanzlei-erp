"""One-time handoff, online ERP session checks and service-bound opaque grants."""

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from contextlib import contextmanager
from copy import deepcopy
from urllib.parse import unquote, urlsplit

import frappe
from frappe.config import get_site_config

from kanzlei_erp.ai_connector.errors import fail, text
from kanzlei_erp.posteingang_permissions import staff

TICKET_TTL = 60
GRANT_TTL = 300
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


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


def browser_chat_origin(settings):
	value = settings.get("browser_chat_origin")
	if not isinstance(value, str) or not 1 <= len(value) <= 300:
		fail("CONNECTOR_UNAVAILABLE", 503)
	try:
		origin = urlsplit(value)
		host, port = origin.hostname, origin.port
	except ValueError:
		fail("CONNECTOR_UNAVAILABLE", 503)
	if origin.scheme not in {"http", "https"} or not host or "%" in host or port == 0:
		fail("CONNECTOR_UNAVAILABLE", 503)
	try:
		address = ipaddress.ip_address(host)
	except ValueError:
		labels = host.split(".")
		if any(not HOST_LABEL.fullmatch(label) for label in labels) or re.fullmatch(
			r"(?:[0-9]+|0x[0-9a-f]*)", labels[-1]
		):
			fail("CONNECTOR_UNAVAILABLE", 503)
		authority = host
		loopback = host == "localhost" or host.endswith(".localhost")
	else:
		authority = f"[{address.compressed}]" if address.version == 6 else str(address)
		loopback = address.is_loopback
	if port is not None and port != (443 if origin.scheme == "https" else 80):
		authority += f":{port}"
	if value != f"{origin.scheme}://{authority}" or (origin.scheme == "http" and not loopback):
		fail("CONNECTOR_UNAVAILABLE", 503)
	return value


def browser_binding(state, proof):
	if not all(isinstance(value, str) and TOKEN_PATTERN.fullmatch(value) for value in (state, proof)):
		fail()


def _key(kind, token):
	if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
		fail("UNAUTHENTICATED", 401)
	return frappe.cache.make_key("kanzlei-ai:" + kind + ":" + hashlib.sha256(token.encode()).hexdigest())


def _live_session(subject, sid, *, for_update=False):
	if not staff(subject) or not sid or sid == "Guest":
		fail("UNAUTHENTICATED", 401)
	settings = config()
	if settings.get("allowed_users") is not None and subject not in settings["allowed_users"]:
		fail("UNAUTHENTICATED", 401)
	row = frappe.db.get_value(
		"Sessions",
		{"sid": sid, "user": subject},
		["lastupdate", "sessiondata"],
		as_dict=True,
		order_by="sid",
		for_update=for_update,
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


def _validate(record, settings, *, lock_session=False):
	if (
		not record
		or record.get("exp", 0) <= time.time()
		or record.get("aud") != settings["audience"]
		or record.get("key") != settings["service_key_sha256"]
	):
		fail("UNAUTHENTICATED", 401)
	_live_session(record["subject"], record["sid"], for_update=lock_session)
	return record


def issue_ticket():
	return _issue_ticket("ticket", config())


def issue_browser_ticket(state, challenge):
	settings = config()
	origin = browser_chat_origin(settings)
	browser_binding(state, challenge)
	request = frappe.local.request
	csrf = frappe.session.get("data") or {}
	csrf = csrf.get("csrf_token")
	# The generic Frappe CSRF middleware can allow trusted referrers. This
	# browser-only endpoint also requires the actual session cookie and token.
	if (
		request.headers.get("Origin") != request.host_url.rstrip("/")
		or unquote(request.cookies.get("sid", "")) != frappe.session.sid
		or not csrf
		or not hmac.compare_digest(request.headers.get("X-Frappe-CSRF-Token", "").encode(), csrf.encode())
	):
		fail("UNAUTHENTICATED", 401)
	return _issue_ticket("browser-ticket", settings, state=state, challenge=challenge, browser_origin=origin)


def _issue_ticket(kind, settings, **binding):
	_live_session(frappe.session.user, frappe.session.sid)
	now = int(time.time())
	token = secrets.token_urlsafe(32)
	record = {
		"subject": frappe.session.user,
		"sid": frappe.session.sid,
		"aud": settings["audience"],
		"key": settings["service_key_sha256"],
		"exp": now + TICKET_TTL,
		**binding,
	}
	# Raw Redis commands deliberately fail closed; cache helpers can swallow outages.
	frappe.cache.set(_key(kind, token), json.dumps(record), ex=TICKET_TTL)
	return {"version": 1, "ticket": token, "expires_at": record["exp"]}


def exchange_ticket(ticket):
	settings = service()
	raw = frappe.cache.getdel(_key("ticket", ticket))
	record = _validate(json.loads(raw) if raw else None, settings)
	return _grant(record)


def exchange_browser_ticket(ticket, state, verifier, chat_origin):
	settings = service()
	browser_binding(state, verifier)
	challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode()[:-1]
	key = _key("browser-ticket", ticket)
	raw = frappe.cache.get(key)
	_bound_browser_ticket(raw, settings, state, challenge, chat_origin)
	consumed = frappe.cache.getdel(key)
	if consumed != raw:
		fail("UNAUTHENTICATED", 401)
	record = _bound_browser_ticket(consumed, service(), state, challenge, chat_origin, lock_session=True)
	del record["state"], record["challenge"]
	return _grant(record)


def _bound_browser_ticket(raw, settings, state, challenge, chat_origin, *, lock_session=False):
	origin = browser_chat_origin(settings)
	record = json.loads(raw) if raw else None
	if (
		chat_origin != origin
		or not record
		or record.get("browser_origin") != origin
		or not hmac.compare_digest(record["state"], state)
		or not hmac.compare_digest(record["challenge"], challenge)
	):
		fail("UNAUTHENTICATED", 401)
	return _validate(record, settings, lock_session=lock_session)


def _grant(record):
	token = secrets.token_urlsafe(32)
	record["exp"] = int(time.time()) + GRANT_TTL
	frappe.cache.set(_key("grant", token), json.dumps(record), ex=GRANT_TTL)
	return {"version": 1, "access_token": token, "token_type": "bearer", "identity": identity_record(record)}


def resolve(access_token):
	settings = service()
	raw = frappe.cache.get(_key("grant", access_token))
	record = _validate(json.loads(raw) if raw else None, settings)
	if "browser_origin" in record and record["browser_origin"] != browser_chat_origin(settings):
		fail("UNAUTHENTICATED", 401)
	return record


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
