# CI/CD optimization verification — 2026-10-14

The changes retain all acceptance checks and publish only the exact image verified
on main. Runtime behavior and pipeline coordination were reviewed independently.
The default GitHub scenario concurrency remains 1 until a standard-runner comparison
confirms the benefit of 2. Existing README/email-document edits were excluded.

## Image comparison

Both Dockerfiles were built from the same application source for linux/amd64 using
the local classic Docker store on an ARM host with emulation. Docker-reported sizes:

| Image | Bytes | Decimal GB |
| --- | ---: | ---: |
| Original Dockerfile from 5fb44bf | 7,934,852,830 | 7.93 |
| Optimized Dockerfile | 5,388,742,840 | 5.39 |

Reduction: 2,546,109,990 bytes (32.1%). The Suite installation layer decreased from
4.59 GB to 2.04 GB. Yarn/pip/uv download caches are absent; socket.io-client and Python
imports still work. A changed REVISION reused the Suite layer. GitHub containerd
metrics use Docker-reported packed size; that value is not directly comparable to
the classic store's uncompressed size.

## Acceptance and timings

Full disposable application/HTTP/Drive/Socket.IO and database/public/private files
backup/restore passed, as did legacy Suite upgrade and interrupted-install replay.
Both modes executed 224 integration + 21 unit application results (245 total), plus
root JavaScript and HTTP/realtime acceptance checks.

| Mode | Application seconds | Upgrade seconds | Approximate wall seconds |
| --- | ---: | ---: | ---: |
| Serial | 586.100 | 172.966 | 759.1 |
| Parallel, after startup ordering fix | 581.365 | 193.423 | 581.4 |

This local wall-time reduction is 23.4%; it is not a GitHub-runner benchmark.
An initial parallel run exposed Nginx caching the backend's pre-restore IP and
returning HTTP 502. Frontend now depends on started backend/websocket endpoints.
Three additional Docker stop/start cycles with deliberately changed upstream IPs
passed, followed by the full successful parallel acceptance run.

## Host and protocol checks

- 68 deployment/CI/release tests passed on the final code, including origin/attempt
  binding of receipts, failed/incomplete CI, immutable tags, plain OCI manifests and
  indexes, exact local image identity for classic/containerd stores, duplicate
  deployment, legacy preflight, and successive cancellation signals with cleanup.
- Shell syntax, ShellCheck, actionlint (with the existing queue compatibility ignore),
  Python 3.9 syntax compatibility, correctness lint and git diff checks passed.
- Real disposable OCI registry promotion preserved the root manifest digest and
  repeated promotion reused it. The temporary registry was removed.
- Real GitHub artifact download verified redirects without forwarding authorization,
  verified the archive digest and rejected an archive that was not a receipt.
- The Docker 28 containerd image-ID behavior was checked against Moby's pinned
  v28.0.4 ImageInspect implementation: Descriptor/Id identify the root manifest;
  the classic store identifies the platform configuration. Both paths are tested.

Production was not deployed or pruned. GitHub execution of the new workflow requires
pushing the branch with workflow-authorized credentials; release publication and
automatic continuation activate when the new CI/controller workflows reach main.
