# Kanzlei Navigation Implementation Plan

**Goal:** Make Mandants, work, time tracking, and billing available through one focused Kanzlei interface.

**Architecture:** Ship Frappe v16 Workspace Sidebar and Desktop Icon documents in the Kanzlei app. Use a boot-session hook to expose the approved navigation, retain ERPNext document permissions, and load terminology translations without editing ERPNext.

**Tech Stack:** Frappe/ERPNext v16, Python, JSON, CSV, Docker Compose.

## Task 1: Navigation and boot

Files: `kanzlei_erp/tests/test_navigation.py`, `kanzlei_erp/navigation.py`, `kanzlei_erp/hooks.py`, `kanzlei_erp/workspace_sidebar/kanzlei.json`, `kanzlei_erp/workspace_sidebar/einstellungen.json`, and `kanzlei_erp/desktop_icon/*.json`.

1. Add failing integration tests for the six real navigation targets, filtered Desk boot output, and settings visible only to administrators.
2. Run `bench --site development.localhost run-tests --module kanzlei_erp.tests.test_navigation` in the Frappe container and verify the expected failures.
3. Add standard sidebar/icon documents and the supported `boot_session` hook. Do not modify standard ERPNext documents.
4. Migrate the development site, rerun tests, and verify the actual boot output.

## Task 2: Terminology

Files: `kanzlei_erp/translations/en.csv`, `kanzlei_erp/translations/de.csv`, `kanzlei_erp/translations/ru.csv`, and `kanzlei_erp/tests/test_navigation.py`.

1. Verify Customer terminology tests fail before translations exist.
2. Add user-facing Mandant terms and consistent titles for the six sections.
3. Clear caches and verify list/form/Link field terminology in the browser.

## Task 3: Verification and documentation

Files: `README.md` and `docs/production.md`.

1. Document activation on installation/migration and the difference between navigation and permissions.
2. Run app tests, Python lint, and Git diff checks.
3. Open Mandants, a Mandant form, Timesheet, Sales Invoice, and Task calendar in the local browser; verify the sidebar stays Kanzlei.
4. Save a screenshot and commit the verified change.
