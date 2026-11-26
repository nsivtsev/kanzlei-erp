# Posteingang One-Click Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three-stage Posteingang review with one proposal card: **Übernehmen** or **Manuell erstellen**, plus a 10-minute **Rückgängig**.

**Architecture:** The server builds a ready-made card (`simple_card`) from the latest successful Run, matching and defaults. One new atomic operation `confirm_simple` turns the card (or the manually edited card) into a validated review and reuses the existing task-creation core extracted from `confirm`. `undo_confirm` deletes untouched tasks and reopens the letter. The Desk page is rewritten around a list + card + PDF layout; the old stage editor is deleted. Analysis is not auto-applied in the worker: `confirm_simple` applies the Run inside its own transaction.

**Tech Stack:** Frappe v16 / ERPNext (Python, FrappeTestCase), vanilla JS Desk page + `public/js/*.bundle.js`, `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-26-posteingang-one-click-design.md` and the mockup `docs/superpowers/specs/2026-11-26-posteingang-one-click-mockup.html` (the visual reference for Task 7).

## Global Constraints

- All existing guards stay: `expected_revision`, `request_key` idempotency via `_begin`, `_authorized`, `ASSIGNEE_NOT_ALLOWED`, `validate_review(..., confirm=True)`, private original, event history.
- `human_verified=True` is set only by `confirm_simple`; audit texts must be literally true. Use exactly:
  - customer reason: `"Vorschlag übernommen"` (single suggested candidate), `"Manuell gewählt"`, `"Mehrere Empfänger, Mandant manuell gewählt"`;
  - external date basis: `"Vorschlag übernommen (Fundstelle S. {page})"`, `"Vom Benutzer eingetragen: „{wording}“"` (relative/uncertain), `"Manuell eingetragen"`;
  - rejected proposal action: `"Nicht ausgewählt"`;
  - no tasks, proposal said none: `"Vom Assistenten: keine Handlung erforderlich, übernommen"`; no tasks otherwise: `"Ohne Aufgabe abgelegt"`.
- Relative / uncertain deadlines are never converted to a date by code.
- Working date: deadline minus 5 working days (Mon–Fri minus Holiday List of the default Company), never before today; no deadline → today + 7 calendar days.
- Default assignee: `received_by` if `staff(received_by)`, else the session user.
- Undo: same user as `confirmed_by`, within 600 seconds of `confirmed_at`, only if every created Task's `modified` equals the value stored at confirmation.
- UI copy follows the codebase convention: English source strings in `__()` (e.g. `__('Accept')`), with the mockup's German copy as the `de.csv` value and a Russian `ru.csv` value. The German audit texts above are stored data, not UI strings, and stay literal.
- Do not change `Posteingang Settings` defaults (`auto_analysis_enabled`, `external_analysis_enabled` stay 0); enabling them is the owner's decision.
- Python commands run inside the dev container:
  `docker compose --env-file .env -f compose.yml exec frappe bash -lc 'cd /workspace/development/frappe-bench && bench --site development.localhost run-tests --app kanzlei_erp --module <module>'`
  JS: `node --test tests/<file>.test.cjs` from the repo root.

## Review Focus

1. **Double click / lost response on Übernehmen** — the same `request_key` must replay, never create a second task set (test in Task 4).
2. **Proposal changed between load and click** (new Run finished, letter revision bumped) — `REVISION_CONFLICT`, card reloads, nothing created (test in Task 4).
3. **Legacy letters with a human draft** (action rows exist from the old editor) — card opens in manual mode with the draft values, `confirm_simple` does not silently drop them (tests in Tasks 2 and 4).
4. **Deadline already closer than 5 working days or in the past** — working date is today, not a past date (test in Task 1).
5. **Undo after someone touched a task** (status change, reassignment) — refused with `TASK_CHANGED`, letter stays Completed (test in Task 5).

---

### Task 1: Working-date rule

**Files:**
- Create: `kanzlei_erp/posteingang_simple.py`
- Test: `kanzlei_erp/tests/test_posteingang_simple.py`

**Interfaces:**
- Produces: `working_date(deadline: datetime.date | None, today: datetime.date, holidays: frozenset[datetime.date]) -> datetime.date`; `company_holidays() -> frozenset[datetime.date]` (reads `Global Defaults.default_company` → `Company.default_holiday_list` → `Holiday.holiday_date`; empty set if any link is missing).

- [ ] **Step 1: Write failing tests** (plain `unittest.TestCase`, no DB for `working_date`)

```python
from datetime import date
def test_five_working_days_before_deadline(self):
	self.assertEqual(working_date(date(2026, 12, 15), date(2026, 11, 26), frozenset()), date(2026, 12, 8))
def test_skips_weekend_and_holidays(self):
	self.assertEqual(working_date(date(2027, 1, 15), date(2026, 11, 26), frozenset()), date(2027, 1, 8))
	self.assertEqual(working_date(date(2027, 1, 15), date(2026, 11, 26), frozenset({date(2027, 1, 12)})), date(2027, 1, 7))
def test_never_before_today(self):
	self.assertEqual(working_date(date(2026, 11, 30), date(2026, 11, 26), frozenset()), date(2026, 11, 26))
	self.assertEqual(working_date(date(2026, 11, 1), date(2026, 11, 26), frozenset()), date(2026, 11, 26))
def test_no_deadline_is_one_week(self):
	self.assertEqual(working_date(None, date(2026, 11, 26), frozenset()), date(2026, 12, 3))
```
Plus one FrappeTestCase: a Company with a Holiday List holding `2027-01-12` → `company_holidays()` contains it; with no default company → `frozenset()`.

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: kanzlei_erp.posteingang_simple`)
- [ ] **Step 3: Implement both functions in `posteingang_simple.py`**
- [ ] **Step 4: Run module `kanzlei_erp.tests.test_posteingang_simple`, expect PASS**
- [ ] **Step 5: Commit** — also add the spec and mockup files: `git add docs/superpowers kanzlei_erp/posteingang_simple.py kanzlei_erp/tests/test_posteingang_simple.py && git commit -m "Add Posteingang one-click spec and working-date rule"`

### Task 2: Card projection

**Files:**
- Modify: `kanzlei_erp/posteingang_simple.py`
- Modify: `kanzlei_erp/posteingang_workspace.py:84` (`get_letter_workspace` adds `"card": simple.simple_card(doc)`)
- Test: `kanzlei_erp/tests/test_posteingang_simple.py`

**Interfaces:**
- Consumes: `working_date`, `company_holidays` (Task 1); `review._latest_successful(doc)`, `review._actions(name)`, `matching._match(extraction)`, `matching._customer(name)`, `staff(user)`.
- Produces: `simple_card(doc) -> dict` with this exact shape (later tasks and the JS rely on it):

```python
{"state": "analyzing" | "failed" | "not_started" | "proposal" | "draft" | "done",
 "run": str | None,                       # latest successful Run used for the proposal
 "customer": {"suggested": {"customer", "customer_name", "why", "page"} | None,
              "candidates": [{"customer", "customer_name", "why"}],   # max 3, from _match
              "ambiguity": "none" | "multiple_matches" | "multiple_addressees" | "not_found" | None},
 "tasks": [{"source_index": int | None, "title": str, "description": str,
            "deadline": {"kind": "explicit_date" | "relative" | "not_found" | "uncertain",
                         "date": "YYYY-MM-DD" | None, "wording": str | None, "page": int | None, "quote": str | None},
            "internal_date": "YYYY-MM-DD" | None,   # None only for relative/uncertain
            "assigned_to": str, "assigned_to_name": str}],
 "no_task_reason": str | None,            # extraction.action_assessment == "none_found"
 "undo_until": "ISO datetime" | None}     # filled by Task 5
```

State rules (pin each with a test): `Queued`/`Processing` → `analyzing`; `Error`, or extraction `readability == "unreadable"` → `failed`; `Received`, or Needs Review without a successful Run → `not_started`; Needs Review/Needs Clarification with any `Incoming Letter Action` row → `draft` (tasks come from the rows, not the Run); with a successful Run and no rows → `proposal`; `Completed` → `done`. `suggested` is set only when `ambiguity == "none"`, exactly one candidate, and that candidate is `can_select` (candidates without write access are returned with `"can_select": false` and are not selectable); `why` is built from the first reason (`"Steuernummer … im Schreiben"`-style text from `identifier_type` + evidence quote) and `page` from its evidence.

- [ ] **Step 1: Write failing tests** using `PosteingangTestCase` and the synthetic fixture Run pattern from `test_posteingang_confirmation.py::test_all_sixteen_gold_reviews_through_runtime`: one test per state above; one asserting `suggested` is None for `multiple_matches`; one asserting a relative deadline yields `internal_date is None` and the wording/page are present; one asserting `assigned_to == doc.received_by` for an operator uploader.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement `simple_card` and add it to `get_letter_workspace`**
- [ ] **Step 4: Run `test_posteingang_simple` and `test_posteingang_workspace`, expect PASS**
- [ ] **Step 5: Commit** `"Project a one-click Posteingang card"`

### Task 3: Extract the task-creation core from `confirm`

**Files:**
- Modify: `kanzlei_erp/posteingang_confirmation.py:325-400`
- Test: `kanzlei_erp/tests/test_posteingang_confirmation.py` (existing, unchanged)

**Interfaces:**
- Produces: `_complete(doc, review: dict, fields: dict, receipt, payload: dict, operation: str) -> dict` — everything in `confirm` from `accepted = [...]` to the `_finish(...)` return (Task permission, assignee check, correction readiness, atomic write, Task insert, assignment, `_finish(doc, operation, ...)`). The result dict additionally contains `"tasks_modified": {task_name: str(modified)}` read after assignment. `confirm` keeps its own pre-checks (`MULTIPLE_ADDRESSEES`, `RESULT_NOT_APPLIED`, status `Needs Review`) and calls `_complete(..., "confirm")`.

- [ ] **Step 1: Run `test_posteingang_confirmation` and `test_posteingang_confirmation_concurrency` to record the green baseline**
- [ ] **Step 2: Extract `_complete`; add `tasks_modified` to the result**
- [ ] **Step 3: Rerun both modules, expect PASS with the same count; add one assertion to `test_confirm_creates_one_task_and_standard_assignment` that `result["tasks_modified"][task.name] == str(task.modified)`**
- [ ] **Step 4: Commit** `"Extract Posteingang confirmation core"`

### Task 4: `confirm_simple`

**Files:**
- Modify: `kanzlei_erp/posteingang_confirmation.py` (new whitelisted function; add `"selected_customer", "customer_reason"` to `WRITABLE`; make `_write_actions(..., replace=True, run=..., proposals=...)` skip `original_proposal_json` when `source_index is None`)
- Modify: `kanzlei_erp/public/js/posteingang_review.bundle.js` (`review_rpc` accepts `confirm_simple`; add message for `TASK_CHANGED`, `UNDO_EXPIRED`)
- Test: `kanzlei_erp/tests/test_posteingang_simple.py`

**Interfaces:**
- Consumes: `_begin`, `_check_revision`, `_complete` (Task 3), `simple_card`, `working_date`, `company_holidays`, `matching._match`, `matching._customer`.
- Produces: `@frappe.whitelist(methods=["POST"]) confirm_simple(letter=None, expected_revision=None, request_key=None, origin=None, customer=None, tasks=None, **extra) -> dict` (same result shape as `confirm`, plus `tasks_modified`).
  - `origin` ∈ `{"proposal", "manual"}`; `tasks` is a JSON list (max `max_actions`) of `{"source_index": int|None, "title": str≤140, "description": str≤4000 (may be "" → stored as title), "external_date": "YYYY-MM-DD"|None, "internal_date": "YYYY-MM-DD"|None, "assigned_to": str}`; `[]` means "ablegen ohne Aufgabe".
  - Allowed statuses: `Needs Review`, `Needs Clarification`; `Received`/`Error` only with `origin="manual"`; else `INVALID_TRANSITION`.
  - `origin="proposal"` requires the letter to have no action rows (state `proposal`) and every `source_index` to exist in the latest Run; else `INVALID_REQUEST`. A proposal task whose deadline kind is `relative`/`uncertain` must carry `external_date`, else `DEADLINE_REQUIRED`.
  - `internal_date` None → `working_date(external_date, today, company_holidays())`.
  - In one `_atomic()`: set `selected_customer`/`customer_reason` (reason per Global Constraints; `multiple_addressees` is allowed here), write actions (`replace=True`; proposal actions not in `tasks` become `decision="reject"`, `rejection_reason="Nicht ausgewählt"`), set `applied_run` to the latest Run for proposal origin, build the review with `mode="confirm"`, `human_verified=True`, `assignment_decision="assigned"`, `scheduling_decision="scheduled"`, `needs_planning=False`, bases per Global Constraints, run `validate_review(confirm=True)`, then `_complete(..., "confirm_simple")`. The event payload stores `{"origin", "customer", "tasks"}`.

- [ ] **Step 1: Write failing tests**
  - proposal happy path: one Task, `kanzlei_customer` = suggested, `exp_end_date` = working date, event `confirm_simple` with `origin="proposal"`, letter `Completed`, `human_verified` stored, `customer_reason == "Vorschlag übernommen"`;
  - two proposal actions, one sent → one Task, other action row `reject` / `"Nicht ausgewählt"`;
  - `tasks=[]` with `none_found` proposal → `no_actions_reason == "Vom Assistenten: keine Handlung erforderlich, übernommen"`, no Task;
  - relative deadline without `external_date` → `DEADLINE_REQUIRED`, nothing written; with it → basis starts `"Vom Benutzer eingetragen"`;
  - `multiple_addressees` + explicit customer → Completed, reason `"Mehrere Empfänger, Mandant manuell gewählt"`;
  - `origin="manual"` on an `Error` letter → Completed; `origin="proposal"` on `Error` → `INVALID_TRANSITION`;
  - `origin="proposal"` on a letter with legacy action rows → `INVALID_REQUEST`;
  - same `request_key` twice → second returns `replayed=True`, Task count 1;
  - stale `expected_revision` → `REVISION_CONFLICT`, no Task;
  - assignee without Customer read → `ASSIGNEE_NOT_ALLOWED`, nothing written;
  - customer the operator can only read (not write) → `PermissionError`, nothing written.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `test_posteingang_simple`, `test_posteingang_confirmation`, `test_posteingang_task_access`, expect PASS**
- [ ] **Step 5: Commit** `"Add atomic one-click Posteingang confirmation"`

### Task 5: `undo_confirm`

**Files:**
- Modify: `kanzlei_erp/posteingang_confirmation.py`
- Modify: `kanzlei_erp/posteingang_task_access.py` (new context manager next to `creating_task`)
- Modify: `kanzlei_erp/posteingang_simple.py` (`undo_until` in the card)
- Test: `kanzlei_erp/tests/test_posteingang_simple.py`

**Interfaces:**
- Produces: `undoing_confirmation(letter_name: str, tasks: set[str])` context manager in `posteingang_task_access.py`; `protect_task` and `_todo_check` allow deletion only for those task names while it is active. `@frappe.whitelist(methods=["POST"]) undo_confirm(letter=None, expected_revision=None, request_key=None, **extra) -> dict` (`_finish` result with `status="Needs Review"`).
- Rules: letter `Completed`, latest confirm-type event is `confirm_simple`, `confirmed_by == session user`, now − `confirmed_at` ≤ 600 s (else `UNDO_EXPIRED`), no `replaces_letter`/`replacement_letter`, every created Task's current `modified` equals `tasks_modified` from the receipt (else `TASK_CHANGED`). In one `_atomic()`, in this order (the Link `Incoming Letter Action.created_task` blocks Task deletion otherwise): 1) clear `created_task` and delete the action rows (the card returns to `proposal`); 2) inside `undoing_confirmation`, delete the assignment ToDos and then the Tasks with `frappe.delete_doc` (not `frappe.db.delete`, so link checks and `on_trash` hooks run); 3) clear `selected_customer`, `customer_reason`, `applied_run`, `confirmed_*`, `confirmation_receipt_json`, `no_actions_reason`, set `review_outcome="pending"`, `status="Needs Review"`, event `undo_confirm`.
- `simple_card` sets `undo_until = confirmed_at + 600 s` only when the rules above would currently pass for the session user.

- [ ] **Step 1: Write failing tests** (deletion goes through `frappe.delete_doc`): undo within window → Task and ToDo gone, letter `Needs Review`, card `state == "proposal"`, then `confirm_simple` again creates a fresh Task; after 601 s (patch `now_datetime`) → `UNDO_EXPIRED`; Task status changed → `TASK_CHANGED`; other operator → `PermissionError` or `UNDO_EXPIRED`-free refusal; plain Task deletion outside undo still denied (`test_posteingang_task_access` stays green); legacy `confirm` letters cannot be undone.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `test_posteingang_simple`, `test_posteingang_task_access`, `test_posteingang_confirmation`, expect PASS**
- [ ] **Step 5: Commit** `"Allow undoing a one-click Posteingang confirmation"`

### Task 6: Client card model

**Files:**
- Create: `kanzlei_erp/public/js/posteingang_simple.bundle.js`
- Test: `tests/posteingang-simple.test.cjs` (same `vm` harness style as `tests/posteingang-review.test.cjs`)

**Interfaces:**
- Consumes: `card` shape from Task 2.
- Produces on `frappe.posteingang`:
  - `cardChoice(card) -> {customer: string|null, selected: boolean[], deadlines: (string|null)[], manual: null}` — initial UI choice: `customer = card.customer.suggested?.customer ?? null`, all tasks selected, deadlines from explicit dates.
  - `missingFields(card, choice) -> [{key: "customer" | "deadline:<i>", text: string}]`.
  - `primaryAction(card, choice) -> {kind: "accept" | "archive" | "manual", label: string}` — labels `"Übernehmen"`, `"{n} Aufgaben übernehmen"`, `"Ohne Aufgabe ablegen"`, `"Manuell erstellen"` (for `failed`/`not_started`).
  - `simplePayload(card, choice) -> {origin: "proposal", customer, tasks}` matching Task 4; dates converted from `TT.MM.JJJJ` to ISO; throws `Error("MISSING")` if `missingFields` is non-empty.
  - `manualPayload(form) -> {origin: "manual", customer, tasks}` from `{customer, rows: [{title, description, external_date, internal_date, assigned_to}]}`.
  - `listChip(row) -> {tone: "ready"|"need"|"busy"|"fail"|"done", text: string}` from a `get_queue` row (`need` when `matching.ambiguity !== "none"` or `deadline.relative_count + deadline.uncertain_count > 0`).

- [ ] **Step 1: Write failing tests** — `multiple_matches` card → `missingFields` = `[{key:"customer", …}]`; after setting customer → `[]`; relative task → `deadline:0` missing until `"10.12.2026"` set, then payload has `external_date: "2026-12-10"`, `internal_date: null`; unchecking all tasks → `primaryAction.kind === "archive"` and `tasks: []`; `failed` card → `kind === "manual"`; `listChip` for each status.
- [ ] **Step 2: `node --test tests/posteingang-simple.test.cjs`, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `"Add Posteingang one-click client model"`

### Task 7: Rewrite the Desk page

**Files:**
- Modify: `kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.js` (replace the stage editor)
- Modify: `kanzlei_erp/kanzlei_erp/page/posteingang/posteingang.css`
- Modify: `kanzlei_erp/public/js/posteingang_intake.bundle.js` (`open({files})` preselects dropped files)
- Modify: `kanzlei_erp/hooks.py` / bundle loading if `posteingang_simple.bundle.js` must be required alongside `posteingang_workspace.bundle.js`
- Test: `tests/posteingang-flow.test.cjs`, `tests/posteingang.test.cjs` (update to the new page), `tests/posteingang-simple.test.cjs`

**Interfaces:**
- Consumes: `get_letter_workspace().card` (Task 2), `review.confirm_simple`, `review.undo_confirm` via the existing `transport` (`review_rpc`), `frappe.posteingang.open` (intake), `get_queue` with `state: "open"` and `state: "history"`, Task 6 helpers.
- Layout and copy follow the mockup exactly: rail (drop zone + „PDF auswählen“, „Offen · N“, „Erledigt“), card sections, PDF pane toggled by „Original“, `S. N ansehen` scrolls the iframe to `#page=N`, toast with `aria-live="polite"` bound to the confirmed letter id, „Nächstes Dokument“. Container breakpoints 1100px / 680px. Use Frappe CSS variables (`--card-bg`, `--border-color`, `--primary`, `--text-muted`), not the mockup's fixed hex values.
- Behaviour to keep from the old page: `client.check()` user binding, `suspend()` on route change, clearing protected state on `PERMISSION`, revision reload on `REVISION_CONFLICT` with a notice, frozen `request_key` reuse on unknown outcome.
- Manual form keeps typed values across re-render (store per letter in page state). Customer field uses `matching.search_customers`; assignee uses `review.search_assignees`.
- `draft` card (legacy letter with a human draft) opens directly in the manual form, prefilled from `card.tasks`; it submits with `origin="manual"`.
- Header shows „Eingang: TT.MM.JJJJ“ or „Eingang: unbekannt“ read-only. There is no server operation to change the receipt date after upload, so the mockup's „ändern“ link is dropped (the date is set in the upload dialog, as today).
- `not_started` card shows „Analyse starten“ (existing `analysis.queue_analysis`) when `analysis.allowed_operations` contains `"queue_analysis"` (works for `provider=fake` with both flags off), plus „Manuell erstellen“.
- Must keep, rebuilt in the new layout:
  - **Correction** — „Korrektur anlegen“ on `done` cards when `review.allowed_operations` has `create_correction`, and the „Ursprüngliche Aufgabe prüfen“ disposition block when it has `correction_disposition` (same RPCs and dialogs as today, moved, not redesigned).
  - **Discard letter** — secondary „…“ menu on open cards: „Dokument verwerfen“ with required reason, via existing `analysis.cancel`. This is the only exit for mail that belongs to no Mandant (advertising, misdirected, wrong upload), because `confirm_simple` requires a customer.

- [ ] **Step 1: Update JS tests for the new page: removed stage labels absent; card render for each `state`; Übernehmen calls `confirm_simple` once on double click; undo from toast targets the confirmed letter after switching; „Dokument verwerfen“ calls `analysis.cancel` with the reason; „Korrektur anlegen“ appears only with `create_correction`; „Analyse starten“ follows `analysis.allowed_operations`**
- [ ] **Step 2: Run `node --test tests/posteingang*.test.cjs`, expect FAIL**
- [ ] **Step 3: Implement the page and CSS; delete stage-only code paths (steps, human-verified checkbox, retained-result dialog, per-action reason fields)**
- [ ] **Step 4: Run `node --test tests/*.test.cjs`, expect PASS; `node --check` on changed files**
- [ ] **Step 5: Commit** `"Rebuild Posteingang page around the proposal card"`

### Task 8: Copy, docs, acceptance

**Files:**
- Modify: `kanzlei_erp/translations/de.csv`, `kanzlei_erp/translations/ru.csv` (every new `__()` string; remove rows only used by the deleted editor)
- Modify: `docs/posteingang.md` (section «Основной процесс» rewritten to the new two-button flow, undo, defaults, legacy letters)
- Test: `kanzlei_erp/tests/test_posteingang_acceptance.py` only if it drives the removed UI flow (keep server scenarios)

- [ ] **Step 1: Add translations; run `test_navigation` (translation loading) and the JS suite**
- [ ] **Step 2: Update `docs/posteingang.md`**
- [ ] **Step 3: `bench build --app kanzlei_erp` and `bench --site development.localhost clear-cache` in the container**
- [ ] **Step 4: Browser scenario on `development.localhost` (Playwright/Orca browser, `provider=fake`, a PDF from `kanzlei_erp/tests/fixtures/posteingang/` because fake answers only known hashes): upload → „Analyse starten“ (auto-analysis is off) → card → Übernehmen → open Task → Rückgängig → card back → Manuell erstellen → Aufgabe erstellen. Repeat the card view at 390px and in Russian.**
- [ ] **Step 5: Run all Posteingang Python modules touched (`test_posteingang_simple`, `_confirmation`, `_confirmation_concurrency`, `_task_access`, `_workspace`, `_acceptance`)**
- [ ] **Step 6: Commit** `"Document and translate one-click Posteingang"`
