# Production deployment path

Production will use the [official Frappe Docker Compose stack](https://github.com/frappe/frappe_docker/blob/main/docs/01-getting-started/01-choosing-a-deployment-method.md), not the development Compose file. This repository itself is a Frappe app (the Python package and `pyproject.toml` are at its root). The production image will include Frappe, ERPNext, and a reviewed revision of this repository. The [official image build guide](https://github.com/frappe/frappe_docker/blob/main/docs/02-setup/02-build-setup.md) describes `apps.json` and the `compose.yaml` overrides.

Before deployment, decide the hosting provider and region, domain and TLS termination, secret store, backup destination and restore test, monitoring, and update window. Prepare a persistent database and site volumes. Build the image in CI, verify it in a staging environment, then apply migrations and deploy the image to production. Keep production credentials outside Git and container image layers.

The local `development/frappe-bench` directory and MariaDB volume are development data. They are not deployment artifacts. Production receives versioned code and configuration; any real data migration requires a separate, tested backup and restore procedure.

Installing and migrating the Kanzlei app also activates its focused Desk navigation, Mandant terminology, and form customizations. No manual workspace edits are required on production. Daily navigation contains Mandanten, FiBu-Perioden, Aufgaben, Kalender, Zeiterfassung, and E-Mail for assigned mailbox users. Einstellungen is available under the existing System Manager condition and contains the general Work Schedule, Sales Invoice, and Payment Entry lists. A saved Mandant provides filtered Wiederholungen and FiBu packages with permission-dependent creation actions. Mandanten remains the home page until the planned Übersicht exists.

The saved Customer form now includes a **Dokumente** tab. Its list is assembled from existing File, Communication, FiBu, and Task links, so historical documents already in ERPNext appear after the code, fixtures, migration, and asset build are deployed; no Gmail re-import or file copying is needed. The server rechecks Customer, source, and File read permissions for each page and each download. ZIP downloads are limited to 100 local files and 100 MiB. In staging, verify it with an administrator and a staff user with access to only selected Mandanten, including a linked email attachment and a FiBu file. Documents without an ERPNext link to the Mandant remain outside the list.

Mandant billing defaults and standard receivable/credit settings are on Abrechnung, displayed for Administrator and System Manager. Task's extended fields and dependencies are on Erweitert. These are interface changes: standard DocTypes, stored values, modules, and permissions remain intact. Hidden tabs and navigation are not a permission boundary; configure staff document permissions during onboarding. Validate the Kanzlei's billing process before issuing real invoices through Einstellungen.

For an application update, deploy the reviewed revision and run these commands in the site's Bench environment (the production image build may perform the asset build):

```bash
bench --site <site-name> migrate
bench build --app kanzlei_erp
```

The migration imports the app's Property Setter and Custom Field fixtures, including the Customer Documents Tab/HTML fields, indexed Task Mandant field, and supported `field_order` overrides containing all standard and Kanzlei custom fields. It creates the FiBu Package and FiBu Supplement DocTypes and their database uniqueness constraints. Customer, package, supplement, and Task actions load through `doctype_js`; refresh Desk after updating assets. Repeated migrations must retain field order, unique constraints, and existing FiBu records without duplicate sidebar links/setters. App-owned fixtures overwrite their corresponding customizations, so review local form customizations before rollout.

In staging, create/save/reopen a Mandant and a Task; check the Dokumente tab with linked email, direct files, FiBu package and supplement files, and Task/Rückfrage attachments. Try the incoming-email shortcut, metadata search, source links, pagination, and a ZIP of selected files; confirm it contains only those selected files. Revoke a source link between list and download and confirm the server rejects the download. Also check both main and Erweitert Task tabs, template parameters, dependencies, and completion. Confirm that Wiederholungen filters by the selected Mandant and creation prefills it; save a disposable schedule and verify generated Tasks. Create a September FiBu package with an October internal deadline, reject a duplicate, add a Task and a Question, verify closure is blocked until both are completed or cancelled, and close it with a note. Add a late private document in a numbered supplement, close that supplement, and confirm the original period, closure, and transfer history remain unchanged. Check a Projects User with Customer read permission and another without it; the latter must not see the package, linked Task, or private file. Check the employee/admin navigation and Abrechnung visibility with existing authorized test accounts, administrative invoice/payment access, calendar, Timesheet, and mailbox-assignment behavior. Save and refresh a Mandant to ensure sales actions and financial indicators remain absent. Run the app tests on a test-enabled staging site:

```bash
bench --site <staging-site> run-tests --app kanzlei_erp
```

Select German for the site during onboarding. To reproduce the local default and Administrator preference, run once after migration:

```bash
bench --site <site-name> execute kanzlei_erp.localization.configure_german_language --kwargs '{"user": "Administrator"}'
```

The command uses standard System Settings and User language fields. New users without a language preference inherit German; existing users keep their own preferences. Normal migrations do not reset these choices.
