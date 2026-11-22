# Plan 4 · review notes (2026-11-22)

[Plan 4](4%20Back-end%20hardening.md) was reviewed against the audit and the
code at `2bb3a7a`. Its description of the code is mostly right. Several of
its instructions would break something if built as written, so it is a
**draft: rewrite it before anyone builds from it.** This file is what the
rewrite has to deal with.

Points marked **(checked)** were confirmed by opening the code. The rest come
from a second reviewer and were not re-opened; check them while rewriting.
File and line numbers are as of `2bb3a7a` and will have moved.

## Would break something as written

- **Package 1, change 5 (checked).** "A migration that raises stops
  start-up" would stop every restart. The `ALTER TABLE ... ADD COLUMN` lines
  in `init_db()` raise "duplicate column name" on an already-migrated
  database by design, and the `except` around them is what makes them safe
  to repeat. "Duplicate column" must count as done; anything else is the
  error.
- **Package 1, rollback (checked).** The deploy's backup is taken before the
  pull and the install, and the old app keeps writing until the restart. The
  scheduler also runs a pass as soon as the new app starts. Restoring that
  backup can therefore erase `sends` rows (the same step goes out twice) and
  opt-outs recorded since, which undoes B-1. The **Decide first** asks the
  operator to accept a much smaller loss than the real one. Also missing:
  stopping the service before copying the file back, and the `-wal` and
  `-shm` files.
- **Package 1, tests (checked).** The workflow runs only on a push to
  `master`, so "open a pull request and confirm the job goes red" cannot
  happen as written, and adding a `pull_request` trigger without guarding the
  deploy job would deploy from a pull request. The rollback has no test at
  all. The test job also needs `playwright-stealth`
  (`gmaps_email_scraper.py` imports it at the top). "Read `pip freeze` from
  the deploy log" isn't possible: the install runs with `-q`.
- **Package 9, database viewer (checked).** The table list is fixed today
  and only `smtp_accounts` has masked columns. "Generated from the schema"
  would show an admin both operators' leads, `users.password_hash` and
  `worker_keys.api_key`. It needs the operator's decision and a list of
  tables and columns that are never shown.
- **Package 5 (checked).** Refusing loopback addresses breaks the existing
  audit test, which scans a server on 127.0.0.1; the plan gives the tests no
  way round it. A 20-second limit for the whole audit cuts PageSpeed, whose
  own timeout is 90 seconds, so most audits would lose their scores.
- **Package 6, change 5 (checked).** The WhatsApp row's Open chat button
  builds its link from the message held in the list (`openWaFromRow`).
  Trimming the list means a fetch before `window.open`, which can lose the
  click's permission to open a window, on the screen used most. The sent
  list is safe to trim.
- **Package 6, change 2 (checked).** Removing Calling's per-lead history
  loop breaks an assertion in `tests/test_calling.py` ("prior contact rides
  along with each lead").

## Wrong or stale

- **Package 2's fault text (checked).** `tests/test_resilience.py` already
  runs `scheduler.process_queue()` with a replaced `send_email` (Plan 1).
  Still true: the real `send_email`, the reply scan and the bounce parser
  are run by no test.
- **Package 3 (checked).** `google_api_key` is a stored secret the list
  omits. The same `_secret_key` verifies unsubscribe links, so "the app
  still serves pages" with the key missing must cover them, or opt-out
  clicks are dropped. `get_or_create_secret` must never create a new secret
  because unsealing failed. Not checked: the test looks in `settings` and
  `smtp_accounts` but worker keys are in `worker_keys`; nothing is said
  about the plain-text backups that already exist, a `VACUUM` after sealing,
  or rotating the passwords.
- **Package 4 (checked).** A campaign whose only account is paused falls
  back to the legacy SMTP settings today (`send_email` when no account is
  given). The plan doesn't say what should happen, or how an account
  failure is told from a recipient failure.
- **Package 8.** Says the request helper "has no error handling"; it already
  turns a non-JSON error into `{error: ...}`, and many callers toast that, so
  a toast added inside the helper would double up. Its "fourteen routes" are
  listed nowhere. A route converter answers 404, not 400, for a bad id.

## Shape

- Packages 4 to 9 have no "Done when".
- Package 7 has two options and no change section; the gunicorn command is
  in the systemd unit on the VM, not in the repo.
- Package 8 adds a new screen (an Activity log for non-admins) while the
  introduction says nothing changes how the app looks.
- One **Decide first** (where the standalone HTML pages go) has no
  recommendation.
- Choices made silently that are the operator's: a shared sending account
  paused automatically for both operators; `SECRET_KEY` set on the VM (signs
  everyone out if it isn't set today); log kept 30 days instead of 60; a
  monthly `VACUUM` that holds the database; `Dockerfile` and `fly.toml`
  deleted; new packages on the server (`cryptography`, `openpyxl`).
- No package names the `tools/audit/checks/` script that should show its fix,
  as [README.md](README.md) asks. One exists for B-4.

## Not covered, and not explicitly left out

- B-24 (backups that leave the server): the operator's job, but say so.
- What Plan 2 added: how long `cal_events` bodies and sent
  `team_notifications` are kept; a rate limit on the Cal.com route; a
  delivery lost when it arrives during a long lock. See "Open after Plan 2"
  in [README.md](README.md), including the rule about pruning.
- B-12's cap on how many messages one pass reads.
- The sign-out link being a plain GET, and bulk delete of addresses being a
  hard delete (both carried in the audit's §7).
