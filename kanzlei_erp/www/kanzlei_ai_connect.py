"""Complete the chat browser handoff using the current ERP session."""

from urllib.parse import urlencode

import frappe
from frappe.sessions import get_csrf_token

from kanzlei_erp.ai_connector import auth
from kanzlei_erp.ai_connector.api import HEADERS
from kanzlei_erp.ai_connector.errors import ConnectorError, fail

no_cache = 1
sitemap = 0
POPUP_PATH = "/kanzlei-ai-connect"


def get_context(context):
	frappe.local.response_headers.update(
		{
			**HEADERS,
			"Referrer-Policy": "no-referrer",
			"Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; "
			"frame-ancestors 'none'; base-uri 'none'",
		}
	)
	context.title = "ERP-Verbindung"
	try:
		origin = auth.browser_chat_origin(auth.config())
		request = frappe.local.request
		args = request.args
		if request.method != "GET":
			fail("METHOD_NOT_ALLOWED", 405)
		if set(args) != {"state", "challenge"} or any(len(args.getlist(key)) != 1 for key in args):
			fail()
		state, challenge = args["state"], args["challenge"]
		auth.browser_binding(state, challenge)
	except ConnectorError as error:
		context.http_status_code = error.status
		return
	if frappe.session.user == "Guest":
		return_path = POPUP_PATH + "?" + urlencode({"state": state, "challenge": challenge})
		frappe.redirect("/login?" + urlencode({"redirect-to": return_path}))
		return
	context.handoff = {
		"origin": origin,
		"state": state,
		"challenge": challenge,
		"csrf_token": get_csrf_token(),
	}
