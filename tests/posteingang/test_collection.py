"""Collector CLI is inert until a marked disposable site is explicitly selected."""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


class CollectionTests(unittest.TestCase):
	def setUp(self):
		path = ROOT / "scripts/run_posteingang_evaluation.py"
		self.assertTrue(path.exists(), "AP10 collector missing")
		spec = importlib.util.spec_from_file_location("ap10_collector", path)
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)

	def args(self, *extra):
		return self.module.parser().parse_args(
			[
				"--site",
				"posteingang-ap10.localhost",
				"--actor",
				"ap10-collector@example.invalid",
				"--output",
				"/tmp/ap10-test",
				*extra,
			]
		)

	def test_default_provider_is_fake_and_site_requires_marker(self):
		args = self.args()
		self.assertEqual(args.provider, "fake")
		with self.assertRaisesRegex(ValueError, "disposable"):
			self.module.check_invocation(args, {})
		self.module.check_invocation(args, {"posteingang_acceptance_site": 1})

	def test_main_site_cannot_be_made_eligible_by_marker(self):
		args = self.args()
		args.site = "development.localhost"
		with self.assertRaises(ValueError):
			self.module.check_invocation(args, {"posteingang_acceptance_site": 1})

	def test_live_requires_explicit_external_and_request_limit(self):
		args = self.args("--provider", "mistral")
		with self.assertRaises(ValueError):
			self.module.check_invocation(args, {"posteingang_acceptance_site": 1})
		args.allow_external = True
		with self.assertRaises(ValueError):
			self.module.check_invocation(args, {"posteingang_acceptance_site": 1})
		args.max_requests = 2
		self.module.check_invocation(args, {"posteingang_acceptance_site": 1})

	def test_real_actor_is_not_a_synthetic_setup_target(self):
		args = self.args()
		args.actor = "real@example.com"
		with self.assertRaises(ValueError):
			self.module.check_invocation(args, {"posteingang_acceptance_site": 1})

	def test_runtime_collector_is_a_separate_guarded_test_helper(self):
		self.assertTrue(
			(ROOT / "kanzlei_erp/tests/posteingang_acceptance_helpers.py").is_file(),
			"guarded collector runtime missing",
		)

	def test_help_does_not_import_frappe(self):
		self.assertNotIn("frappe", sys.modules)
		with self.assertRaises(SystemExit) as ctx:
			self.module.parser().parse_args(["--help"])
		self.assertEqual(ctx.exception.code, 0)


class CollectorReviewTests(CollectionTests):
	def test_live_autostart_refused_before_collection(self):
		self.assertTrue(hasattr(self.module, "check_configuration"), "live preflight guard missing")
		args = self.args("--provider", "mistral", "--allow-external", "--max-requests", "2")
		config = {
			"enabled": 1,
			"external_analysis_enabled": 1,
			"provider": "mistral",
			"max_retries": 0,
			"auto_analysis_enabled": 1,
		}
		with self.assertRaises(ValueError):
			self.module.check_configuration(args, config, 1)
		config["auto_analysis_enabled"] = 0
		self.module.check_configuration(args, config, 1)

	def test_duplicate_receipt_cannot_adopt_old_run(self):
		self.assertTrue(hasattr(self.module, "check_receipt"), "receipt ownership guard missing")
		with self.assertRaises(ValueError):
			self.module.check_receipt({"outcome": "duplicate"})
		self.module.check_receipt({"outcome": "created"})

	def test_actor_must_belong_to_this_journal_and_have_only_expected_roles(self):
		self.assertTrue(hasattr(self.module, "check_actor"), "actor ownership guard missing")
		for owned, roles in (
			(False, ["Projects User", "Posteingang Operator"]),
			(True, ["Projects User", "Posteingang Operator", "Sales Manager"]),
		):
			with self.assertRaises(ValueError):
				self.module.check_actor(owned, roles)
		self.module.check_actor(True, ["All", "Guest", "Desk User", "Projects User", "Posteingang Operator"])

	def test_run_provider_actor_and_source_must_match(self):
		self.assertTrue(hasattr(self.module, "check_run"), "run provenance guard missing")
		run = {
			"provider": "fake",
			"queued_by": "ap10-test@example.invalid",
			"letter": "ours",
			"generation": 1,
			"configuration": {"provider": "fake"},
			"snapshot": {"original_file": "file", "content_sha256": "hash", "page_count": 1},
		}
		letter = {
			"name": "ours",
			"current_run": "run",
			"analysis_generation": 1,
			"received_by": "ap10-test@example.invalid",
			"original_file": "file",
			"content_sha256": "hash",
			"page_count": 1,
		}
		with self.assertRaises(ValueError):
			self.module.check_run(
				run, letter, "ap10-test@example.invalid", "mistral", {"provider": "mistral"}
			)
		self.module.check_run(run, letter, "ap10-test@example.invalid", "fake", {"provider": "fake"})


class CollectorSourceTests(CollectionTests):
	def test_swapped_journal_letter_cannot_be_labelled_with_another_case_hash(self):
		self.assertTrue(hasattr(self.module, "check_source"), "manifest source guard missing")
		case = {"sha256": "expected", "page_count": 1, "received_date_known": False, "received_date": None}
		letter = {
			"content_sha256": "different",
			"page_count": 1,
			"received_date_known": False,
			"received_date": None,
		}
		with self.assertRaises(ValueError):
			self.module.check_source(letter, case)
		letter["content_sha256"] = "expected"
		self.module.check_source(letter, case)
		letter["received_date_known"] = True
		letter["received_date"] = "2026-10-25"
		with self.assertRaises(ValueError):
			self.module.check_source(letter, case)


class CollectorResumeTests(CollectionTests):
	def test_terminal_outcome_can_be_read_after_settings_epoch_changes(self):
		self.assertTrue(hasattr(self.module, "resume_action"), "resume state guard missing")
		self.assertEqual(self.module.resume_action("Succeeded", 1, 2, 9), "observe")
		self.assertEqual(self.module.resume_action("Failed", 1, 2, 9), "observe")
		self.assertEqual(self.module.resume_action("Queued", 0, 9, 9), "dispatch")
		for status, attempt, epoch in [("Running", 1, 9), ("Queued", 1, 9), ("Queued", 0, 2)]:
			with self.assertRaises(ValueError):
				self.module.resume_action(status, attempt, epoch, 9)

	def test_code_fingerprint_covers_matching_and_collector_dependencies(self):
		import tempfile

		self.assertTrue(hasattr(self.module, "code_hash"), "full code fingerprint missing")
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "kanzlei_erp").mkdir()
			p = root / "kanzlei_erp/mandant_identifiers.py"
			p.write_text("first")
			first = self.module.code_hash(root)
			p.write_text("changed")
			self.assertNotEqual(first, self.module.code_hash(root))
