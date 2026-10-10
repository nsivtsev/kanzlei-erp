# Kanzlei work overview acceptance

Date: 2026-10-10. Environment: `development.localhost:8100`, Frappe/ERPNext 16, branch `codex/kanzlei-overview`.

## Delivered behavior

The native Desk page at `/desk/kanzlei-overview` shows overdue/today/next-seven-day counts, paginated team tasks, a deadline agenda and five assigned-mailbox incoming emails. Task and agenda queries share the same filters and preserve standard permissions. Questions, groups, templates and terminal statuses are excluded. Plain-text mail previews are escaped; full HTML and attachment URLs are absent from the home API. No new DocTypes, fields, calendar synchronization or polling were introduced.

Kanzlei sidebar order is Overview, Mail when permitted, Tasks, Calendar, Customers, FiBu periods, Questions, Time recording, administrative Settings when permitted. The application home, empty Desk/App paths and return from Settings target the overview. Explicit form/list/calendar links remain intact. From the root site's Desktop, the Kanzlei icon opens the overview directly, without the former settings folder dialog.

A permission edge discovered in testing was corrected in the existing FiBu query helper: Customer **select** permission for Link fields is not Customer **read** permission. A user with select-only Customer access now gets only readable shares in the FiBu task filter. Existing FiBu permission tests remain green.

## Automated validation

Final logs are retained under `/tmp/overview-*-final.log` inside the Frappe container; the application test runner also writes `logs/frappe.testing.log` in Bench.

- `test_overview`: 13 passing integration tests.
- `test_navigation`: 17 passing integration tests.
- Full application: 132 passing legacy integration tests and 21 passing unspecified-category tests; both categories report **OK**.
- Node: 6 passing tests covering late responses, independent errors, pagination/refresh races, retry/count behavior, blank/deep routes and localStorage repair.
- Current Ruff check of changed Python files and development fixture script: passed.
- `node --check` for changed page/navigation JS: passed.
- `git diff --check`: passed.
- Development database/site files were backed up before migration. Migration and asset build passed; migration was repeated on development, and the targeted migration test executes the patch twice without modifying unrelated preferences.

Tests cover date/year/month and full-Datetime-day boundaries; legacy blank Task kind; ordinary FiBu-linked tasks; unreadable Customer redaction; multi-assignment counts; pagination; mail-only/Website/Guest access; shared-only Task access; mailbox isolation across successive users; mail statuses; safe HTML previews and attachment flags; no mail read/status/flag-queue side effects. Request error and race behavior uses controlled Node promises, rather than slowing or breaking the live server.

## Browser validation

Fictitious `DEMO Übersicht` records were used: 23 Tasks, two assigned employees, one assigned fake mailbox and five incoming messages. Existing DEMO Tasks were also visible. No emails were sent or external mail connections configured. The fixture registry is saved as a private site JSON file so cache refreshes cannot erase it. A complete seed → clear-cache → cleanup cycle was checked. Original user preferences and mailbox assignments were restored and all newly created fixture documents removed after acceptance.

| Check | Evidence / result |
|---|---|
| Russian, light theme | [1440](kanzlei-overview/ru-light-1440.jpg), [1024](kanzlei-overview/ru-light-1024.jpg), [768](kanzlei-overview/ru-light-768.jpg), [375](kanzlei-overview/ru-light-375.jpg) |
| German, light and dark themes | [Light](kanzlei-overview/de-light-1440.jpg), [Dark](kanzlei-overview/de-dark-1440.jpg) |
| Russian, dark theme | [1440](kanzlei-overview/ru-dark-1440.jpg), [375](kanzlei-overview/ru-dark-375.jpg) |
| Missing assigned mailbox | [Russian dark / 375](kanzlei-overview/ru-dark-no-account-375.jpg); Mail sidebar entry hidden, explanatory state rendered, task counts preserved |
| Responsive layout | DOM document scroll width equals viewport client width at 1440, 1024, 768 and 375; single content column at narrow widths |
| Long subjects / multiple assignees | Text wraps; both Elena Demo and Maria Demo appear on separate lines |
| Task filter and pagination | Today has 10 tasks: 8 initially, 10 after Show more; no further button. Agenda has 11: 8 initially, 11 after Show more |
| New Task | Opens the native full Task form directly, without Quick Entry; no document saved during this check |
| Task and return | Task link opens native Task form; returning reloads current category from page zero |
| Calendar | Opens native Task Calendar at `/desk/task/view/calendar/default`; calendar UI displayed |
| Communication | Opens native Communication form; content and Reply action available |
| Inbox | Opens assigned `DEMO Übersicht` Inbox; five fake incoming messages shown |
| All tasks | Opens native Task List using exclusion filters for Questions/groups/templates |
| Keyboard | Tab from Urgent focuses Overdue; visible focus outline; Enter selects category |
| Root / Kanzlei launch | Root Desktop shows separate Kanzlei and Settings links. Kanzlei opens overview directly |
| Empty Desk entry | `/desk` becomes `/desk/kanzlei-overview` before Desktop renders |

Access-denied users, independent failed requests and slow-response races are validated by integration/client tests. They were not simulated by changing live production permissions or injecting failures into the development server. RU/DE screens and responsive/theme behavior were checked in the live in-app browser. Screenshots with fake email data are safe to retain in this repository.

## Release state

Local development migration/build/cache refresh completed. No production release, merge or push performed. Before production rollout, take the project's normal backup and apply migration/build/cache clear on the target site.
