# Unified Compose Implementation Plan

**Goal:** Run ERP and independent AI services in one Compose project locally and in production.

**Architecture:** Root Compose includes the AI fragment with its own environment; local AI volumes retain their original names. Production enables digest-pinned AI services only by opt-in and runs explicit migrations; ERP restore leaves AI stopped.

**Validation:** Check local and production Compose configurations, credential preservation, production lifecycle and restore behavior, and the deployment unit suite.

**Completed:** Root, devcontainer and AI compatibility entry points resolve to the same 11-service local project. Existing local AI containers were moved to `kanzlei-erp` using the original external volumes; schema revision and durable table counts matched the pre-move baseline. API liveness/readiness, worker ping, internal ERP access, and host ERP/MinIO probes passed. Private environment files remained byte-for-byte unchanged. The six stopped legacy AI containers were removed without removing volumes.

Production configuration passed with AI disabled and enabled, with no datastore host ports and a loopback API. All 152 deployment tests, Ruff, shell syntax and diff checks passed. Independent review covered migration ordering, readiness failure, backup failure and atomic restore of current AI credentials. Production was not deployed; AI backups and image publication remain separate work.
