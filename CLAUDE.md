# ShoutReach

Self-hosted cold-outreach platform (Flask + SQLite): email sequences, cold
calling, and manual WhatsApp outreach, used by two operators whose leads are
walled off from each other. Live at https://shoutreach.hexiv.co.

## Start here

**Read [`docs/Handover.md`](docs/Handover.md) before changing anything.** It's
the current state of the project: what's deployed and what isn't, the
multi-operator ownership model and the rules that keep it from leaking, how
scrape workers and deploys work, and what's next. Skipping it risks
re-deriving, or contradicting, decisions already made and tested.

## Hard rules

- **Don't push to `master` without asking.** It deploys straight to production
  and re-runs `init_db()` migrations against the live database.
- **Never automate WhatsApp sending** in any form — no WhatsApp library, no
  browser driving a WhatsApp session, no scheduled send.
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
`./outreach.db`.** For a quick "does it load" check, point `DB_PATH` at a
throwaway file first. There's no UI test in the repo; see Handover §8 for how
the redesign was smoke-tested in a real browser.

## Other docs

- `docs/WhatsApp Module Handover.md` — how the WhatsApp module and the
  `contacts` → `businesses` split were designed. Historical: its booking-gap
  check, review step and "uncommitted" notes are all superseded (Handover §5).
- `docs/audits/` — security and architecture audits.
  `Full App Audit 2026-09-09.md` has a status block saying what's still open.
  It lists unfixed security findings, so keep the repo private.
- `README.md` — setup and user-facing overview.
