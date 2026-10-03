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

The Desk opens the **Mandanten** list and has one **Kanzlei** sidebar: **Mandanten**, **Aufgaben**, **Kalender**, **Wiederholungen**, **Zeiterfassung**, and **Rechnungen**. Standard ERPNext workspaces are hidden from the navigation. Administrator and System Manager also see **Einstellungen**, which contains company, service, activity, billing, user, printing, and import configuration. **Zur Kanzlei** returns to the daily workspace.

Each Mandant uses ERPNext's Customer record, presented as **Mandant** in the interface. Its sales-team, loyalty, portal, and sales-reference sections are hidden. The Connections tab shows work, schedules, time entries, invoices, and payments. Create Mandants directly in **Mandanten**, or import a list through **Einstellungen → Datenimport**. For a single piece of work, create an **Aufgabe**, choose its **Mandant**, enter the work in **Subject**, and set **Expected End Date** as the due date. Set **Expected Start Date** to the same day to show it as a one-day calendar entry. Assign the Task to a user if needed, and mark it Completed when done.

For repeating work, open [Work Schedule](http://development.localhost:8100/desk/work-schedule) and enter a Mandant, work description, frequency (weekly, monthly, quarterly, or yearly), first due date, optional last due date, and optional assignee. Saving creates ordinary Tasks for due dates from the past 30 days through the next 365 days. A daily job keeps that window populated. Each generated Task has its own status and can be completed or rescheduled independently. Extending or editing a schedule creates newly due Tasks; it does not rewrite Tasks already created. Disabling a schedule stops future generation and leaves existing Tasks in place.

Open the [Task calendar](http://development.localhost:8100/desk/task/view/calendar/default) to see the Mandant and work on each due date. Use the **Mandant** filter above the calendar to focus on one client. The normal Task list remains available for status and assignee tracking.

For a quick local walkthrough, populate two clearly fictitious Mandants, one single Task, and one monthly schedule:

```bash
docker compose --env-file .env -f .devcontainer/docker-compose.yml exec -T frappe bash -lc \
  'cd /workspace/development/frappe-bench && env/bin/python /workspace/scripts/seed_local_demo.py'
```

The seed script is safe to rerun and only accepts the `development.localhost` site. The calendar is an operational planner; due dates entered here are not calculated statutory deadlines.

**Zeiterfassung** uses ERPNext Timesheet, with a Mandant and time entries. **Rechnungen** uses ERPNext Sales Invoice. Configure service items, activity types, rates, and billing settings for your Kanzlei before creating real invoices. This version exposes these standard workflows; it does not automatically turn completed Tasks into invoices.

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

The command is safe to rerun after a connection interruption or after adding Contacts. It uses a private, per-account checkpoint under the site's private files directory and reports imported and existing messages, unlinked messages, failures, and unsaved attachments. Treat the import as finished only when `complete` is `true`, `failures` is empty, and `unsaved_attachments` is zero; resolve the listed failures or raise file-size limits and rerun until the folders and attachments reconcile. Keep the private checkpoint with the site backup.

After the archive is reconciled, enable **Incoming** again and leave synchronization set to **ALL** for new mail. Test the standard Inbox, Mandant timelines, Sent-folder behavior, and private attachment access with an assigned employee and a user without mailbox access before daily use.

The focused interface is part of the app and activates on installation/migration. ERPNext's underlying modules remain installed for billing and data dependencies. Hiding navigation does not change document permissions; staff roles must still grant the intended access to Customer, Task, Work Schedule, Timesheet, and Sales Invoice.

Local setup selects German (`de`) in System Settings and for Administrator. Users without an explicit language preference inherit the site's German default. Kanzlei translations extend ERPNext's German localization; English translations are not replaced with German text. An administrator can change a user's language in **Einstellungen → Benutzer**. Migrations do not overwrite language preferences.

## Development and production

The local stack is for development. Keep this app in Git and use the same Frappe/ERPNext major version in both environments. See [production notes](docs/production.md) for the deployment path.
