# Local ERPNext Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Run a local ERPNext development instance that supports future custom app development and a separate production deployment.

**Architecture:** Use the official Frappe Docker development service layout with an ARM64 bench container, MariaDB, and Redis. Keep configuration in this repository, bench data and secrets outside version control, and document production as a separate built-image deployment.

**Tech Stack:** Docker Compose, Frappe Bench, ERPNext `version-16`, MariaDB, Redis.

---

### Task 1: Repository and upstream configuration

**Files:** `.gitignore`, `.devcontainer/docker-compose.yml`, `README.md`

1. Initialize Git in the empty project and save the approved design.
2. Inspect and adapt the current official Frappe Docker development Compose example.
3. Bind development ports to loopback, select native ARM64 images, and preserve bench data.
4. Validate the Compose file with `docker compose config`.

### Task 2: Local instance

**Files:** `README.md`, local ignored bench and secrets files

1. Start the Compose services and verify their status.
2. Initialize a `version-16` bench and install ERPNext on `development.localhost`.
3. Enable developer mode and start the Frappe development server.
4. Verify `list-apps` and the HTTP login page on port 8100.

### Task 3: Production path

**Files:** `README.md`, `docs/production.md`

1. Document custom image creation with pinned app versions.
2. Document the deployment inputs needed later: host, domain, TLS, secrets, and backups.
3. Verify instructions against the official Frappe Docker production documentation.
