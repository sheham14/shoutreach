# ShoutReach

Self-hosted cold-outreach platform (Flask + SQLite). Full context in
`docs/` — check there before assuming something needs explaining from
scratch.

## Active handover

**If a WhatsApp module, a `contacts`/`businesses` schema question, or
cross-channel duplicate handling comes up, read
[`docs/WhatsApp Module Handover.md`](docs/WhatsApp%20Module%20Handover.md)
first.** It covers a schema migration (the old `contacts` table split into
`businesses` + `email_leads`/`call_leads`/`wa_leads`) and a cross-channel
duplicate-confirmation feature that landed together, sitting uncommitted in
the working tree, plus the full punch list for the WhatsApp module that
hasn't been started yet. Skipping it risks re-deriving (or contradicting)
decisions already made and tested.

## Tests

No pytest — each `tests/test_*.py` is a standalone script, exit 0 on pass:

```bash
for f in tests/test_*.py; do python "$f"; done
```

## Other reference docs

- `docs/audits/` — prior security/architecture audits (Fable Audit and
  companion study guide).
- `README.md` — project overview.
