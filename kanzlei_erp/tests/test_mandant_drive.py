import shutil
from unittest.mock import patch
from urllib.parse import quote

import frappe
from frappe.tests.utils import FrappeTestCase


class TestMandantDrive(FrappeTestCase):
	def setUp(self):
		from kanzlei_erp import mandant_drive

		self.api = mandant_drive
		# Test fixtures exceed the site's ordinary per-minute user creation limit.
		self.user_throttle = patch("frappe.core.doctype.user.user.throttle_user_creation")
		self.user_throttle.start()
		self.addCleanup(self.user_throttle.stop)
		self.paths = []
		self.cache_keys = []
		self.customer = frappe.get_doc({
			"doctype": "Customer", "customer_name": f"_Drive {frappe.generate_hash(8)}",
			"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories",
		}).insert()
		self.reader = self.make_user("reader")
		self.writer = self.make_user("writer")
		self.stranger = self.make_user("stranger")
		frappe.share.add("Customer", self.customer.name, self.reader.name, read=1)
		frappe.share.add("Customer", self.customer.name, self.writer.name, read=1, write=1)

	def tearDown(self):
		frappe.set_user("Administrator")
		for path in getattr(self, "paths", []):
			if path.is_dir():
				shutil.rmtree(path)
			elif path.is_file():
				path.unlink()
		for key in getattr(self, "cache_keys", []):
			frappe.cache().delete_value(key)
		super().tearDown()

	def make_user(self, label):
		return frappe.get_doc({
			"doctype": "User", "email": f"drive-{label}-{frappe.generate_hash(8)}@example.invalid",
			"first_name": label, "send_welcome_email": 0, "roles": [{"role": "Projects User"}],
		}).insert()

	def root(self):
		from suite.drive.utils.files import FileManager

		result = self.api.create_folder(self.customer.name)
		root = frappe.get_doc("File", result["folder"])
		manager = FileManager()
		if root.file_url and not manager.flat:
			path = manager.get_local_path(root.file_url)
			if path.exists():
				self.paths.append(path)
		return root

	def child(self, root, name="invoice.xml"):
		from suite.drive.utils import create_drive_file
		from suite.drive.utils.files import FileManager

		manager = FileManager()
		file = create_drive_file(name, root.name, "XML Data", lambda f: "/" + str(manager.get_disk_path(f)), "application/xml", 11)
		manager.get_local_path(file.file_url).write_bytes(b"<invoice />")
		return file

	def test_creation_is_private_and_idempotent(self):
		from suite.drive.api.permissions import user_has_permission

		root = self.root()
		second = self.api.create_folder(self.customer.name)
		self.assertEqual(second["folder"], root.name)
		self.assertEqual(root.kanzlei_drive_customer, self.customer.name)
		self.assertEqual(frappe.db.get_value("Customer", self.customer.name, "kanzlei_drive_folder"), root.name)
		self.assertTrue(root.is_private)
		self.assertEqual(root.owner, "Administrator")
		self.assertFalse(user_has_permission(root, "read", "Guest"))
		self.assertFalse(user_has_permission(root, "read", self.stranger.name))
		self.assertTrue(second["url"].startswith("/drive/d/"))

	def test_mandanten_list_follows_docshares_and_revocation(self):
		for user, can_create in ((self.reader, False), (self.writer, True)):
			frappe.set_user(user.name)
			result = self.api.list_mandanten(search=self.customer.name)
			self.assertEqual(len(result["rows"]), 1)
			row = result["rows"][0]
			self.assertEqual(row["customer"], self.customer.name)
			self.assertEqual(row["customer_name"], self.customer.customer_name)
			self.assertEqual(row["state"], "missing")
			self.assertEqual(row["can_create"], can_create)
			self.assertEqual(row["customer_url"], "/desk/customer/" + quote(self.customer.name, safe=""))
			self.assertFalse(result["has_next"])
		frappe.set_user(self.stranger.name)
		self.assertEqual(self.api.list_mandanten(search=self.customer.name)["rows"], [])
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user(self.reader.name)
		self.assertEqual(self.api.list_mandanten(search=self.customer.name)["rows"], [])

	def test_mandanten_list_rejects_guest_and_portal_and_handles_absent_suite(self):
		frappe.db.set_value("User", self.stranger.name, "user_type", "Website User")
		for user in ("Guest", self.stranger.name):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError):
				self.api.list_mandanten()
		frappe.set_user(self.reader.name)
		with patch("frappe.get_installed_apps", return_value=["frappe", "erpnext", "kanzlei_erp"]):
			self.assertEqual(self.api.list_mandanten(), {"rows": [], "has_next": False, "next_start": None})

	def test_mandanten_list_ready_and_corrupt_roots_are_isolated(self):
		root = self.root()
		frappe.set_user(self.reader.name)
		row = self.api.list_mandanten(search=self.customer.name)["rows"][0]
		self.assertEqual((row["state"], row["folder"], row["url"]), ("ready", root.name, "/drive/d/" + root.name))
		self.assertFalse(row["can_create"])
		frappe.set_user("Administrator")
		for field, value in (("status", "Trashed"), ("is_folder", 0), ("kanzlei_drive_customer", "wrong")):
			old = frappe.db.get_value("File", root.name, field)
			frappe.db.set_value("File", root.name, field, value)
			row = self.api.list_mandanten(search=self.customer.name)["rows"][0]
			self.assertEqual(row["state"], "unavailable")
			self.assertIsNone(row["folder"])
			self.assertIsNone(row["url"])
			self.assertFalse(row["can_create"])
			frappe.db.set_value("File", root.name, field, old)
		frappe.db.set_value("Customer", self.customer.name, "kanzlei_drive_folder", "missing-" + frappe.generate_hash(8))
		self.assertEqual(self.api.list_mandanten(search=self.customer.name)["rows"][0]["state"], "unavailable")

	def test_mandanten_pagination_refills_denied_documents_without_skipping(self):
		prefix = "_Navigation " + frappe.generate_hash(8)
		customers = []
		for index in range(5):
			customers.append(frappe.get_doc({"doctype": "Customer", "customer_name": prefix,
				"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories"}).insert())
			frappe.share.add("Customer", customers[-1].name, self.reader.name, read=1)
		customers.sort(key=lambda doc: doc.name)
		denied = {customers[0].name, customers[2].name}
		original = frappe.has_permission
		def permission(doctype, ptype="read", doc=None, **kwargs):
			if doctype == "Customer" and ptype == "read" and doc and (doc if isinstance(doc, str) else doc.name) in denied:
				return False
			return original(doctype, ptype, doc=doc, **kwargs)
		frappe.set_user(self.reader.name)
		with patch("frappe.has_permission", side_effect=permission):
			first = self.api.list_mandanten(search=prefix, start=-1, page_length=2)
			self.assertEqual([row["customer"] for row in first["rows"]], [customers[1].name, customers[3].name])
			self.assertTrue(first["has_next"])
			second = self.api.list_mandanten(search=prefix, start=first["next_start"], page_length=2)
			self.assertEqual([row["customer"] for row in second["rows"]], [customers[4].name])
			self.assertFalse(second["has_next"])
			self.assertIsNone(second["next_start"])
		self.assertEqual(len(self.api.list_mandanten(search=prefix, page_length=0)["rows"]), 1)
		self.assertEqual(len(self.api.list_mandanten(search=customers[4].name)["rows"]), 1)

	def test_mandant_entity_navigation_trims_ancestors_and_uses_current_customer(self):
		from suite.drive.api.permissions import get_entity_with_permissions
		root = self.root()
		child = self.child(root)
		frappe.db.set_value("Customer", self.customer.name, "customer_name", "Current customer title")
		for user in ("Administrator", self.reader.name):
			frappe.set_user(user)
			result = get_entity_with_permissions(child.name)
			self.assertEqual(result["mandant"], {"customer": self.customer.name,
				"customer_name": "Current customer title", "folder": root.name,
				"customer_url": "/desk/customer/" + quote(self.customer.name, safe="")})
			self.assertEqual([crumb["name"] for crumb in result["breadcrumbs"]], [root.name, child.name])
		frappe.set_user(self.stranger.name)
		self.assertIsNone(self.api.entity_navigation(child, self.stranger.name))
		with self.assertRaises(frappe.PermissionError):
			get_entity_with_permissions(child.name)
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		self.assertIsNone(self.api.entity_navigation(child, self.reader.name))

	def test_navigation_ordinary_files_preserve_suite_response(self):
		from suite.drive.utils import get_user_folder
		from suite.drive.api.permissions import get_entity_with_permissions
		ordinary = get_user_folder("Administrator")
		self.assertIsNone(self.api.entity_navigation(ordinary, "Administrator"))
		self.assertNotIn("mandant", get_entity_with_permissions(ordinary.name))

	def test_mandanten_system_manager_and_user_permissions(self):
		self.stranger.add_roles("System Manager")
		frappe.set_user(self.stranger.name)
		self.assertEqual(self.api.list_mandanten(search=self.customer.name)["rows"], [])
		frappe.set_user("Administrator")
		self.stranger.add_roles("Sales Manager")
		frappe.set_user(self.stranger.name)
		row = self.api.list_mandanten(search=self.customer.name)["rows"][0]
		self.assertEqual(row["customer"], self.customer.name)
		self.assertFalse(row["can_create"])
		frappe.set_user("Administrator")
		self.stranger.add_roles("Sales Master Manager")
		frappe.set_user(self.stranger.name)
		self.assertTrue(self.api.list_mandanten(search=self.customer.name)["rows"][0]["can_create"])
		frappe.set_user("Administrator")
		other = frappe.get_doc({"doctype": "Customer", "customer_name": "_Restricted " + frappe.generate_hash(8),
			"customer_type": "Company", "customer_group": "Commercial", "territory": "All Territories"}).insert()
		self.reader.add_roles("Sales User")
		frappe.get_doc({"doctype": "User Permission", "user": self.reader.name,
			"allow": "Customer", "for_value": self.customer.name, "apply_to_all_doctypes": 1}).insert()
		frappe.set_user(self.reader.name)
		self.assertEqual(self.api.list_mandanten(search=other.name)["rows"], [])
		self.assertEqual(self.api.list_mandanten(search=self.customer.name)["rows"][0]["customer"], self.customer.name)

	def test_navigation_rejects_forged_client_marker_and_broken_root(self):
		from suite.drive.api.permissions import get_entity_with_permissions
		from suite.drive.utils import get_user_folder
		root = self.root()
		ordinary = get_user_folder("Administrator")
		forged = frappe._dict(name=ordinary.name, kanzlei_drive_customer=self.customer.name)
		self.assertIsNone(self.api.entity_navigation(forged, "Administrator"))
		frappe.db.set_value("Customer", self.customer.name, "kanzlei_drive_folder", "missing-" + frappe.generate_hash(8))
		self.assertIsNone(self.api.entity_navigation(root, self.reader.name))
		frappe.db.set_value("Customer", self.customer.name, "kanzlei_drive_folder", root.name)
		with patch("suite.drive.api.permissions.get_valid_breadcrumbs", return_value=[{"name": "secret-ancestor"}]):
			self.assertEqual(get_entity_with_permissions(root.name)["breadcrumbs"], [])

	def test_native_documents_are_rejected_in_managed_folders(self):
		root = self.root()
		frappe.set_user(self.writer.name)
		for doctype in ("Writer Document", "Sheet", "Presentation"):
			with self.subTest(doctype=doctype), self.assertRaises(frappe.PermissionError):
				doc = frappe.get_doc({"doctype": "File", "file_name": "Untitled",
					"folder": root.name, "is_private": 1, "file_type": "Document",
					"content_doctype": doctype, "content_docname": "not-created"})
				doc.flags.file_created = True
				doc.insert(ignore_links=True)

	def test_form_snapshot_saves_business_changes_after_folder_synchronization(self):
		frappe.set_user(self.writer.name)
		snapshot = frappe.get_doc("Customer", self.customer.name).as_dict()
		root = self.root()
		self.assertEqual(frappe.db.get_value("Customer", self.customer.name, "modified"), snapshot.modified)
		snapshot.customer_name = "Edited Mandant"
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(snapshot).save()
		snapshot.kanzlei_drive_folder = self.api.get_folder(self.customer.name)["folder"]
		frappe.get_doc(snapshot).save()
		stored = frappe.get_doc("Customer", self.customer.name)
		self.assertEqual(stored.customer_name, "Edited Mandant")
		self.assertEqual(stored.kanzlei_drive_folder, root.name)

	def test_parallel_creator_retries_snapshot_conflict_before_creating_any_storage(self):
		root = self.root()
		doc = frappe.get_doc("Customer", self.customer.name)
		with patch.object(self.api, "_require_customer", side_effect=[frappe.QueryDeadlockError(), doc]) as require, \
			patch("frappe.db.rollback") as rollback, patch("suite.drive.utils.create_drive_file") as create:
			result = self.api.create_folder(self.customer.name)
			self.assertEqual(result["folder"], root.name)
			rollback.assert_called_once()
			self.assertEqual(require.call_count, 2)
			create.assert_not_called()

	def test_snapshot_retry_rechecks_permission_and_stops_before_storage(self):
		with patch.object(self.api, "_require_customer", side_effect=[frappe.QueryDeadlockError(), frappe.PermissionError()]), \
			patch("frappe.db.rollback") as rollback, patch("suite.drive.utils.create_drive_file") as create:
			with self.assertRaises(frappe.PermissionError):
				self.api.create_folder(self.customer.name)
			rollback.assert_called_once()
			create.assert_not_called()

	def test_synchronized_mapping_cannot_be_cleared_or_replaced(self):
		root = self.root()
		other = self.child(root)
		frappe.set_user(self.writer.name)
		for mapping in (None, other.name):
			with self.subTest(mapping=mapping):
				doc = frappe.get_doc("Customer", self.customer.name)
				doc.kanzlei_drive_folder = mapping
				with self.assertRaises(frappe.PermissionError):
					doc.save()
				self.assertEqual(frappe.db.get_value("Customer", self.customer.name, "kanzlei_drive_folder"), root.name)

	def test_folder_synchronization_does_not_bypass_business_timestamp_conflicts(self):
		root = self.root()
		frappe.set_user(self.writer.name)
		stale = frappe.get_doc("Customer", self.customer.name)
		current = frappe.get_doc("Customer", self.customer.name)
		current.customer_name = "Other tab saved"
		current.save()
		stale.kanzlei_drive_folder = self.api.get_folder(self.customer.name)["folder"]
		stale.customer_name = "Stale changes"
		with self.assertRaises(frappe.TimestampMismatchError):
			stale.save()
		stored = frappe.get_doc("Customer", self.customer.name)
		self.assertEqual(stored.customer_name, "Other tab saved")
		self.assertEqual(stored.kanzlei_drive_folder, root.name)

	def test_native_creators_reject_managed_folder_before_creating_content(self):
		root = self.root()
		for create in (self.api.create_document, self.api.create_sheet, self.api.create_presentation):
			with self.subTest(create=create.__name__), self.assertRaisesRegex(frappe.PermissionError, "Upload files"):
				create(parent=root.name)

	def test_http_links_follow_customer_access_and_cannot_alias_storage(self):
		from suite.drive.api.files import create_link
		from suite.drive.api.permissions import user_has_permission

		root = self.root()
		frappe.set_user(self.writer.name)
		link = create_link("Source", "https://example.invalid/source", root.name)
		self.assertTrue(user_has_permission(link, "read", self.reader.name))
		with self.assertRaises(frappe.PermissionError):
			create_link("Alias", "/private/files/other-customer.pdf", root.name)
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.writer.name)
		self.assertFalse(user_has_permission(link, "read", self.writer.name))

	def test_writer_can_create_reader_cannot(self):
		frappe.set_user(self.reader.name)
		self.assertTrue(self.api.get_folder(self.customer.name)["folder"] is None)
		self.assertFalse(self.api.get_folder(self.customer.name)["can_create"])
		with self.assertRaises(frappe.PermissionError):
			self.api.create_folder(self.customer.name)
		frappe.set_user(self.writer.name)
		self.assertTrue(self.api.get_folder(self.customer.name)["can_create"])
		self.root()

	def test_reader_can_open_but_other_customer_is_denied(self):
		root = self.root()
		frappe.set_user(self.reader.name)
		self.assertEqual(self.api.get_folder(self.customer.name)["folder"], root.name)
		frappe.set_user(self.stranger.name)
		with self.assertRaises(frappe.PermissionError):
			self.api.get_folder(self.customer.name)

	def test_descendants_inherit_current_customer_read_and_write(self):
		from suite.drive.api.files import create_folder
		from suite.drive.api.permissions import user_has_permission

		root = self.root()
		frappe.set_user(self.writer.name)
		subfolder = create_folder("Belege", root.name)
		file = self.child(subfolder)
		self.assertEqual(file.kanzlei_drive_customer, self.customer.name)
		self.assertTrue(user_has_permission(file, "upload", self.writer.name))
		self.assertTrue(user_has_permission(file, "read", self.reader.name))
		self.assertFalse(user_has_permission(file, "write", self.reader.name))
		self.assertFalse(user_has_permission(subfolder, "share", self.writer.name))
		self.assertTrue(frappe.has_permission("File", "read", file, user=self.reader.name))

	def test_revocation_denies_the_uploading_owner_and_suite_admin(self):
		from suite.drive.api.permissions import user_has_permission

		root = self.root()
		frappe.set_user(self.writer.name)
		file = self.child(root)
		self.assertEqual(file.owner, self.writer.name)
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.writer.name)
		user = frappe.get_doc("User", self.writer.name)
		user.append("roles", {"role": "Suite Admin"})
		user.save()
		frappe.clear_cache(user=self.writer.name)
		for ptype in ("read", "write", "upload", "share"):
			self.assertFalse(user_has_permission(file, ptype, self.writer.name))

	def test_drive_listing_and_direct_download_follow_revocation(self):
		from suite.drive.api.files import get_file_content
		from suite.drive.api.list import files

		root = self.root()
		file = self.child(root)
		frappe.set_user(self.reader.name)
		self.assertEqual([r["name"] for r in files(root.name)], [file.name])
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user(self.reader.name)
		with self.assertRaises(frappe.PermissionError):
			files(root.name)
		with self.assertRaises(frappe.PermissionError):
			get_file_content(file.name)

	def test_orm_list_does_not_expose_owned_files_after_revocation(self):
		root = self.root()
		frappe.set_user(self.writer.name)
		file = self.child(root)
		self.assertIn(file.name, frappe.get_list("File", pluck="name", limit_page_length=0))
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.writer.name)
		frappe.set_user(self.writer.name)
		self.assertNotIn(file.name, frappe.get_list("File", pluck="name", limit_page_length=0))

	def test_raw_mapping_and_marker_changes_are_rejected(self):
		root = self.root()
		file = self.child(root)
		customer = frappe.get_doc("Customer", self.customer.name)
		customer.kanzlei_drive_folder = None
		with self.assertRaises(frappe.PermissionError):
			customer.save()
		file.kanzlei_drive_customer = None
		with self.assertRaises(frappe.PermissionError):
			file.save()

	def test_root_cannot_be_moved_trashed_or_deleted(self):
		from suite.drive.utils import get_user_folder
		from kanzlei_erp.suite_setup import trash_or_restore

		root = self.root()
		with self.assertRaises(frappe.PermissionError):
			root.move(get_user_folder().name)
		with self.assertRaises(frappe.PermissionError):
			trash_or_restore([root.name])
		with self.assertRaises(frappe.PermissionError):
			root.permanent_delete()

	def test_move_out_is_rejected_before_storage_changes(self):
		from suite.drive.utils import get_user_folder
		from suite.drive.utils.files import FileManager

		root = self.root()
		file = self.child(root)
		path = FileManager().get_local_path(file.file_url)
		with self.assertRaises(frappe.PermissionError):
			file.move(get_user_folder().name)
		self.assertEqual(path.read_bytes(), b"<invoice />")
		self.assertEqual(frappe.db.get_value("File", file.name, "folder"), root.name)

	def test_path_and_webdav_batch_cannot_bypass_customer_permissions(self):
		from suite.drive.utils import generate_upward_path
		from suite.drive.webdav.perms import parent_access, resolve_children_access

		root = self.root()
		frappe.set_user(self.writer.name)
		file = self.child(root)
		path = generate_upward_path(root.name, self.reader.name)
		self.assertTrue(path[-1]["read"])
		self.assertTrue(resolve_children_access(path, [file], self.reader.name)[file.name]["read"])
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.writer.name)
		path = generate_upward_path(root.name, self.writer.name)
		self.assertFalse(parent_access(path, root, self.writer.name)["read"])
		self.assertFalse(resolve_children_access(path, [file], self.writer.name)[file.name]["read"])

	def test_ordinary_drive_files_keep_their_existing_permissions(self):
		from suite.drive.utils import get_user_folder
		from suite.drive.api.permissions import user_has_permission

		frappe.set_user(self.stranger.name)
		folder = frappe.get_doc("File", get_user_folder().name)
		self.assertTrue(user_has_permission(folder, "write"))
		self.assertIsNone(self.api.entity_access(folder, self.stranger.name))

	def test_native_trash_rejects_root_before_touching_storage(self):
		from suite.drive.api.files import toggle_entity_status
		from suite.drive.utils.files import FileManager

		root = self.root()
		path = FileManager().get_local_path(root.file_url)
		with self.assertRaises(frappe.PermissionError):
			toggle_entity_status(root, FileManager(), set())
		self.assertTrue(path.exists())

	def test_preview_token_cannot_bypass_revocation_of_issuer_access(self):
		from suite.drive.api.files import create_auth_token, get_file_content

		root = self.root()
		file = self.child(root)
		frappe.set_user(self.reader.name)
		token = create_auth_token(file.name)
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			get_file_content(file.name, token=token)

	def test_other_employee_can_edit_files_uploaded_by_colleague(self):
		root = self.root()
		file = self.child(root)
		frappe.set_user(self.writer.name)
		file = frappe.get_doc("File", file.name)
		file.rename("renamed.xml")
		self.assertEqual(frappe.db.get_value("File", file.name, "file_name"), "renamed.xml")

	def test_orm_list_of_read_only_user_contains_only_their_mandant(self):
		root = self.root()
		file = self.child(root)
		frappe.set_user(self.reader.name)
		self.assertIn(root.name, frappe.get_list("File", pluck="name", limit=0))
		self.assertIn(file.name, frappe.get_list("File", pluck="name", limit=0))
		frappe.set_user(self.stranger.name)
		self.assertNotIn(root.name, frappe.get_list("File", pluck="name", limit=0))
		self.assertNotIn(file.name, frappe.get_list("File", pluck="name", limit=0))

	def test_raw_file_edits_cannot_change_storage_key_or_root_structure(self):
		root = self.root()
		file = self.child(root)
		frappe.set_user(self.writer.name)
		for field, value in (("file_url", "/private/files/another-clients-invoice.xml"), ("is_folder", 1), ("file_type", "Link")):
			doc = frappe.get_doc("File", file.name)
			doc.set(field, value)
			with self.assertRaises(frappe.PermissionError):
				doc.save()
		for field, value in (("status", "Trashed"), ("is_folder", 0)):
			doc = frappe.get_doc("File", root.name)
			doc.set(field, value)
			with self.assertRaises(frappe.PermissionError):
				doc.save()

	def test_finished_archive_rechecks_each_member_after_revocation(self):
		from suite.drive.api.files import _download_cache_key, build_download_archive, download_status
		from suite.drive.utils.files import FileManager

		root = self.root()
		file = self.child(root)
		token = f"test-{frappe.generate_hash(length=12)}"
		key = _download_cache_key(token)
		self.cache_keys.append(key)
		build_download_archive(token, [root.name], "test.zip", self.reader.name)
		entry = frappe.cache().get_value(key)
		self.assertEqual(entry["status"], "ready")
		self.paths.append(FileManager().get_local_path(entry["key"]))
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.reader.name)
		frappe.set_user(self.reader.name)
		with self.assertRaises(frappe.PermissionError):
			download_status(token)
		self.assertIn(file.name, entry["files"])

	def test_ancestor_trash_rejects_managed_descendant_before_storage(self):
		from suite.drive.api.files import create_folder, toggle_entity_status
		from suite.drive.utils import get_user_folder
		from suite.drive.utils.files import FileManager

		ancestor = create_folder(f"_Drive ancestor {frappe.generate_hash(length=8)}", get_user_folder().name)
		self.paths.append(FileManager().get_local_path(ancestor.file_url))
		with patch("suite.drive.utils.get_user_folder", return_value=ancestor):
			root = self.root()
		path = FileManager().get_local_path(root.file_url)
		with self.assertRaises(frappe.PermissionError):
			toggle_entity_status(ancestor, FileManager(), set())
		self.assertTrue(path.exists())

	def test_action_policy_writer_reader_and_root_matrix(self):
		from suite.drive.api.files import create_folder
		root = self.root()
		folder = create_folder("_Policy folder", root.name)
		child = self.child(root)
		for user, writable in ((self.writer.name, True), (self.reader.name, False)):
			frappe.set_user(user)
			policies = self.api.entity_actions_batch([root.name, folder.name, child.name], user)
			for name, is_folder, is_root in ((root.name, True, True), (folder.name, True, False), (child.name, False, False)):
				policy = policies[name]
				self.assertEqual(policy["root"], root.name)
				self.assertEqual(policy["is_root"], is_root)
				self.assertEqual(policy["actions"], {
					"rename": writable and not is_root, "move": writable and not is_root,
					"trash": writable and not is_root, "restore": False, "delete": False,
					"share": False, "upload": writable and is_folder,
					"create_folder": writable and is_folder, "create_link": writable and is_folder,
					"create_native": False})

	def test_action_policy_ignores_forged_fields_and_fails_closed(self):
		from suite.drive.utils import get_user_folder
		root = self.root()
		ordinary = get_user_folder("Administrator")
		forged = frappe._dict(name=ordinary.name, kanzlei_drive_customer=self.customer.name)
		self.assertEqual(self.api.entity_actions_batch([forged], "Administrator"), {})
		frappe.db.set_value("Customer", self.customer.name, "kanzlei_drive_folder", "missing-root")
		policy = self.api.entity_actions_batch([root.name], self.writer.name)[root.name]
		self.assertIsNone(policy["root"])
		self.assertFalse(any(policy["actions"].values()))
		for user in ("Guest", self.stranger.name):
			self.assertEqual(self.api.entity_actions_batch([root.name], user), {})

	def test_action_policy_checks_customer_once_per_batch_and_handles_trash(self):
		root = self.root()
		children = [self.child(root, name=f"policy-{i}.xml") for i in range(4)]
		frappe.db.set_value("File", children[0].name, "status", "Trashed")
		with patch("kanzlei_erp.mandant_drive._customer_access", wraps=self.api._customer_access) as access:
			policies = self.api.entity_actions_batch([root.name] + [c.name for c in children], self.writer.name)
			self.assertEqual(access.call_count, 1)
		policy = policies[children[0].name]
		self.assertTrue(policy["actions"]["restore"])
		self.assertTrue(policy["actions"]["delete"])
		self.assertFalse(policy["actions"]["move"])
		self.assertFalse(policy["actions"]["rename"])

	def test_action_policy_is_returned_by_detail_lists_and_search(self):
		from suite.drive.api.permissions import get_entity_with_permissions
		from suite.drive.api.list import files, favourites, recents, trash
		from suite.drive.api.files import search, set_favourite, remove_or_restore, track_visit
		root = self.root()
		frappe.set_user(self.writer.name)
		child = self.child(root, "PolicyAcceptanceUnique.xml")
		frappe.set_user(self.writer.name)
		self.assertTrue(get_entity_with_permissions(root.name)["write"])
		self.assertFalse(get_entity_with_permissions(root.name)["mandant_policy"]["actions"]["rename"])
		for row in files(entity_name=root.name):
			self.assertEqual(row["mandant_policy"]["root"], root.name)
		original_sql = frappe.db.sql
		with patch("frappe.db.sql", side_effect=lambda query, *a, **kw: [frappe._dict(child.as_dict())] if "AGAINST" in str(query) else original_sql(query, *a, **kw)):
			found = [row for row in search("PolicyAcceptanceUnique") if row["name"] == child.name]
		self.assertEqual(len(found), 1)
		self.assertTrue(found[0]["mandant_policy"]["actions"]["move"])
		set_favourite([{"name": child.name, "is_favourite": True}])
		track_visit(entity_name=child.name)
		for listing in (favourites, recents):
			row = next(row for row in listing() if row["name"] == child.name)
			self.assertEqual(row["mandant_policy"]["root"], root.name)
		remove_or_restore([child.name])
		row = next(row for row in trash() if row["name"] == child.name)
		self.assertTrue(row["mandant_policy"]["actions"]["restore"])
		frappe.set_user("Administrator")
		frappe.share.remove("Customer", self.customer.name, self.writer.name)
		frappe.set_user(self.writer.name)
		self.assertEqual(self.api.entity_actions_batch([root.name, child.name], self.writer.name), {})
		with self.assertRaises(frappe.PermissionError):
			get_entity_with_permissions(root.name)

	def test_action_policy_admin_roles_do_not_bypass_customer_access(self):
		root = self.root()
		self.stranger.add_roles("Suite Admin", "System Manager")
		self.assertEqual(self.api.entity_actions_batch([root.name], self.stranger.name), {})
		frappe.db.set_value("User", self.stranger.name, "user_type", "Website User")
		frappe.share.add("Customer", self.customer.name, self.stranger.name, read=1, write=1)
		self.assertEqual(self.api.entity_actions_batch([root.name], self.stranger.name), {})

	def test_managed_links_rename_and_move_without_touching_storage(self):
		from suite.drive.api.files import create_link, create_folder, rename, move
		root = self.root()
		frappe.set_user(self.writer.name)
		folder = create_folder("Link destination", root.name)
		link = create_link("Original link", "https://example.invalid/original", root.name)
		with patch("suite.drive.utils.files.FileManager.move") as transfer:
			rename(link.name, "Renamed link")
			move([link.name], folder.name)
			transfer.assert_not_called()
		link.reload()
		self.assertEqual(link.file_url, "https://example.invalid/original")
		self.assertEqual(link.file_name, "Renamed link")
		self.assertEqual(link.folder, folder.name)
