"""Host-safe migration safety tests; no live Frappe site is touched."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kanzlei_erp import s3_migration as migration


class FakeS3:
	def __init__(self):
		self.objects = {}
		self.uploads = 0

	def get_object(self, Bucket, Key):
		if Key not in self.objects:
			raise KeyError(Key)
		return {"Body": io.BytesIO(self.objects[Key])}

	def head_object(self, Bucket, Key):
		if Key not in self.objects:
			error = KeyError(Key)
			error.response = {"Error": {"Code": "404"}}
			raise error
		return {"ContentLength": len(self.objects[Key])}

	def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
		self.uploads += 1
		self.objects[Key] = Path(Filename).read_bytes()


class TestMigrationAlgorithms(unittest.TestCase):
	def test_metadata_snapshot_order_is_independent_of_database_collation(self):
		fake = SimpleNamespace(get_all=lambda *args, **kwargs: [{"name": "alpha"}, {"name": "Users"}])
		self.assertEqual([row["name"] for row in migration._rows(fake)], ["Users", "alpha"])

	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.site = Path(self.temp.name)
		(self.site / "private/files/drive/.trash").mkdir(parents=True)
		(self.site / "public/files").mkdir(parents=True)
		self.rows = [
			dict(name="Drive", file_url="/private/files/drive", is_folder=1),
			dict(name="Users", file_url="/private/files/drive/Users", is_folder=1),
		]

	def blob(self, relative, content=b"document"):
		path = self.site / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(content)
		return path

	def inventory(self):
		return migration.build_inventory(self.rows, self.site, "erp/test", ".thumbnails")

	def test_traversal_and_symlink_escape_are_rejected(self):
		for value in ("/private/files/../../site_config.json", "/etc/passwd", "private/files/../secret"):
			with self.subTest(value=value), self.assertRaises(ValueError):
				migration.guarded_path(self.site, value)
		(self.site / "private/files/escape").symlink_to("/etc")
		with self.assertRaises(ValueError):
			migration.guarded_path(self.site, "/private/files/escape/passwd")

	def test_nested_trash_source_and_previews_are_included(self):
		self.rows.extend(
			[
				dict(
					name="deleted",
					folder="Drive",
					is_folder=1,
					status="Trashed",
					file_url="/private/files/drive/former",
				),
				dict(
					name="child",
					folder="deleted",
					status="Trashed",
					file_url="/private/files/drive/former/nested/file.pdf",
					file_name="file.pdf",
				),
			]
		)
		source = self.blob("private/files/drive/.trash/deleted/nested/file.pdf")
		self.blob("private/files/drive/.thumbnails/child.thumbnail", b"preview")
		report = self.inventory()
		self.assertEqual(report["conflicts"], [])
		self.assertEqual(report["missing"], [])
		self.assertEqual(len(report["entries"]), 2)
		self.assertEqual(report["entries"][0]["source"], str(source.resolve()))
		self.assertEqual(report["entries"][0]["key"], "erp/test/drive/child")

	def test_shared_framework_urls_copy_once_and_external_content_skips(self):
		self.blob("private/files/mail.pdf")
		for name in ("a", "b"):
			self.rows.append(dict(name=name, folder="Home/Attachments", file_url="/private/files/mail.pdf"))
		self.rows.extend(
			[
				dict(name="external", file_url="https://elsewhere.example/a"),
				dict(
					name="db",
					folder="Drive",
					content_doctype="Writer Document",
					file_url="/private/files/drive/writer",
				),
			]
		)
		report = self.inventory()
		self.assertEqual(len(report["entries"]), 1)
		self.assertEqual(report["entries"][0]["files"], ["a", "b"])
		self.assertEqual(len(report["skipped"]), 2)
		self.assertEqual(report["conflicts"], [])

	def test_unregistered_file_and_missing_original_are_reported(self):
		self.blob("public/files/orphan.bin")
		self.rows.append(dict(name="missing", file_url="/private/files/not-found.pdf"))
		report = self.inventory()
		self.assertEqual(report["missing"][0]["file"], "missing")
		self.assertIn("public/files/orphan.bin", report["unregistered"])

	def test_copy_resume_checks_actual_bytes_and_never_overwrites_foreign_objects(self):
		self.blob("private/files/mail.pdf")
		self.rows.append(dict(name="mail", file_url="/private/files/mail.pdf"))
		report = self.inventory()
		s3 = FakeS3()
		migration.copy_entries(report, s3, "bucket", lambda: None)
		migration.copy_entries(report, s3, "bucket", lambda: None)
		self.assertEqual(s3.uploads, 1)
		migration.verify_entries(report, s3, "bucket", lambda: None)
		self.assertEqual(report["entries"][0]["state"], "verified")
		s3.objects[report["entries"][0]["key"]] = b"foreign content"
		with self.assertRaisesRegex(ValueError, "collision"):
			migration.copy_entries(report, s3, "bucket", lambda: None)
		self.assertEqual(s3.uploads, 1)

	def test_changed_source_invalidates_copy_and_activation_snapshot(self):
		path = self.blob("private/files/mail.pdf")
		self.rows.append(dict(name="mail", file_url="/private/files/mail.pdf"))
		report = self.inventory()
		path.write_bytes(b"changed")
		with self.assertRaisesRegex(ValueError, "changed"):
			migration.copy_entries(report, FakeS3(), "bucket", lambda: None)

	def test_removed_records_do_not_require_already_deleted_bytes(self):
		self.rows.append(
			dict(
				name="removed",
				folder="Drive",
				status="Removed",
				file_url="/private/files/drive/already-deleted.pdf",
			)
		)
		report = self.inventory()
		self.assertEqual(report["missing"], [])
		self.assertEqual(report["skipped"][0]["file"], "removed")

	def test_source_replaced_with_symlink_is_rejected_before_upload(self):
		path = self.blob("private/files/mail.pdf")
		self.rows.append(dict(name="mail", file_url="/private/files/mail.pdf"))
		report = self.inventory()
		external = self.site / "outside.pdf"
		external.write_bytes(b"document")
		path.unlink()
		path.symlink_to(external)
		with self.assertRaisesRegex(ValueError, "changed"):
			migration.copy_entries(report, FakeS3(), "bucket", lambda: None)

	def test_native_aliases_are_reported_as_ownership_conflicts(self):
		self.blob("private/files/drive/shared.pdf")
		for name in ("a", "b"):
			self.rows.append(dict(name=name, folder="Drive", file_url="/private/files/drive/shared.pdf"))
		report = self.inventory()
		self.assertTrue(report["conflicts"])

	def test_verification_reads_bytes_and_marks_corruption(self):
		self.blob("private/files/mail.pdf")
		self.rows.append(dict(name="mail", file_url="/private/files/mail.pdf"))
		report = self.inventory()
		s3 = FakeS3()
		migration.copy_entries(report, s3, "bucket", lambda: None)
		s3.objects[report["entries"][0]["key"]] = b"corrupted"
		with self.assertRaisesRegex(ValueError, "SHA-256"):
			migration.verify_entries(report, s3, "bucket", lambda: None)
		self.assertEqual(report["entries"][0]["state"], "failed")

	def test_framework_thumbnail_is_migrated_with_protected_source_file(self):
		self.blob("private/files/mail.pdf")
		self.blob("public/files/thumbnail-mail.png", b"preview bytes")
		self.rows.append(dict(name="mail", file_url="/private/files/mail.pdf",
			thumbnail_url="/files/thumbnail-mail.png"))
		report = self.inventory()
		previews = [item for item in report["entries"] if item["kind"] == "derivative"]
		self.assertEqual(len(previews), 1)
		self.assertTrue(previews[0]["key"].startswith("erp/test/derived/"))
		self.assertEqual(previews[0]["source_file"], "mail")
		self.assertEqual(previews[0]["urls"], ["/files/thumbnail-mail.png"])
		self.assertNotIn("public/files/thumbnail-mail.png", report["unregistered"])

	def test_shared_standard_thumbnail_copies_once_and_retains_all_owners(self):
		self.blob("private/files/mail.pdf")
		self.blob("public/files/thumbnail-mail.png", b"shared preview")
		for name in ("mail-a", "mail-b"):
			self.rows.append(dict(name=name, file_url="/private/files/mail.pdf",
				thumbnail_url="/files/thumbnail-mail.png"))
		report = self.inventory()
		previews = [item for item in report["entries"] if item["kind"] == "derivative"]
		self.assertEqual(report["conflicts"], [])
		self.assertEqual(len(previews), 1)
		self.assertEqual(previews[0]["files"], ["mail-a", "mail-b"])
		self.assertEqual(previews[0]["source_file"], "mail-a")

	def test_standard_thumbnail_alias_of_native_preview_copies_only_once(self):
		self.blob("private/files/drive/native.pdf")
		self.blob("private/files/drive/.thumbnails/native.thumbnail", b"preview bytes")
		self.rows.append(dict(name="native", folder="Drive", file_url="/private/files/drive/native.pdf",
			thumbnail_url="/private/files/drive/.thumbnails/native.thumbnail"))
		report = self.inventory()
		self.assertEqual(len(report["entries"]), 2)
		preview = next(item for item in report["entries"] if item["kind"] == "thumbnail")
		self.assertEqual(preview["source_file"], "native")
		self.assertEqual(preview["urls"], ["/private/files/drive/.thumbnails/native.thumbnail"])

	def test_head_authorization_failure_is_not_treated_as_missing(self):
		class DeniedS3(FakeS3):
			def head_object(self, **kwargs):
				error = PermissionError("access denied")
				error.response = {"Error": {"Code": "AccessDenied"}}
				raise error

		self.blob("private/files/mail.pdf")
		self.rows.append(dict(name="mail", file_url="/private/files/mail.pdf"))
		s3 = DeniedS3()
		with self.assertRaises(PermissionError):
			migration.copy_entries(self.inventory(), s3, "bucket", lambda: None)
		self.assertEqual(s3.uploads, 0)

	def test_trash_original_url_reuse_does_not_bind_deleted_bytes(self):
		self.rows.extend(
			[
				dict(
					name="old", folder="Drive", status="Trashed", file_url="/private/files/drive/reused.pdf"
				),
				dict(name="new", folder="Drive", status="Active", file_url="/private/files/drive/reused.pdf"),
			]
		)
		self.blob("private/files/drive/.trash/old", b"old bytes")
		self.blob("private/files/drive/reused.pdf", b"new bytes")
		report = self.inventory()
		self.assertEqual(report["conflicts"], [])
		self.assertEqual(migration._bindings_for(report)["/private/files/drive/reused.pdf"]["files"], ["new"])

	def test_journal_is_atomic_private_and_rejects_invalid_identifiers(self):
		path = self.site / "journal.json"
		migration.write_journal(path, {"state": "copied"})
		self.assertEqual(path.stat().st_mode & 0o777, 0o600)
		self.assertEqual(json.loads(path.read_text()), {"state": "copied"})
		for value in ("../other", "/tmp/other", "x.json", ""):
			with self.assertRaises(ValueError):
				migration.validate_run_id(value)


class TestMigrationAdministrativeGuards(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.path = Path(self.temp.name) / "run.json"
		self.rows = []
		self.epoch = 7
		self.marker = ""
		self.commits = 0
		self.bindings = {}

		class Settings(dict):
			def __getattr__(self, name):
				return self.get(name)

			def __setattr__(self, name, value):
				self[name] = value

			def set(self, name, value):
				self[name] = value

			def save(self, **kwargs):
				return self

		self.settings = Settings(
			enabled=0, flat=0, root_folder="drive", bucket="bucket", thumbnail_prefix=".thumbnails"
		)
		self.data = dict(
			version=1,
			run_id="run",
			site="test",
			state="verified",
			rows=[],
			prefix="erp/test",
			entries=[],
			folders=[],
			skipped=[],
			conflicts=[],
			missing=[],
			unregistered=[],
			epoch=7,
			settings=migration._settings_snapshot(self.settings),
		)
		migration.write_journal(self.path, self.data)
		self.s3 = FakeS3()

		@contextmanager
		def configuring():
			yield

		def bind(url, key, size, sha, content_type, source_file=None):
			self.bindings[url] = dict(name=url, file_url=url, object_key=key, file_size=size, sha256=sha,
				source_file=source_file)

		storage = SimpleNamespace(
			settings=lambda: self.settings,
			mutation_epoch=lambda: self.epoch,
			client=lambda **kwargs: self.s3,
			configuration_change=configuring,
			bind=bind,
			get_binding=lambda url: self.bindings.get(url),
		)
		self.frappe = SimpleNamespace(
			conf={"maintenance_mode": 1},
			clear_cache=lambda: None,
			db=SimpleNamespace(
				get_single_value=lambda *args, **kwargs: self.marker,
				set_single_value=self.set_marker,
				set_value=self.set_value,
				commit=self.commit,
				rollback=lambda: None,
				delete=lambda *args: None,
			),
		)

		@contextmanager
		def journal(run_id):
			yield self.frappe, self.path.parent, self.path, json.loads(self.path.read_text())

		self.patches = [
			patch.object(migration, "_journal", journal),
			patch.object(migration, "_rows", lambda _: self.rows),
			patch.dict(sys.modules, {"kanzlei_erp.object_storage": storage}),
			patch("kanzlei_erp.object_storage", storage, create=True),
		]
		for item in self.patches:
			item.start()
			self.addCleanup(item.stop)

	def set_marker(self, doctype, field, value):
		self.marker = value

	def set_value(self, doctype, name, field, value, **kwargs):
		for row in self.rows:
			if row["name"] == name:
				row[field] = value

	def commit(self):
		self.commits += 1

	def test_activation_requires_maintenance_and_current_metadata(self):
		self.frappe.conf["maintenance_mode"] = 0
		with self.assertRaisesRegex(ValueError, "maintenance"):
			migration.activate("run")
		self.frappe.conf["maintenance_mode"] = 1
		self.rows.append(dict(name="new upload"))
		with self.assertRaisesRegex(ValueError, "metadata changed"):
			migration.activate("run")
		self.assertEqual(self.commits, 0)
		self.assertEqual(self.settings.enabled, 0)

	def test_unchanged_activation_rolls_back_and_modified_epoch_blocks_it(self):
		migration.activate("run")
		self.assertEqual(self.settings.enabled, 1)
		self.assertEqual(self.marker, "run")
		self.epoch += 1
		with self.assertRaisesRegex(ValueError, "changed after activation"):
			migration.rollback("run")
		self.epoch -= 1
		migration.rollback("run")
		self.assertEqual(self.settings.enabled, 0)
		self.assertEqual(self.marker, "")
		self.assertEqual(json.loads(self.path.read_text())["state"], "rolled_back")

	def test_recovery_distinguishes_committed_and_rolled_back_activation(self):
		self.data["state"] = "activation_prepared"
		migration.write_journal(self.path, self.data)
		self.marker = "run"
		self.assertEqual(migration.recover("run")["state"], "active")
		self.marker = ""
		migration.write_journal(self.path, self.data)
		self.assertEqual(migration.recover("run")["state"], "verified")


def run_minio_rehearsal(access_key, secret_key):
	"""Opt-in real database/MinIO rehearsal. Never called by host test discovery.

	Only s3-test.localhost/s3-migration.localhost and the restricted test bucket/prefix are permitted.
	Credentials must be supplied through a protected local process, never CLI args.
	"""
	import shutil
	import uuid

	import frappe
	from suite.drive.utils import create_drive_file, get_root_folder, get_users_folder

	from kanzlei_erp import object_storage as storage

	if frappe.local.site not in ("s3-test.localhost", "s3-migration.localhost"):
		raise ValueError("Real migration rehearsal is restricted to the disposable S3 test sites")
	frappe.set_user("Administrator")
	storage.configure()
	if storage.settings().enabled or frappe.db.count("File", {"is_folder": 0}):
		raise ValueError("Rehearsal requires an empty disposable disk-backed site")
	original_settings = storage.settings().as_dict()
	original_maintenance = frappe.conf.get("maintenance_mode")
	original_names = set(frappe.get_all("File", pluck="name"))
	token = uuid.uuid4().hex
	disk_root = "migration-rehearsal-" + token
	prefix = "tests/s3-test.localhost/migration-" + token
	created_paths = []
	client = None
	run_id = None
	try:
		with storage.configuration_change():
			settings = storage.settings()
			settings.update(
				dict(
					enabled=0,
					flat=0,
					root_folder=disk_root,
					bucket="stb-golke-test",
					endpoint_url="http://minio:9000",
					aws_key=access_key,
					aws_secret=secret_key,
					signature_version="s3v4",
					kanzlei_s3_region="us-east-1",
					kanzlei_s3_prefix=prefix,
					kanzlei_s3_addressing_style="path",
					kanzlei_s3_public_endpoint="http://localhost:9002",
				)
			)
			settings.save(ignore_permissions=True)
		root = get_root_folder()
		get_users_folder()
		native_url = "/private/files/" + disk_root + "/native.pdf"
		native = create_drive_file("native.pdf", root.name, "PDF", native_url, mime_type="application/pdf")
		native_path = migration.guarded_path(frappe.get_site_path(), native_url)
		native_path.write_bytes(b"native PDF original")
		created_paths.append(native_path)
		preview_path = native_path.parent / ".thumbnails" / (native.name + ".thumbnail")
		preview_path.write_bytes(b"preview original")
		created_paths.append(preview_path)
		frappe.db.set_value("File", native.name, "thumbnail_url", "/" + str(preview_path.relative_to(Path(frappe.get_site_path()).resolve())))
		attachment = frappe.get_doc(
			dict(
				doctype="File", file_name="migration-" + token + ".xml", content=b"<migration/>", is_private=1
			)
		).insert()
		attachment_url = attachment.file_url
		attachment_path = migration.guarded_path(frappe.get_site_path(), attachment_url)
		created_paths.append(attachment_path)
		standard_preview_url = "/files/thumbnail-" + token + ".png"
		standard_preview_path = migration.guarded_path(frappe.get_site_path(), standard_preview_url)
		standard_preview_path.parent.mkdir(parents=True, exist_ok=True)
		standard_preview_path.write_bytes(b"standard Frappe preview")
		created_paths.append(standard_preview_path)
		frappe.db.set_value("File", attachment.name, "thumbnail_url", standard_preview_url)
		# A database-backed Writer document remains a directory/content record;
		# its registered image File is nevertheless an ordinary stored original.
		from suite.writer.api.docs import create_document

		writer = create_document(title="migration-writer-" + token, parent=root.name)
		writer_content_name = writer.content_docname
		writer_path = migration.guarded_path(frappe.get_site_path(), writer.file_url)
		embed_url = "/" + str(
			writer_path.relative_to(Path(frappe.get_site_path()).resolve()) / ".embeds" / "image.png"
		)
		embed = create_drive_file("image.png", writer.name, "Image", embed_url, mime_type="image/png")
		embed_path = migration.guarded_path(frappe.get_site_path(), embed_url)
		embed_path.write_bytes(b"Writer embedded image")
		created_paths.append(embed_path)
		trash_parent_url = "/private/files/" + disk_root + "/old-folder"
		trash_parent = create_drive_file("old-folder", root.name, "Folder", trash_parent_url)
		trash_child = create_drive_file(
			"old.pdf",
			trash_parent.name,
			"PDF",
			trash_parent_url + "/nested/old.pdf",
			mime_type="application/pdf",
		)
		frappe.db.set_value("File", trash_parent.name, "status", "Trashed")
		frappe.db.set_value("File", trash_child.name, "status", "Trashed")
		trash_path = native_path.parent / ".trash" / trash_parent.name / "nested" / "old.pdf"
		trash_path.parent.mkdir(parents=True)
		trash_path.write_bytes(b"trashed descendant")
		created_paths.append(trash_path)
		frappe.db.commit()
		frappe.conf.maintenance_mode = 1
		# Simulate the maintenance setting for this isolated process only: no
		# frontend request is sent and no background test workers use this site.
		result = migration.inventory()
		run_id = result["run_id"]
		if result["missing"] or result["conflicts"]:
			raise AssertionError(result)
		migration.copy(run_id)
		migration.copy(run_id)  # idempotent actual-S3 resumption
		client = storage.client()
		journal_path = Path(frappe.get_site_path("private", "s3-migrations", run_id + ".json"))
		data = json.loads(journal_path.read_text())
		first = data["entries"][0]
		client.put_object(Bucket="stb-golke-test", Key=first["key"], Body=b"corrupted")
		try:
			migration.verify(run_id)
		except ValueError as error:
			assert "SHA-256" in str(error)
		else:
			raise AssertionError("Corrupt remote body passed verification")
		try:
			migration.copy(run_id)
		except ValueError as error:
			assert "collision" in str(error)
		else:
			raise AssertionError("Resume overwrote corrupt remote body")
		# Explicitly repair only the isolated test object, then resume.
		client.delete_object(Bucket="stb-golke-test", Key=first["key"])
		migration.copy(run_id)
		migration.verify(run_id)
		current_rows = migration._canonical(migration._rows(frappe))
		original_rows = migration._canonical(json.loads(journal_path.read_text())["rows"])
		if current_rows != original_rows or storage.mutation_epoch() != data["epoch"]:
			before = {row["name"]: row for row in original_rows}
			differences = {
				row["name"]: {
					key: (before.get(row["name"], {}).get(key), value)
					for key, value in row.items()
					if before.get(row["name"], {}).get(key) != value
				}
				for row in current_rows
				if before.get(row["name"]) != row
			}
			raise AssertionError(
				{
					"before_epoch": data["epoch"],
					"current_epoch": storage.mutation_epoch(),
					"differences": differences,
				}
			)
		migration.activate(run_id)
		assert storage.settings().enabled == 1
		assert frappe.get_doc("File", native.name).file_url.startswith(migration.S3_URL_PREFIX)
		assert storage.get_binding(native_url)
		assert storage.get_binding(attachment_url)
		standard_preview_binding = storage.get_binding(standard_preview_url)
		assert standard_preview_binding.source_file == attachment.name
		assert client.get_object(Bucket="stb-golke-test", Key=standard_preview_binding.object_key)["Body"].read() == b"standard Frappe preview"
		assert frappe.db.exists("Writer Document", writer_content_name)
		assert frappe.get_doc("File", writer.name).content_docname == writer_content_name
		assert frappe.get_doc("File", trash_child.name).status == "Trashed"
		for name, expected in (
			(embed.name, b"Writer embedded image"),
			(trash_child.name, b"trashed descendant"),
		):
			with storage.open_file(frappe.get_doc("File", name)) as source:
				assert source.read() == expected
		with storage.open_file(frappe.get_doc("File", attachment.name)) as stream:
			assert stream.read() == b"<migration/>"
		# Ensure process-loss recovery uses the DB commit marker.
		data = json.loads(journal_path.read_text())
		data["state"] = "activation_prepared"
		migration.write_journal(journal_path, data)
		assert migration.recover(run_id)["state"] == "active"
		migration.rollback(run_id)
		assert storage.settings().enabled == 0
		assert frappe.get_doc("File", native.name).file_url == native_url
		assert storage.get_binding(native_url) is None
		assert native_path.read_bytes() == b"native PDF original"
		assert attachment_path.read_bytes() == b"<migration/>"
		# Re-activate to exercise actual WebDAV overwrite of both a mapped
		# framework URL and a native flat object, then prove rollback is fenced.
		write_run = migration.inventory()["run_id"]
		migration.copy(write_run)
		migration.verify(write_run)
		migration.activate(write_run)
		from suite.drive.webdav.context import build
		from suite.drive.webdav.put import _overwrite
		from werkzeug.wrappers import Request
		for name, payload in ((attachment.name, b"<webdav>mapped</webdav>"), (native.name, b"<webdav>native</webdav>")):
			scratch = Path(frappe.get_site_path("private", "webdav-migration-" + uuid.uuid4().hex + ".xml"))
			scratch.write_bytes(payload)
			created_paths.append(scratch)
			context = build(Request.from_values(path="/dav/rehearsal", method="PUT", content_type="application/xml"), "Administrator")
			# Fixture originals are intentionally tiny byte strings; thumbnail
			# rendering is separate from the real S3/DB overwrite transaction.
			context.manager.can_create_thumbnail = lambda file: False
			row = frappe.get_doc("File", name)
			response = _overwrite(context, row, scratch, len(payload), migration.hashlib.sha256(payload).hexdigest())
			assert response.status_code == 204
			frappe.db.commit()
			with storage.open_file(frappe.get_doc("File", name)) as stream:
				assert stream.read() == payload
		assert frappe.get_doc("File", attachment.name).file_url == attachment_url
		native_alias_key = storage.get_binding(native_url).object_key
		assert client.get_object(Bucket="stb-golke-test", Key=native_alias_key)["Body"].read() == b"<webdav>native</webdav>"
		try:
			migration.rollback(write_run)
		except ValueError as error:
			assert "changed after activation" in str(error)
		else:
			raise AssertionError("Rollback after WebDAV writes was not fenced")
		return dict(
			site=frappe.local.site,
			objects=len(data["entries"]),
			checks=[
				"inventory",
				"copy",
				"resume",
				"corruption",
				"collision",
				"verify",
				"activate",
				"read",
				"recover",
				"rollback",
				"writer-embed",
				"nested-trash",
				"standard-thumbnail",
				"webdav-mapped-overwrite",
				"webdav-native-alias-overwrite",
				"post-write-rollback-fence",
			],
		)
	finally:
		frappe.db.rollback()
		with storage.configuration_change():
			# Test-only direct cleanup avoids permission-tree side effects and
			# restores the disposable site rather than running a live migration.
			new_names = set(frappe.get_all("File", pluck="name")) - original_names
			if new_names:
				frappe.db.delete(
					"Writer Document",
					{
						"name": [
							"in",
							frappe.get_all(
								"File",
								filters={
									"name": ["in", list(new_names)],
									"content_doctype": "Writer Document",
								},
								pluck="content_docname",
							),
						]
					},
				)
				frappe.db.delete("File", {"name": ["in", list(new_names)]})
				for doctype, field in (
					("Drive Permission", "entity"),
					("Drive Entity Log", "entity_name"),
					("Drive Entity Activity Log", "entity"),
				):
					frappe.db.delete(doctype, {field: ["in", list(new_names)]})
			for mapping in frappe.get_all(
				"S3 File Object", filters={"object_key": ["like", prefix + "/%"]}, pluck="name"
			):
				frappe.db.delete("S3 File Object", {"name": mapping})
			settings = storage.settings()
			settings.update(
				{
					field: original_settings.get(field)
					for field in (*migration.CONFIG_FIELDS, "kanzlei_s3_migration_run")
				}
			)
			settings.save(ignore_permissions=True)
			frappe.db.commit()
		frappe.conf.maintenance_mode = original_maintenance
		for path in created_paths:
			path.unlink(missing_ok=True)
		shutil.rmtree(Path(frappe.get_site_path("private", "files", disk_root)), ignore_errors=True)
		if client:
			for page in client.get_paginator("list_objects_v2").paginate(
				Bucket="stb-golke-test", Prefix=prefix + "/"
			):
				for item in page.get("Contents", []):
					client.delete_object(Bucket="stb-golke-test", Key=item["Key"])


if __name__ == "__main__":
	unittest.main()
