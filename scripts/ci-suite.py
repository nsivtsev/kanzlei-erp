"""Suite upgrade/upload/restore acceptance, exclusively on disposable CI sites."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import frappe
import pymupdf


def pdf():
	doc = pymupdf.open()
	doc.new_page().insert_text((72, 72), "Kanzlei Suite verification")
	return doc.tobytes()


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("action", choices=("legacy-seed", "legacy-verify", "upload", "verify", "damage"))
	parser.add_argument("--site", default="ci.localhost")
	args = parser.parse_args()
	assert args.site in ("ci.localhost", "suite-upgrade.localhost"), "Disposable CI sites only"
	os.chdir("/home/frappe/frappe-bench/sites")
	frappe.init(site=args.site, sites_path="/home/frappe/frappe-bench/sites")
	frappe.connect()
	frappe.set_user("Administrator")
	state_path = Path(frappe.get_site_path("private", "suite-acceptance.json"))
	try:
		state = json.loads(state_path.read_text()) if state_path.exists() else {}
		if args.action == "legacy-seed":
			assert "suite" not in frappe.get_installed_apps()
			for doctype, field, name in (("Customer Group", "customer_group_name", "All Customer Groups"), ("Territory", "territory_name", "All Territories")):
				if not frappe.db.exists(doctype, name):
					frappe.get_doc({"doctype": doctype, field: name, "is_group": 1}).insert()
			group = frappe.get_doc({"doctype": "Customer Group", "customer_group_name": "Suite fixture group", "is_group": 0, "parent_customer_group": "All Customer Groups"}).insert()
			territory = frappe.get_doc({"doctype": "Territory", "territory_name": "Suite fixture territory", "is_group": 0, "parent_territory": "All Territories"}).insert()
			customer = frappe.get_doc({"doctype": "Customer", "customer_name": "Suite upgrade fixture", "customer_type": "Company", "customer_group": group.name, "territory": territory.name}).insert()
			content = pdf()
			file = frappe.get_doc({"doctype": "File", "file_name": "legacy-suite-check.pdf", "is_private": 1, "attached_to_doctype": "Customer", "attached_to_name": customer.name, "content": content}).insert()
			state.update(legacy=file.name, customer=customer.name, legacy_url=file.file_url, legacy_hash=hashlib.sha256(content).hexdigest())
		elif args.action == "legacy-verify":
			from kanzlei_erp.mandant_documents import _safe_local_path
			file = frappe.get_doc("File", state["legacy"])
			file.check_permission("read")
			assert file.attached_to_name == state["customer"]
			assert file.file_url == state["legacy_url"]
			assert hashlib.sha256(_safe_local_path(file).read_bytes()).hexdigest() == state["legacy_hash"]
		elif args.action == "upload":
			assert "suite" in frappe.get_installed_apps()
			from verify_drive import check
			state.update(check("http://frontend:8080", args.site, os.environ["ADMIN_PASSWORD"]))
		elif args.action in ("verify", "damage"):
			from suite.drive.utils.files import FileManager
			assert "suite" in frappe.get_installed_apps()
			assert "kanzlei_erp" in frappe.get_installed_apps()
			file = frappe.get_doc("File", state["file"])
			assert file.folder == state["folder"]
			path = FileManager().get_local_path(file.file_url)
			if args.action == "damage":
				path.unlink()
			else:
				assert hashlib.sha256(path.read_bytes()).hexdigest() == state["sha256"]
		frappe.db.commit()
		state_path.write_text(json.dumps(state))
		state_path.chmod(0o600)
		print("Suite acceptance passed: " + args.action + " on " + args.site)
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
