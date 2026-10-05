from unittest.mock import patch

import frappe
from frappe.boot import get_bootinfo
from frappe.tests.utils import FrappeTestCase
from frappe.translate import get_translations_from_apps

from kanzlei_erp import navigation
from kanzlei_erp.navigation import communication_has_permission, configure_desk


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

	def test_sidebar_focuses_on_daily_work(self):
		items = get_bootinfo().workspace_sidebar_item.get("kanzlei", {}).get("items", [])
		links = [(item["label"], item["link_to"] or item["url"]) for item in items]
		self.assertEqual(
			links[:5],
			[
				("Mandanten", "Customer"),
				("FiBu-Perioden", "FiBu Package"),
				("Aufgaben", "Task"),
				("Kalender", "/desk/task/view/calendar/default"),
				("Zeiterfassung", "Timesheet"),
			],
		)
		if navigation._has_assigned_email_account(frappe.session.user):
			self.assertEqual(links[5], ("E-Mail", "/desk/communication/view/inbox"))
			links.pop(5)
		self.assertEqual(links[5], ("Einstellungen", "/desk/company"))
		self.assertEqual(len(links), 6)

	def test_email_inbox_is_hidden_without_a_user_email_assignment(self):
		boot = get_bootinfo()
		with patch.object(navigation, "_has_assigned_email_account", return_value=False):
			configure_desk(boot)
		labels = [item["label"] for item in boot.workspace_sidebar_item["kanzlei"]["items"]]
		self.assertNotIn("E-Mail", labels)

	def test_email_inbox_is_visible_to_users_with_a_user_email_assignment(self):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"kanzlei-mail-test-{frappe.generate_hash(length=8)}@example.invalid",
				"first_name": "Kanzlei Mail Test",
				"send_welcome_email": 0,
				"roles": [{"role": "Projects User"}, {"role": "Accounts User"}, {"role": "Inbox User"}],
			}
		).insert()
		email_account = frappe.get_doc(
			{
				"doctype": "Email Account",
				"email_id": f"shared-{frappe.generate_hash(length=8)}@example.invalid",
				"enable_outgoing": 1,
				"always_use_account_email_id_as_sender": 1,
			}
		).insert(ignore_permissions=True)
		user.append("user_emails", {"email_account": email_account.name})
		user.save(ignore_permissions=True)
		try:
			frappe.set_user(user.name)
			boot = get_bootinfo()
			configure_desk(boot)
			items = boot.workspace_sidebar_item["kanzlei"]["items"]
			mail_links = [item for item in items if item["label"] == "E-Mail"]
			self.assertEqual(len(mail_links), 1)
			self.assertEqual(mail_links[0]["url"], "/desk/communication/view/inbox")
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("Email Account", email_account.name, ignore_permissions=True, force=True)

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
		self.assertNotIn("Rechnungen", labels)
		self.assertNotIn("Wiederholungen", labels)

	def test_admin_settings_link_is_not_duplicated(self):
		boot = get_bootinfo()
		configure_desk(boot)
		labels = [item["label"] for item in boot.workspace_sidebar_item["kanzlei"]["items"]]
		del boot
		self.assertEqual(labels.count("Einstellungen"), 1)

	def test_admin_settings_includes_standard_email_configuration(self):
		boot = get_bootinfo()
		configure_desk(boot)
		labels = [item["label"] for item in boot.workspace_sidebar_item["einstellungen"]["items"]]
		self.assertIn("E-Mail-Konto", labels)
		self.assertIn("E-Mail-Domain", labels)
		self.assertIn("E-Mail-Queue", labels)
		links = {item["label"]: item["link_to"] for item in boot.workspace_sidebar_item["einstellungen"]["items"]}
		self.assertEqual(links["Wiederholungen"], "Work Schedule")
		self.assertEqual(links["Rechnungen"], "Sales Invoice")
		self.assertEqual(links["Zahlungen"], "Payment Entry")

	def test_linked_mandant_readers_can_open_mail_and_private_attachments(self):
		communication = frappe._dict(
			name="COMM-TEST",
			communication_medium="Email",
			email_account="Shared Mailbox",
			reference_doctype=None,
			reference_name=None,
		)
		with (
			patch("kanzlei_erp.navigation.frappe.get_roles", return_value=["Inbox User"]),
			patch("kanzlei_erp.navigation.frappe.db.exists", return_value=False),
			patch(
				"kanzlei_erp.navigation.frappe.get_all",
				return_value=["MANDANT-A", "MANDANT-B"],
			),
			patch(
				"kanzlei_erp.navigation.frappe.has_permission",
				side_effect=lambda doctype, ptype, doc, user: doc == "MANDANT-B",
			),
		):
			self.assertTrue(communication_has_permission(communication, "read", user="mail-reader@example.invalid"))

	def test_unknown_mail_is_not_readable_to_staff_without_the_mailbox(self):
		communication = frappe._dict(
			name="COMM-UNKNOWN",
			communication_medium="Email",
			email_account="Shared Mailbox",
			reference_doctype=None,
			reference_name=None,
		)
		with (
			patch("kanzlei_erp.navigation.frappe.get_roles", return_value=["Inbox User"]),
			patch("kanzlei_erp.navigation.frappe.db.exists", return_value=False),
			patch("kanzlei_erp.navigation.frappe.get_all", return_value=[]),
		):
			self.assertFalse(communication_has_permission(communication, "read", user="mail-reader@example.invalid"))

	def test_customer_is_presented_as_mandant(self):
		translations = get_translations_from_apps("de")
		self.assertEqual(translations.get("Customer"), "Mandant")
		self.assertEqual(translations.get("Customers"), "Mandanten")
		self.assertEqual(translations.get("Customer Name"), "Mandantenname")

	def test_work_schedule_fields_have_german_translations(self):
		terms = get_translations_from_apps("de")
		translations = {source: terms.get(source) for source in ("Work", "First Due Date", "Last Due Date")}
		del terms
		for source, expected in (
			("Work", "Tätigkeit"),
			("First Due Date", "Erste Fälligkeit"),
			("Last Due Date", "Letzte Fälligkeit"),
		):
			self.assertEqual(translations.get(source), expected)

	def test_english_does_not_contain_german_overrides(self):
		translations = get_translations_from_apps("en")
		self.assertNotEqual(translations.get("Customers"), "Mandanten")
		self.assertNotEqual(translations.get("Customer Name"), "Mandantenname")
		self.assertNotEqual(translations.get("Add {0}"), "{0} anlegen")

	def test_services_and_filter_helpers_have_german_translations(self):
		terms = get_translations_from_apps("de")
		translations = {
			source: terms.get(source)
			for source in ("Item", "Item Name", "Item Group", "Clear all filters", "Begin typing for results.")
		}
		del terms
		self.assertEqual(translations["Item"], "Leistung")
		self.assertEqual(translations["Item Name"], "Leistungsname")
		self.assertEqual(translations["Item Group"], "Leistungsgruppe")
		self.assertEqual(translations["Clear all filters"], "Alle Filter löschen")
		self.assertEqual(translations["Begin typing for results."], "Für Ergebnisse bitte Text eingeben.")

	def test_fibu_work_has_german_labels(self):
		terms = get_translations_from_apps("de")
		self.assertEqual(terms.get("FiBu Package"), "FiBu-Paket")
		self.assertEqual(terms.get("FiBu Supplement"), "FiBu-Ergänzung")
		self.assertEqual(terms.get("New Question"), "Neue Rückfrage")
		self.assertEqual(terms.get("Preparation Stage"), "Bearbeitungsphase")
		self.assertEqual(terms.get("Waiting for Mandant"), "Warten auf Mandant")
		self.assertEqual(terms.get("Next Action Assignee"), "Zuständig für nächsten Schritt")

	def test_mandant_connections_focus_on_work(self):
		data = frappe.get_meta("Customer").get_dashboard_data()
		self.assertEqual(
			[item for group in data.transactions for item in group["items"]],
			["FiBu Package", "Task", "Work Schedule", "Timesheet"],
		)
		self.assertEqual(data.non_standard_fieldnames["Task"], "kanzlei_customer")
