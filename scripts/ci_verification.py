"""Small, versioned receipts binding a tested image to a successful main CI run."""

import json
import re
from pathlib import Path

REPOSITORY = "nsivtsev/kanzlei-erp"
IMAGE = "ghcr.io/" + REPOSITORY
SHA_RE = re.compile(r"[a-f0-9]{40}\Z")
IMAGE_RE = re.compile(re.escape(IMAGE) + r"@sha256:[a-f0-9]{64}\Z")
SUITES = ("deployment", "smoke")
LEGACY_SUITES = ("deployment", "application", "upgrade")


def validate_receipt(receipt, run, revision):
    if not SHA_RE.fullmatch(revision):
        raise ValueError("Expected a full Git commit SHA")
    if (run.get("event"), run.get("head_branch"), run.get("head_sha"), run.get("status"),
            run.get("conclusion"), run.get("path"), run.get("head_repository", {}).get("full_name")) != (
            "push", "main", revision, "completed", "success", ".github/workflows/ci.yml", REPOSITORY):
        raise ValueError("Receipt must come from a successful CI push on this repository's main branch")
    version = receipt.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        raise ValueError("Unknown verification receipt version")
    if (receipt.get("revision"), receipt.get("platform"),
            receipt.get("run_id"), receipt.get("run_attempt")) != (
            revision, "linux/amd64", run["id"], run["run_attempt"]):
        raise ValueError("Verification receipt does not match the requested commit and CI attempt")
    required = LEGACY_SUITES if version == 1 else SUITES
    if receipt.get("suites") != {name: "passed" for name in required}:
        raise ValueError("All mandatory verification scenarios must pass")
    image = receipt.get("image", "")
    if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
        raise ValueError("Verification receipt must reference an immutable Kanzlei image digest")
    return image


def make_receipt(revision, image, run_id, run_attempt, metrics):
    if not SHA_RE.fullmatch(revision) or not IMAGE_RE.fullmatch(image):
        raise ValueError("Invalid revision or immutable image reference")
    results = {}
    for name in ("smoke",):
        path = Path(metrics) / (name + ".json")
        if not path.is_file():
            raise ValueError("Mandatory scenario result is missing: " + name)
        result = json.loads(path.read_text())
        if result.get("name") != name or result.get("exit_code") != 0:
            raise ValueError("Scenario did not pass: " + name)
        results[name] = result
    return {
        "schema_version": 2, "revision": revision, "image": image, "platform": "linux/amd64",
        "run_id": int(run_id), "run_attempt": int(run_attempt),
        "suites": {name: "passed" for name in SUITES}, "metrics": results,
    }
