"""Synthetic fixtures only; historical states deliberately use test-side SQL."""

from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


def acceptance_test_site(site, config):
	"""Only explicitly marked local test sites; never a production hostname."""
	flags = (config.get("allow_tests"), config.get("posteingang_acceptance_site"))
	return bool(
		all(type(flag) in (bool, int) and flag == 1 for flag in flags)
		and isinstance(site, str)
		and (site == "ci.localhost" or (site.startswith("posteingang-ap10") and site.endswith(".localhost")))
	)


class PosteingangTestCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		throttle = patch("frappe.core.doctype.user.user.throttle_user_creation")
		throttle.start()
		self.addCleanup(throttle.stop)
		self.paths = []
		for doctype, field, name, extra in (
			("Customer Group", "customer_group_name", "All Customer Groups", {"is_group": 1}),
			(
				"Customer Group",
				"customer_group_name",
				"Commercial",
				{"parent_customer_group": "All Customer Groups"},
			),
			("Territory", "territory_name", "All Territories", {"is_group": 1}),
		):
			if not frappe.db.exists(doctype, name):
				frappe.get_doc({"doctype": doctype, field: name, **extra}).insert()
		self.customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "AP02 " + frappe.generate_hash(length=8),
				"customer_type": "Company",
				"customer_group": "Commercial",
				"territory": "All Territories",
			}
		).insert()
		self.operator = self.user("operator", ["Projects User", "Posteingang Operator"])
		self.reader = self.user("reader", ["Projects User"])
		self.stranger = self.user("stranger", ["Projects User"])
		self.manager = self.user("manager", ["System Manager", "Posteingang Operator"])
		self.website = self.user("website", [], "Website User")
		frappe.share.add("Customer", self.customer.name, self.reader.name, read=1)
		frappe.share.add("Customer", self.customer.name, self.manager.name, read=1)
		from kanzlei_erp.posteingang_setup import DEFAULTS

		frappe.db.set_single_value("Posteingang Settings", {**DEFAULTS, "enabled": 1})

	def user(self, label, roles, user_type="System User"):
		return frappe.get_doc(
			{
				"doctype": "User",
				"email": f"ap02-{label}-{frappe.generate_hash(length=8)}@example.invalid",
				"first_name": label,
				"send_welcome_email": 0,
				"user_type": user_type,
				"roles": [{"role": r} for r in roles],
			}
		).insert()

	def letter(self):
		from kanzlei_erp.posteingang_model import create_received_letter

		frappe.set_user(self.operator.name)
		content = (
			Path(frappe.get_app_path("kanzlei_erp")) / "tests/fixtures/posteingang/pdfs/AP01-01.pdf"
		).read_bytes()
		# Unique bytes avoid testing public intake dedup (AP-03).
		content += ("\n% " + frappe.generate_hash(length=16)).encode()
		doc = create_received_letter(
			actor=self.operator.name,
			original={"content": content, "file_name": "synthetic-" + frappe.generate_hash(length=10) + ".pdf", "page_count": 1},
			receipt={"received_date_known": False, "received_date": None},
			operation={
				"key": frappe.generate_hash(length=64),
				"fingerprint": frappe.generate_hash(length=64),
			},
		)
		file = frappe.get_doc("File", doc.original_file)
		from kanzlei_erp.object_storage import stored

		if not stored(file):
			self.paths.append(Path(file.get_full_path()))
		return doc

	def confirm_fixture(self, doc, quarantine=False):
		frappe.set_user("Administrator")
		frappe.db.set_value(
			"Incoming Letter",
			doc.name,
			{
				"status": "Cancelled" if quarantine else "Completed",
				"confirmed_customer": self.customer.name,
				"selected_customer": self.customer.name,
				"confirmed_by": self.operator.name,
				"confirmed_at": frappe.utils.now_datetime(),
				"correction_quarantined": int(quarantine),
			},
			update_modified=False,
		)
		return frappe.get_doc("Incoming Letter", doc.name)

	def tearDown(self):
		frappe.set_user("Administrator")
		for path in self.paths:
			path.unlink(missing_ok=True)
		super().tearDown()
