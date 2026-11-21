# Plan 1 · Fix now

**For:** a coding session. **Branch:** `plan-1-fix-now`. **Source:** audit §2,
§5, §6. Read [README.md](README.md) in this folder first; its rules apply.

Seven packages, one commit each, in this order. Nothing here changes how the
app looks beyond removing visible faults.

Two fix-now items from the audit are **not** in this plan: F-23 and F-24 (the
WhatsApp Booked tab). That tab is replaced by a Meetings page in Plan 2.
B-24 (backups) is the operator's own job; see the README.

---

## Package 1 · Opt-outs become permanent (B-1)

**The fault.** An unsubscribe or bounce is only a `status` on whichever
`email_leads` rows exist at that moment. Three ordinary actions bring the
address back as `active` and onto `get_due_enrollments`: the other operator
importing it later; restoring a deleted lead after the opt-out; editing the
lead's status. `tools/audit/checks/suppression_*.py` and
`security_findings.py` show all three.

**The design (decided).**

- A new table, `suppressed_addresses(email TEXT PRIMARY KEY, reason TEXT NOT
  NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')))`. `email` is
  lower-cased and trimmed. `reason` is `unsubscribed` or `bounced`;
  `unsubscribed` wins if both happen. **No owner column, and no owner filter
  anywhere it is read.** That is the hard rule in CLAUDE.md, not an oversight.
- `unsubscribe_contact` and `mark_bounced` write this table **always**, even
  when no `email_leads` row matches. (Today the public unsubscribe link says
  "unsubscribed" for a lead that is in Recently deleted, and records nothing.)
  `increment_soft_bounce` must do the same at the moment it turns an address
  into a bounce.
- `address_suppression(email)` becomes the single question "may this address
  be emailed?". It reads the new table first, then falls back to today's scan
  of rows.
- **A late-arriving copy must end up exactly as a copy that existed at the
  time would have.** Whatever `unsubscribe_contact` does to another operator's
  existing row today — the row's `status`, its business's `do_not_contact`,
  its enrollments — is what must happen to a row created or restored later.
  Write that as one helper and call it from every place below; don't
  re-implement it per call site.

**Where it must be enforced.**

| Place | Function | What changes |
|---|---|---|
| Any import that creates an address | `upsert_businesses`, `upsert_wa_leads`, `create_email_lead` | A new row for a suppressed address is created suppressed. The conflict branch must never turn a suppressed row `active` |
| Typed for a meeting invite | `add_business_email` | Already checks; route it through the same helper |
| Restore from Recently deleted | `restore_deleted_leads` / `_restore_one` | After the rows are re-inserted, apply suppression to every restored address |
| Editing a lead | `update_email_lead` | Refuse to move a suppressed address to any other status, with a plain message. Setting `unsubscribed` or `bounced` by hand goes through the helper, so it is remembered too. The Status control in the edit form (`cf-status`) stays |
| The send loop | `get_due_enrollments` | Add `AND NOT EXISTS (SELECT 1 FROM suppressed_addresses s WHERE s.email = el.email)`. This is the backstop: even a row forced `active` by hand is not sent |

**Migration.** One guarded one-shot, `_migrate_suppressed_addresses`, with its
own settings marker like the others: copy every address whose row is
`unsubscribed` or `bounced`, for every owner, into the table. Re-running
`init_db()` must not undo anything.

**Not in this package.** There is no screen to remove an address from the
list, on purpose. Bulk delete of addresses stays a hard delete (Fable 3.8);
with this table it no longer forgets a bounce.

**Tests.** A new `tests/test_suppression.py`, covering at least: opt-out then
the other operator imports (browser route and worker route); the same for a
bounce; a WhatsApp import that carries an email; opt-out while the lead is in
Recently deleted, then restore; editing status back is refused; an address
with no row at all is remembered; a row forced `active` in SQL is still absent
from `get_due_enrollments`; the migration backfills both operators; nothing in
the table or its queries mentions an owner. Add the cross-operator import case
to `tests/test_ownership.py` beside the existing suppression section.

**Done when** the three check scripts show the address suppressed on every
path, and Handover §3 names the table in its "deliberately span every
operator" list.

---

## Package 2 · Activity log is admin-only (B-2)

`GET /api/logs` (`api_logs`) has no admin check. Add `@admin_required`.

Log lines carry both operators' recipient addresses and subjects, so even an
admin sees the other operator's sending here. Giving log lines an owner is a
later job; note it in the audit as the remaining half of B-2 rather than
marking B-2 fully resolved.

**Test:** in `tests/test_security.py`, a non-admin gets 403.
**Check:** `tools/audit/checks/nonadmin_access.py` (needs the local server).

---

## Package 3 · Signing out really signs out (B-3)

**The fault.** A sign-in is a signed cookie the server trusts without looking
the user up. Deleting an account or changing its password leaves an open
session working, and `is_admin` is read from the cookie.

**The design (decided).**

- `users.session_version INTEGER NOT NULL DEFAULT 0`, added through the
  existing column loop in `init_db`.
- Sign-in stores it in the session as `sv`.
- `_require_login` loads the user row once per request (keep it on `g`). The
  request is refused — session cleared, 401 for `/api/`, redirect otherwise —
  when the user no longer exists, or when the session's `sv` differs from the
  row's. **A session with no `sv` counts as version 0.** That keeps today's
  live sessions and every test's hand-made session working until a password
  change bumps the version.
- `is_admin` comes from the row on every request, not from the cookie.
  `admin_required` and `me()` read it from there.
- `change_password` bumps `session_version`. When someone changes their **own**
  password, their current session is re-issued with the new version so they
  stay signed in on that device and are signed out everywhere else.
  `reset_password.py` bumps it too.

**Tests.** A new `tests/test_sessions.py`: a deleted user's session is
refused; an admin whose password another admin changed is refused; changing
your own password keeps this session and kills a second one; a demoted admin
loses admin on the next request; a hand-made session without `sv` still works
for an existing user. Run the whole suite: every other test file fakes
sessions and must still pass untouched.

**Check:** `security_findings.py`, section S-3.

---

## Package 4 · The sender can't be stopped by one error (B-6, B-23)

**The faults.** `process_queue` walks every operator's campaigns inside one
`try`, so an error in one campaign ends the pass for all after it.
`_run_loop` has no `try` of its own, and the jobs' error handlers call
`add_log`; when that write hits "database is locked", the thread ends and is
never restarted.

**The changes.**

1. `process_queue`: move the `try/except` inside the campaign loop. Log the
   campaign's name and carry on to the next.
2. `update_campaign` and `create_campaign`: check and bound what they store.
   `daily_limit` 0–2000; the two hours 0–24; `min_delay_secs` and
   `max_delay_secs` 0–3600 with min ≤ max; `bounce_pause_pct` 0–100; `status`
   only the values the app already uses. The PATCH route answers 400 with a
   plain message instead of storing a bad value.
3. `_run_loop`: wrap one whole pass in `try/except Exception` that logs to the
   process log and continues. Nothing may end the loop except `stop()`.
4. `add_log` must not raise. If its write fails, write the line to the process
   log and return.
5. A watchdog: `scheduler.ensure_running()` restarts the thread if it is found
   dead, under a lock. Call it from somewhere cheap and frequent — the
   Dashboard's data route is enough.
6. Make it visible. Record when the last pass finished. `/api/dashboard`
   returns `sender: {alive, last_pass_at}`; the Dashboard shows one quiet line
   ("Email sender last ran 2 minutes ago") that turns into a warning when the
   last pass is more than ten minutes old. Keep the markup minimal; the
   Dashboard is redrawn in the redesign.
7. `app.py` skips `scheduler.start()` when `SHOUTREACH_NO_SCHEDULER=1`. Set it
   in the `boot()` of every test file you touch in this plan. Converting the
   remaining test files is welcome but not required here.

**Tests.** In `tests/test_resilience.py`: a campaign with a bad stored value
doesn't stop the next campaign's send (fake `send_email`); the PATCH route
refuses bad values; the loop survives a pass that raises; the loop survives
`add_log` failing; `ensure_running` restarts a dead thread.

**Checks:** `scheduler_shared_loop.py` and `scheduler_thread_death.py`.

---

## Package 5 · Calling → Reopen (B-22)

`api_reopen_call_lead` is guarded with `@owned("call_lead", "cid")`, but the
page sends a business id, so it answers 404 for the operator's own lead.
Guard on the business instead. Another operator's lead must still be 404.

**Test:** in `tests/test_calling.py`, with a lead whose business id and
call-lead id differ. **Check:** `calling_reopen.py`.

---

## Package 6 · Ask before sending real email (B-9)

Two buttons start real email with no confirmation.

- **Activate** (`activateCampaign`): before posting, show the app's own dialog
  (`chooseDialog`): "Start sending? This will email **N** people, up to **X**
  a day, starting **when**." Confirm button: "Start sending".
- **Enroll All Active** (`enrollAll`): the same dialog with the number of
  addresses, and one extra sentence when the campaign is already active.

The server supplies the numbers: a small route returning, for one campaign of
the caller's, how many enrolled people are actually sendable (queued, active,
not suppressed, business not do-not-contact), the daily limit, and whether the
send window is open now. For Enroll All, the count of the caller's active
addresses not already enrolled.

**Tests:** the counts respect the owner, suppression and do-not-contact.

---

## Package 7 · Visible faults

Each is small. Verify by running the local copy and looking
(`tools/audit/README.md`): take the desktop and phone walk-throughs before and
after.

| Finding | Change |
|---|---|
| **F-20** Two "Done" buttons on Email → To do | The row draws a desktop button cell and a phone one (`m-keep`); on a computer both show. Give the phone-only cell its own class and hide it at desktop widths. The heading row should then match the number of cells |
| **F-21 / F-33** Raw timestamps | One helper for showing a time: "Today 3:30 pm", "Tomorrow 11:00 am", "Wed 25 Nov, 10:00 am", and with the year when it isn't this year. It must take the kind of value: **database timestamps are UTC** (`sent_at`, `called_at`, `created_at`, `next_send_at`, `sent_date`) and are converted to the viewer's time; **times a person typed are wall-clock** (`next_call_at`, `next_action_at`, `meetings.starts_at`) and are shown exactly as stored — Handover §5c forbids converting them. Replace the places that chop the raw string in `calling.js`, `campaigns.js`, `whatsapp.js` and the lead panel. `whenLocal`, `shortDate` and `localDateTime` become thin callers of the new helper or are removed |
| **F-22** Phone menu button covers the logo | Hide the menu button while the drawer is open; the dimmed area already closes it |
| **F-25** "Check for email replies & send" shown to non-admins | Hide it unless the signed-in user is an admin |
| **F-30** iPhone zooms on every text box | In the phone-width block of `main.css`, text boxes, dropdowns and text areas get a 16 px font size. Some fields set their size inline, so this rule needs `!important`; say so in a comment |
| **F-9** WhatsApp list rows 100 px tall | In the To do list: no Campaign column when one campaign is selected; otherwise the campaign on one line, cut with "…" and the full name in a tooltip. No Version column unless a lead in the list has a version. The number never wraps |
| **B-10** "Sent today" is the whole install's | `get_stats` returns the operator's own sends today, and separately the shared cap's tally and limit. The Dashboard shows "Sent today N" and beneath it, small, "all accounts: 67 of 200" |
| **B-14** Scraper status polled forever | Stop the timer when leaving Get leads; poll only while a scrape is queued or running |
| **B-15** WhatsApp opens with 11 requests | Fetch the summary once and the ready list once per open, and don't load the first lead twice |

**Tests:** B-10 gets a test (a second operator's sends don't appear in the
first's figure). The rest are checked by eye; say in the hand-back which you
looked at and at what widths.

---

## Out of scope

B-4, B-5, B-11, B-12, B-13, B-18 and B-25 to B-30 are real and are not in
this plan. Don't start them here.

## Hand-back

List, per package: the commit, the tests added, the check script's output
after the fix, and anything you chose differently from this plan and why.
