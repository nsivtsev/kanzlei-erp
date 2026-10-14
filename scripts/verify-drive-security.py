#!/usr/bin/env python3
"""Disposable local/CI fixtures and real HTTP/Socket.IO acceptance. Never production."""

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import frappe
import frappe.share


def provision(site):
	from frappe.utils.password import update_password
	from kanzlei_erp.mandant_drive import create_folder
	from suite.drive.utils import create_drive_file, get_user_folder
	from suite.drive.utils.files import FileManager

	run = secrets.token_hex(4)
	state = {"site": site, "run": run, "users": {}, "homes": []}
	state["webdav_enabled"] = frappe.db.get_single_value("Drive Disk Settings", "webdav_enabled")
	state["webdav_methods"] = frappe.db.get_single_value("Drive Disk Settings", "webdav_allowed_methods")
	frappe.db.set_single_value("Drive Disk Settings", {"webdav_enabled": 1, "webdav_allowed_methods": ""})
	frappe.clear_document_cache("Drive Disk Settings", "Drive Disk Settings")
	customer = frappe.get_doc({"doctype": "Customer", "customer_name": "_Drive Security " + run,
		"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories"}).insert()
	state["customer"] = customer.name
	form_customer = frappe.get_doc({"doctype": "Customer", "customer_name": "_Drive Form " + run,
		"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories"}).insert()
	state["form_customer"] = form_customer.name
	with patch("frappe.core.doctype.user.user.throttle_user_creation"):
		for label in ("writer", "reader", "stranger", "revoked", "manager", "portal"):
			roles = [] if label == "portal" else ["System Manager" if label == "manager" else "Projects User"]
			user = frappe.get_doc({"doctype": "User", "email": f"drive-security-{run}-{label}@example.invalid",
				"first_name": "Drive Security " + label, "send_welcome_email": 0,
				"roles": [{"role": role} for role in roles]}).insert()
			password = secrets.token_urlsafe(32)
			update_password(user.name, password)
			state["users"][label] = {"user": user.name, "password": password}
			if label in ("writer", "reader", "revoked"):
				frappe.share.add("Customer", customer.name, user.name, read=1, write=int(label == "writer"))
			if label == "writer":
				frappe.share.add("Customer", form_customer.name, user.name, read=1, write=1)
			frappe.set_user(user.name)
			state["homes"].append(get_user_folder().name)
			frappe.db.set_value("Drive Settings", user.name, "webdav_enabled", 1)
			frappe.set_user("Administrator")
	root = create_folder(customer.name)["folder"]
	state["root"] = root
	manager = FileManager()
	file = create_drive_file("original.xml", root, "XML Data", lambda doc: "/" + str(manager.get_disk_path(doc)), "application/xml", 11)
	manager.get_local_path(file.file_url).write_bytes(b"<invoice />")
	state["original"] = {"name": file.name, "url": file.file_url}
	frappe.set_user(state["users"]["stranger"]["user"])
	alias = create_drive_file("old-alias.xml", state["homes"][2], "XML Data",
		lambda doc: "/" + str(manager.get_disk_path(doc)), "application/xml", 5)
	manager.get_local_path(alias.file_url).write_bytes(b"alias")
	state["alias"] = {"name": alias.name, "own_url": alias.file_url}
	alias.db_set("file_url", file.file_url)  # Only the fixture simulates a pre-fix row.
	from kanzlei_erp.tests.drive_samples import MIMES, samples
	from suite.writer.api.docs import create_document
	from suite.drive.api.files import create_link
	state["download_samples"] = [{"name": "byte-sample." + extension, "mime": MIMES[extension],
		"data": base64.b64encode(content).decode(), "sha256": hashlib.sha256(content).hexdigest()}
		for extension, content in samples().items()]
	frappe.set_user(state["users"]["writer"]["user"])
	state["hosted"] = create_document(title="Hosted acceptance", parent=state["homes"][0]).name
	create_link("Source acceptance", "https://example.invalid/source", state["homes"][0])
	frappe.set_user("Administrator")
	frappe.db.commit()
	return state


def validate_state(state):
	assert state["customer"] == "_Drive Security " + state["run"]
	assert state["form_customer"] == "_Drive Form " + state["run"]
	if state.get("tabs_customer"):
		assert state["tabs_customer"] == "_Drive Form Tabs " + state["run"]
	assert all(user["user"].startswith("drive-security-" + state["run"] + "-") for user in state["users"].values())


def revoke(state):
	validate_state(state)
	frappe.share.remove("Customer", state["customer"], state["users"]["revoked"]["user"])
	frappe.clear_cache(user=state["users"]["revoked"]["user"])
	frappe.db.commit()


def _cleanup(state):
	from kanzlei_erp.drive_storage import invalidate
	validate_state(state)
	frappe.db.rollback()
	frappe.set_user("Administrator")
	if frappe.db.exists("File", state["alias"]["name"]):
		frappe.db.set_value("File", state["alias"]["name"], "file_url", state["alias"]["own_url"])
	customers = [state["customer"], state["form_customer"]]
	if state.get("tabs_customer"):
		customers.append(state["tabs_customer"])
	roots = [frappe.db.get_value("Customer", customer, "kanzlei_drive_folder") for customer in customers]
	for customer in customers:
		frappe.db.set_value("File", {"kanzlei_drive_customer": customer}, "kanzlei_drive_customer", "")
		frappe.db.set_value("Customer", customer, "kanzlei_drive_folder", None)
	invalidate()
	for root in [*filter(None, roots), *state["homes"]]:
		pending, names = [root], []
		while pending:
			name = pending.pop()
			if name in names or not frappe.db.exists("File", name):
				continue
			names.append(name)
			pending.extend(frappe.get_all("File", filters={"folder": name}, pluck="name"))
		for name in reversed(names):
			frappe.db.set_value("File", name, "status", "Removed")
			invalidate()
			frappe.delete_doc("File", name, ignore_permissions=True, force=True)
	for customer in customers:
		frappe.delete_doc("Customer", customer, ignore_permissions=True, force=True)
	for user in state["users"].values():
		frappe.delete_doc("User", user["user"], ignore_permissions=True, force=True)
	frappe.db.set_single_value("Drive Disk Settings", {"webdav_enabled": state["webdav_enabled"],
		"webdav_allowed_methods": state["webdav_methods"] or ""})
	frappe.clear_document_cache("Drive Disk Settings", "Drive Disk Settings")
	frappe.db.commit()


def cleanup(state):
	# Workers can update a shared table while fixtures are being removed. A
	# retry starts a fresh transaction and only touches this run's test records.
	for attempt in range(3):
		try:
			return _cleanup(state)
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			if attempt == 2:
				raise
			time.sleep(0.2)


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", required=True, choices=["development.localhost", "ci.localhost"])
	parser.add_argument("--fixtures", action="store_true", required=True)
	parser.add_argument("--mode", choices=["verify", "setup", "revoke", "cleanup", "verify-form"], default="verify")
	parser.add_argument("--base", default="http://127.0.0.1:8000")
	parser.add_argument("--socket", default="http://127.0.0.1:9100")
	parser.add_argument("--state-file", type=Path)
	args = parser.parse_args()
	bench_dir = Path.cwd()
	os.chdir(bench_dir / "sites")
	frappe.init(site=args.site, sites_path=str(bench_dir / "sites"))
	frappe.connect()
	frappe.set_user("Administrator")
	try:
		assert frappe.conf.allow_tests, "Fixtures require an explicitly test-enabled site"
		if args.mode in ("revoke", "cleanup", "verify-form"):
			state = json.loads(args.state_file.read_text() if args.state_file else sys.stdin.read())
			assert state["site"] == args.site
			if args.mode == "verify-form":
				validate_state(state)
				assert frappe.db.get_value("Customer", state["form_customer"], "kanzlei_drive_folder") == state["form_root"]
				assert frappe.db.count("File", {"kanzlei_drive_customer": state["form_customer"], "is_folder": 1}) == 1
				assert frappe.db.get_value("Customer", state["form_customer"], "customer_name") == "Other tab " + state["run"]
				print("Concurrent folder and Customer save checks passed")
			else:
				(revoke if args.mode == "revoke" else cleanup)(state)
			return
		state = provision(args.site)
		state.update(base=args.base, socket=args.socket, python=sys.executable, script=str(Path(__file__).resolve()))
		if args.mode == "setup":
			assert args.state_file, "Setup requires a private temporary state file"
			fd = os.open(args.state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
			with os.fdopen(fd, "w") as file:
				json.dump(state, file)
			print("Temporary Drive acceptance fixtures created")
			return
		try:
			runner = Path(__file__).with_suffix(".cjs")
			subprocess.run(["node", str(runner)], input=json.dumps(state), text=True, check=True, cwd=bench_dir)
		finally:
			cleanup(state)
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
