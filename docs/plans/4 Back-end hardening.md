# Plan 4 · Back-end hardening

**Draft, under review: do not build from this yet.**

**For:** a coding session. **Branch:** `plan-4-hardening`, from `master` once
Plan 2 is merged. **Source:** the audit's back-end findings still open after
Plans 1 and 2 (§5, §6) and the back-end items carried in §7. Read
[README.md](README.md) in this folder first; its rules apply.

Written 2026-11-22. Nothing in it is decided by the operator yet: every choice
the operator must make is marked **Decide first**, with a recommendation.
Settle those before the package that needs them, and record the answer here.

Nine packages, one commit each, in this order. The first two make every later
change safer: deploys that test and can roll back, and real tests for the
code that emails strangers unattended. Nothing here changes how the app looks,
apart from a few plain messages where today something fails silently.

**Not in this plan:** B-21 (content-security policy — needs the inline
handlers gone, which happens screen by screen in the redesign; switch it on at
the end of redesign phase 5); B-31 (the shape of the code — redesign phases
3–4); the VM's own settings (region, IP, journal size, compression — someone
has to look at the server, §7); `.xlsx` import and the cross-operator "already
talking to them" notice (deferred by choice, Handover §9).

---

## Package 1 · Deploys that test, stop, and can go back (B-18, B-27)

**The faults.** A push to `master` deploys without running a single test.
Package versions are open-ended (`flask>=3.0`), and the deploy step is a
third-party action pinned to a tag its owner can move. The script doesn't
stop on a failed pull or install, nothing checks the site came back, and a
failed migration leaves the site answering 502 until someone signs in to the
VM. Migration failures are logged at a level nobody sees.

**The changes.**

1. A `test` job in `.github/workflows/deploy.yml` that the `deploy` job
   `needs:`. Python 3.11 (the server's). It installs `requirements.txt` plus
   what two test files import from the scraper (`beautifulsoup4`,
   `playwright` — the package only, no browsers) and runs every
   `tests/test_*.py`, failing on the first non-zero exit.
2. Pin versions. Generate a lock file from the versions now on the VM
   (`pip freeze` there — ask the operator to run it, or read it from the
   last deploy log) and install from it, in CI and on the server.
3. Pin the deploy action to a full commit SHA, with the tag in a comment.
4. The remote script: `set -euo pipefail`; remember the commit before the
   pull; after the restart, wait and fetch `http://127.0.0.1:<port>/login`
   on the VM until it answers 200, up to 60 seconds. If it doesn't: put back
   the database backup this deploy just took, `git reset --hard` to the
   remembered commit, reinstall, restart, and fail the job so the run shows
   red.
5. `init_db()` logs a failed migration step at `ERROR`, not `debug`, and a
   migration that raises stops start-up with that error in the journal
   (today some are swallowed).

**Decide first.** Rolling back puts back the database as it was before the
deploy, so anything written in the minute between restart and failure is
lost. Recommendation: accept that — a failed start-up writes nothing.

**Tests.** The workflow itself is checked by running it: open a pull request
whose branch deliberately breaks one test and confirm the job goes red and
no deploy runs. A `tests/test_migrations.py` case: a migration that raises is
logged at ERROR and stops `init_db`.

**Done when** a push with a failing test never reaches the server, and a
deploy whose app won't start puts the previous version back on its own.

---

## Package 2 · A real test of the sending loop (B-26)

**The fault.** `send_email`, the reply and bounce parsers, and "move to the
next step after a send" are never run by any test; the two that mention
sending only search source text. This is the code that emails strangers
unattended, and package 4 changes it.

**The changes.**

1. Every test file sets `SHOUTREACH_NO_SCHEDULER=1` before importing `app`
   (Plan 1 did this only for the files it touched). Likely the cause of the
   occasional "database is locked" in the suite.
2. `tests/test_send_loop.py`: a fake SMTP server (`smtpd`-style, or
   `aiosmtpd` if added to dev requirements; **no real network**) and a fake
   IMAP mailbox serving canned messages. Drive full passes of
   `scheduler.process_queue` with an injected clock: step 1 sends, the
   enrolment advances, step 2 waits its delay; the per-campaign and shared
   caps hold; the send window holds; a reply (canned message threaded to the
   sent one) stops the sequence; an auto-reply doesn't; a hard bounce
   suppresses the address for every operator; three soft bounces do; an
   unsubscribe link works end to end.
3. Feed the reply and bounce parsers real-shaped messages (Gmail, Outlook,
   Zoho Mail bounce formats) kept as fixtures, scrubbed like
   `tests/fixtures/cal_*.json`.

**Done when** the suite exercises one full send-reply-bounce cycle against
fakes, and every test file runs with the sender off.

---

## Package 3 · Secrets encrypted at rest (B-13)

**The fault.** In plain text in the database, and in each of the five
database copies every deploy keeps: every sending account's SMTP and IMAP
password, the AI keys (`anthropic_api_key`, `gemini_api_key`,
`openai_api_key`), the workers' keys, the Cal.com webhook secret, and
`_secret_key`, which signs unsubscribe links and booking references — and
the session cookie too, unless `SECRET_KEY` is set in the server's
environment (`app.secret_key` falls back to it; check the VM). (The website key is already stored only as a hash; the
Zoho secrets went with Plan 2.) Anyone with one backup file has all of it.
The audit calls this the largest remaining risk that is not a bug.

**The design.**

- Encrypt those values with a key that lives **outside the database**, in
  the server's environment (a systemd `EnvironmentFile` readable only by the
  app's user, not in the repo, not in any backup of the database).
- One module, `secretbox.py`: `seal(text) -> "enc:v1:..."` and
  `open_(stored) -> text`, used at the few places these values are written
  and read. A value without the `enc:` prefix is read as plain text, so the
  migration can run in place and a half-done one still works.
- A guarded one-shot migration seals every existing value. Take a backup
  first, as always; this one changes stored secrets.
- If the key is missing at start-up, the app still starts, but sending,
  reply checks, AI and the Cal.com route refuse with a plain message on the
  Dashboard and in the log — never a crash loop, never silently sending
  nothing.

**Decide first.**

1. *The library.* Recommendation: `cryptography` (Fernet: authenticated,
   well reviewed). A new dependency on the server.
2. *Where the key lives and who backs it up.* Recommendation: a file on the
   VM (`/etc/shoutreach/secret.env`), with a copy kept by the operator in a
   password manager. Lose the key and every stored password must be typed in
   again — say so in the Handover.
3. *Worker keys.* Today a person can reveal their own worker key again in My
   settings. Either encrypt it (keeps "Show"), or store only a hash like the
   website key (shown once, at creation or rotation). Recommendation: hash,
   shown once — a worker key is a credential, not something to read back.
4. *`_secret_key`.* Moving it to the environment as well would invalidate
   every unsubscribe link already sent unless the old key is kept for
   checking. Recommendation: seal it like the rest; don't move it. And make
   sure `SECRET_KEY` is set on the VM, so sessions are signed with a key that
   isn't in the database at all.

**Tests.** `tests/test_secrets.py`: values round-trip; nothing in the
`settings` or `smtp_accounts` tables, or a backup copy, contains a known
password after the migration; plain values from before still read; the
migration runs once; with no key, sending refuses with the message and the
app still serves pages; `GET /api/settings` still masks.

**Done when** a copy of the database alone reveals no password or key.

---

## Package 4 · The sender: failures, replies, bounces (B-11, B-28, B-12)

**The faults.**

- *B-11:* a send that fails for any reason but a bounce records nothing, so
  it is retried on every pass forever — one wrong password means every lead
  on that account fails every cycle, each with a log line and the pause
  between emails, holding up the other operator's campaigns in the same loop.
- *B-28:* reply detection reads only the newest 50 messages per inbox and
  remembers nothing between checks; a reply pushed past 50th place is never
  seen and the sequence keeps emailing someone who answered.
- *B-12:* the bounce check downloads the whole body of every unread message
  of the last seven days in each inbox, every five minutes, and again next
  time for non-bounces — on a 1 GB server, one inbox with big attachments is
  enough. It also logs in a second time after the reply check.

**The changes.**

1. *Failed sends.* Count attempts on the enrolment and when it last failed.
   Back off (for example 15 minutes, 1 hour, 6 hours). After three failures
   in a row on one **sending account**, pause that account: it is skipped by
   every campaign until an admin fixes it, and the Dashboard says which
   account and why in one line. A send that fails for the recipient (not the
   account) is given up after the third attempt and the enrolment marked so.
2. *Replies.* Remember, per inbox, the highest IMAP UID examined (and the
   mailbox's UIDVALIDITY). Each check reads only newer UIDs, all of them,
   in batches; a UIDVALIDITY change rereads the last seven days once.
3. *Bounces.* Fetch headers first (`BODY.PEEK[HEADER]`), and the body only
   for messages whose From or Subject look like a bounce, capped in size.
   Remember UIDs examined, like replies. One login per inbox per cycle for
   both checks.

**Tests** (on package 2's fakes): a failing account is paused after three and
the other campaigns still send; back-off times; a recipient failure gives up
after three; a reply at position 120 is found; a non-bounce is never fetched
in full; a mailbox with a 30 MB message doesn't load it.

---

## Package 5 · The lead audit can't be turned on the server (B-4, B-5)

**The faults.** *B-4:* "Run checks" fetches whatever is in a lead's website
field, from the server, following redirects — reproduced fetching an address
on the server itself. *B-5:* it reads up to 2 MB and runs pattern searches
whose time grows with the square of the page; a page built to defeat them
measured 33 seconds at 24 KB, and a 199 KB compressed response expanded to
459 MB, on a 1 GB server with one worker.

**The changes** (`audit.py`).

1. Resolve the host before connecting and refuse loopback, private,
   link-local, multicast and reserved addresses (IPv4 and IPv6), on the first
   request and on **every redirect hop** (follow redirects by hand, at most
   five), and in the certificate check. Connect to the address that was
   checked, so a DNS answer can't change between check and fetch.
2. Cap the downloaded body at 512 KB and the decompressed size at 2 MB,
   reading in chunks; stop at the cap.
3. Replace the regex scans of raw HTML with the standard library's
   `html.parser` (one pass collecting title, meta, scripts, links), so time
   is linear in the page size.
4. One overall time limit for the whole audit (for example 20 seconds); any
   check still running is reported as timed out.

**Tests:** extend `tests/test_contacts_hub.py`'s audit section with a local
`http.server`: a URL on 127.0.0.1 is refused; a public-looking URL that
redirects to 127.0.0.1 is refused; a page built to defeat the old patterns
finishes in under a second; a compression bomb stops at the cap.

---

## Package 6 · Imports, restores and big lists don't lock the database (B-25, B-16, B-30)

**The faults.** *B-25:* matching a business by name loads all of the
operator's businesses for every imported or restored row and compares them in
Python — 500 new rows took about a minute and a half on 8,000 businesses, and
a restore does it inside one long write, so other writes fail after 10
seconds. *B-16:* Calling → To do looks up contact history one lead at a time,
up to 200 per load, for a field the queue table never uses; no index on
`sends(email_lead_id)`; seven counts per campaign on the campaigns list and
Dashboard; a connection per query. *B-30:* WhatsApp → To do downloads 844 KB
for a list that shows a name and a number; the sent list carries every
message's text for a dialog that doesn't show it. The audit's performance
table adds: WhatsApp CSV export opens the database once per lead (13.5 s for
2,806); audit screenshots stored inside each business row (182 MB of memory
to export 600 audited leads); indexes to add and to drop.

**The changes.**

1. A stored, indexed name key on `businesses` (the normalised name plus
   locality the matcher already computes), backfilled once; `find_existing_
   business` matches on it with a query instead of a scan. Restore does its
   matching before its first write.
2. Delete Calling → To do's per-lead history loop (the queue ignores it).
3. Indexes: add `sends(email_lead_id)` and `call_campaign_members(call_lead_id)`;
   drop `wa_leads_business_idx` and `call_leads_business_idx` (duplicates of
   unique indexes).
4. Campaign counts in one grouped query per page, not seven per campaign.
5. WhatsApp's To do and sent lists send only the columns they show; a lead's
   message, follow-ups and notes come with the lead when it is opened.
6. The WhatsApp export reads in one query; audit screenshots move to their
   own table, joined only where shown.

**Tests:** a timing test on a generated database of 8,000 businesses (import
500 new rows in under 5 seconds; restore 500 in under 5 seconds; other writes
keep working during both); response size of WhatsApp To do under 100 KB for
500 leads; every existing matching test still passes unchanged.

---

## Package 7 · Nothing outside holds up the one worker (B-17)

**The fault.** There is one worker by design (the scheduler runs in it).
Whatever it waits for, both operators wait for: the DNS (MX) check on import
— up to 500 lookups at 5 seconds — and an AI rewording (30 seconds). (Zoho
went with Plan 2.)

**Decide first.** Two ways:

- *Threads:* run gunicorn with one worker and several threads
  (`--workers 1 --threads 4`). Every slow request stops blocking the others;
  the scheduler stays single. SQLite with one connection per query and WAL
  is fine with this, but every module-level cache and lock must be checked.
- *Background jobs:* do the MX check and the AI call on a thread, the way the
  lead audit already does, and have the page poll.

Recommendation: threads — one change covers every slow request, including
ones not yet written — after auditing the module-level state for thread
safety (list it in the hand-back).

**Tests:** with a fake slow DNS resolver, a second request answers while an
import's MX check is running; the scheduler still starts exactly once.

---

## Package 8 · Errors you can see, and logs that don't grow forever (B-29, B-20, B-2's second half)

**The faults.** *B-29:* the browser's request helper has no error handling —
a dropped connection makes a click do nothing; a server error comes back as
an HTML page the scripts can't read; nothing records it anywhere. *B-20:*
an activity line for every mailbox check and a warning on every tick once the
daily cap is reached; deleted rows never shrink the file. *B-2, second half:*
log lines have no owner, so an admin reads the other operator's recipients
and subjects. *§7:* admin actions (settings saved, accounts changed, users
added or removed, passwords reset) leave no record.

**The changes.**

1. `api()` in `utils.js`: a timeout, a catch for network failures, and a
   toast with a plain sentence; every caller keeps working with the same
   return shape.
2. A Flask error handler for `/api/` paths that answers JSON
   (`{"ok": false, "error": "..."}`) and writes one log line with the route.
3. Fourteen routes answer 400 instead of 500 on a malformed id or field
   (B-8): validate ids with route converters and `int()` guards.
4. Log lines get an `owner_id` (0 for install-wide events). Non-admins see
   their own lines in an Activity log of their own; admins see the
   install-wide lines and their own, not the other operator's recipients.
   **Decide first:** whether admins should see both operators' send lines
   (recommendation: no — "admin is not a back door", Handover §3).
5. Admin actions write a log line naming who did what (never the values).
6. Keep 30 days of log (today 60), stop logging routine mailbox checks and
   repeat cap warnings (one per day), and `VACUUM` once a month from the
   scheduler, off-hours.

**Tests:** a route that raises answers JSON 500 and logs; a malformed id
answers 400 on each of the fourteen routes; a non-admin's log has only their
lines; the other operator's send line is absent from an admin's view.

---

## Package 9 · Small items

| Finding | Change |
|---|---|
| **B-7, rest** | `POST /api/settings` refuses keys containing `:` or starting with `_` (they have their own routes). The admin can no longer write another person's `audit_links:<id>` |
| **B-8, rest** | Calling's lead card adds `https://` to a website and refuses `javascript:` and other schemes, like the other two places. Both `.ics` routes escape carriage returns. The Unsubscribed and Invalid-MX CSVs, built in the browser, get the same formula protection as server exports |
| **B-19** | Delete `Dockerfile` and `fly.toml` (unused and broken). Move the standalone HTML pages and the SSH reference out of the repo root (a `site/` folder, or out of the repo; **Decide first** which) |
| **§7, openpyxl** | The campaign "Export Excel" button needs `openpyxl`, which isn't in `requirements.txt`: add it (pinned, package 1's lock file) |
| **§7, sign-in** | Successful sign-ins no longer count against the failed-attempt allowance |
| **§7, database viewer** | The admin Database viewer's table list is generated from the schema, not a stale list naming `contacts` |
| **§7, double send** | A crash between handing an email to SMTP and recording it can send it twice; record "sending" before the handoff and reconcile on start (Low; do it only if it stays small) |
| **Small tells, B-8** | The import overlap notice still names the other operator; deliberate and documented — leave it, unless the operator decides otherwise |

**Tests** for each row that has behaviour (settings keys refused; `javascript:`
refused; CR escaped; formula prefix on the two CSVs; sign-in allowance;
viewer lists the real tables).

---

## Hand-back

Per package: the commit, the tests added, what was checked by hand, every
**Decide first** answer and who gave it, and anything done differently from
this plan and why. Mark each finding resolved in the audit, in place, the way
[audits/README.md](../audits/README.md) describes, and update the audit's
status block and the Handover (§6 for the deploy and the secret key, §3 if
the log's visibility changes).
