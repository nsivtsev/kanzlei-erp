import importlib.util
import hashlib
import io
import json
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


class RegistryPromotionTests(unittest.TestCase):
	def registry(self, revision="1" * 40):
		self.assertTrue(hasattr(release, "Registry"), "Registry promotion implementation is missing")
		config = json.dumps({"architecture": "amd64", "os": "linux",
			"config": {"Labels": {"org.opencontainers.image.revision": revision}}}).encode()
		config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
		manifest = json.dumps({"schemaVersion": 2,
			"mediaType": "application/vnd.oci.image.manifest.v1+json",
			"config": {"digest": config_digest}, "layers": [{"size": 100}]}).encode()
		digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
		manifests = {digest: manifest, "sha-" + "1" * 40: manifest}
		writes = []

		def open_url(request, **_kwargs):
			if "/token?" in request.full_url:
				return Response(b'{"token":"bearer"}')
			if "/blobs/" in request.full_url:
				return Response(config)
			reference = request.full_url.rsplit("/", 1)[-1]
			if request.get_method() == "PUT":
				writes.append(request.data)
				manifests[reference] = request.data
			if reference not in manifests:
				raise urllib.error.HTTPError(request.full_url, 404, "absent", {}, None)
			body = manifests[reference]
			return Response(body, {"Docker-Content-Digest": "sha256:" + hashlib.sha256(body).hexdigest()})

		return release.Registry("actor", "token", open_url), release.IMAGE + "@" + digest, manifest, writes, manifests

	def test_promotion_copies_exact_manifest_without_rebuilding_or_overwriting(self):
		registry, image, manifest, writes, _ = self.registry()
		self.assertEqual(registry.promote(image, "v1.0.0", "1" * 40), image)
		self.assertEqual(writes, [manifest])
		self.assertEqual(registry.promote(image, "v1.0.0", "1" * 40), image)
		self.assertEqual(writes, [manifest])

	def test_existing_tag_with_another_digest_is_never_overwritten(self):
		registry, image, _, writes, manifests = self.registry()
		manifests["v1.0.0"] = b'{"other":"image"}'
		with self.assertRaisesRegex(ValueError, "reassigned"):
			registry.promote(image, "v1.0.0", "1" * 40)
		self.assertEqual(writes, [])

	def test_mismatched_revision_blocks_promotion(self):
		registry, image, _, writes, _ = self.registry(revision="2" * 40)
		with self.assertRaisesRegex(ValueError, "revision"):
			registry.promote(image, "v1.0.0", "1" * 40)
		self.assertEqual(writes, [])

	def test_manifest_body_must_match_the_requested_digest(self):
		registry, image, _, writes, manifests = self.registry()
		manifests[image.split("@", 1)[1]] = b'{"tampered":true}'
		with self.assertRaisesRegex(ValueError, "digest"):
			registry.inspect(image, "1" * 40)
		self.assertEqual(writes, [])

	def test_candidate_sha_tag_can_be_resolved(self):
		registry, image, _, _, _ = self.registry()
		self.assertEqual(registry.existing("sha-" + "1" * 40), image)

	def test_credentials_are_not_forwarded_to_registry_blob_storage(self):
		self.assertTrue(hasattr(release, "SafeRedirect"), "Safe redirect implementation is missing")
		request = urllib.request.Request("https://ghcr.io/v2/test/blobs/digest",
			headers={"Authorization": "Bearer private-token"})
		redirected = release.SafeRedirect().redirect_request(request, None, 302, "Found", {},
			"https://pkg-containers.githubusercontent.com/blob")
		self.assertIsNone(redirected.get_header("Authorization"))
		with self.assertRaises(ValueError):
			release.SafeRedirect().redirect_request(request, None, 302, "Found", {}, "http://example.com/blob")

	def test_index_preserves_root_digest_and_checks_selected_platform_config(self):
		registry, child_image, manifest, writes, manifests = self.registry()
		index = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
			"manifests": [{"digest": child_image.split("@", 1)[1], "platform": {"os": "linux", "architecture": "amd64"}}]}).encode()
		digest = "sha256:" + hashlib.sha256(index).hexdigest()
		manifests[digest] = index
		image = release.IMAGE + "@" + digest
		content = registry.inspect(image, "1" * 40)
		self.assertEqual(content["body"], index)
		self.assertEqual(content["config_digest"], json.loads(manifest)["config"]["digest"])
		self.assertEqual(registry.promote(image, "v1.0.0", "1" * 40), image)
		self.assertEqual(writes, [index])


if __name__ == "__main__":
	unittest.main()
