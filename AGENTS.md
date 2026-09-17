# ShoutReach

Self-hosted cold-outreach platform (Flask + SQLite). Full context in
`docs/` — check there before assuming something needs explaining from
scratch.

## Start here

**Read [`docs/Handover.md`](docs/Handover.md) before changing anything.** It's
the current state of the project: what's deployed, the two-operator ownership
model and the rules that keep leads walled off, scrape workers, deploys, and
what's next. [`CLAUDE.md`](CLAUDE.md) has the same hard rules as below.

## Hard rules

- **Don't push to `master` without asking.** It deploys straight to production
  and re-runs `init_db()` migrations against the live database.
- **Never automate WhatsApp sending** in any form — no WhatsApp library, no
  browser driving a WhatsApp session, no scheduled send. A send is recorded
  only when the operator says it went.
- **Never add an owner filter to unsubscribe or bounce suppression.** It
  deliberately spans every operator.
- **`@owned` only guards ids in the URL.** Ids in a request body must be
  filtered with `_own_business_ids` or `require_owned`.

## Tests

No pytest — each `tests/test_*.py` is a standalone script, exit 0 on pass:

```bash
for f in tests/test_*.py; do python "$f"; done
```

**Importing `app` (or calling `db.init_db()`) runs migrations against
`./outreach.db`.** Point `DB_PATH` at a throwaway file first.

## Other reference docs

- `docs/WhatsApp Module Handover.md` — historical design record; superseded
  where Handover §5 says so.
- `docs/audits/` — security and architecture audits.
  `Full App Audit 2026-09-09.md` has a status block saying what's still open.
- `README.md` — setup and user-facing overview.
