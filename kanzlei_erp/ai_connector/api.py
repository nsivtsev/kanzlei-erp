"""Connector RPCs and session-authenticated originals with content-free HTTP errors."""

import hashlib
import inspect
import json
from functools import partial, wraps

import frappe
from werkzeug.wrappers import Response

from kanzlei_erp.ai_connector import auth, mail, policy
from kanzlei_erp.ai_connector.errors import ConnectorError, fail

HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def endpoint(function=None, *, method="POST"):
	if function is None:
		return partial(endpoint, method=method)
	allowed = set(inspect.signature(function).parameters) - {"extra"}

	@wraps(function)
	def guarded(*args, **kwargs):
		try:
			request = getattr(frappe.local, "request", None)
			if request and request.method != method:
				fail("METHOD_NOT_ALLOWED", 405)
			# Frappe's RPC dispatcher adds its resolved command to form_dict.
			# Accept only this exact endpoint, retaining strict application fields.
			command = kwargs.pop("cmd", None)
			if command is not None and command != function.__module__ + "." + function.__name__:
				fail()
			if (
				set(kwargs) - allowed
				or type(kwargs.get("version", 1)) is not int
				or kwargs.get("version", 1) != 1
			):
				fail()
			if hasattr(frappe.local, "response_headers"):
				frappe.local.response_headers.update(HEADERS)
			return function(*args, **kwargs)
		except Exception as error:
			code, status = "CONNECTOR_UNAVAILABLE", 503
			if isinstance(error, ConnectorError):
				code, status = error.code, error.status
			elif isinstance(error, (frappe.PermissionError, frappe.DoesNotExistError, FileNotFoundError)):
				code, status = "FORBIDDEN", 403
			elif isinstance(error, frappe.ValidationError):
				code, status = "INVALID_REQUEST", 400
			else:
				# Operators need an error category, never input or a private traceback.
				frappe.logger("ai_connector").error("Connector failure category: %s", type(error).__name__)
			return Response(
				json.dumps({"code": code}), status=status, mimetype="application/json", headers=HEADERS
			)

	return guarded


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def issue_ticket(version=1, **extra):
	return auth.issue_ticket()


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def exchange_ticket(ticket=None, version=1, **extra):
	return auth.exchange_ticket(ticket)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def issue_browser_ticket(state=None, challenge=None, version=1, **extra):
	return auth.issue_browser_ticket(state, challenge)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def exchange_browser_ticket(ticket=None, state=None, verifier=None, chat_origin=None, version=1, **extra):
	return auth.exchange_browser_ticket(ticket, state, verifier, chat_origin)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def identity(access_token=None, version=1, **extra):
	return auth.identity_record(auth.resolve(access_token))


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def revoke(access_token=None, version=1, **extra):
	return auth.revoke(access_token)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def list_clients(access_token=None, cursor=None, limit=50, version=1, **extra):
	with auth.principal(access_token):
		return policy.clients(limit, cursor)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def check_scope(access_token=None, scope=None, version=1, **extra):
	with auth.principal(access_token):
		return policy.check_scope(scope)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def list_documents(access_token=None, client_id=None, cursor=None, limit=50, query=None, version=1, **extra):
	with auth.principal(access_token):
		return policy.documents(client_id, limit, cursor, query=query)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def authorize_sources(access_token=None, scope=None, sources=None, version=1, **extra):
	if mail.enabled():
		from kanzlei_erp.ai_connector import inventory

		with inventory.current_permission_context(), auth.principal(access_token) as record:
			inventory.refresh_principal_permissions(record["subject"])
			return policy.authorize_sources(scope, sources)
	with auth.principal(access_token):
		return policy.authorize_sources(scope, sources)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def check_processing_policy(operation=None, scope=None, sources=None, access_token=None, version=1, **extra):
	from kanzlei_erp.ai_connector import external_processing

	return external_processing.check(operation, scope, sources, access_token)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def resolve_source_locator(access_token=None, scope=None, source=None, version=1, **extra):
	from kanzlei_erp.ai_connector import inventory

	with inventory.current_permission_context(), auth.principal(access_token) as record:
		inventory.refresh_principal_permissions(record["subject"])
		return policy.resolve_source_locator(scope, source)


@frappe.whitelist(methods=["GET"])
@endpoint(method="GET")
def download_mail_attachment(customer=None, file_id=None, revision=None, **extra):
	from kanzlei_erp.ai_connector import inventory
	from kanzlei_erp.ai_connector.errors import text

	source = {"source_id": "file:" + text(file_id), "revision": revision, "client_id": text(customer)}
	user, sid = frappe.session.user, frappe.session.sid

	def refresh():
		auth._live_session(user, sid)
		inventory.refresh_principal_permissions(user)

	with inventory.current_permission_context():
		refresh()
		content = mail.read_current(source, before_final_projection=refresh)
	return Response(
		content,
		mimetype="application/pdf",
		headers={**HEADERS, "Content-Disposition": 'inline; filename="attachment.pdf"'},
	)


def _source_mimetype(source):
	return mail.MIME_TYPE if source["source_id"].startswith("email:") else "application/octet-stream"


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def read_source(access_token=None, scope=None, source=None, version=1, **extra):
	with auth.principal(access_token):
		content = policy.read_source(scope, source, authenticate_current=lambda: auth.resolve(access_token))
	return Response(
		content,
		mimetype=_source_mimetype(source),
		headers={
			**HEADERS,
			"X-AI-Revision": source["revision"],
			"X-AI-Content-SHA256": hashlib.sha256(content).hexdigest(),
		},
	)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint
def background_read_source(source=None, version=1, **extra):
	"""Read one background-ingestion source under its fixed ERP identity."""
	from kanzlei_erp.ai_connector import inventory

	# Do not borrow the RPC connection: its snapshot may predate a permission
	# revocation. The isolated context is read-only and restores this request's
	# DB object and permission caches unchanged when the response is built.
	with inventory.current_permission_context():
		user, clients, _ = inventory.settings()
		inventory.refresh_principal_permissions(user)
		with inventory.principal(user):
			content = inventory.read_current(
				source, clients, before_final_projection=lambda: inventory.refresh_principal_permissions(user)
			)
			value = policy.reference(source)
	return Response(
		content,
		mimetype=_source_mimetype(value),
		headers={
			**HEADERS,
			"X-AI-Revision": value["revision"],
			"X-AI-Content-SHA256": hashlib.sha256(content).hexdigest(),
		},
	)


@frappe.whitelist(allow_guest=True, methods=['GET', 'POST'])
@endpoint
def inventory(cursor=None, limit=50, version=1, **extra):
	from kanzlei_erp.ai_connector import inventory as feed
	return feed.inventory(cursor, limit)


@frappe.whitelist(allow_guest=True, methods=['GET', 'POST'])
@endpoint
def changes(cursor=None, limit=50, version=1, **extra):
	from kanzlei_erp.ai_connector import inventory as feed
	return feed.changes(cursor, limit)
