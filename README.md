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

The saved Mandant form also has a **Dokumente** tab. It brings together files attached to the Mandant, linked incoming and outgoing email, FiBu packages and supplements, Tasks and Rückfragen, plus explicitly linked FiBu evidence. Use the source, email direction, file type, date, accounting-period, and metadata search filters; **E-Mail-Eingang** is a shortcut to incoming mail attachments. Each file appears once even if it has several readable contexts. Open a source from its context link, open PDFs and common images in a new tab, or select loaded local files for a ZIP download (maximum 100 files / 100 MiB). File access is checked again when opening or building the archive.

The tab only shows documents and email already stored and linked in ERPNext. It does not import mail, guess a Mandant from a sender or filename, or expose an unlinked historical email. The displayed date is the email date for an email attachment and the ERPNext file creation date otherwise; it is not an extracted invoice date or accounting period. Upload, rename, move, and delete actions remain in their existing source forms. The document list is for Kanzlei staff and is not available in the client portal.

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

## Shared email inbox

The **Kanzlei → E-Mail** link opens ERPNext's standard Email Inbox for users who have an account assigned in **Einstellungen → Benutzer → User Email**. Give those staff the **Inbox User** role as well. A staff member who only needs mail on Mandant records also needs **Inbox User**, but does not need the User Email assignment; that role can open private attachments only when the linked Mandant is readable to that staff member. Such users do not see the Inbox link. System Managers configure accounts under **Einstellungen → Email Account**, domains under **Email Domain**, and delivery under **Email Queue**. Assign the same shared account to each Inbox user; ERPNext then grants those users access to the shared mailbox.

Create one standard **Email Account** for the shared address. Configure IMAP and SMTP with TLS and certificate validation, choose the IMAP `INBOX` folder, select **ALL** for synchronization, and make the account the default incoming and outgoing account. Set the shared address as the sender. Keep automatic Contact creation disabled. Also turn off **Enable Automatic Linking in Documents** on every Email Account while using this workflow; ERPNext applies that setting across accounts, and the archive importer blocks it to keep unknown addresses unlinked. Enter the host names, ports, login, and password in ERPNext; never add mailbox credentials to this repository. Check that the provider stores newly sent mail in its server-side **Sent** folder, and enable ERPNext's Sent-folder append option if the provider does not do so itself.

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

The local stack is for development. Keep this app in Git and use the same Frappe/ERPNext major version in both environments. See [production notes](docs/production.md) for the deployment path.
