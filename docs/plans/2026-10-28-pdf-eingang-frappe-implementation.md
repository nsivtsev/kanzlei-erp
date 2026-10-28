# PDF Eingang with Frappe UI — Implementation Plan

**Goal:** Implement the approved single-operator PDF review flow using native Frappe controls and existing guarded services.

**Architecture:** Keep WorkspaceClient, ReviewClient, revision guards and frozen mutation intents. Add a small pure flow projection, replace the Page presentation with an inbox and three review steps, and use `frappe.ui.form.make_control`, Page actions and Dialog. Store protected drafts in memory only. Keep uploads and corrections on existing services.

**Tech Stack:** Frappe v16 Page, native form controls, scoped CSS variables, Node test runner, Frappe integration tests.

## 1. Flow and queue grouping

- Add behavioral tests in `tests/posteingang-flow.test.cjs`: forward-step validation, required reasons, pending actions, external versus internal dates, zero-action decision.
- Add a grouped-queue test in `kanzlei_erp/tests/test_posteingang_workspace.py`: ready documents and running analysis must not overlap; retain existing permission boundaries.
- Run the tests to observe missing behavior, then implement flow helpers in `kanzlei_erp/public/js/posteingang_workspace.bundle.js` and grouped status selection in `kanzlei_erp/posteingang_workspace.py`.

## 2. Frappe inbox and guided review

- Replace layout in `kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js` and `.css`.
- Inbox: work views, compact rows, native search/sort controls, explicit error versus empty state, existing pagination.
- Review: protected PDF beside a three-step editor, native Date/Data/Small Text/Select/Check controls, contextual assignment/rejection/scheduling reasons.
- Reuse current source-changing guards, matching, analysis, explicit save, unknown-outcome recovery, conflict comparison and administrative correction.
- Add page-level behavior tests for save-before-advance and confirmation guards.

## 3. Summary and completion

- Show exact accepted/rejected actions and reasons before confirmation; remove redundant confirmation preview dialog.
- Keep explicit human verification and retained-analysis decision. Confirm only after validation.
- Show server receipt links and explicit next-document action without automatic navigation.

## 4. Localization and documentation

- Add all new user-facing strings to `kanzlei_erp/translations/de.csv` and `ru.csv`.
- Update `docs/posteingang.md` to describe the implemented flow and preserve operational caveats.

## 5. Acceptance and review

- Run `node --test tests/posteingang-flow.test.cjs tests/posteingang.test.cjs tests/posteingang-review.test.cjs tests/posteingang-matching.test.cjs tests/posteingang-jobs.test.cjs tests/posteingang-intake.test.cjs`.
- Run the Frappe workspace integration module on a marked local test site. Queue filtering warrants backend validation because it must preserve per-document access checks and pagination.
- Exercise one real Frappe browser flow with synthetic data, plus German/Russian strings and 375px/desktop layout as applicable.
- Request an independent code review per the requesting-code-review skill, fix actionable findings, rerun affected checks.
- Leave the checkout reviewable; the task owner creates the PR and deploys.

## Implemented and verified — 28.10.2026

- Native Frappe Page/form controls and Dialog, work views, three-step review, contextual fields, explicit save boundaries, final summary and permission-filtered receipt links implemented.
- 54 JS tests pass across flow, workspace, review, matching, analysis and intake. Regression cases cover initial control values, date change before save, first dialog search, loading state, deferred planning and hidden Task references.
- Workspace integration suite on fresh marked `posteingang-ap10-ux.localhost`: 17 pass, 1 S3-specific skip. Additional receipt-date contract test passes. The first run on populated `posteingang-ap10-final.localhost` failed three first-page assertions because that site already held more than 20 letters; existing records were preserved. An overlapping seed/test run caused a transient Contact transaction conflict; the date test passed when run separately.
- Application asset build passes. No image build, migrations, backup/restore acceptance or release pipeline changes.
- Real Frappe browser: synthetic letter → explicit Customer reason → document save → accepted task → assignee → typed internal date → summary/human check → created Task → protected source. A second manual letter completes without tasks and with a reason.
- German UI exercised at desktop and 375px; Russian queue/review/result inspected at desktop/1024px. No horizontal overflow observed in the workspace at 375px and 1024px. Native calendar selection and keyboard date editing verified; detached datepickers are disposed when switching steps.
- Independent code review findings fixed: protected reference refresh in summary, receipt action metadata join, task count independent of Task read permission, and receipt date separate from upload time.
- Live external AI calls, S3 and administrative correction browser flow were not exercised; those services were not changed.
- Work remains uncommitted on `feature/pdf-eingang-frappe-ux`; the task owner creates the PR and deploys.
