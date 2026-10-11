import importlib.util
import io
import unittest
import urllib.error
from pathlib import Path

spec = importlib.util.spec_from_file_location(
	"release_image", Path(__file__).resolve().parents[2] / "scripts/release-image.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class Response(io.BytesIO):
	def __init__(self, body=b"", headers=None):
		super().__init__(body)
		self.headers = headers or {}


class ReleaseLookupTests(unittest.TestCase):
	def lookup(self, responses):
		calls = []

		def open_url(request, **_kwargs):
			calls.append(request)
			response = responses.pop(0)
			if isinstance(response, Exception):
				raise response
			return response

		return release.existing_image("v1.0.0", "actor", "token", open_url), calls

	def error(self, status):
		return urllib.error.HTTPError("https://example.invalid", status, "fixture", {}, None)

	def test_existing_release_resolves_immutable_digest(self):
		result, calls = self.lookup(
			[
				Response(b'{"token":"bearer"}'),
				Response(headers={"Docker-Content-Digest": "sha256:" + "a" * 64}),
			]
		)
		self.assertEqual(result, release.IMAGE + "@sha256:" + "a" * 64)
		self.assertEqual(calls[-1].method, "HEAD")

	def test_missing_tag_is_only_a_manifest_404(self):
		result, _ = self.lookup([Response(b'{"token":"bearer"}'), self.error(404)])
		self.assertIsNone(result)

	def test_registry_permission_error_is_not_missing_release(self):
		with self.assertRaisesRegex(RuntimeError, "403"):
			self.lookup([Response(b'{"token":"bearer"}'), self.error(403)])

	def test_first_package_checks_authenticated_api_absence(self):
		result, calls = self.lookup([self.error(403), self.error(404)])
		self.assertIsNone(result)
		self.assertIn("api.github.com", calls[-1].full_url)

	def test_existing_inaccessible_package_blocks_publication(self):
		with self.assertRaisesRegex(RuntimeError, "exists"):
			self.lookup([self.error(403), Response(b"{}")])

	def test_network_error_does_not_allow_publication(self):
		with self.assertRaises(urllib.error.URLError):
			self.lookup([urllib.error.URLError("offline")])


if __name__ == "__main__":
	unittest.main()
