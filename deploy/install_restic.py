#!/usr/bin/env python3
"""Install the pinned official Restic binary after verifying its SHA-256."""

import argparse
import bz2
import hashlib
import json
import os
import platform
import tempfile
import urllib.request
from pathlib import Path


def install(destination):
	lock = json.loads(Path(__file__).with_name("restic-release.json").read_text())
	machine = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
	asset = platform.system().lower() + "_" + str(machine)
	if asset not in lock["assets"]:
		raise ValueError("No verified Restic binary pinned for " + asset)
	filename = "restic_" + lock["version"] + "_" + asset + ".bz2"
	url = "https://github.com/restic/restic/releases/download/v" + lock["version"] + "/" + filename
	with urllib.request.urlopen(url, timeout=60) as response:
		compressed = response.read()
	if hashlib.sha256(compressed).hexdigest() != lock["assets"][asset]:
		raise ValueError("Official Restic download checksum mismatch; nothing installed")
	destination = Path(destination)
	destination.parent.mkdir(parents=True, exist_ok=True)
	fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".restic-")
	try:
		with os.fdopen(fd, "wb") as handle:
			handle.write(bz2.decompress(compressed))
			handle.flush()
			os.fsync(handle.fileno())
		os.chmod(temporary, 0o755)
		os.replace(temporary, destination)
	finally:
		if os.path.exists(temporary):
			os.unlink(temporary)
	print("Installed verified Restic " + lock["version"])


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--destination", type=Path, default=Path("/usr/local/bin/restic"))
	install(parser.parse_args().destination)
