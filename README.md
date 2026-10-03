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

The focused interface is part of the app and activates on installation/migration. ERPNext's underlying modules remain installed for billing and data dependencies. Hiding navigation does not change document permissions; staff roles must still grant the intended access to Customer, Task, Work Schedule, Timesheet, and Sales Invoice.

## Development and production

The local stack is for development. Keep this app in Git and use the same Frappe/ERPNext major version in both environments. See [production notes](docs/production.md) for the deployment path.
