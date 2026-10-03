import frappe
from frappe.boot import get_bootinfo
from frappe.tests.utils import FrappeTestCase
from frappe.translate import get_translations_from_apps

from kanzlei_erp.navigation import configure_desk


class TestKanzleiNavigation(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_admin_sees_only_kanzlei_and_settings(self):
		boot = get_bootinfo()
		sidebars = set(boot.workspace_sidebar_item)
		icons = {icon.label for icon in boot.desktop_icons}
		apps = [app["app_name"] for app in boot.app_data]
		pages = boot.workspaces["pages"]
		del boot
		self.assertEqual(sidebars, {"kanzlei", "einstellungen"})
		self.assertEqual(icons, {"Kanzlei", "Einstellungen"})
		self.assertEqual(apps, ["kanzlei_erp"])
		self.assertEqual(pages, [])

	def test_sidebar_links_to_work_time_and_billing(self):
		items = get_bootinfo().workspace_sidebar_item.get("kanzlei", {}).get("items", [])
		self.assertEqual(
			[(item["label"], item["link_to"] or item["url"]) for item in items],
			[
				("Mandanten", "Customer"),
				("Aufgaben", "Task"),
				("Kalender", "/desk/task/view/calendar/default"),
				("Wiederholungen", "Work Schedule"),
				("Zeiterfassung", "Timesheet"),
				("Rechnungen", "Sales Invoice"),
				("Einstellungen", "/desk/company"),
			],
		)

	def test_staff_has_kanzlei_without_administration(self):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"kanzlei-test-{frappe.generate_hash(length=8)}@example.invalid",
				"first_name": "Kanzlei Test",
				"send_welcome_email": 0,
				"roles": [{"role": "Projects User"}, {"role": "Accounts User"}],
			}
		).insert()
		frappe.set_user(user.name)
		boot = get_bootinfo()
		sidebars = set(boot.workspace_sidebar_item)
		icons = {icon.label for icon in boot.desktop_icons}
		labels = [item["label"] for item in boot.workspace_sidebar_item.get("kanzlei", {}).get("items", [])]
		boot.workspace_sidebar_item["kanzlei"]["items"].append(
			{"label": "Einstellungen", "url": "/desk/company", "link_to": None}
		)
		configure_desk(boot)
		persisted_labels = [item["label"] for item in boot.workspace_sidebar_item["kanzlei"]["items"]]
		del boot
		self.assertEqual(sidebars, {"kanzlei"})
		self.assertEqual(icons, {"Kanzlei"})
		self.assertNotIn("Einstellungen", labels)
		self.assertNotIn("Einstellungen", persisted_labels)

	def test_admin_settings_link_is_not_duplicated(self):
		boot = get_bootinfo()
		configure_desk(boot)
		labels = [item["label"] for item in boot.workspace_sidebar_item["kanzlei"]["items"]]
		del boot
		self.assertEqual(labels.count("Einstellungen"), 1)

	def test_customer_is_presented_as_mandant(self):
		for language in ("en", "de"):
			translations = get_translations_from_apps(language)
			self.assertEqual(translations.get("Customer"), "Mandant")
			self.assertEqual(translations.get("Customers"), "Mandanten")
			self.assertEqual(translations.get("Customer Name"), "Mandantenname")

	def test_mandant_connections_use_work_and_billing_links(self):
		data = frappe.get_meta("Customer").get_dashboard_data()
		self.assertEqual(
			[item for group in data.transactions for item in group["items"]],
			["Task", "Work Schedule", "Timesheet", "Sales Invoice", "Payment Entry"],
		)
		self.assertEqual(data.non_standard_fieldnames["Task"], "kanzlei_customer")
