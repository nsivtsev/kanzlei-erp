#!/usr/bin/env python3
"""Read-only SSH preflight, also usable with deployment bundles predating this CI."""

import argparse
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def check_current(root, image, revision, tag, *, health):
    root = Path(root)
    current = root / "shared/current.json"
    if not current.exists() or (root / "shared/pending.json").exists():
        return False
    state = json.loads(current.read_text())
    if (state.get("image"), state.get("revision"), state.get("tag")) != (image, revision, tag):
        return False
    health(state)
    return True


def storage_diagnostics(root):
    docker_root = subprocess.check_output(["docker", "info", "--format", "{{.DockerRootDir}}"], text=True).strip()
    paths = [Path(root), Path(docker_root), Path("/var/lib/containerd")]
    for path in dict.fromkeys(paths):
        if not path.exists():
            continue
        info = os.statvfs(path)
        available = info.f_bavail * info.f_frsize
        print(f"Storage {path}: {available / 1024**3:.1f} GiB free, {info.f_favail} free inodes", flush=True)
        if available == 0 or (info.f_files and info.f_favail == 0):
            raise RuntimeError("No space or inodes available before image pull: " + str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"ghcr\.io/nsivtsev/kanzlei-erp@sha256:[a-f0-9]{64}", args.image) or not re.fullmatch(
            r"[a-f0-9]{40}", args.revision) or not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", args.tag):
        raise ValueError("Invalid release identity")
    root = args.root.resolve()

    def health(state):
        release = Path(state["release"]).resolve()
        release.relative_to(root / "releases")
        spec = importlib.util.spec_from_file_location("deployed_manage", release / "deploy/manage.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = module.read_config(root)
        if state["site"] != config["SITE_NAME"] or state.get("project", config["PROJECT_NAME"]) != config["PROJECT_NAME"]:
            raise ValueError("Current deployment configuration differs from saved state")
        compose = module.Compose(root, release, args.image, module.Commands(), config)
        module.smoke_check(compose)
        compose.running()

    # Match the lock used by archived deployment managers. A pending migration
    # must reach the manager's recovery guard rather than being marked healthy.
    with (root / ".deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another deployment or restore is running") from None
        if check_current(root, args.image, args.revision, args.tag, health=health):
            print("Already deployed and healthy: " + args.tag, flush=True)
            print("SKIP")
            return
        storage_diagnostics(root)
        print("DEPLOY")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit("Deployment preflight failed: " +
                 (str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__))
