# Kanzlei ERP

Local ERPNext development environment for a small Steuerkanzlei. The stack follows the [official Frappe Docker development setup](https://github.com/frappe/frappe_docker/blob/main/docs/05-development/04-alternate-setup.md).

## Requirements

- Docker Engine and Docker Compose
- Git and OpenSSL
- An ARM64 Mac with at least 6 GB available to Docker (8 GB recommended) and approximately 60 GB of Docker disk capacity

## First start

```bash
bash scripts/setup-local.sh
```

The script generates local credentials in the ignored `.env`, starts MariaDB, Redis, and the Frappe development container, initializes a `version-16` bench, creates `development.localhost`, installs ERPNext and the app in this repository, and starts `bench start`. First setup downloads images and source code and can take a while. Re-running the script keeps the existing database and site.

Open [http://development.localhost:8100](http://development.localhost:8100) and log in as `Administrator`. The password is stored as `LOCAL_ADMIN_PASSWORD` in `.env`. The `.localhost` domain normally resolves to loopback; if it does not on your machine, add `127.0.0.1 development.localhost` to your hosts file.

To inspect or stop the services:

```bash
docker compose --env-file .env -f .devcontainer/docker-compose.yml ps
docker compose --env-file .env -f .devcontainer/docker-compose.yml logs -f frappe
docker compose --env-file .env -f .devcontainer/docker-compose.yml down
```

The Kanzlei ERP Frappe app lives at the repository root and is linked into the ignored `development/frappe-bench/apps/kanzlei_erp/` at runtime. Bench and site data are ignored; MariaDB data is in a named Docker volume. `down` preserves both; `down -v` removes the database, so use it only to discard the local site.

## Mandants and work calendar

The Desk opens the **Mandanten** list and has one **Kanzlei** sidebar: **Mandanten**, **FiBu-Perioden**, **Rückfragen**, **Aufgaben**, **Kalender**, **Zeiterfassung**, followed by **E-Mail** when an email account is assigned to the user. Standard ERPNext workspaces are hidden from the navigation. Administrator and System Manager also see **Einstellungen**, which contains the general **Wiederholungen**, **Rechnungen**, and **Zahlungen** lists as well as company, service, activity, billing, user, printing, and import configuration. **Zur Kanzlei** returns to the daily workspace.

Each Mandant uses ERPNext's Customer record, presented as **Mandant** in the interface. Sales, territory, loyalty, portal, and internal-customer accounting sections are hidden. **Account Manager** is in Details. Administrator and System Manager see the **Abrechnung** tab with billing currency, the Kanzlei's company bank account, price list, payment terms, and standard receivable/credit settings. The company bank account is for the Kanzlei's own billing. The Connections tab shows FiBu packages, Tasks, schedules, and time entries; sales/payment actions, ledger shortcuts, and financial indicators are hidden. Create Mandants directly in **Mandanten**, or import a list through **Einstellungen → Datenimport**.

The saved Mandant form also has a **Dokumente** tab. It brings together files attached to the Mandant, files and HTTP(S) links in the Mandant's Drive folder and its subfolders, linked incoming and outgoing email, FiBu packages and supplements, Tasks and Rückfragen, plus explicitly linked FiBu evidence. Use the source, email direction, file type, date, accounting-period, and metadata search filters; **E-Mail-Eingang** is a shortcut to incoming mail attachments. Each File appears once even if it has several readable contexts; different files with the same name remain separate. Open the Drive folder or another source from its context link, open PDFs and common images in a new tab, or select loaded stored files for a ZIP download (maximum 100 files / 100 MiB). HTTP(S) links open in a new tab and are excluded from ZIP. File access and active Drive ancestry are checked again when opening or building the archive. Reopening the tab or clicking Refresh updates the list after Drive changes.

The tab lists documents already linked in ERPNext and files already stored under the Mandant's Drive root. It does not copy files between ERP and Drive, import mail, guess a Mandant from a sender or filename, or expose an unlinked historical email. A Drive filename or folder alone does not link a file to a FiBu period. The displayed date is the email date for an email attachment and the ERPNext file creation date otherwise; it is not an extracted invoice date or accounting period. Upload, rename, move, and delete actions remain in their existing source forms. The document list is for Kanzlei staff and is not available in the client portal.

For a single piece of work, create an **Aufgabe**, choose its **Mandant**, enter the work in **Subject**, and set **Expected End Date** as the due date. Set **Expected Start Date** to the same day to show it as a one-day calendar entry. The main form includes status, priority, expected time, and description; a schedule link appears when present. **Erweitert** contains Project, Issue, Type, Color, Weight, Parent Task, Group/Template, template timing, Progress, Milestone, dependencies, and actual time/cost details. Existing values and standard completion rules, including Completed On, are preserved. Assignments, comments, attachments, and the Timesheet link remain available.

For repeating work, open a saved Mandant and choose **Wiederholungen** to see schedules filtered to that client. **Erstellen → Wiederholung anlegen** prefills the Mandant and appears when the user can create Work Schedules; the list action requires read permission. Administrators can also open the general list under **Einstellungen → Wiederholungen**. Enter a work description, frequency (weekly, monthly, quarterly, or yearly), first due date, optional last due date, and optional assignee. Saving creates ordinary Tasks for due dates from the past 30 days through the next 365 days. A daily job keeps that window populated. Each generated Task has its own status and can be completed or rescheduled independently. Extending or editing a schedule creates newly due Tasks; it does not rewrite Tasks already created. Disabling a schedule stops future generation and leaves existing Tasks in place.

## FiBu accounting-period packages

Create a **FiBu-Paket** from a saved Mandant with **Erstellen → FiBu-Paket anlegen**, or open **FiBu-Perioden** for the full list. Choose an active, non-stock service from Leistungen and a calendar month, quarter, or year. The saved period boundaries stay fixed even when work deadlines move; the same Mandant, service, and period can have only one package. Creation is manual, and existing Tasks and Wiederholungen remain unlinked until a staff member links them.

The package records the responsible user, optional deputy, document receipt date, internal completion date, and external deadline with its source. **Erstellen → Neue Aufgabe** creates an ordinary Task; **Neue Rückfrage** creates a managed Question Task with the Mandant and period context. Add private documents and readable Communication links in the corresponding tables. The transfer log records an event date, note, server-set author/time, and optional private file or Communication evidence. Closing requires a note and all package Tasks and Questions to be Completed or Cancelled; it does not confirm DATEV receipt.

The **Vorbereitung** section tracks Sammlung, Prüfung, Klärung, Bereit, Übermittlung, and Eingang bestätigt. Use **Arbeitsablauf → Bearbeitungsphase ändern** to move through the allowed sequence; Prüfung can go straight to Bereit when no clarification is needed. Returning to an earlier phase requires a note. **Arbeitskontext aktualisieren** records the next action and its assignee, with an optional Task from this same period. Waiting for a Mandant is a separate flag with a required reason; reviewing an answer and clearing the flag remain staff actions. Each change records its author, time, previous and new values, and note. Preparation stages also apply independently to late supplements.

Packages and supplements created before this workflow was introduced keep their closure history. Their earlier preparation stage is shown as unrecorded instead of inferred from closure or transfer history.

When material arrives after closure, use **Erstellen → Ergänzung anlegen** on the closed package. The numbered supplement copies the responsible user and deputy, while its work deadlines are entered anew. Add the late documents and Tasks to the open supplement. Its own closure keeps the original package closure and transfer history intact; a further change requires another supplement. The FiBu list shows whether open supplements exist.

### Mandant sources and period completeness

Use **Accounting Sources / Источники учета / Buchhaltungsquellen** on the Mandant to configure individually named accounts, cards, invoices, and other actual sources. Multiple sources in one category remain separate. Effective dates restrict the expected interval; blank boundaries mean unrestricted validity. Customer write permission is required, and the server assigns permanent source identifiers.

A newly saved FiBu Package snapshots only sources active during its accounting period. Editing the Mandant later does not rewrite that snapshot. **Completeness / Комплектность / Vollständigkeit** above the documents shows statuses, missing date ranges, and sources awaiting review. Use **Update** to enter private File, readable Communication, or HTTP/HTTPS evidence with covered dates. Evidence does not advance a status automatically: Partially Received needs evidence; Received and Checked need gap-free coverage. Checked separately records the employee and time. Editing checked evidence or expected dates clears that verification and requires an explicit repeat check. Not Applicable requires a reason; a no-operations statement is still evidence.

Checklist items cannot be deleted. Add a missed source through the Completeness menu or explain non-applicability; the same menu opens the immutable history. Incomplete or empty checklists warn on Ready and closure without blocking those actions. Mandatory Freigabe remains BL-011. Supplements select only affected original sources or add a manual source within the original accounting period; original statuses and evidence remain independent. Normal package duplication starts new work with a fresh profile snapshot and no prior documents or verification.

Migration adds structures without backfilling old periods. Staff use **Initialize Checklist** once on an existing open period; repeating it creates no duplicates. Closed historical work shows that completeness was not previously recorded. Checklist evidence also appears in the Mandant Documents tab with deduplication and its original access checks; external references are opened as links and are not included in ZIP downloads.

Both FiBu document types expose `initialize_checklist(expected_modified)`, `add_checklist_source(..., expected_modified)`, and `update_checklist_entry(..., expected_modified)`. Supply the current document `modified` value: actions lock and reload the persisted record, check write permission and reject stale versions. Ordinary saves cannot change checklist state or review metadata. See the [BL-003/004 acceptance report](docs/reviews/2026-10-08-bl003-bl004-verification.md).

### Managed Rückfragen (BL-007)

Create one **Question Task** for each independently resolvable issue in an open period or supplement. Use **Neue Rückfrage** on that work context, the question button beside a checklist source, or **Rückfragen** in the sidebar. Checklist creation prefills the source and currently missing date ranges. The main responsible employee defaults to the work context responsible; ordinary Task assignments remain available for other participants. Both require existing access and cannot grant access to another Mandant.

Use the **Rückfrage / Уточнение** actions on the saved Task to record a request, an answer, a decision, cancellation or reopening. A request requires a Contact linked to the Mandant, request date, expected response date and a readable Communication or description. An answer requires its date and content; a partial answer also requires the remaining issue. Several requests and answers stay in immutable history. A shared Communication may be linked to several Questions, each resolved independently, without changing the Communication's links or permissions. Optional context links are a permanent checklist entry key, readable private File and free-text operation description.

Question states are **Entwurf → Antwort ausstehend → Antwort erhalten → Gelöst**, with **Abgebrochen** for cancellation. They map to Task Open, Working, Pending Review, Completed and Cancelled. Standard Overdue does not change the Question state. An answer keeps the question open; resolution requires a confirmed decision and records its employee/time on the server. Resolution without a client answer is allowed. Cancellation and reopening require reasons; reopening retains earlier history and is available only in an open work context. Task saves, mass completion, Project completion and standalone history APIs cannot bypass those actions. Standard dependencies still apply to completion. An unresolved managed Question blocks period or supplement closure even if an external process corrupts its ordinary Task status.

**Rückfragen** shows all unresolved questions by default, with Mandant, period, responsible, state, overdue-response and reminder filters, totals and pagination. Period and supplement forms show waiting counts, deadlines and the unanswered part. Waiting for Mandant, preparation stage and checklist completeness remain separate staff actions. Resolving a question used as the period's next action prompts the employee to update that action.

A daily job creates one in-app **Notification Log / Alert** for each reminder cycle in the site's time zone. Changing the reminder date or main responsible starts a new cycle; unrelated edits retain it. An answer clears the waiting reminder, and staff can set a fresh reminder for review. Resolution and cancellation clear it. Disabled employees or employees without access receive no notification, and the overview shows the issue. Concurrent workers use context/Task locks, atomic notification/history markers and a full job retry on database conflicts. This feature does not send email, synchronize a mailbox or recognize answers automatically (BL-008–009), or introduce Freigabe (BL-011).

Existing historical questions remain untracked and display **Question management was not previously recorded**. Staff explicitly initialize an open historical question; old requests, answers and decisions are not inferred from Task status. Initialization requires removing any existing assignments or shares for users without period access. Closed legacy records retain their earlier behavior. New copies start in Draft without previous correspondence, decisions, history or sent reminders.

The server module `kanzlei_erp.fibu_questions` exposes `create_question(context_doctype, context_name, values)`, `update_question(name, action, values, expected_modified)` and `get_questions(filters, start, page_length)`. Updates require the current Task `modified` value. Creation supports a stable `creation_key` for safe retries; the UI retains it for the dialog and prevents duplicate submissions. Actions lock the package, optional supplement and Task in that order, reload and recheck access. See the [implementation plan](docs/plans/2026-10-08-bl007-implementation.md) and [acceptance report](docs/reviews/2026-10-08-bl007-verification.md).

Projects User and System Manager need **read access to the Mandant** in addition to their FiBu role. Being named responsible or deputy does not grant that access. Configure Customer read permissions during staff onboarding; private files and linked mail keep their original access rules. Package creation, links, list results, direct form access, and Task work context enforce the Mandant boundary on the server.

Open the [Task calendar](http://development.localhost:8100/desk/task/view/calendar/default) to see the Mandant and work on each due date. Use the **Mandant** filter above the calendar to focus on one client. The normal Task list remains available for status and assignee tracking.

For a quick local walkthrough, populate two clearly fictitious Mandants, one single Task, and one monthly schedule:

```bash
docker compose --env-file .env -f .devcontainer/docker-compose.yml exec -T frappe bash -lc \
  'cd /workspace/development/frappe-bench && env/bin/python /workspace/scripts/seed_local_demo.py'
```

The seed script is safe to rerun and only accepts the `development.localhost` site. The calendar is an operational planner; due dates entered here are not calculated statutory deadlines.

**Zeiterfassung** uses ERPNext Timesheet, with a Mandant and time entries. **Rechnungen** (Sales Invoice) and **Zahlungen** (Payment Entry) are available in Einstellungen for administrative use. They are outside the daily FiBu preparation workflow. Configure and verify service items, activity types, rates, billing settings, and applicable calculation/print requirements before using them for Kanzlei billing. This version does not automatically turn completed Tasks into invoices.

## Transactional email

Production service emails use the planned Resend SMTP configuration with sender
`notifications@stb-golke.de`. Follow the [transactional email runbook](docs/transactional-email.md)
for domain verification, ERPNext settings, and password-reset delivery checks.
The runbook does not imply that production setup or delivery verification is complete.
When this account is configured, keep it as Default Outgoing and select the shared
mailbox explicitly for staff correspondence; the shared-inbox setup below must not
replace the transactional default.

## Shared email inbox

The **Kanzlei → E-Mail** link opens ERPNext's standard Email Inbox for users who have an account assigned in **Einstellungen → Benutzer → User Email**. Give those staff the **Inbox User** role as well. A staff member who only needs mail on Mandant records also needs **Inbox User**, but does not need the User Email assignment; that role can open private attachments only when the linked Mandant is readable to that staff member. Such users do not see the Inbox link. System Managers configure accounts under **Einstellungen → Email Account**, domains under **Email Domain**, and delivery under **Email Queue**. Assign the same shared account to each Inbox user; ERPNext then grants those users access to the shared mailbox.

Create one standard **Email Account** for the shared address. Configure IMAP and SMTP with TLS and certificate validation, choose the IMAP `INBOX` folder, select **ALL** for synchronization, and make the account the default incoming account. Make it Default Outgoing only if no dedicated transactional account is configured. Set the shared address as the sender. Keep automatic Contact creation disabled. Also turn off **Enable Automatic Linking in Documents** on every Email Account while using this workflow; ERPNext applies that setting across accounts, and the archive importer blocks it to keep unknown addresses unlinked. Enter the host names, ports, login, and password in ERPNext; never add mailbox credentials to this repository. Check that the provider stores newly sent mail in its server-side **Sent** folder, and enable ERPNext's Sent-folder append option if the provider does not do so itself.

Before importing mail, create a **Contact** for every known sender or recipient address and link each Contact to the appropriate **Mandant**. ERPNext then links matching email Communication records to those Mandanten in their existing timeline. Unknown addresses stay unlinked. If a Contact belongs to several Mandanten, the message appears with each of them. The email and its private attachments remain available through the standard Communication record.

### Import an existing mailbox archive

Make a database and site-files backup before importing a real archive. Prepare the Contacts first when possible. Use a System Manager shell in the Bench directory. The preview reads the mailbox without changing messages and reports folder counts, message and attachment sizes, and attachments over the configured file limits:

```bash
bench --site SITE execute kanzlei_erp.email_archive.preview --kwargs '{"email_account_name":"SHARED ACCOUNT NAME"}'
```

Pause **Enable Incoming** on that account before the import so the scheduled receiver does not change the mailbox while it is being read. Leave IMAP and its credentials configured, and keep automatic Contact creation disabled. The importer reads INBOX first, then the other selectable folders; it skips drafts, spam, and trash. It records historical sent mail as Sent Communication records and never sends those messages. Supply any additional shared-address aliases used in old sent mail:

```bash
bench --site SITE execute kanzlei_erp.email_archive.import_archive --kwargs '{"email_account_name":"SHARED ACCOUNT NAME","sender_aliases":["old-address@example.com"]}'
```

The command is safe to rerun after a connection interruption or after adding Contacts. It uses a private, per-account checkpoint under the site's private files directory and reports imported and existing messages, unlinked messages, failures, and unsaved attachments. Every pass checks private attachment bytes against the MIME parts, including messages already in ERPNext or in the checkpoint. Missing originals are added without deleting earlier files. Encoded display names are decoded after parsing address boundaries, so commas inside names do not become extra recipients.

Before importing original JPEG attachments, disable **Strip EXIF Metadata from Uploaded Images** in System Settings. This is a site-wide setting affecting future JPEG uploads; leaving it enabled can change attachment bytes. The importer reports transformed or missing originals as unsaved attachments instead of claiming completion. Previously transformed copies remain available when a rerun adds the originals.

Treat the import as finished only when `complete` is `true`, `failures` is empty, and `unsaved_attachments` is zero; resolve the listed failures, file-processing settings, or file-size limits and rerun until the folders and attachments reconcile. Compare each folder's UID/UIDVALIDITY inventory with the checkpoint, accounting for the same Gmail message in several folders. A repeat pass over an unchanged inventory must not create new Communication or File records. Keep the private checkpoint with the site backup.

For Gmail, first verify that a sent test message appears exactly once in the server Sent folder. Enable ERPNext **Append Emails to Sent Folder** only if the server does not save that copy, using the actual IMAP folder name. Assign the account through each employee's **User Email** table; Inbox visibility alone does not verify permission to open messages or private attachment URLs.

After the archive is reconciled, enable **Incoming** again and leave synchronization set to **ALL** for new mail. Test the standard Inbox, Mandant timelines, Sent-folder behavior, and private attachment access with an assigned employee and a user without mailbox access before daily use.

The local Gmail pilot and archive verification are recorded in [the BL-009 acceptance report](docs/reviews/2026-10-09-bl009-verification.md), with completed checks for delivery, server Sent copies, private attachment access, and the full archive snapshot.

The focused interface is part of the app and activates on installation/migration. ERPNext's underlying modules remain installed for billing and data dependencies. Hiding navigation does not change document permissions; staff roles must still grant the intended access to Customer, Task, Work Schedule, Timesheet, and Sales Invoice.

Local setup selects German (`de`) in System Settings and for Administrator. Users without an explicit language preference inherit the site's German default. Kanzlei ERP provides German and Russian translations for its interface, including its workspace labels. Russian-speaking staff can select Russian (`ru`) per user in **Einstellungen → Benutzer**. ERPNext's own interface uses the translations supplied by the installed ERPNext version. Migrations do not overwrite language preferences.

## Development and production

### Suite Drive

Suite is bundled at commit `2db13737524b271e32cae80a64a85b5ff112e55f`, pinned in
`deploy/suite.env` for both local setup and production builds. Run
`bash scripts/setup-local.sh` to add it to the local site. Before its first
installation on an existing local site, setup backs up the database and files.
Setup pauses Bench while installing dependencies and rebuilding, then starts it
again; repeating it preserves the site and Drive files. A Suite checkout at a
different commit stops setup rather than overwriting it.

Every saved **System User**, including staff without Customer access and System
Managers, sees **Dateien / Files / Файлы** in Kanzlei when Suite is installed.
It follows **Zeiterfassung**, before administrative **Einstellungen**, and opens
`/drive` in a new tab. Guest and Website User do not receive this staff entry.
Drive's top sidebar group includes **Zur Kanzlei / Back to Kanzlei / В Kanzlei**,
returning to `/desk/kanzlei-overview` in the current tab. The return link waits
for a successfully loaded profile with `user_type = System User`.
Create an ordinary folder in Home, upload a PDF, and download it to verify access.
Kanzlei remains the ERP home and ERPNext owns company setup; Suite's separate
onboarding is marked complete without configuring its Mail, Meet or other products.
Archive migration and additional backup scheduling are future steps.
Suite's own roles and permission system
remain active: hiding the navigation link is not a server-side access boundary.
ERP attachments are managed from their original documents; Drive rename,
move, trash and permanent deletion reject them before touching stored bytes,
including operations on a containing folder.

Production images contain Suite code, dependencies and built assets. Deployment
installs Suite on existing sites after the verified pre-update snapshot and
before migration. CI covers fresh installation, an upgrade retaining a Customer
and its private attachment, PDF upload/download and Drive file backup/restore.

Saved Mandanten have **Drive Folder / Drive-Ordner / Папка Drive**. Before a
folder exists, a staff user with Customer write access sees **Create Drive
Folder**; this creates one private native Drive folder and records its stable
File ID on the Customer. Repeating the action opens the existing folder. A
Customer name change does not create another folder. Existing Mandanten get
folders on demand; there is no bulk backfill or copying of ERP attachments.

Access to the folder and all new descendants follows the current Customer
permissions: staff readers can browse/download; staff writers can upload and
manage files/subfolders. Owners and Suite Admin cannot bypass a revoked Customer
permission. Ordinary Guest/portal access and manual Drive shares do not grant
access. Short-lived preview tokens recheck their issuer's current access; ZIP
downloads recheck selected entities and archive members. The root and its
containing folders cannot be renamed, moved, trashed or deleted through Drive;
files/subfolders can move within the same Mandant. Cross-boundary moves and
raw changes to protected metadata/storage keys are rejected before storage
operations. These folders support uploaded files, subfolders and HTTP(S) links.
Native Writer Documents, Sheets and Slides are rejected server-side because
their content permissions require a separate integration. Create them in
ordinary Drive folders. The existing **Dokumente** tab still lists ERP-linked material.

This access bridge requires the tracked Frappe and Suite patches applied by
`scripts/fetch-suite.sh`, also copied into the production build. Patch application
is idempotent and checks the source context; incompatible upstream updates stop
setup/build. The installer accepts the exact v1–v5 predecessors and rolls
back both dependency patches atomically on conflict, preserving unrelated edits.
After applying patches, migrate the site, build Suite and Kanzlei assets and
clear Desk cache. Migration also invalidates Suite's cached user profiles so
the new `user_type` field is available immediately. A missing/trashed root is
reported for administrator attention;
clicking the action never silently creates a replacement. See the
[folder design](docs/plans/2026-10-12-mandant-drive-design.md).

The [staff navigation verification](docs/reviews/2026-10-14-drive-navigation-verification.md)
records automated and browser acceptance for BL-046.

Drive now includes **Mandanten** at `/drive/mandanten` for confirmed staff,
including mobile navigation. The searchable list includes readable Customers
with ready, missing or unavailable folders; write access enables the existing
idempotent folder-creation action. ERP records open separately, and client
folders and their descendants return through Mandanten breadcrumbs using the
current Customer title. Personal folders remain in Home. Lists refresh on entry,
visible-tab return and explicit refresh, with no persisted list cache or polling.
`kanzlei_erp.mandant_drive.list_mandanten` paginates readable records only and
checks current Customer rights; granted and revoked access appear on refresh.
Suite entity navigation context is supplied after entity read authorization.
The exact BL-046 patch is preserved as v4. See
[BL-047 acceptance](docs/reviews/2026-10-14-drive-mandanten-verification.md).

Drive receives a server-verified `mandant_policy` through the batched
`drive_entity_actions_batch` hook after read authorization. Menus, selections,
shortcuts, uploads, paste, trash and drag-and-drop apply the same action rules.
Forbidden commands are hidden; an ERP notice links to the Customer separately.
Writers can upload files and create folders/HTTP(S) links in the root and its
subfolders. Root rename/move/removal, native editors and Drive sharing are hidden.
Readers retain viewing and downloading. Creation waits for current permissions;
returning to the tab refreshes them and stale route/session responses are ignored.
Moves validate all sources and the destination, stay within one Mandant and only
update the list after server success. Ordinary files retain Home/Site destinations
and cannot move into managed folders. The exact BL-047 patch is preserved as v5;
clean/v1–v5 upgrades, repeat application and atomic rollback are covered. See
[BL-048 acceptance](docs/reviews/2026-10-15-drive-actions-verification.md).

Drive also binds each File to its physical object. A second File cannot reuse
Mandant bytes, thumbnails, trash objects or a protected directory, even when
its creator can currently read the original. Legacy aliases are denied at
permission and storage boundaries; they are not automatically repaired or
deleted. Ordinary ERP attachment copies retain their shared URL after source
and destination permission checks.

List changes are delivered after commit as an addressed
`drive-folder-changed` signal containing only a folder ID. Drive reloads its
permission-checked list; File updates, comments and presence also recheck
current access. Rollbacks send no invalidation.

Before rollout, run this read-only audit as an administrator and manually
investigate any reported aliases:

```sh
bench --site <site> execute kanzlei_erp.drive_storage.audit_storage_aliases
```

Update both dependency patches and frontend assets together, then migrate,
rebuild Suite and Kanzlei assets, clear cache and restart backend, workers and
websocket. `scripts/fetch-suite.sh` upgrades the exact previous patches without
resetting unrelated checkout changes. Already issued S3 presigned URLs remain
valid until their expiry; this change prevents an alias from obtaining one.
The [security verification](docs/reviews/2026-10-13-drive-security-verification.md)
records local/S3-mock, HTTP, browser and image acceptance. The disposable
HTTP/Socket.IO scenario is included in `scripts/ci-image.sh`; it requires four
temporary staff sessions and does not run against production.

Uploaded DOCX, PPTX, XLSX, PDF and XML files download with their original bytes
and are included recursively in folder ZIPs. Uploaded Office files open a local
File card with a download button; no Microsoft preview is requested. Native
Writer, Sheets and Slides documents keep their editors. ZIPs report skipped
native documents and links, count only readable items, deduplicate overlapping
selections and preserve files when archive names collide. Unreadable uploaded
bytes fail the whole build. Old archive tokens require rebuilding.

The v2 Suite patch preserves the previous security patch. Update backend and Suite
assets together, back up the site, migrate, rebuild, clear cache and restart services. No File schema change
or data migration is required. See the
[download verification](docs/reviews/2026-10-13-drive-downloads-verification.md).

Creating or reopening a Mandant folder synchronizes its protected link in the
open Customer form while preserving unsaved business fields. Saving a stale
form with an empty link waits for its current creation and checks the latest
folder first, including folders created in another tab. Genuine conflicts in
business fields still require the normal Frappe conflict workflow. Parallel
creation retries a snapshot lock conflict before touching storage and returns
the same root. See the
[form verification](docs/reviews/2026-10-13-drive-form-sync-verification.md).

The local stack is for development. Production deploys to Hetzner through GitHub Actions on `vX.Y.Z` tags, using a tested GHCR image digest, HTTPS, pre-update backups and explicit recovery. Follow the [server setup and deployment runbook](docs/deployment.md); see [production notes](docs/production.md) for application onboarding and acceptance checks.

### Work overview

Kanzlei now opens `/desk/kanzlei-overview`. The standard Desk page combines permission-aware ordinary Task counts, upcoming task deadlines and the five latest open incoming emails from the current user's assigned mailboxes. Managed Questions, groups and templates are excluded; ordinary FiBu-linked Tasks remain subject to existing Customer/FiBu access rules. The calendar panel covers tomorrow through today + seven days, using the site's date and timezone. Opening the page does not change Tasks, assignments or mail read state.

Mail opens the standard Communication form; full Task Calendar and Inbox remain available. Each block loads and retries independently. Counts show `—` while unavailable, and are never presented as zero on errors. Refresh happens on entry/return and with the Refresh button, without polling.

For rollout, back up the site's database and files, run `bench --site <site> migrate`, `bench build --app kanzlei_erp`, then `bench --site <site> clear-cache`. The patch `detach_settings_icon` changes only the old Einstellungen → Kanzlei relationship in saved Desktop layouts. Repeat execution is safe and preserves other layout entries and ordering.

Run the overview and navigation modules through Bench and the client checks with `node --test tests/*.test.cjs`. Development-only fictitious fixtures can be created with `env/bin/python /workspace/scripts/seed_overview_demo.py` from the container's Bench directory; pass `cleanup` afterward to remove the created records and restore the saved Administrator language, theme and email assignments. Do not run fixture setup on production. See [overview acceptance](docs/acceptance/kanzlei-overview.md) for validation and screenshots.
