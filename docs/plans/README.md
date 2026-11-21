# Build plans

Written 2026-11-21 from [the full audit](../audits/Full%20Audit%202026-11-21.md)
and [the Cal.com handoff](../Cal.com%20Booking%20Handoff.md). Each plan is for
a coding session that has not seen the conversations behind it.

| Plan | What | Depends on |
|---|---|---|
| [1 · Fix now](1%20Fix%20now.md) | Three security holes, the sender's reliability, and a dozen visible faults. Nothing looks different afterwards | — |
| [2 · Cal.com integration](2%20Cal.com%20integration.md) | Website enquiries and bookings arrive in ShoutReach; a Meetings page; ShoutReach's own Zoho booking is removed | Plan 1, packages 1 and 4 |
| [3 · Redesign, phase 1](3%20Redesign%20phase%201.md) | The new colours, typeface, icons and controls on today's layouts. Nothing moves | Plan 1, package 7 |

Later plans, not written yet: back-end hardening (B-4, B-5, B-11, B-12, B-13,
B-18, B-25 to B-30), and redesign phases 2 to 5 (audit §8).

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
- **Commit the audit before the first build starts**, so the coding session's
  first commit doesn't swallow it. Nothing here is committed yet.
- **Cal.com:** create the webhook only when Plan 2 says the address is ready.
