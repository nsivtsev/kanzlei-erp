# Russian Interface Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete the Kanzlei ERP Russian translation so each German dictionary key and each German literal used in Kanzlei workspace metadata has a Russian value.

**Architecture:** Extend the existing Frappe translation CSV at `kanzlei_erp/translations/ru.csv`; include German literal workspace labels as additional keys, preserve all format placeholders, and retain the existing per-user language mechanism and German site default. Use a read-only CSV comparison and diff review to verify key coverage and formatting without changing framework or ERPNext locale files.

**Tech Stack:** Frappe CSV translations, Python standard-library CSV reader.

---

### Task 1: Complete Russian translations for all German dictionary entries

**Files:**
- Modify: `kanzlei_erp/translations/ru.csv`
- Reference: `kanzlei_erp/translations/de.csv`
- Design: `docs/plans/2026-10-05-russian-interface-design.md`

**Step 1: Identify German source strings missing from the Russian dictionary**

Run a read-only comparison using Python's `csv` module to compare first-column keys from both dictionaries. Keep the German dictionary as the completeness baseline.

Expected: a finite list of missing source strings; no files are changed by this step.

**Step 2: Add a Russian translation for each missing source string**

Append rows to `ru.csv` for every missing key. Use natural Russian terminology consistently: `Mandant` → `клиент`, `FiBu` → `бухгалтерский учёт` or an appropriate contextual form, `Task` → `задача`, `Work Schedule` → `повторение`, and `Preparation Stage` → `этап подготовки`. Update any existing Russian values that still contain German loan terms. Preserve punctuation, capitalization where it carries UI meaning, and every `{0}`-style placeholder.

**Step 3: Add translations for literal German workspace labels**

Add Russian mappings for the titles and item labels in `kanzlei_erp/workspace_sidebar/kanzlei.json` and `kanzlei_erp/workspace_sidebar/einstellungen.json`, as well as the literal `Mandant` field label in app fixtures and DocTypes. Preserve German labels for German-language users.

**Step 4: Verify dictionary parity and placeholders**

Run a read-only Python comparison of CSV keys and placeholder tokens. Expected: no German keys missing in Russian, no duplicated Russian keys, and no placeholder mismatch for source keys shared with German.

**Step 5: Review the diff**

Inspect the CSV diff for malformed quoting, inconsistent terms, and Russian wording errors. Confirm no source labels or German translations were changed.
