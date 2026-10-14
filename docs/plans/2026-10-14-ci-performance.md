# CI/CD performance implementation plan

Goal: reduce PR/release wait and runner minutes while retaining every existing
acceptance check. Work in the current branch. No paid or self-hosted runner changes.

1. Remove package download caches in their creation layers. Keep runtime dependencies.
   Make dependency cache independent of commit metadata; use GHA v2 main-only exports
   and the Docker driver with containerd on ephemeral GitHub runners.
2. Split application/restore and legacy Suite upgrade into isolated scenarios on one
   runner, defaulting to serial until a GitHub benchmark confirms parallel savings.
   Record timings and ensure cancellation performs scenario cleanup. Order frontend
   startup after backend/websocket to avoid stale Nginx DNS after restore.
3. Main publishes the exact tested SHA image and a versioned verification receipt.
   Release validates the exact successful main run and digest, copies the manifest,
   and deploys matching configuration without rebuilding or repeating integration CI.
4. Continue early tags by workflow_run → workflow_dispatch, with no polling runner.
   Preserve immutable tags, fail-closed registry errors, and legacy retry behavior.
   Duplicate healthy deployments do not repeat backup/migration; pending migrations
   retain existing recovery guards. Report disk/inode capacity before pulling.
5. Validate behavior with host tests, actionlint/ShellCheck, actual disposable Docker
   acceptance, cache/provenance comparisons and image size measurements. Do not deploy
   production or prune its data as part of local validation.

Targets on warmed GitHub cache: PR ≤10 min; main+publish ≤12 min; ready release ≤7 min;
aggregate runner minutes at least 30% lower. Measure queue time independently.
