import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PythonCompatibilityTests(unittest.TestCase):
	def test_deployment_tooling_supports_python_39_syntax(self):
		# Host-side tooling must not inherit the Frappe app's Python 3.14 syntax.
		sources = [
			*(ROOT / "deploy").rglob("*.py"),
			ROOT / "scripts/release-image.py",
			*(ROOT / "tests/deploy").rglob("*.py"),
		]
		for source in sorted(sources):
			with self.subTest(source=str(source.relative_to(ROOT))):
				try:
					ast.parse(source.read_text(), filename=str(source), feature_version=(3, 9))
				except SyntaxError as error:
					self.fail("Deployment tooling requires Python 3.9-compatible syntax: " + str(error))
