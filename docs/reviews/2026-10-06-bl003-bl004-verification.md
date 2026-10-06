# BL-003/004 verification — 6 October 2026

BL-003 source profiles and BL-004 period completeness checklists are implemented and accepted against the requested workflow.

The Customer **Datenquellen** table stores one row per source with a stable ID, category, optional service, active dates, and note. A new FiBu package copies its applicable sources and clips expected dates to the accounting period. An unconfirmed profile remains uninitialized; a confirmed empty profile is distinct. Later Customer edits do not rewrite saved package snapshots.

FiBu packages and supplements have independent checklist, evidence, and append-only event tables. Checklist changes use version-checked server methods that validate permissions, open status, evidence ownership, coverage, and required reasons. A confirmed or reviewed status requires evidence dates to cover the expected interval without gaps. Checklist gaps remain advisory and do not block Ready or closure.

The browser acceptance used a synthetic Customer with two bank accounts and no cash-register source. A September package showed each account separately, both expected from 1–30 September, with no evidence for either. The second account was marked partially received for 1–14 September, then fully received with a contiguous 15–30 September link, and finally reviewed. The summary then showed one missing source and one reviewed source. The first account remained Expected with no evidence. The period moved from Collection through Review to Ready while that missing source stayed visible. Event history showed the reasons, author, and timestamps. No real customer files or documents were used.

The integration suite additionally verifies that changing a Customer source profile does not rewrite an existing package snapshot; evidence access is restricted to the same Mandant; direct checklist-table edits, stale versions, invalid coverage, and invalid history are rejected; supplements start empty and leave their parent package unchanged; and incomplete checklists do not block Ready or closure.

## Verification

- `bench --site development.localhost migrate` passed twice on the local Docker bench.
- `bench build --app kanzlei_erp` passed.
- `kanzlei_erp.tests.test_fibu_checklist_integration`: 13 tests passed, including the browser-serialized history regression and incomplete-checklist Ready test.
- The full app suite ran 81 categorized and 28 uncategorized tests. All BL-003/004 tests passed, as did the uncategorized tests. Three existing navigation tests failed because the local site is set to Russian while those assertions expect German labels: `test_admin_settings_includes_standard_email_configuration`, `test_email_inbox_is_visible_to_users_with_a_user_email_assignment`, and `test_sidebar_focuses_on_daily_work`.
- The browser acceptance exposed a serialization edge case: Frappe sends existing history timestamps as strings, while server-loaded rows use `datetime` values. Both checklist and workflow history comparisons now canonicalize timestamps; regression coverage passes.

The repository remains uncommitted for review. The three locale-dependent full-suite failures are unrelated to BL-003/004 behavior and do not affect the acceptance result.
