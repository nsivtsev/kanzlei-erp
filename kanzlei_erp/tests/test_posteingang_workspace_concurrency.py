"""Independent writer commits while the read projection is being assembled."""

import multiprocessing
from pathlib import Path
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from kanzlei_erp.tests.posteingang_helpers import PosteingangTestCase


def _write(site, sites, letter, action, start, done):
	frappe.init(site, sites_path=sites, force=True)
	frappe.connect()
	try:
		if not start.wait(20):
			raise AssertionError("read synchronization timed out")
		frappe.db.set_value("Incoming Letter Action", action, "title", "New committed title")
		frappe.db.sql("UPDATE `tabIncoming Letter` SET revision=revision+1 WHERE name=%s", letter)
		frappe.db.commit()
		done.set()
	finally:
		frappe.destroy()


class TestWorkspaceConcurrency(PosteingangTestCase):
	def test_aggregate_keeps_one_snapshot_after_concurrent_commit_and_stale_initial_snapshot(self):
		from kanzlei_erp import posteingang_confirmation as review
		from kanzlei_erp import posteingang_jobs as jobs
		from kanzlei_erp import posteingang_workspace as workspace
		letter = self.letter()
		jobs.manual_review(letter.name, 1, "manual")
		review.add_manual_action(letter.name, "Original title", "Synthetic description", 2, "add")
		action = frappe.db.get_value("Incoming Letter Action", {"parent": letter.name}, "name")
		frappe.db.commit()
		frappe.db.sql("SELECT revision FROM `tabIncoming Letter` WHERE name=%s", letter.name)
		context = multiprocessing.get_context("spawn")
		start, done = context.Event(), context.Event()
		process = context.Process(target=_write, args=(frappe.local.site, str(Path(frappe.local.sites_path).resolve()), letter.name, action, start, done))
		process.start()
		projection = jobs.analysis_projection

		def interleave(doc):
			result = projection(doc)
			start.set()
			self.assertTrue(done.wait(25), "independent writer must commit before review projection")
			return result

		try:
			with patch.object(frappe.local, "request", Request(EnvironBuilder(method="POST").get_environ()), create=True), patch.object(jobs, "analysis_projection", side_effect=interleave):
				result = workspace.get_letter_workspace(letter.name)
			self.assertIsInstance(result, dict)
			self.assertEqual(result["revision"], 3)
			self.assertEqual(result["review"]["review"]["actions"][0]["title"], "Original title")
			self.assertEqual({result[k]["revision"] for k in ("analysis", "matching", "review")}, {3})
			frappe.db.rollback()
			self.assertEqual(frappe.db.get_value("Incoming Letter", letter.name, "revision"), 4)
		finally:
			process.join(5)
			if process.is_alive():
				process.kill()
				process.join(5)
