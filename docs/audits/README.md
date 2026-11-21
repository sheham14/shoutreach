# Audits

## Active

| Document | State |
|---|---|
| [Full Audit 2026-11-21.md](Full%20Audit%202026-11-21.md) | **The one list to work from.** Front end (43 findings and a proposed redesign), back end, and every item the older audits left open (§7). Its status block says what's done. Screenshots it refers to are in [evidence-2026-11-21/](evidence-2026-11-21/) — fake data only. |

It describes unfixed holes (B-1 to B-6) and unencrypted secrets (B-13), so
the repo stays private.

## Resolved or superseded

Retired here rather than deleted — a closed audit is a record of the work, and
three of these were never in version control, so deleting them would have been
permanent.

| Document | Why it is retired |
|---|---|
| [Fable Audit.md](resolved/Fable%20Audit.md) | **Superseded 2026-11-21, not fully resolved.** 7 of 13 findings closed; the rest were re-checked and carried into the Full Audit's §7. |
| [Full App Audit 2026-09-09.md](resolved/Full%20App%20Audit%202026-09-09.md) | **Superseded 2026-11-21, mostly not actioned.** Every open item was re-checked and carried into the Full Audit's §7. |
| [Opus Audit.md](resolved/Opus%20Audit.md) | Every finding verified fixed by the Fable audit's §1 table. |
| [Opus Fixes.md](resolved/Opus%20Fixes.md) | A changelog of applied fixes, since verified against the code. |
| [Fable Progress.md](resolved/Fable%20Progress.md) | A session checkpoint that declares itself complete. Scaffolding for the two documents it produced. |

## Convention

- A finding is marked `✅ RESOLVED` **in place**, keeping the original text, with
  the commit that closed it and how it was verified. "Resolved" means verified,
  not attempted.
- A document moves to `resolved/` when every finding is closed, or explicitly
  reclassified as won't-fix with a reason, **or when a later audit has
  re-checked its open items and carried them forward** — in which case it gets
  a "Superseded" note at the top saying where they went. Partial progress on
  the current audit stays in `audits/` with a status header.
- Anything deliberately not fixed gets a recorded reason, so the next audit does
  not re-derive it from scratch.
- Regression tests accompany resolved findings — see `tests/test_security.py`
  and `tests/test_migrations.py`. The point is that a reintroduced bug fails the
  suite instead of waiting for another review.
- Findings carry an id (F-12, B-3). Refer to them by id in commits and in the
  Handover.

Not an audit, so it lives one level up: [Fable Study Guide.md](../Fable%20Study%20Guide.md).
