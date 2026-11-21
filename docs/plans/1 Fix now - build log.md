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
| 2 Activity log admin-only | B-2 | done (first half) | `a53ccac` |
| 3 Sessions | B-3 | done | `e541e80` |
| 4 Sender resilience | B-6, B-23 | done | `21bfe79` |
| 5 Calling → Reopen | B-22 | done | `7b9ea95` |
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

### Package 2 — B-2 — done (`a53ccac`)

- `@admin_required` on `GET /api/logs`. Test: `tests/test_security.py` §7.
- `nonadmin_access.py` now prints `GET /api/logs -> 403`.
- Audit marks B-2 "resolved, first half": owners on log lines still open.
- Harness: `python tools/audit/serve.py seed` then run `serve.py` in the
  background; a seeded copy is at `/tmp/claude-0/audit-seed.db` (not durable).
  Restart the server after code changes (it imports the app once).

### Package 3 — B-3 — done (`e541e80`)

- `users.session_version` (column loop). Login stores `sv`. `_require_login`
  loads the user (`g.user`), refuses deleted users or stale `sv` (missing = 0).
- `is_admin()` and `_current_user()` in app.py; `admin_required`, `me()`,
  `/api/users/me`, the password route and the two built-in-outcome checks use
  the row. `session["is_admin"]` is still written, for display only.
- `change_password(..., end_sessions=True)` returns the new version; the
  sign-in hash upgrade passes `end_sessions=False` (otherwise a sign-in that
  re-hashes would sign itself out).
- Test: `tests/test_sessions.py`. Check: security_findings S-3 now 401.
- Suite: 27 files pass.
- Operator note: user mid-turn said "if you have a better suggestion for
  anything feel free to give your feedback" — collect suggestions for the
  hand-back.

### Package 4 — B-6, B-23 — done (`21bfe79`)

- scheduler.py: `_process_campaign` (one campaign, own try), `_one_pass` (body
  of the loop), `_run_loop` guards each pass, `_heartbeat`/`_last_pass_at`,
  `ensure_running()`, `status()`. Inter-email pause is `_stop_event.wait`.
- db.py: `_check_campaign_fields`, `CampaignValueError`, `CAMPAIGN_STATUSES`;
  `add_log` never raises.
- app.py: `SHOUTREACH_NO_SCHEDULER`, 400 on bad create/PATCH, dashboard route
  calls `ensure_running()` and returns `sender`.
- Dashboard: `#dash-sender` line under the Email card (`_renderSender`).
- Choice (differs from plan wording): `last_pass_at` is stamped at the start
  and end of each pass and before each pause, not only at the end — one pass
  with sends due can take 20+ minutes (10 sends x up to 120 s per campaign),
  which would otherwise trip the 10-minute warning.
- `test_email_checking_toggle.py` inspected `_run_loop` source for the reply
  gate; it now inspects `_run_loop` + `_one_pass`.
- The two scheduler check scripts plant "lots" in SQL when the save refuses it.
- Suite: 27 files pass.

### Package 5 — B-22 — done (`7b9ea95`)

- `@owned("business", "cid")` on `api_reopen_call_lead`. No other route used
  the `call_lead` guard. Test: `tests/test_calling.py` §25 (verified to fail on
  the old guard). `calling_reopen.py`: 6/6 now 200.
- Harness restore: `cp /tmp/claude-0/audit-seed.db /tmp/shoutreach-audit/audit.db`.
