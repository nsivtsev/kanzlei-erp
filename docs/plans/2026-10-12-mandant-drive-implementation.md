# Mandant Drive folders implementation plan

> Execute sequentially in this session using test-driven-development and
> verification-before-completion; request independent code review before delivery.

**Goal:** Open one private Drive folder from each Mandant and enforce current
Customer permissions on its whole subtree.

**Architecture:** Customer stores its root File ID; managed Files inherit an
immutable Customer link. A pinned Suite patch calls Kanzlei's access provider
before owner/admin/share shortcuts, also in path and WebDAV resolution.

**Tech Stack:** Frappe 16, ERPNext 16, pinned Suite, Python, Desk JavaScript.

## Task 1: Prove the backend contract

Create `kanzlei_erp/tests/test_mandant_drive.py`. Exercise real Customer shares
and native Suite creation/listing APIs as Administrator and ordinary users.
First run must fail because the folder service does not exist. Cover idempotent
creation, inheritance, read/write separation, immediate revocation, owner and
Suite Admin bypass, immutable links, root protection, boundary moves and ORM
listing. Include native File permission and WebDAV batch resolution checks.

Command (from repo):

```sh
docker compose --env-file .env -f .devcontainer/docker-compose.yml exec -T frappe bash -lc 'cd /workspace/development/frappe-bench && bench --site development.localhost run-tests --module kanzlei_erp.tests.test_mandant_drive'
```

## Task 2: Implement access and storage integration

Create `kanzlei_erp/mandant_drive.py`, add hidden read-only no-copy fields to
`fixtures/custom_field.json`, and register document events/access hooks in
`hooks.py`. Extend `suite_file.py` to protect the managed root and reject moves
across Customer boundaries before any disk operation.

Store the Suite access extension as `deploy/suite-mandant-access.patch` and
apply it idempotently from `scripts/fetch-suite.sh`. Copy the patch in
`deploy/Dockerfile` and allow it in `.dockerignore`. Verify the exact pinned
Suite commit and fail for unexpected source conflicts. Patch central access,
upward path/breadcrumb resolution, ORM criterion and WebDAV batch shortcuts.

Use a trusted Python ContextVar only around root creation, row-lock Customer,
create a private native Drive folder, set ownership to Administrator and store
the stable mapping. New children inherit Customer from their parent in
before_validate. Prevent client mutation of both mapping fields, cross-boundary
moves, root trash/deletion and external shares on managed items.

Migrate the local site and rerun Task 1 until all cases pass. Add focused
regression cases for issues found, run red/green before repairing.

## Task 3: Customer action and verification

Add a small Customer action in `public/js/customer.js`; saved staff forms query
the folder state, offer create only to Customer writers, then open the existing
folder in a new tab. Ignore stale responses after form navigation. Include
German and Russian translations, tests in `tests/mandant-drive.test.cjs`, and
user documentation in README plus a verification report.

Run the new Node test red/green, Frappe tests for Drive integration, navigation
and Mandant documents, existing Node tests, deployment unit tests, shell syntax,
ruff and the Kanzlei asset build. Request a reviewer focused on permissions and
storage boundaries, address material findings and repeat affected checks.

Deliver local implementation and validation evidence; no production deploy.
