# Build plans

Written 2026-11-21 from [the full audit](../audits/Full%20Audit%202026-11-21.md)
and [the Cal.com handoff](../Cal.com%20Booking%20Handoff.md). Each plan is for
a coding session that has not seen the conversations behind it.

| Plan | What | Status (2026-11-23) |
|---|---|---|
| [1 · Fix now](1%20Fix%20now.md) | Three security holes, the sender's reliability, and a dozen visible faults. Nothing looks different afterwards | **Deployed** 2026-11-22 (`bea9be1`). [Build log](1%20Fix%20now%20-%20build%20log.md) |
| [2 · Cal.com integration](2%20Cal.com%20integration.md) | Website enquiries and bookings arrive in ShoutReach; a Meetings page; ShoutReach's own Zoho booking is removed | **Deployed** 2026-11-22 (`3eed5be`, then `4f5b303`). [Build log](2%20Cal.com%20integration%20-%20build%20log.md). What it left open is listed below |
| [3 · Redesign, phase 1](3%20Redesign%20phase%201.md) | The new colours, typeface, icons and controls on today's layouts. Nothing moves | **Reviewed, fixes in, ready to merge** on `plan-3-redesign-phase-1` (built 2026-11-22, reviewed and fixed 2026-11-23; not merged). [Build log and hand-back](3%20Redesign%20phase%201%20-%20build%20log.md) |
| [4 · Back-end hardening](4%20Back-end%20hardening.md) | Deploys that test and roll back, a real test of the sender, secrets encrypted at rest, failed sends and missed replies, the lead audit's safety, imports that don't lock the database, errors you can see | **Draft. Do not build from it.** It was reviewed on 2026-11-22 and has to be rewritten first: see [its review notes](4%20Back-end%20hardening%20-%20review%20notes.md) |

Later plans, not written yet: redesign phases 2 to 5 (audit §8).

## Open after Plan 2

None of these blocked the merge. The first three wait on the operator.

- **Should an enquiry reopen a closed lead?** A lead already on Not
  interested, Won or Replied that fills in the website form keeps that stage
  and gets no automatic follow-up (`run_website_followups` only follows up a
  business on `enquired` or no stage; `file_website_enquiry` never changes an
  existing stage). Its owner still gets the team email and the Dashboard row.
  Decide before the websites start posting.
- **The time zone of website meetings.** A Cal.com time is converted once
  into the Cal.com account's zone (America/St_Johns today) and shown with no
  zone named. Right for anyone in that zone. If the other operator reads
  times in a different zone, show the zone beside Cal.com times.
- **The admin download of deliveries** (`GET /api/website-settings/cal-events`)
  shows an admin both operators' bookings in full, and `cal_events` bodies
  are never deleted. It was added to capture payloads; that is done. If it is
  replaced, keep a list of outcomes (time, trigger, result) with no bodies.
  **Any pruning must keep what the order handling reads:**
  `_cal_events_naming` searches stored bodies for a booking's uid, so prune
  only old bodies, or first store uid, rescheduleUid and trigger in columns.
- **A delivery that arrives while the database is locked for more than ten
  seconds** answers 500 and is not stored (`record_cal_event` runs before
  the route's `try`). Read from the code, not reproduced. A delivery that is
  stored and then fails is retried. Belongs with Plan 4's work on locks.
- **A reschedule that arrives before its original and is then given up**
  leaves no lead (build log, "notes for later", 2).
- **The .co site's Cal.com fields** (`metadata.clinic`, `metadata.locale`,
  the notes labels) are in no captured fixture. Capture one hexiv.co booking
  into `tests/fixtures/`.
- **A follow-up campaign that is deleted after it was chosen** leaves its id
  in `website_followup:<uid>`; nothing is queued and nothing says so.
- **Cal.com strings aren't length-capped** the way the website route's are.

**The two websites still have to post enquiries.** Until `HexivUK` and
`hexivsite` each get the server route described in the Plan 2 build log
("The websites", contract A), ShoutReach hears about bookings only. People
who reach the calendar and don't book never arrive, so the follow-up and the
enquiry email have nothing to act on. Separate repos, separate sessions.

## Rules for every plan

- **Read first:** [CLAUDE.md](../../CLAUDE.md), then
  [Handover.md](../Handover.md) §3 (the wall between operators), §7 (hard
  constraints) and §8 (tests), then the audit findings the plan names.
- **Work on a branch**, one per plan (`git switch -c plan-1-fix-now` from
  `master`). **Never push to `master`**: it deploys to production and runs
  migrations on the live database. The operator merges and deploys.
- **One commit per package.** A package is done when its tests pass, the whole
  suite passes (`for f in tests/test_*.py; do python "$f"; done`), and the
  matching script in [tools/audit/checks/](../../tools/audit/) shows the fixed
  behaviour.
- **Mark each finding resolved** in the audit, in place, the way
  [audits/README.md](../audits/README.md) describes: `✅ RESOLVED`, the commit,
  how it was checked. Update the audit's status block and the Handover.
- **Never import `app` or call `db.init_db()` without `DB_PATH` pointing at a
  throwaway file.** The default is the real `./outreach.db`.
- **The server runs Python 3.11.** A backslash inside an f-string's `{}` is a
  syntax error there. The Windows console is cp1252: print ASCII in tests.
- **Hard rules that these plans touch:** WhatsApp is never sent automatically;
  opt-outs and bounces bind every operator, so nothing that stores or checks
  them may carry an owner; ids in a request body are filtered by owner; another
  operator's row answers 404.
- **Findings are cited by id and function name, not line number.** Lines move.
- **If the plan and the code disagree, stop and say so** in the hand-back
  rather than guessing. Decisions marked *decided* are the operator's and are
  not to be reopened.

## Handing back

Each plan ends with a short hand-back note: what was built, what was skipped
and why, anything that surprised you, and the commands a reviewer should run.
The review is done separately, against the plan and the audit.

## For the operator (not for the coding session)

- **A backup that leaves the server (B-24).** First check whether one already
  exists: Google Cloud Console → Compute Engine → Snapshots → Snapshot
  schedules. If the VM's disk has none, create one (daily, keep 14 days) and
  attach it to the disk. About ten minutes, no code.
- **Finish setting up Plan 2 on the live site.** Team setup → Website and
  bookings: who receives each site's leads (a booking for a site with no one
  set waits until someone is), the Cal.com booking link, the two waits, and
  the website key (shown once; it goes on each site's hosting as a
  server-only variable). Each person, My settings → Website enquiries that
  don't book: the email campaign, the WhatsApp campaign and a notification
  address.
- **Still to test** (left for the end by choice): a booking made from "Copy
  booking link" lands on that lead; one booking through hexiv.co; one real
  enquiry end to end once the sites post. Delete the test lead from
  2026-11-22.
- **The Cal.com webhook** is created and working. Nothing to do.
