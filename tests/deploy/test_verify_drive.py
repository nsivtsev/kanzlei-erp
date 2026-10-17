import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PORTAL_HTML = '<div id="portal-workspaces"><a href="/desk/kanzlei-overview">ERP</a><a href="/drive">Drive</a></div>'


class LandingChecked(Exception):
	"""Stop before the unrelated Drive upload phase."""


class DriveLandingTests(unittest.TestCase):
	def check_landing(self, home_page, portal_html=PORTAL_HTML):
		spec = importlib.util.spec_from_file_location("verify_drive", ROOT / "deploy/verify_drive.py")
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		visited = []

		class Session:
			headers = {}

			def post(self, url, **kwargs):
				return SimpleNamespace(
					raise_for_status=lambda: None,
					json=lambda: {"message": "Logged In", "home_page": home_page},
				)

			def get(self, url, **kwargs):
				visited.append(url)
				if url.endswith("/desk/kanzlei-overview"):
					raise LandingChecked(visited)
				return SimpleNamespace(raise_for_status=lambda: None, text=portal_html)

		with patch.dict(sys.modules, {"requests": SimpleNamespace(Session=Session), "pymupdf": SimpleNamespace()}):
			module.check("http://frontend:8080", "ci.localhost", "test-only")

	def test_portal_login_is_verified_before_entering_erp_and_drive(self):
		for home in ("kanzlei-portal", "/kanzlei-portal"):
			with self.subTest(home=home):
				with self.assertRaises(LandingChecked) as result:
					self.check_landing(home)
				self.assertEqual(result.exception.args[0], [
					"http://frontend:8080/kanzlei-portal",
					"http://frontend:8080/desk/kanzlei-overview",
				])

	def test_old_erp_or_suite_login_landing_is_rejected(self):
		for home in ("/desk/kanzlei-overview", "/suite"):
			with self.subTest(home=home), self.assertRaisesRegex(AssertionError, "login landing"):
				self.check_landing(home)

	def test_missing_portal_or_workspace_links_are_rejected(self):
		for html in ("<form>Login</form>", PORTAL_HTML.replace('href="/drive"', 'href="/other"'),
			PORTAL_HTML.replace('href="/desk/kanzlei-overview"', 'href="/other"')):
			with self.subTest(html=html), self.assertRaisesRegex(AssertionError, "Portal"):
				self.check_landing("/kanzlei-portal", html)


if __name__ == "__main__":
	unittest.main()
