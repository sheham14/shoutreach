# Plan 1 · Fix now — build log

A running log of the Plan 1 build, kept so a later session can pick up where
this one stopped. Newest entry last. Branch: `claude/vibrant-archimedes-i2ilk9`
(the session's branch, used instead of the plan's `plan-1-fix-now` with the
operator's go-ahead). Nothing is pushed to `master`.

## How to resume

1. `git fetch origin claude/vibrant-archimedes-i2ilk9 && git switch claude/vibrant-archimedes-i2ilk9`
2. Read the last entry below: it names the package in progress and what's left.
3. Environment set-up that was needed in a fresh container:
   `pip install --ignore-installed blinker -r requirements.txt Pillow beautifulsoup4 playwright playwright-stealth`
   (Debian's blinker blocks a plain install; `test_extraction.py` and
   `test_resilience.py` import the scraper, which needs playwright.)

## Status

| Package | Finding | State | Commit |
|---|---|---|---|
| 1 Opt-outs permanent | B-1 | done | `9e51276` |
| 2 Activity log admin-only | B-2 | not started | |
| 3 Sessions | B-3 | not started | |
| 4 Sender resilience | B-6, B-23 | not started | |
| 5 Calling → Reopen | B-22 | not started | |
| 6 Confirm before sending | B-9 | not started | |
| 7 Visible faults | F-20, F-21/33, F-22, F-25, F-30, F-9, B-10, B-14, B-15 | not started | |

## Log

### 2026-11-21 — start

- Baseline: all 25 test files pass (after installing the worker's packages).
- No `./outreach.db` exists in the checkout; tests use their own temp DBs.

### Package 1 — B-1 — done (`9e51276`)

- New table `suppressed_addresses(email PK, reason, created_at)`, no owner.
  Helpers in db.py under `# ── Suppression`: `_record_suppression`,
  `_suppressed_reason`, `_apply_suppression` (the one helper every call site
  uses). `unsubscribe_contact`, `mark_bounced`, `increment_soft_bounce` write
  the table even with no row. Soft-bounce count now reads MAX across copies.
- Enforced in: `upsert_businesses`, `upsert_wa_leads`, `create_email_lead`,
  `add_business_email`, `_restore_one`, `update_email_lead` (refuses moving a
  suppressed address to another status; setting unsubscribed/bounced by hand
  is recorded; changing the address to a suppressed one applies it), and
  `get_due_enrollments` (NOT EXISTS backstop).
- Migration `_migrate_suppressed_addresses`, marker `_migrated_suppressed_addresses`.
- Choice: unsubscribed rows are never downgraded to bounced by a later bounce
  (old `mark_bounced` overwrote them). A bounced address can still be set to
  unsubscribed by hand; nothing else.
- Choice: `add_business_email` now also marks the business do-not-contact when
  the address unsubscribed (same as an existing copy would get).
- Checks: suppression_db, suppression_routes, security_findings S-1a/S-1c all
  show the address suppressed and off the due list. (suppression_db's bulk
  delete step calls a function that doesn't exist, `delete_contacts_bulk` —
  a fault in the check script, not the app.)
- Suite: 26 files pass.
