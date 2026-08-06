# Audits

## Active

| Document | State |
|---|---|
| [Fable Audit.md](Fable%20Audit.md) | **7 of 13 findings resolved.** Open: send retry/backoff (3.3), five low-severity items (3.6–3.10), and the architectural items in §5. |

## Resolved

Retired here rather than deleted — a closed audit is a record of the work, and
three of these were never in version control, so deleting them would have been
permanent.

| Document | Why it is retired |
|---|---|
| [Opus Audit.md](resolved/Opus%20Audit.md) | Every finding verified fixed by the Fable audit's §1 table. Its deferred items are restated in Fable §5, which is now canonical. |
| [Opus Fixes.md](resolved/Opus%20Fixes.md) | A changelog of applied fixes, since verified against the code. |
| [Fable Progress.md](resolved/Fable%20Progress.md) | A session checkpoint that declares itself complete. Scaffolding for the two documents it produced. |

## Convention

- A finding is marked `✅ RESOLVED` **in place**, keeping the original text, with
  the commit that closed it and how it was verified. "Resolved" means verified,
  not attempted.
- A document moves to `resolved/` only when every finding is closed **or**
  explicitly reclassified as won't-fix with a reason. Partial progress stays in
  `audits/` with a status header.
- Anything deliberately not fixed gets a recorded reason, so the next audit does
  not re-derive it from scratch.
- Regression tests accompany resolved findings — see `tests/test_security.py`
  and `tests/test_migrations.py`. The point is that a reintroduced bug fails the
  suite instead of waiting for another review.

Not an audit, so it lives one level up: [Fable Study Guide.md](../Fable%20Study%20Guide.md).
