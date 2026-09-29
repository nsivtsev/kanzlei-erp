# Mandant Work Calendar Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Put one-off and recurring Mandant work into a single ERPNext Task calendar.

**Architecture:** A tracked Frappe app adds Task custom fields, a Work Schedule DocType, a daily occurrence generator, and a Calendar JS hook. Customer and Task remain ERPNext records; recurring rules create normal Tasks.

**Tech Stack:** Frappe/ERPNext v16, Python, JavaScript, MariaDB.

---

### Task 1: App and tests

**Files:** `.gitignore`, `kanzlei_erp/**`, `pyproject.toml`, `README.md`

1. Generate a Frappe app, keep its source at the repository root, and link it into the existing bench.
2. Write failing tests for monthly, quarterly, yearly, and weekly due-date generation, including month-end and leap-year cases.
3. Implement the smallest recurrence-date helper and run the tests.

### Task 2: Data model and generation

**Files:** Work Schedule DocType, Task Custom Field fixtures, hooks, generator, integration tests

1. Write failing integration tests for creation of a Task, no duplicates on retry, and no Tasks for disabled schedules.
2. Add Work Schedule metadata and Task custom fields.
3. Implement the generator and Task calendar-title hook.
4. Install the app on the development site and run the tests.

### Task 3: Calendar and pilot

**Files:** Calendar hook JS, setup instructions, development test data

1. Add a Task calendar hook that displays the Mandant and filters by Customer.
2. Verify its metadata is served by Frappe and that both one-off and recurring Tasks appear.
3. Create two fictitious local Customers and a small pilot; keep this data out of fixtures and Git.
4. Re-run tests, check the HTTP site, document how to create and use the records, and commit.
