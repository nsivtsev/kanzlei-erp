"""Independent process/DB transactions. Run only against a disposable site."""

import io
import multiprocessing
from pathlib import Path
from unittest.mock import patch

import frappe
from werkzeug.datastructures import FileStorage, MultiDict

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


def _upload_worker(site, sites_path, actor, key, content, barrier, queue):
	frappe.init(site, sites_path=sites_path, force=True)
	frappe.connect()
	frappe.set_user(actor)
	try:
		from kanzlei_erp.posteingang import _receive
		from kanzlei_erp.posteingang_pdf import validate_pdf

		# Deliberately establish an old snapshot before either request writes.
		frappe.db.sql("SELECT COUNT(*) FROM `tabIncoming Letter`")

		def synchronized_pdf(data, limit):
			barrier.wait(timeout=25)
			return validate_pdf(data, limit)

		with patch("kanzlei_erp.posteingang.validate_pdf", synchronized_pdf):
			result = _receive(io.BytesIO(content), "synthetic.pdf", key, "false", fresh=True)
		frappe.db.commit()
		queue.put(result)
	except Exception as exc:
		frappe.db.rollback()
		queue.put({"error": str(exc)})
	finally:
		frappe.destroy()


class TestConcurrentIntake(PosteingangTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user(self.operator.name)
		self.content = (
			Path(frappe.get_app_path("kanzlei_erp")) / "tests/fixtures/posteingang/pdfs/AP01-01.pdf"
		).read_bytes()
		self.content += ("\n% " + frappe.generate_hash(length=16)).encode()
		frappe.db.commit()

	def parallel(self, same_key, different_content=False, two_actors=False):
		context = multiprocessing.get_context("spawn")
		barrier, queue = context.Barrier(2), context.Queue()
		key = frappe.generate_hash(length=30)
		processes = []
		for i in range(2):
			actor = self.manager.name if i and two_actors else self.operator.name
			worker_key = key if same_key or not i else key + "other"
			content = self.content + (b"\n% changed" if i and different_content else b"")
			process = context.Process(
				target=_upload_worker,
				args=(
					frappe.local.site,
					str(Path(frappe.local.sites_path).resolve()),
					actor,
					worker_key,
					content,
					barrier,
					queue,
				),
			)
			process.start()
			processes.append(process)
		try:
			results = [queue.get(timeout=45) for _ in processes]
			for process in processes:
				process.join(timeout=10)
				self.assertEqual(process.exitcode, 0)
			return results
		finally:
			for process in processes:
				if process.is_alive():
					process.kill()
					process.join()

	def test_same_key_and_old_snapshots_create_one_letter(self):
		results = self.parallel(same_key=True)
		self.assertEqual({r.get("outcome") for r in results}, {"created"}, results)
		self.assertEqual(len({r["letter"] for r in results}), 1)
		self.assertEqual(sorted(r["replayed"] for r in results), [False, True])

	def test_two_operators_same_bytes_get_created_and_duplicate(self):
		results = self.parallel(same_key=False, two_actors=True)
		self.assertEqual({r.get("outcome") for r in results}, {"created", "duplicate"}, results)
		self.assertEqual(len({r["letter"] for r in results}), 1)

	def test_same_key_different_bytes_conflicts(self):
		results = self.parallel(same_key=True, different_content=True)
		self.assertEqual(sum(r.get("outcome") == "created" for r in results), 1, results)
		self.assertTrue(any("IDEMPOTENCY_CONFLICT" in r.get("error", "") for r in results), results)

	def test_commit_failure_cleans_local_bytes_and_all_rows(self):
		from frappe.model.base_document import get_controller

		from kanzlei_erp.posteingang import _save_receipt, receive

		File = get_controller("File")
		paths = []
		original = File.after_insert

		def capture(file):
			paths.append(Path(file.get_full_path()))
			return original(file)

		def explode():
			raise RuntimeError("synthetic before_commit failure")

		def receipt(*args):
			_save_receipt(*args)
			frappe.db.before_commit.add(explode)

		before = {
			d: frappe.db.count(d)
			for d in ("Incoming Letter", "File", "Incoming Letter Event", "Incoming Letter Intake Receipt")
		}
		files = MultiDict([("file", FileStorage(io.BytesIO(self.content), filename="synthetic.pdf"))])
		with (
			patch.object(frappe.local, "request", frappe._dict(files=files), create=True),
			patch("kanzlei_erp.posteingang._save_receipt", receipt),
			patch.object(File, "after_insert", capture),
		):
			with self.assertRaisesRegex(RuntimeError, "synthetic before_commit failure"):
				receive(
					request_key=frappe.generate_hash(length=30),
					received_date_known="false",
					cmd="kanzlei_erp.posteingang.receive",
				)
		for d, count in before.items():
			self.assertEqual(frappe.db.count(d), count)
		self.assertTrue(paths)
		self.assertTrue(all(not p.exists() for p in paths))

	def test_unknown_commit_outcome_preserves_s3_original(self):
		from kanzlei_erp import object_storage as storage
		from kanzlei_erp.posteingang import get_intake_result, receive
		from kanzlei_erp.tests.test_object_storage import TestObjectStorage
		if "suite" not in frappe.get_installed_apps():
			self.skipTest("Suite S3 integration")
		original_settings = storage.settings().as_dict()
		frappe.set_user("Administrator")
		TestObjectStorage.setUp(self)
		frappe.set_user(self.operator.name)
		frappe.db.commit()
		key = frappe.generate_hash(length=30)
		files = MultiDict([("file", FileStorage(io.BytesIO(self.content), filename="synthetic.pdf"))])
		commit = frappe.db.commit
		def lost_ack():
			commit()
			raise ConnectionError("synthetic lost COMMIT acknowledgement")
		try:
			with patch.object(frappe.local, "request", frappe._dict(files=files), create=True):
				# Only fail recovery rollback after the real COMMIT has completed.
				rollback = frappe.db.rollback
				committed = []
				def commit_then_disconnect():
					committed.append(True)
					lost_ack()
				def unavailable_rollback(*args, **kwargs):
					if committed:
						raise ConnectionError("synthetic unavailable recovery connection")
					return rollback(*args, **kwargs)
				with patch.object(frappe.db, "commit", commit_then_disconnect), patch.object(frappe.db, "rollback", unavailable_rollback):
					with self.assertRaises(ConnectionError):
						receive(request_key=key, received_date_known="false")
			result = get_intake_result(key)
			self.assertEqual(result["outcome"], "created")
			file = frappe.get_doc("File", result["original_file"])
			self.assertEqual(file.get_content(encodings=[]), self.content)
			# Synthetic rows only: do not leave references to a MemoryS3 backend.
			frappe.db.delete("Incoming Letter Intake Receipt", {"letter": result["letter"]})
			frappe.db.delete("Incoming Letter Event", {"letter": result["letter"]})
			frappe.db.delete("Incoming Letter", {"name": result["letter"]})
			frappe.db.delete("File", {"name": result["original_file"]})
		finally:
			frappe.set_user("Administrator")
			with storage.configuration_change():
				settings = storage.settings()
				settings.update({k: v for k, v in original_settings.items() if k not in {"modified", "creation", "owner", "modified_by"}})
				settings.save()
			frappe.db.commit()
