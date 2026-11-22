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
| 6 Confirm before sending | B-9 | done | `a1344fe` |
| 7 Visible faults | F-20, F-21/33, F-22, F-25, F-30, F-9, B-10, B-14, B-15 | done | `ebcc86c` |

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

### Package 6 — B-9 — done (`a1344fe`)

- `db.get_campaign_send_preview` → {sendable, enrollable};
  `GET /api/campaigns/<id>/send-preview` adds status, daily_limit,
  window_open, next_open, starts_label.
- `activateCampaign(id, reopen)` and `enrollAll()` confirm via chooseDialog.
  The detail page's Activate used to call `openCampaign` straight after
  (before the answer); it now passes `reopen=true` instead.
- Test: `tests/test_suppression.py` §7. Browser: both dialogs on the harness.
- Browser script: `/tmp/claude-0/b9.py` (Chromium is at
  `/opt/pw-browsers/chromium-1243/chrome-linux64/chrome`; call functions that
  open chooseDialog as `() => { fn(); }` or evaluate waits forever).

### Package 7 — visible faults — done (`ebcc86c`)

- F-20: `td.m-only` (phone-only cell) in main.css; Email To do uses it.
- F-21/F-33: `fmtWhen(value, kind, opts)` in tables.js ('utc' converts,
  'wall' never does); `whenLocal`/`shortDate`/`localDateTime` call it; all
  display chops replaced. `_clock` uses a no-break space before am/pm.
  `pipelineOverdue` compares with the local date.
- F-22: `body.drawer-open` hides `.sidebar-toggle` (utils.js + main.css).
- F-25: `#run-now-btn` starts `hidden`; app.js shows it for admins.
- F-30: 16px `!important` on inputs/selects/textareas in the phone block.
- F-9: `_renderWaQueue` — no Campaign column when one is picked, `.cell-clip`
  (150px) otherwise, Version only if used; `.wa-queue-table` lets names wrap
  and trims padding so the number fits at 1280.
- B-10: `get_stats` `today` = own sends (SQL, UTC); `all_accounts_today` /
  `all_accounts_limit`; `#dash-cap` line. Test: test_ownership.py §7b.
- B-14: `_scraperPollEvery`/`stopScraperPoll`; `showSection` stops it; 2 s
  busy, 15 s idle (deviation: plan said poll only while busy).
- B-15: `refreshWaTotals` fetches with `since`, `refreshWaCounts` reuses it
  within 2 s. 7 requests, no repeats.
- Harness gotcha: serve.py caches Jinja templates — restart it after editing
  templates, or screenshots show the old markup.

### Follow-up fix (`c1eeb61`)

- Rehearsed the upgrade on a database built by `c92729a`: only
  `suppressed_addresses` (6 rows = every opted-out address) and two settings
  markers change; a second `init_db()` changes nothing; sessions version 0.
- Found: the min ≤ max delay check ran on every save, so a campaign stored
  with min > max could no longer be paused, activated or auto-paused. Now it
  runs only when a save changes a delay. Test in test_resilience.py §7.
- All changed .py files compile under Python 3.11.

## Hand-back

**Built:** all seven packages. 16 of the audit's 19 fix-now items; the other
three are B-24 (operator's backup job) and F-23/F-24 (moved to Plan 2).
Suite: 27 test files, all pass (two new: test_suppression.py,
test_sessions.py; sections added to test_ownership, test_security,
test_calling, test_resilience).

**Check scripts after the fix** (all seven in tools/audit/checks):
suppression_db / suppression_routes / security_findings S-1a, S-1c — the
address ends suppressed and off the due list on every path; S-3 — a deleted
admin's session gets 401; S-6 — "lots" refused with 400; scheduler_shared_loop
— the other campaign still sends; scheduler_thread_death — alive after 16 s
locked; nonadmin_access — /api/logs 403, cofounder's "today" 0; calling_reopen
— 6/6 reopen. Still failing, out of scope: S-10 (B-8, backslash in a name),
S-4 (B-4, the audit fetching internal addresses).

**Chosen differently from the plan, and why:**

1. B-14: idle Get leads polls every 15 s instead of stopping, so "Worker
   connected" stays true when no scrape runs (the page's own stated design).
2. B-23: `last_pass_at` is also stamped at the start of a pass and before each
   pause; one pass with sends due can exceed the 10-minute warning.
3. B-1: a later bounce no longer overwrites "unsubscribed" (unsubscribed wins,
   as the plan says for the table); `add_business_email` now also sets
   do-not-contact for an unsubscribed address, like any late copy.
4. B-9: when no one enrolled is sendable, Activate still asks, with a sentence
   saying nothing will go out yet. Enroll All with nothing to add just says
   so in a toast.
5. Branch: `claude/vibrant-archimedes-i2ilk9` instead of `plan-1-fix-now`
   (session setup; the operator agreed).

**Surprises:** the audit check `suppression_db.py` calls
`db.delete_contacts_bulk`, which doesn't exist (its bulk-delete step never
ran). `measure.py` opens WhatsApp with showSection *and* setTab, which
double-loads the To do tab — that, not the app, is most of its "11 requests".
`test_email_checking_toggle.py` read `_run_loop`'s source; it now reads
`_one_pass` too. Section 13 of test_calling.py only passed because ids
coincided — §25 now pins B-22 with differing ids.

**Checked by eye** (Chromium, harness data): Email To do at 1440×900 and
390×844; WhatsApp To do at 1440×900 and 1280×720 (and campaign picked);
Dashboard at 1440×900; phone menu open at 390×844; Calling To do at 1440×900;
Activate and Enroll All dialogs at 1280×860; non-admin sidebar. Not checked:
a real iPhone (F-30).

**Reviewer commands:**

    for f in tests/test_*.py; do python "$f" || echo "FAIL $f"; done
    for c in suppression_db suppression_routes security_findings scheduler_shared_loop scheduler_thread_death; do python tools/audit/checks/$c.py; done
    python tools/audit/serve.py seed && python tools/audit/serve.py &   # then:
    python tools/audit/checks/nonadmin_access.py; python tools/audit/checks/calling_reopen.py

**Suggestions beyond the plan** (not built):

- The Dashboard's sender warning tells the operator to reload; the watchdog
  already restarts the thread on that load, so the line could say "restarted"
  when `ensure_running()` returns True.
- `enroll_contacts_bulk` doesn't check `suppressed_addresses` itself (the due
  list does). Harmless now, but enrolling a suppressed address shows it as
  "queued" in the campaign until the loop skips it; refusing at enrol would be
  clearer.
- Log lines still carry no owner (the other half of B-2) — worth doing with
  the Settings redesign, as the audit says.
- Before deploying: take a manual backup and confirm `timedatectl` on the VM
  says UTC (Handover §6a), since "Sent today" now compares in SQL.
