# WhatsApp Module Handover

> **Status (2026-09-06):** Phases 1 and 2 of 3 are done, tested, and sitting
> **uncommitted** in the working tree (see [§4](#4-current-repo-state)). Phase
> 3 — the WhatsApp module itself — has not been started beyond two empty
> tables. Read this whole document before touching code; it exists so a
> session with zero memory of the work below can pick it up correctly.

---

## 0. Why this refactor happened

The actual ask was: add a WhatsApp outreach channel to ShoutReach (see
`docs/` generally and the project's own memory for the original spec — short
version: import clinic leads scraped from Google Maps, detect a "booking
gap" on their website as a cheap plain-HTTP check, let the operator confirm
the signal, draft a WhatsApp opener from a template, and open a prefilled
`wa.me` link for the operator to review and send **manually** — sending is
never automated, that is a hard constraint, not a preference).

Partway into planning that, the operator asked for calling and email leads
to live in **separate tables** instead of sharing the old single `contacts`
table the way calling currently did. That request grew into a full schema
redesign, because doing it properly meant solving "is this business already
being worked on another channel" as a real, structural question rather than
a fuzzy phone/domain match repeated three times. That redesign is Phases 1
and 2 below. **Phase 3 is the WhatsApp module the operator actually asked
for**, and it can now be built on top of a schema that already has the
identity and cross-channel-duplicate problems solved.

---

## 1. What's done — Phase 1: `contacts` → `businesses` split

The old schema had one `contacts` table doing triple duty as email contact,
call lead, and (implicitly) business identity. It's now:

- **`businesses`** — the shared identity: name, phone, phone_normalized,
  website, domain, address, city, country, category, rating, review_count,
  web_status, source_job_id, extra, do_not_contact, notes, created_at.
- **`email_leads`** — one row per email address, `business_id` FK. A
  business can have several (a shared billing inbox, an owner's personal
  address); which one is sendable is decided by `_pick_business_winner`
  (the replacement for the old domain-based arbitration).
- **`call_leads`** — one row per business that has ever been dialled,
  created lazily by `get_or_create_call_lead` the first time it's needed
  (added to a campaign, or actually called) — never pre-created on import.
- **`wa_leads`** + **`wa_log`** — schema exists (see [§5](#5-whats-left--phase-3-the-whatsapp-module)), nothing else does yet.

**Identity resolution** lives in two functions in `db.py` that share one
resolution order (this used to be two separate, drifting copies of the same
logic — see the bug list below):

- `find_existing_business(conn, email, phone, website, company, address, exclude_id)`
  — read-only, four-tier lookup: an email already on file → normalized phone
  → canonical domain (falling back to the email's own domain if no website
  is given, skipping freemail) → company name + locality as a last resort.
- `find_or_create_business(conn, row)` — wraps the above with create-if-missing
  and a fill-blanks-never-overwrite merge on update.

**Two real bugs were found and fixed** while writing the regression tests
for this (not found any other way — worth remembering next time something
here feels "obviously correct"):

1. `find_or_create_business` had no fallback to the email's own domain when
   no separate website was given — a business pasted in as just "name,
   email" got a fresh row per address instead of merging.
2. A re-imported email that already exists but arrives with a *different*
   claimed company/website (shared billing inbox, re-scrape with a typo'd
   site) spun up a phantom second, orphaned business instead of updating the
   one the address already belongs to.

**Migration**: `_split_contacts_into_channels` in `db.py` runs once inside
`init_db()`, is idempotent, refuses to double-run if a stray `contacts`
table reappears next to an already-populated `businesses` table, and
correctly merges multiple old contact rows for one business into one
business + several `email_leads`. Every FK that pointed at `contacts`
(`enrollments`, `sends`, `call_log`, `call_campaign_members`) is repointed
in the same pass. **This has not run against the real production database
yet** — it's only been exercised against synthetic legacy-shaped databases
in `tests/test_migrations.py`. The production `outreach.db` on the GCP box
is small (operator said ~20 contacts) and disposable, so this is low-risk,
but it hasn't happened.

**Naming**: the sidebar still says "Contacts," and `/api/contacts/*` URLs
are unchanged on purpose — the plan is to rename to Email/Calling/WhatsApp
in one pass when the WhatsApp sidebar entry gets built, rather than
touching every template twice. Don't be surprised that the code underneath
`/api/contacts/*` is now about `email_leads`, not a `contacts` table that no
longer exists.

**`scraper_worker.py` / `gmaps_email_scraper.py`** (the operator's *local*
scraper, not part of this repo's server-side code) posts to
`/api/contacts/import`, which still works unchanged — the response now
additionally carries `business_ids` and `conflicts` (see Phase 2), which the
worker ignores safely since it only reads `inserted`.

---

## 2. What's done — Phase 2: cross-channel duplicate confirmation

The operator's decision, when asked: a business already active on one
channel should raise a **confirm-before-proceeding** flag when being added
to another, not just an informational badge.

**`db.py`**:
- `get_channel_presence(conn, business_ids)` — batched (not N+1) check of
  which of email/call/whatsapp each business already has a row on.
- `find_cross_channel_conflicts(rows, channel)` — for raw import rows
  (resolves each to a business via `find_existing_business`, read-only, then
  checks presence). A business with presence only on `channel` itself is
  **not** a conflict — that's a normal re-import.
- `channel_conflicts_for_businesses(business_ids, channel)` — same idea for
  callers that already have resolved business ids (the call-campaign
  picker).

**`app.py`** — wired into every place a channel is actually attached:
- `POST /api/contacts/import` — a row with an email resolving to a business
  already on calling/WhatsApp is held back; clean rows import immediately.
  Response gains `conflicts: [{business_id, business_name, channels, row}]`.
  Resubmitting the same rows with `confirm_conflicts: true` pushes them
  through unconditionally.
- `POST /api/call-campaigns` (create with initial leads) and
  `POST /api/call-campaigns/<id>/members` — same pattern, `channel="call"`.
- **The scrape worker is exempt** (checked via the existing
  `_has_valid_worker_key()`) — it runs unattended, there's nobody there to
  confirm anything with.

**Frontend**: `confirmChannelConflicts(res, resend)` in `static/js/utils.js`
— one shared helper, a native `confirm()` listing what's flagged and why,
resends with `confirm_conflicts: true` on accept. Used by:
- `static/js/contacts.js` — `importContacts()` / `mergeImportResults()`
  (merges the two calls' `inserted` counts honestly rather than reporting
  only the confirmed pass).
- `static/js/calling.js` — `_addCallCampaignMembers()`, plus
  `_importForCalling()` / `_resolveImportConflicts()` for the add-leads
  modal's manual-paste and CSV tabs, which can also carry an email column
  and so go through the *same* email-channel check before the call-channel
  check.

**This is exactly what Phase 3 will plug into.** When the WhatsApp importer
exists, it needs nothing new here — just call
`find_cross_channel_conflicts(rows, channel="whatsapp")` and
`channel_conflicts_for_businesses(ids, channel="whatsapp")` the same way.

---

## 3. Test suite

Rewritten/added this session (schema split broke every file that touched
`contacts`):

`test_migrations.py`, `test_duplicates.py`, `test_dedupe_and_guards.py`,
`test_contacts_paging.py`, `test_calling.py` (the largest, 21 sections),
`test_rendering.py`, `test_send_window.py`, `test_variants.py`, and the new
`test_cross_channel.py`.

Untouched and still passing as-is: `test_accounts.py`, `test_extraction.py`,
`test_resilience.py`, `test_security.py`, `test_worker_api.py`,
`recall_harness.py` (a scraper-recall tool, not a test).

Run the whole suite:

```bash
for f in tests/test_*.py; do python "$f"; done
```

All 14 test files pass as of this writing (each script exits 0 on pass, 1 on
failure, and prints `ALL PASS` or a `FAILURES: [...]` list — there is no
pytest runner, they're standalone scripts).

---

## 4. Current repo state

**Nothing from this session has been committed.** `git status` shows these
18 files modified (or, for the last one, new) by Phases 1–2:

```
app.py  db.py  scheduler.py  sender.py
static/js/calling.js  static/js/contacts.js  static/js/utils.js
templates/modals/add_call_leads.html  templates/sections/contacts.html
tests/test_calling.py  tests/test_contacts_paging.py
tests/test_dedupe_and_guards.py  tests/test_duplicates.py
tests/test_migrations.py  tests/test_rendering.py
tests/test_send_window.py  tests/test_variants.py
tests/test_cross_channel.py   [new file]
```

**Three other files show as modified/untracked but are NOT from this
session** — they predate it (confirmed against the git status captured at
the very start of this work): `shoutreach-internal.html` (modified),
`demo-dashboard.html` and `shoutreach-portfolio.html` (untracked, new). Don't
attribute those to Phases 1–2 or assume they're related; they're the
operator's own prior, separate work.

If you're picking this up fresh: **check with the operator before doing
anything destructive** (`git checkout`, `git reset`, etc.) — none of this is
backed up anywhere except the working tree. Committing the Phase 1–2 work
before starting Phase 3 is strongly recommended so a fresh WhatsApp-module
mistake can't tangle itself up with the schema-split diff, but that's the
operator's call, not something to do unprompted.

---

## 5. What's left — Phase 3: the WhatsApp module

Nothing below exists yet except the raw tables. This is the actual feature
the operator originally asked for.

### 5a. Schema already in place (Phase 1)

```sql
wa_leads: id, business_id, wa_number, country, number_type, wa_status,
          signal_type, signal_detail, signal_confirmed, draft_message,
          template_variant, sent_date, replied, followup_count, paused,
          moved_to, notes, created_at
wa_log:   id, wa_lead_id, kind, message, template_variant, sent_at
```

Indexes already exist, including `wa_leads_due_idx` on
`(sent_date) WHERE replied = 0 AND paused = 0` — built for the follow-up-due
query specifically.

### 5b. Hard constraints (from the original spec — do not relitigate these)

- **Sending is 100% manual.** The only WhatsApp interaction this app ever
  has is constructing a `wa.me` link with prefilled text for the operator to
  open and send themselves. No unofficial WhatsApp Web automation, no
  headless browser driving a WhatsApp session (not even read-only, not even
  "just to check a number"), no scheduled/background send of any kind. If a
  feature request starts to smell like auto-send, stop and ask rather than
  building it — this is the single most important boundary in the module
  and the whole reason it isn't built the way the email module auto-sends.
- **"Sent" means the button was clicked, nothing more.** No delivery/read
  confirmation is possible from outside WhatsApp. Provide a manual way to
  correct `sent_date` on a row.
- **Follow-ups are infinite, not capped.** No auto-dormant-after-N rule —
  that was the original plan and the operator explicitly overrode it. A lead
  keeps cycling at the configured interval (default 3 days) until marked
  replied or manually paused (`wa_leads.paused`).
- **Signal detection is a plain HTTP fetch by default** — booking keywords /
  known widget signatures (Calendly, JaneApp, Fresha, Acuity, Mindbody,
  Vagaro, Setmore, Square Appointments). No per-lead agent loop, no
  multi-tool research, no web search per lead. Cost and latency must stay
  low at real volume.
- **The operator confirms every signal before anything drafts from it.**
  Nothing sends based on an unverified guess.
- **Drafting is templated, not agent-driven.** One cheap paraphrase call per
  *batch* (not per lead) through the existing BYOK provider abstraction
  (`_AI_CALLERS` in `app.py`, already supports Claude/Gemini/OpenAI) for
  phrasing variety, constrained to never alter the confirmed fact.
- **No number-validity pre-check** (researched and decided against — see
  the conversation this handover summarizes if the reasoning needs
  revisiting; short version: no free/clean way exists, and a wasted
  `wa.me` click already tells the operator for free).

### 5c. Build order (roughly — each is independently useful)

1. **`format_whatsapp_number(raw, country)`** — new, separate from the
   existing NANP-only `normalize_phone`. Needs country to turn a locally-
   formatted Gulf number (e.g. a UAE `050 123 4567`) into the digits-only
   international form `wa.me` requires. Next campaign targets UAE and Qatar
   specifically — start with those two.
2. **CSV/Excel import** for WhatsApp leads — new endpoint following the
   `/api/contacts/import` pattern, a country picker per import (the scrape
   already knows its target country/city, so this is mostly a fallback for
   pasted CSVs), wired through `upsert_businesses` + the Phase 2
   cross-channel check (`channel="whatsapp"` — no new backend work needed
   for that part).
3. **Signal detection** — plain fetch per lead against the keyword/widget
   list. **Must run as a background pass**, not inline in a request: the
   server is `gunicorn --workers 1` (intentional, so the scheduler doesn't
   duplicate) — blocking that one worker on network fetches for a 200-row
   CSV would freeze the whole app, including the email scheduler. Reuse
   `scheduler.py`'s background-thread pattern rather than inventing a new
   one. Still-open decision, needs the operator's input before building:
   what happens for a page that comes back "suspiciously empty" (likely a
   JS-rendered SPA a plain fetch can't see into) — proposed options were
   (a) a local worker script mirroring `scraper_worker.py` that runs a real
   browser and pushes results back via API, or (c) skip true rendering for
   v1 and flag it as "couldn't tell, check by hand." Leaning towards (c) to
   start. **Don't just pick one — ask.**
4. **Confirm/correct review UI** — new UI concept, no existing precedent to
   copy. Table of suggested signals; the operator approves or edits before
   drafting can happen for that lead. `wa_leads.signal_confirmed` gates it.
5. **Draft generation** — two templates (gap-found / no-gap branches), one
   batched paraphrase call for phrasing variety, stored in
   `wa_leads.draft_message` + `template_variant`.
6. **The WhatsApp section itself** — new sidebar entry, new template
   (`templates/sections/whatsapp.html`), new JS (`static/js/whatsapp.js`),
   lead table with inline-editable drafts and an "Open in WhatsApp" button
   building `https://wa.me/<digits>?text=<encoded>`. This is the point to
   also do the Contacts→Email / sidebar rename mentioned in §1, since the
   templates are being restructured anyway.
7. **Follow-up cadence** — a live SQL query against `wa_leads_due_idx`
   (sent_date old enough, not replied, not paused), computed on page load,
   **not** a scheduled job — nothing here should touch the network
   unattended, on principle, not just for the send button.
8. **"Not on WhatsApp" → move to Calling/Email** — a manual action (there's
   no way to detect this automatically) that creates/reuses a `call_leads`
   or files-as-prospect `email_leads` row for the same business, and marks
   `wa_leads.moved_to` rather than deleting the row.
9. **Tests** — `test_whatsapp.py`, comparable in scope to `test_calling.py`.

### 5d. Sizing

Told the operator this is at least as large as Phase 1, larger than Phase
2 — the signal-detection background job and the confirm/draft UI are
genuinely new patterns, not copy-paste of calling/email. Suggested a
three-session split if useful: (phone formatting + import + signal
detection) / (review UI + drafting + lead table) / (cadence + move-action +
tests). The operator chose to stop here and pick Phase 3 up fresh rather
than start it now.

---

## 6. Orientation pointers

- `db.py` — schema in `init_db()` near the top; the split migration is
  `_split_contacts_into_channels` / `_repoint_channel_refs`; identity
  resolution is `find_existing_business` / `find_or_create_business`;
  cross-channel is `get_channel_presence` / `find_cross_channel_conflicts` /
  `channel_conflicts_for_businesses`, all in one block together.
- `app.py` — the AI provider abstraction (`_AI_CALLERS`, `_call_claude_review`
  etc., `/api/ai/review`) is what Phase 3's paraphrase step should reuse
  rather than reinventing.
- `scheduler.py` — the background-thread pattern Phase 3's signal detection
  needs to follow.
- Calling module (`db.py`'s call_* functions, `app.py`'s `/api/calls/*`,
  `templates/sections/calling.html`, `static/js/calling.js`) is the closest
  existing precedent for a channel section's shape — table + detail card +
  outcome buttons — even though WhatsApp's actual flow (confirm-signal →
  draft → manual-open) has no direct equivalent there.
