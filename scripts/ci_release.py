#!/usr/bin/env python3
"""Publish verification receipts and promote the exact image tested on main."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from ci_verification import IMAGE, REPOSITORY, SHA_RE, make_receipt, validate_receipt

spec = importlib.util.spec_from_file_location("release_image", Path(__file__).with_name("release-image.py"))
registry_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry_module)
TAG_RE = registry_module.TAG_RE


def read_receipt_archive(body):
    if len(body) > 1024 * 1024:
        raise ValueError("Verification artifact is too large")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        if archive.namelist() != ["verified-image.json"]:
            raise ValueError("Verification artifact must contain only verified-image.json")
        if archive.getinfo("verified-image.json").file_size > 64 * 1024:
            raise ValueError("Verification receipt is too large")
        receipt = json.loads(archive.read("verified-image.json"))
    if not isinstance(receipt, dict):
        raise ValueError("Verification receipt must be an object")
    return receipt


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, path, data=None):
        return urllib.request.Request("https://api.github.com/repos/" + REPOSITORY + "/" + path,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"},
            data=json.dumps(data).encode() if data is not None else None)

    def api(self, path, data=None):
        try:
            with urllib.request.urlopen(self.request(path, data), timeout=30) as response:
                body = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            raise RuntimeError("GitHub API failed: HTTP " + str(error.code)) from None
        if len(body) > 1024 * 1024:
            raise ValueError("GitHub API response is too large")
        return json.loads(body) if body else None

    def run(self, run_id):
        return self.api("actions/runs/" + str(int(run_id)))

    def runs(self, revision):
        if not SHA_RE.fullmatch(revision):
            raise ValueError("Expected a full commit SHA")
        # A single commit normally has one push run; pagination also covers reruns
        # and repeated pushes without silently overlooking a successful run.
        results = []
        for page in range(1, 11):
            path = ("actions/workflows/ci.yml/runs?branch=main&event=push&head_sha=" + revision
                    + "&per_page=100&page=" + str(page))
            batch = self.api(path)["workflow_runs"]
            results.extend(run for run in batch if run.get("head_sha") == revision)
            if len(batch) < 100:
                return results
        raise ValueError("Too many CI runs to resolve safely")

    def receipt(self, run, revision):
        artifacts = self.api("actions/runs/" + str(run["id"]) + "/artifacts?per_page=100")["artifacts"]
        found = [item for item in artifacts if item["name"] == "verified-image-" + revision and not item["expired"]]
        if len(found) != 1:
            raise ValueError("Verification receipt is absent or expired; rerun CI for commit " + revision)
        item = found[0]
        request = self.request("actions/artifacts/" + str(int(item["id"])) + "/zip")
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(request, timeout=30) as response:
                body = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308):
                raise RuntimeError("Artifact download failed: HTTP " + str(error.code)) from None
            location = error.headers.get("Location", "")
            error.close()
            url = urllib.parse.urlsplit(location)
            host = url.hostname or ""
            if url.scheme != "https" or url.username or url.password or not any(
                    host.endswith(suffix) for suffix in (".blob.core.windows.net", ".actions.githubusercontent.com",
                                                        ".githubusercontent.com")):
                raise ValueError("Unexpected artifact download destination")
            # Signed storage URL: do not forward the GitHub Authorization header.
            with urllib.request.urlopen(location, timeout=30) as response:
                body = response.read(1024 * 1024 + 1)
        expected = item.get("digest")
        if expected and expected != "sha256:" + hashlib.sha256(body).hexdigest():
            raise ValueError("Artifact archive digest mismatch")
        return read_receipt_archive(body)

    def dispatch(self, tag):
        if not TAG_RE.fullmatch(tag):
            raise ValueError("Release tag must be vX.Y.Z")
        self.api("actions/workflows/deploy.yml/dispatches", {"ref": "main", "inputs": {"tag": tag}})


def resolve_release(github, registry, tag, revision):
    if not TAG_RE.fullmatch(tag) or not SHA_RE.fullmatch(revision):
        raise ValueError("Invalid release tag or commit SHA")
    result = {"tag": tag, "revision": revision, "ready": False, "existing": False, "image": ""}
    existing = registry.existing(tag)
    if existing:
        registry.inspect(existing, revision)
        return {**result, "ready": True, "existing": True, "image": existing}
    runs = github.runs(revision)
    successful = [run for run in runs if run.get("status") == "completed" and run.get("conclusion") == "success"]
    if not successful:
        if not runs or any(run.get("status") != "completed" for run in runs):
            return result
        raise ValueError("CI main did not pass for this release commit; rerun its CI before deploying")
    run = successful[0]
    receipt = github.receipt(run, revision)
    image = validate_receipt(receipt, run, revision)
    if registry.existing("sha-" + revision) != image:
        raise ValueError("Candidate digest does not match the successful CI receipt")
    registry.inspect(image, revision)
    return {**result, "ready": True, "image": image, "run_id": run["id"]}


def coordinate(event, github, tags):
    event_run = event["workflow_run"]
    run = github.run(event_run["id"])
    revision = run["head_sha"]
    # Validate both the event and the live API record before consuming an artifact
    # or dispatching a workflow with production privileges.
    for value in (event_run, run):
        if (value.get("event"), value.get("head_branch"), value.get("conclusion"),
                value.get("head_repository", {}).get("full_name"), value.get("head_sha")) != (
                "push", "main", "success", REPOSITORY, revision):
            raise ValueError("Coordinator accepts only successful main CI pushes from this repository")
    releases = sorted((tag for tag in tags if TAG_RE.fullmatch(tag)),
                      key=lambda tag: tuple(map(int, tag[1:].split("."))))
    if releases:
        validate_receipt(github.receipt(run, revision), run, revision)
    for tag in releases:
        github.dispatch(tag)
        print("Queued release " + tag + " after CI " + str(run["id"]), flush=True)
    return releases


def output(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as handle:
            for key, value in values.items():
                if isinstance(value, bool):
                    value = str(value).lower()
                value = str(value)
                if "\n" in value or "\r" in value:
                    raise ValueError("Invalid workflow output")
                handle.write(key + "=" + value + "\n")


def summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as handle:
            handle.write(text + "\n")
    print(text, flush=True)


def publish_receipt(registry, local_image, revision, metrics):
    if (os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("GITHUB_REF"),
            os.environ.get("GITHUB_REPOSITORY")) != ("push", "refs/heads/main", REPOSITORY):
        raise ValueError("Only a main push may publish verified candidate images")
    # Require scenario results before touching the registry.
    receipt = make_receipt(revision, IMAGE + "@sha256:" + "0" * 64,
                           os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], metrics)
    local = json.loads(subprocess.check_output(["docker", "image", "inspect", local_image], text=True))[0]
    if (local["Os"], local["Architecture"], local["Config"]["Labels"].get("org.opencontainers.image.revision")) != (
            "linux", "amd64", revision):
        raise ValueError("Local verified image has an unexpected revision or platform")
    tag = "sha-" + revision
    image = registry.existing(tag)
    started = time.monotonic()
    if not image:
        subprocess.run(["docker", "tag", local_image, IMAGE + ":" + tag], check=True)
        subprocess.run(["docker", "push", IMAGE + ":" + tag], check=True)
        image = registry.existing(tag)
    if not image:
        raise ValueError("Published candidate cannot be resolved")
    info = registry.inspect(image, revision)
    # Engine 28's containerd store identifies an image by its root descriptor;
    # the classic store identifies it by the selected platform's config digest.
    descriptor = local.get("Descriptor") or {}
    exact_image = (descriptor["digest"] == image.split("@", 1)[1]) if descriptor.get("digest") else (
        info["config_digest"] == local["Id"])
    if not exact_image:
        raise ValueError("Published candidate is not the exact locally verified image")
    receipt["image"] = image
    receipt["metrics"]["image"] = {"docker_reported_bytes": local["Size"],
        "compressed_bytes": info["compressed_bytes"], "publish_seconds": round(time.monotonic() - started, 3)}
    destination = Path(metrics) / "verified-image.json"
    destination.write_text(json.dumps(receipt, indent=2) + "\n")
    output({"image": image, "receipt": str(destination)})
    summary(f"Verified main image: `{image}`\n\nCommit: `{revision}`\n\nCI: "
            f"https://github.com/{REPOSITORY}/actions/runs/{receipt['run_id']}\n\n"
            f"Compressed layers: {info['compressed_bytes'] / 1024**2:.0f} MiB")


def report(local_image, metrics):
    rows = []
    for name in ("application", "upgrade"):
        path = Path(metrics) / (name + ".json")
        if path.exists():
            data = json.loads(path.read_text())
            rows.append(f"| {name} | {data['exit_code']} | {data['seconds']:.1f} |")
    summary("### CI scenarios\n\n| Scenario | Exit | Seconds |\n|---|---:|---:|\n" + "\n".join(rows))
    inspected = subprocess.run(["docker", "image", "inspect", local_image], text=True, capture_output=True)
    if inspected.returncode == 0:
        image = json.loads(inspected.stdout)[0]
        size = image["Size"]
        summary(f"\nLocal image: {size / 1024**2:.0f} MiB; layers: {len(image['RootFS']['Layers'])}")
        (Path(metrics) / "image.json").write_text(json.dumps({"docker_reported_bytes": size,
                                                            "layer_count": len(image["RootFS"]["Layers"])}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    candidate = sub.add_parser("candidate")
    candidate.add_argument("revision")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("tag")
    promote = sub.add_parser("promote")
    promote.add_argument("tag")
    promote.add_argument("revision")
    promote.add_argument("image")
    publish = sub.add_parser("publish")
    publish.add_argument("local_image")
    publish.add_argument("revision")
    publish.add_argument("metrics", type=Path)
    sub.add_parser("coordinate")
    reporting = sub.add_parser("report")
    reporting.add_argument("local_image")
    reporting.add_argument("metrics", type=Path)
    args = parser.parse_args()
    if args.command == "report":
        args.metrics.mkdir(parents=True, exist_ok=True)
        report(args.local_image, args.metrics)
        return
    github = GitHub(os.environ["GITHUB_TOKEN"])
    registry = registry_module.Registry(os.environ["GITHUB_ACTOR"], os.environ["GITHUB_TOKEN"])
    if args.command == "candidate":
        if not SHA_RE.fullmatch(args.revision):
            raise ValueError("Invalid candidate revision")
        image = registry.existing("sha-" + args.revision)
        if image:
            registry.inspect(image, args.revision)
        output({"image": image or ""})
    elif args.command == "resolve":
        if not TAG_RE.fullmatch(args.tag):
            raise ValueError("Release tag must be vX.Y.Z")
        revision = subprocess.check_output(["git", "rev-parse", "refs/tags/" + args.tag + "^{commit}"], text=True).strip()
        subprocess.run(["git", "merge-base", "--is-ancestor", revision, "origin/main"], check=True)
        result = resolve_release(github, registry, args.tag, revision)
        output(result)
        if not result["ready"]:
            summary(f"Release `{args.tag}` is waiting for CI main at `{revision}`. "
                    "The coordinator will continue automatically after CI succeeds.\n\n"
                    f"https://github.com/{REPOSITORY}/actions?query=workflow%3ACI+branch%3Amain")
    elif args.command == "promote":
        image = registry.promote(args.image, args.tag, args.revision)
        output({"image": image})
        summary(f"### Verified release\n\nTag: `{args.tag}`\n\nCommit: `{args.revision}`\n\nImage: `{image}`")
    elif args.command == "publish":
        publish_receipt(registry, args.local_image, args.revision, args.metrics)
    elif args.command == "coordinate":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        revision = event["workflow_run"]["head_sha"]
        if not SHA_RE.fullmatch(revision):
            raise ValueError("Invalid CI commit SHA")
        tags = subprocess.check_output(["git", "tag", "--points-at", revision], text=True).splitlines()
        releases = coordinate(event, github, tags)
        summary("Ready releases: " + (", ".join(releases) or "none"))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError,
            zipfile.BadZipFile) as error:
        # Never expose signed URLs, authorization headers or HTTP response bodies.
        sys.exit("CI release failed: " + (str(error) if isinstance(error, (ValueError, RuntimeError))
                                        else type(error).__name__))
