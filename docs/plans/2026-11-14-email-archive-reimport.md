# Email archive reimport implementation plan

**Goal:** Reimport the complete supported IMAP archive from each saved Email Account and display durable, live progress.

**Approved design:** The user chose a button on each Email Account rather than a separate settings page. Reuse the existing idempotent archive importer, including attachment repair and contact linking. Import INBOX, Sent and supported archive folders; retain the existing exclusions for drafts, spam and trash. Show account-specific totals, folder progress and errors in a dialog that can be reopened after navigation or reload. No deletion or outbound sending is introduced.

**Architecture:** Add an administrator-controlled background run with durable database state. An Email Account receiver extension and the worker share an account-scoped Redis lock, so archive import pauses receiving without modifying account configuration or affecting Sent-folder appends. Release the lock on success/failure; recover interrupted workers by verifying queue state and releasing only that run's lock token. Existing console use retains its requirement to disable Incoming. Add optional progress reporting to the existing importer, with exact initial folder UID inventories and committed batch updates.

**Tech stack:** Frappe v16, Python, RQ/Redis, standard Desk dialog and JavaScript, DE/RU translations.

## Tasks

1. Write failing tests for account authorization, duplicate starts, run recovery, receiver exclusion and progress inventory. Run targeted Python tests; implement the run DocType, API and worker, plus importer progress callbacks. Preserve console compatibility and attachment verification.
2. Write failing UI tests for saved/dirty accounts, account-specific progress, terminal states and polling cleanup. Implement the Email Account form hook and dialog with accessible progress, per-folder counters, aliases and clear error/retry state. Register the hook and translations.
3. Add Frappe integration tests for actual database transactions and the existing FakeIMAP importer. Run affected email archive/history, receiver and form checks. Exercise a short browser scenario: start synthetic import, observe progress, reopen and verify a duplicate-free second pass. Include DE/RU and a narrow viewport for new translated UI.
4. Document the action and its operation. Review the diff for permissions, race conditions, interrupted jobs and unrelated changes. Report passing checks and any unavailable verification. The task owner creates the PR and deploys.

**Verification scope:** New job persistence and shared receiver exclusion warrant integration checks of permissions, duplicate runs, timeout/recovery and existing email/archive attachment behavior. No full CI image build or backup/restore acceptance is planned.

## Result

Implemented on the current checkout. Targeted Frappe checks passed: 13 archive tests, 11 background-run tests and 13 email-history tests; nine related JavaScript tests and Ruff passed. Independent transactions cover concurrent starts under an old snapshot; timeout and Redis release failures have regression coverage. A browser scenario using a temporary loopback STARTTLS mailbox imported three synthetic messages, then reused all three on the next pass and restored its state after reload. DE/RU and a 375px viewport were checked. The fixture account, messages, runs and checkpoint were removed and the original local language restored. Real provider connectivity and a production-size archive were not exercised; deployment remains with the task owner.
