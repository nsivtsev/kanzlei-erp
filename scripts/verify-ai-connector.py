"""Local HTTP acceptance using only temporary synthetic ERP records.

Run with services/kanzlei_ai/.venv/bin/python scripts/verify-ai-connector.py.
The existing ERP must be running. No model calls, production sites or real mail.
"""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services/kanzlei_ai"
SITE_CONFIG = ROOT / "development/frappe-bench/sites/development.localhost/site_config.json"
DOCKER = [
	"docker",
	"compose",
	"--env-file",
	str(ROOT / ".env"),
	"-f",
	str(ROOT / ".devcontainer/docker-compose.yml"),
]


def erp(code):
	bootstrap = "import frappe, frappe.share\nfrappe.init('development.localhost', sites_path='.')\nfrappe.connect()\nfrappe.set_user('Administrator')\n"
	command = [
		*DOCKER,
		"exec",
		"-T",
		"frappe",
		"bash",
		"-lc",
		"cd /workspace/development/frappe-bench/sites && /workspace/development/frappe-bench/env/bin/python -",
	]
	result = subprocess.run(
		command, input=bootstrap + code, text=True, capture_output=True, cwd=ROOT, timeout=60
	)
	if result.returncode:
		error_log = Path(tempfile.gettempdir()) / "kanzlei-ai-connector-fixture-error.log"
		error_log.write_text(result.stderr)
		error_log.chmod(0o600)
		raise RuntimeError("ERP fixture operation failed; private details in " + str(error_log))
	return result.stdout


def http(url, payload=None, headers=None):
	request = urllib.request.Request(
		url,
		data=json.dumps(payload).encode() if payload is not None else None,
		headers={"Content-Type": "application/json", **(headers or {})},
	)
	try:
		with urllib.request.urlopen(request, timeout=15) as response:
			return response.status, response.read()
	except urllib.error.HTTPError as error:
		content = error.read()
		error_log = Path(tempfile.gettempdir()) / "kanzlei-ai-connector-http-error.log"
		error_log.write_bytes(content)
		error_log.chmod(0o600)
		return error.code, content


def main():
	if not SITE_CONFIG.exists():
		raise RuntimeError("This check requires the local development.localhost site")
	original_config = json.loads(SITE_CONFIG.read_text())
	old_connector = original_config.get("kanzlei_ai_connector")
	fixture = None
	server = None
	try:
		output = erp("""
from kanzlei_erp.tests.test_ai_connector import TestAIConnector
import base64, json, secrets
case = TestAIConnector()
case.setUp()
csrf = secrets.token_urlsafe(32)
frappe.db.sql('UPDATE `tabSessions` SET sessiondata=%s WHERE sid=%s',
    (json.dumps({'session_expiry':'01:00:00', 'csrf_token':csrf, 'last_updated':frappe.utils.now()}), case.sid))
frappe.db.commit()
print('FIXTURE=' + json.dumps({'reader':case.reader.name, 'client':case.customer.name,
    'other':case.other.name, 'root':case.root.name, 'files':list(case.file_names),
    'sid':case.sid, 'csrf':csrf, 'key':case.key, 'config':case.config,
    'content':base64.b64encode(case.original).decode()}))
""")
		fixture = json.loads(next(line[8:] for line in output.splitlines() if line.startswith("FIXTURE=")))
		settings = json.loads(SITE_CONFIG.read_text())
		settings["kanzlei_ai_connector"] = {**fixture["config"], "allowed_users": [fixture["reader"]]}
		SITE_CONFIG.write_text(json.dumps(settings, indent=2) + "\n")
		environment = {
			**os.environ,
			"AI_ERP_BASE_URL": "http://127.0.0.1:8100",
			"AI_ERP_SITE_NAME": "development.localhost",
			"AI_ERP_SERVICE_KEY": fixture["key"],
			"AI_ERP_ALLOW_INSECURE_HTTP": "true",
		}
		with tempfile.TemporaryFile() as log:
			server = subprocess.Popen(
				[
					str(SERVICE / ".venv/bin/python"),
					"-m",
					"uvicorn",
					"kanzlei_ai.app:create_app",
					"--factory",
					"--host",
					"127.0.0.1",
					"--port",
					"8181",
				],
				cwd=SERVICE,
				env=environment,
				stdout=log,
				stderr=log,
			)
			deadline = time.monotonic() + 15
			while time.monotonic() < deadline:
				try:
					if http("http://127.0.0.1:8181/health/live")[0] == 200:
						break
				except OSError:
					pass
				if server.poll() is not None:
					raise RuntimeError("Temporary AI API failed to start")
				time.sleep(0.1)
			else:
				raise RuntimeError("Temporary AI API startup timed out")
			erp_headers = {
				"X-Frappe-Site-Name": "development.localhost",
				"Cookie": "sid=" + fixture["sid"],
				"X-Frappe-CSRF-Token": fixture["csrf"],
			}
			status, body = http(
				"http://127.0.0.1:8100/api/method/kanzlei_erp.ai_connector.api.issue_ticket", {}, erp_headers
			)
			assert status == 200, "ERP handoff failed"
			ticket = json.loads(body)["message"]["ticket"]
			status, body = http("http://127.0.0.1:8181/v1/auth/exchange", {"ticket": ticket})
			assert status == 200, "AI exchange failed"
			grant = json.loads(body)
			assert grant["identity"]["subject"] == fixture["reader"]
			headers = {"Authorization": "Bearer " + grant["access_token"]}
			assert http("http://127.0.0.1:8181/v1/auth/exchange", {"ticket": ticket})[0] == 401
			status, body = http("http://127.0.0.1:8181/v1/connector/clients", {}, headers)
			if status != 200:
				_erp_status, erp_body = http(
					"http://127.0.0.1:8100/api/method/kanzlei_erp.ai_connector.api.list_clients",
					{"version": 1, "access_token": grant["access_token"]},
					{
						"X-Frappe-Site-Name": "development.localhost",
						"X-Kanzlei-AI-Service-Key": fixture["key"],
					},
				)
				private_log = Path(tempfile.gettempdir()) / "kanzlei-ai-connector-wire-error.log"
				private_log.write_bytes(erp_body)
				private_log.chmod(0o600)
			assert status == 200 and [c["id"] for c in json.loads(body)["items"]] == [fixture["client"]]
			status, body = http(
				"http://127.0.0.1:8181/v1/connector/documents", {"client_id": fixture["client"]}, headers
			)
			assert status == 200, "Document listing failed"
			manifest = json.loads(body)["items"][0]
			source = {
				"source_id": manifest["source_id"],
				"revision": manifest["revision"],
				"client_id": fixture["client"],
			}
			payload = {"scope": {"kind": "client", "client_id": fixture["client"]}, "source": source}
			status, authorization = http(
				"http://127.0.0.1:8181/v1/connector/authorize",
				{"scope": payload["scope"], "sources": [source]},
				headers,
			)
			assert status == 200 and json.loads(authorization)["decisions"][0]["allowed"] is True
			status, body = http("http://127.0.0.1:8181/v1/connector/read", payload, headers)
			assert status == 200 and body == base64.b64decode(fixture["content"]), "Original bytes differ"
			assert (
				hashlib.sha256(body).digest() == hashlib.sha256(base64.b64decode(fixture["content"])).digest()
			)
			assert (
				http(
					"http://127.0.0.1:8181/v1/connector/documents", {"client_id": fixture["other"]}, headers
				)[0]
				== 403
			)
			erp(
				"frappe.share.remove('Customer', "
				+ repr(fixture["client"])
				+ ", "
				+ repr(fixture["reader"])
				+ ")\nfrappe.db.commit()\n"
			)
			assert http("http://127.0.0.1:8181/v1/connector/read", payload, headers)[0] == 403
			erp(
				"frappe.db.delete('Sessions', {'sid': "
				+ repr(fixture["sid"])
				+ "})\nfrappe.cache.hdel('session', "
				+ repr(fixture["sid"])
				+ ")\nfrappe.db.commit()\n"
			)
			assert http("http://127.0.0.1:8181/v1/auth/me", headers=headers)[0] == 401
			print(
				"HTTP acceptance passed: handoff, replay rejection, client isolation, original bytes, permission revocation and ERP logout"
			)
	finally:
		if server is not None:
			server.terminate()
			try:
				server.wait(timeout=10)
			except subprocess.TimeoutExpired:
				server.kill()
				server.wait(timeout=5)
		if fixture is not None:
			current = json.loads(SITE_CONFIG.read_text())
			if old_connector is None:
				current.pop("kanzlei_ai_connector", None)
			else:
				current["kanzlei_ai_connector"] = old_connector
			SITE_CONFIG.write_text(json.dumps(current, indent=2) + "\n")
			erp(
				"""
from pathlib import Path
import json
fixture = json.loads("""
				+ repr(json.dumps(fixture))
				+ """)
for name in fixture['files']:
    if frappe.db.exists('File', name):
        file = frappe.get_doc('File', name)
        Path(file.get_full_path()).unlink(missing_ok=True)
frappe.db.delete('File', {'name':['in', [fixture['root'], *fixture['files']]]})
frappe.db.delete('Sessions', {'sid':fixture['sid']})
frappe.cache.hdel('session', fixture['sid'])
for name in [fixture['client'],fixture['other']]:
    frappe.delete_doc('Customer', name, ignore_permissions=True, force=True)
frappe.delete_doc('User', fixture['reader'], ignore_permissions=True, force=True)
frappe.db.commit()
"""
			)
			print("Synthetic ERP records removed and connector configuration restored")


if __name__ == "__main__":
	main()
