#!/usr/bin/env python3
"""Resolve an existing release digest without treating registry errors as absence."""

import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

IMAGE = "ghcr.io/nsivtsev/kanzlei-erp"
ACCEPT = ", ".join(
	(
		"application/vnd.oci.image.index.v1+json",
		"application/vnd.oci.image.manifest.v1+json",
		"application/vnd.docker.distribution.manifest.list.v2+json",
		"application/vnd.docker.distribution.manifest.v2+json",
	)
)


TAG_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
SHA_RE = re.compile(r"[a-f0-9]{40}\Z")
DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}\Z")


class SafeRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, newurl):
		if urllib.parse.urlsplit(newurl).scheme != "https":
			raise ValueError("Registry redirects must use HTTPS")
		redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
		if redirected and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
			redirected.remove_header("Authorization")
		return redirected


def safe_urlopen(request, *, timeout=30):
	return urllib.request.build_opener(SafeRedirect()).open(request, timeout=timeout)


class Registry:
	def __init__(self, actor, token, open_url=safe_urlopen):
		self.actor, self.token, self.open_url = actor, token, open_url
		self.tokens = {}

	def bearer(self, scope="pull"):
		if scope in self.tokens:
			return self.tokens[scope]
		basic = base64.b64encode((self.actor + ":" + self.token).encode()).decode()
		request = urllib.request.Request(
			"https://ghcr.io/token?service=ghcr.io&scope=repository%3Ansivtsev%2Fkanzlei-erp%3A" + scope,
			headers={"Authorization": "Basic " + basic},
		)
		try:
			with self.open_url(request, timeout=30) as response:
				bearer = json.load(response)["token"]
		except urllib.error.HTTPError as error:
			error.close()
			if error.code != 403:
				raise RuntimeError("GHCR authentication failed: HTTP " + str(error.code)) from None
			# GHCR can deny a pull scope before the package has ever been created.
			# Confirm package absence using the authenticated GitHub API, never infer
			# it from a network failure or an ordinary registry permission error.
			package = urllib.request.Request(
				"https://api.github.com/users/nsivtsev/packages/container/kanzlei-erp",
				headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json"},
			)
			try:
				with self.open_url(package, timeout=30):
					pass
			except urllib.error.HTTPError as api_error:
				api_error.close()
				if api_error.code == 404:
					return None
				raise RuntimeError("Cannot verify GHCR package access: HTTP " + str(api_error.code)) from None
			raise RuntimeError("GHCR package exists but this workflow cannot read it")
		self.tokens[scope] = bearer
		return bearer

	def request(self, suffix, *, method="GET", data=None, content_type=None):
		bearer = self.bearer("pull,push" if method == "PUT" else "pull")
		if not bearer:
			raise ValueError("GHCR package is absent")
		headers = {"Authorization": "Bearer " + bearer, "Accept": ACCEPT}
		if content_type:
			headers["Content-Type"] = content_type
		return urllib.request.Request("https://ghcr.io/v2/nsivtsev/kanzlei-erp/" + suffix,
			headers=headers, method=method, data=data)

	def existing(self, tag):
		if not (TAG_RE.fullmatch(tag) or re.fullmatch(r"sha-[a-f0-9]{40}", tag)):
			raise ValueError("Image tag must be vX.Y.Z or sha-<full commit SHA>")
		if not self.bearer():
			return None
		try:
			with self.open_url(self.request("manifests/" + tag, method="HEAD"), timeout=30) as response:
				digest = response.headers.get("Docker-Content-Digest", "")
		except urllib.error.HTTPError as error:
			error.close()
			if error.code == 404:
				return None
			raise RuntimeError("Cannot resolve image manifest: HTTP " + str(error.code)) from None
		if not DIGEST_RE.fullmatch(digest):
			raise ValueError("Registry returned an invalid digest")
		return IMAGE + "@" + digest

	def content(self, kind, digest):
		if not DIGEST_RE.fullmatch(digest):
			raise ValueError("Invalid content digest")
		try:
			with self.open_url(self.request(kind + "/" + digest), timeout=30) as response:
				body = response.read(4 * 1024 * 1024 + 1)
		except urllib.error.HTTPError as error:
			error.close()
			raise RuntimeError("Cannot read registry content: HTTP " + str(error.code)) from None
		if len(body) > 4 * 1024 * 1024 or "sha256:" + hashlib.sha256(body).hexdigest() != digest:
			raise ValueError("Registry content does not match its digest")
		return body

	def inspect(self, image, revision):
		if not image.startswith(IMAGE + "@") or not SHA_RE.fullmatch(revision):
			raise ValueError("Invalid immutable image or revision")
		body = self.content("manifests", image.split("@", 1)[1])
		manifest = json.loads(body)
		selected = manifest
		if "manifests" in manifest:
			candidates = [item for item in manifest["manifests"]
				if item.get("platform", {}).get("os") == "linux"
				and item.get("platform", {}).get("architecture") == "amd64"]
			if len(candidates) != 1:
				raise ValueError("Image must contain exactly one linux/amd64 manifest")
			selected = json.loads(self.content("manifests", candidates[0]["digest"]))
		config = json.loads(self.content("blobs", selected["config"]["digest"]))
		if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
			raise ValueError("Image platform must be linux/amd64")
		if config.get("config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != revision:
			raise ValueError("Image revision does not match the requested commit")
		if manifest.get("mediaType") not in ACCEPT.split(", "):
			raise ValueError("Unsupported manifest media type")
		return {"body": body, "media_type": manifest["mediaType"],
			"config_digest": selected["config"]["digest"],
			"compressed_bytes": sum(layer["size"] for layer in selected["layers"])}

	def promote(self, image, tag, revision):
		if not TAG_RE.fullmatch(tag):
			raise ValueError("Release tag must be vX.Y.Z")
		existing = self.existing(tag)
		if existing and existing != image:
			raise ValueError("An existing release tag cannot be reassigned to another image")
		content = self.inspect(image, revision)
		if not existing:
			try:
				with self.open_url(self.request("manifests/" + tag, method="PUT",
					data=content["body"], content_type=content["media_type"]), timeout=30):
					pass
			except urllib.error.HTTPError as error:
				error.close()
				raise RuntimeError("Cannot promote verified manifest: HTTP " + str(error.code)) from None
		if self.existing(tag) != image:
			raise ValueError("Promoted release digest differs from the verified image")
		return image


def existing_image(tag, actor, token, open_url=safe_urlopen):
	return Registry(actor, token, open_url).existing(tag)


if __name__ == "__main__":
	try:
		print(existing_image(sys.argv[1], os.environ["GITHUB_ACTOR"], os.environ["GITHUB_TOKEN"]) or "")
	except (OSError, ValueError, KeyError, IndexError, RuntimeError) as error:
		# Do not print request headers, credentials, or response bodies.
		sys.exit("Release lookup failed: " +
			(str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__))
