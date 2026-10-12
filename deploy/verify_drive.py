#!/usr/bin/env python3
"""Explicit Administrator acceptance check; leaves an identifiable test PDF in Drive."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4


def check(base, site, password):
	import pymupdf
	import requests

	session = requests.Session()
	session.trust_env = False
	session.headers["Host"] = site
	response = session.post(base + "/api/method/login", data={"usr": "Administrator", "pwd": password}, timeout=30)
	response.raise_for_status()
	assert response.json()["message"] == "Logged In"
	assert response.json()["home_page"].lstrip("/") == "desk/kanzlei-overview", "Kanzlei login landing page changed"
	desk = session.get(base + "/desk/kanzlei-overview", timeout=30)
	desk.raise_for_status()
	assert "/drive" in desk.text, "Administrator Desk has no Drive link"
	response = session.get(base + "/drive", timeout=30)
	response.raise_for_status()
	token = re.search(r"window\.csrf_token\s*=\s*['\"]([^'\"]+)['\"]", response.text)
	assert token, "Drive HTML has no CSRF token"
	session.headers["X-Frappe-CSRF-Token"] = token.group(1)
	def call(method, values=None):
		response = session.post(base + "/api/method/suite.drive.api.files." + method, data=values or {}, timeout=60)
		response.raise_for_status()
		return response.json()["message"]
	home = call("get_root_folder")["home"]
	folder = call("create_folder", {"file_name": "Kanzlei Releasecheck " + uuid4().hex[:8], "parent": home})
	document = pymupdf.open()
	document.new_page().insert_text((72, 72), "Kanzlei Suite verification - test document")
	content = document.tobytes()
	response = session.post(base + "/api/method/suite.drive.api.files.upload_file", data={"parent": folder["name"], "total_file_size": len(content), "uuid": uuid4().hex}, files={"file": ("suite-check.pdf", content, "application/pdf")}, timeout=90)
	response.raise_for_status()
	file = response.json()["message"]
	response = session.get(base + "/api/method/suite.drive.api.files.get_file_content", params={"entity_name": file["name"], "trigger_download": 1}, timeout=30)
	response.raise_for_status()
	assert hashlib.sha256(response.content).digest() == hashlib.sha256(content).digest(), "Downloaded PDF checksum differs"
	session.post(base + "/api/method/logout", timeout=30).raise_for_status()
	return {"file": file["name"], "folder": folder["name"], "sha256": hashlib.sha256(content).hexdigest()}


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", type=Path)
	parser.add_argument("--container", action="store_true")
	args = parser.parse_args()
	if args.container:
		print(json.dumps(check("http://frontend:8080", os.environ["SITE_NAME"], os.environ["ADMIN_PASSWORD"])))
	else:
		import manage

		if args.root is None:
			parser.error("--root is required")
		with manage.deployment_lock(args.root):
			current = manage.read_json(args.root / "shared/current.json")
			manage.validate_release(current["image"], current["revision"], current["tag"])
			compose = manage.Compose(args.root, Path(current["release"]), current["image"], manage.Commands())
			result = compose.call("run", "--rm", "-T", "--no-deps", "-v", str(Path(__file__).resolve()) + ":/tmp/verify_drive.py:ro", "-e", "SITE_NAME", "-e", "ADMIN_PASSWORD", "backend", "env/bin/python", "/tmp/verify_drive.py", "--container")
			manage.atomic_json(args.root / "shared/drive-acceptance.json", json.loads(result))
			print("Administrator Drive folder/upload/download verification passed for " + current["tag"])


if __name__ == "__main__":
	main()
