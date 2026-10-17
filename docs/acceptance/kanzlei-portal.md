# Kanzlei Portal — local acceptance

Date: 2026-10-17. Site: `development.localhost:8100`.

The shared site home and `/kanzlei-portal` now show the approved Portal. ERP retains its work overview. Portal search, personal recents, persistent pins, profile/settings and sign-out are implemented. No new DocType or database migration is required.

- Portal integration: **14 passed**. Covers staff/Guest/Website User, user isolation, revoked/deleted records, missing Suite, current labels, Drive file permissions, corrupt preferences, stale actors and pin limits.
- Existing navigation integration: **22 passed**.
- Node portal/navigation: **15 passed**, including late responses, session loss, serialized pin writes, delayed opens/logout and native empty Desk home routing.
- Actual HTTP: authenticated root 200; Guest redirected to login; personal HTML returns `no-store,no-cache,must-revalidate,max-age=0`; JSON pin/unpin persisted; GET mutation rejected; sign-out returned 200 and the resulting session reached login.
- Browser under Administrator: portal → ERP overview → native Startbildschirm → portal; recent Customer opened; direct ERP form opening appeared in recents; Drive Mandanten opened; search/empty search, pin/unpin, refresh persistence and keyboard activation passed. Profile/menu labels were inspected.
- DE/RU, light/dark and narrow layouts were inspected. At configured widths 320, 375 and 768 plus the normal desktop viewport, content reflowed without horizontal overflow. Temporary viewport overrides were reset.
- Kanzlei assets built successfully. Python F/E9/B/I checks, JavaScript syntax and whitespace checks passed. Host Ruff is older than the repository's `py314` setting, so its selected checks ran with `--isolated`; Frappe tests ran on the project's Python 3.14 environment. Existing Frappe deprecation warnings remain.

Five clearly labelled browser fixtures were removed. The local administrator's prior portal preferences and theme were restored and checked. Screenshot preview uses demonstration records.

Drive's native recents record files, not ordinary folder navigation; folders opened through this portal are included in its own history. Browser permission-denial and delayed-request cases were covered by automated tests rather than altering live browser permissions. Full local CI image/backup acceptance and production deployment were outside this local change.
