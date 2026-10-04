# BL-001 verification — 4 October 2026

BL-001 adds one manually created FiBu Package per Mandant, non-stock service, and calendar period. The package stores explicit period boundaries, assignments, deadlines, private file and Communication references, Tasks and Questions, immutable transfer entries, and a server-recorded closure. Late work goes into sequential FiBu Supplements under the closed package. The first closure remains unchanged, and the package list shows the server-maintained open-supplement indicator.

## Local acceptance

On `development.localhost`, the fictitious `DEMO Schneider e.K.` / `DEMO BL001 FiBu` package `p2mpv2hnj2` was created for September 2026. Moving its internal deadline from 15 to 20 October left the accounting period at **1–30 September**. The package form displayed its Task and Question sections and rejected closure while `TASK-2026-00006` was open. After the Question was cancelled, closure saved the note, author, and timestamp, alongside the earlier transfer-log entry.

Supplement `tctanf6p41` was created from the closed package with sequence **1**, inherited responsibility, its own October deadline, and the reason for a late bank statement. The synthetic `bl001-late-september-demo.txt` was uploaded as a private File and linked in the supplement; the original package retained its empty document table and original transfer entry. Creating `TASK-2026-00007` from the supplement prefilled Mandant, package, supplement, and Question kind. Its open state blocked supplement closure. After cancellation, supplement closure succeeded; the package still showed its original closure and transfer history, and the open-supplement indicator changed from `1` to `0`. The package and supplement remain on the local development site as fictitious review data.

## Automated verification

Two consecutive `bench --site development.localhost migrate` runs completed, followed by `bench build --app kanzlei_erp`. `bench --site development.localhost run-tests --app kanzlei_erp` passed **58 tests** (49 Frappe integration tests and 9 pure unit tests). The tests cover monthly, quarterly, annual, and leap-year boundaries; duplicate creation and copying; the database composite unique index; permissible changes to Mandant, service, or period; Task context and closure guards; private files and immutable transfer history; supplement numbering and closure; Customer-scoped list/direct permissions; and regressions for navigation, email, calendar, and Wiederholungen.

The uniqueness guarantee for concurrent requests is enforced by the database index on `(customer, service, period_start, period_end)` and checked by an integration test inspecting that index. Staff access was verified with Projects User plus Customer-read role permissions and Customer User Permissions in integration tests; appointment as responsible or deputy alone did not expose another Mandant's package, linked Task, supplement, or private File. A separate employee browser session was not provisioned for the local demo.

Closing a package or supplement records completion of its Kanzlei work only. Preparation stages, answer handling, DATEV receipt checks, automated package creation, and deadline reminders remain in their separate backlog items.
