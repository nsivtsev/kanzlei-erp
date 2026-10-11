#!/usr/bin/env python3
"""Resolve an existing release digest without treating registry errors as absence."""

import base64
import json
import os
import re
import sys
import urllib.error
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


def existing_image(tag, actor, token, open_url=urllib.request.urlopen):
	if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
		raise ValueError("Release tag must be vX.Y.Z")
	basic = base64.b64encode((actor + ":" + token).encode()).decode()
	request = urllib.request.Request(
		"https://ghcr.io/token?service=ghcr.io&scope=repository%3Ansivtsev%2Fkanzlei-erp%3Apull",
		headers={"Authorization": "Basic " + basic},
	)
	try:
		with open_url(request, timeout=30) as response:
			bearer = json.load(response)["token"]
	except urllib.error.HTTPError as error:
		if error.code != 403:
			raise RuntimeError("GHCR authentication failed: HTTP " + str(error.code)) from None
		# GHCR can deny a pull scope before the package has ever been created.
		# Confirm package absence using the authenticated GitHub API, never infer
		# it from a network failure or an ordinary registry permission error.
		package = urllib.request.Request(
			"https://api.github.com/users/nsivtsev/packages/container/kanzlei-erp",
			headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"},
		)
		try:
			with open_url(package, timeout=30):
				pass
		except urllib.error.HTTPError as api_error:
			if api_error.code == 404:
				return None
			raise RuntimeError("Cannot verify GHCR package access: HTTP " + str(api_error.code)) from None
		raise RuntimeError("GHCR package exists but this workflow cannot read it")
	manifest = urllib.request.Request(
		"https://ghcr.io/v2/nsivtsev/kanzlei-erp/manifests/" + tag,
		headers={"Authorization": "Bearer " + bearer, "Accept": ACCEPT},
		method="HEAD",
	)
	try:
		with open_url(manifest, timeout=30) as response:
			digest = response.headers.get("Docker-Content-Digest", "")
	except urllib.error.HTTPError as error:
		if error.code == 404:
			return None
		raise RuntimeError("Cannot resolve release manifest: HTTP " + str(error.code)) from None
	if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
		raise RuntimeError("Registry returned an invalid digest")
	return IMAGE + "@" + digest


if __name__ == "__main__":
	try:
		print(existing_image(sys.argv[1], os.environ["GITHUB_ACTOR"], os.environ["GITHUB_TOKEN"]) or "")
	except (OSError, ValueError, KeyError, IndexError, RuntimeError) as error:
		# Do not print request headers, credentials, or response bodies.
		sys.exit(
			"Release lookup failed: "
			+ (str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__)
		)
