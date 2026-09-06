# WhatsApp Module Handover

> **Status (2026-09-06):** All three phases are built and tested. Phases 1–2
> are already committed (`9784233`, `18a2306`); **Phase 3 (this document's
> original subject) is built, fully tested, and currently uncommitted** — see
> [§4](#4-current-repo-state). A handful of deliberate deferrals remain,
> listed in [§6](#6-deferred--not-done-on-purpose). Read this whole document
> before touching code; it exists so a session with zero memory of the work
> below can pick it up correctly.

---

## 0. Why this refactor happened

The original ask was: add a WhatsApp outreach channel to ShoutReach — import
clinic leads scraped from Google Maps, detect a "booking gap" on their
website as a cheap plain-HTTP check, let the operator confirm the signal,
draft a WhatsApp opener from a template, and open a prefilled `wa.me` link
for the operator to review and send **manually** (sending is never
automated — a hard constraint, not a preference).

Partway into planning that, the operator asked for calling and email leads
to live in **separate tables** instead of sharing the old single `contacts`
table. That grew into a full schema redesign — Phases 1 and 2 below — before
any WhatsApp-specific code existed. This document originally handed Phase 3
off to a fresh session; that session (which produced everything in
[§3](#3-what-got-built--phase-3-the-whatsapp-module)) has now finished it.

---

## 1. What's done — Phase 1: `contacts` → `businesses` split

*(Committed in `9784233`.)*

The old schema had one `contacts` table doing triple duty as email contact,
call lead, and business identity. It's now:

- **`businesses`** — shared identity: name, phone, phone_normalized, website,
  domain, address, city, country, category, rating, review_count,
  web_status, source_job_id, extra, do_not_contact, notes, created_at.
- **`email_leads`** — one row per email address, `business_id` FK.
- **`call_leads`** — one row per business ever dialled, created lazily by
  `get_or_create_call_lead`.
- **`wa_leads`** + **`wa_log`** — WhatsApp, see §3.

**Identity resolution** — `find_existing_business` (read-only, four-tier:
email on file → normalized phone → canonical domain, falling back to the
email's own domain if no website is given → company name + locality) and
`find_or_create_business` (wraps it with create-if-missing). These used to
be two separately-maintained copies of the same logic; unifying them is
also what fixed two real bugs found while testing this (a missing
email-domain fallback, and a re-imported address under a different claimed
company spinning up an orphaned second business).

**Migration**: `_split_contacts_into_channels` in `db.py`, runs once inside
`init_db()`, idempotent, refuses to double-run. **Still has not run against
the real production database** — only exercised against synthetic
legacy-shaped databases in `tests/test_migrations.py`.

**Naming**: sidebar still says "Contacts" / "Cold Calling"; `/api/contacts/*`
URLs are unchanged. See §6 — this is a known, deliberate deferral, not an
oversight.

---

## 2. What's done — Phase 2: cross-channel duplicate confirmation

*(Committed in `18a2306`.)*

A business already active on one channel raises a confirm-before-proceeding
flag when added to another, rather than a passive badge.

- `db.get_channel_presence(conn, business_ids)` — batched presence check.
- `db.find_cross_channel_conflicts(rows, channel)` — for raw import rows.
- `db.channel_conflicts_for_businesses(business_ids, channel)` — for callers
  with already-resolved ids.
- Wired into `/api/contacts/import`, `/api/call-campaigns` (create + add
  members). The scrape worker (API-key auth) is exempt — unattended, nobody
  to confirm with.
- Frontend: `confirmChannelConflicts()` in `static/js/utils.js`, one native
  `confirm()` dialog, shared by Contacts import and the calling add-leads
  modal.

**Phase 3 needed zero new code here** — `find_cross_channel_conflicts(rows,
channel="whatsapp")` and the `/api/wa/import` route just called it, and
`tests/test_whatsapp.py` §15–16 confirms it works correctly for the new
channel out of the box.

---

## 3. What got built — Phase 3: the WhatsApp module

*(Not yet committed — see §4.)*

### Schema (already existed from Phase 1, unchanged)

```sql
wa_leads: id, business_id, wa_number, country, number_type, wa_status,
          signal_type, signal_detail, signal_confirmed, draft_message,
          template_variant, sent_date, replied, followup_count, paused,
          moved_to, notes, created_at
wa_log:   id, wa_lead_id, kind, message, template_variant, sent_at
```

### `wa_status` lifecycle

```
''  (imported, awaiting signal check)
  -> 'signal_ready'  (background scan found a signal, awaiting operator review)
  -> 'confirmed'     (operator locked in the signal, awaiting drafting)
  -> 'drafted'       (batch draft step ran, ready to open in WhatsApp)
  -> 'sent'          (opener or a follow-up was opened; in the cadence)
  -> 'replied' | business.moved_to set   (terminal, either way)
```
`paused` is an orthogonal boolean, settable at any point in `'sent'`.

### New files

- **`wa_signal.py`** — `detect_signal(website)`: one plain HTTP GET (6s
  timeout, 500KB cap), checked against `BOOKING_KEYWORDS` and
  `BOOKING_WIDGET_SIGNATURES` (Calendly, JaneApp, Fresha, Acuity, Mindbody,
  Vagaro, Setmore, Appointy, SimplyBook, Zocdoc, Schedulicity). Returns
  `gap_found` / `no_gap` / `unclear`. `unclear` covers both a failed fetch
  and a page too short to judge honestly (< 200 chars of stripped text) —
  **the decided answer to the "suspiciously empty site" question**: no
  second rendered-browser fetch, no local worker script. Flagged for the
  operator to check by hand. Revisit only if this bucket turns out to be a
  large fraction of real imports.
- **`templates/sections/whatsapp.html`**, **`static/js/whatsapp.js`** — the
  section itself: a stat-grid summary, five bucket tabs (Needs review /
  Ready to send / Follow-up due / In cadence / All), one table whose row
  shape adapts to each lead's own `wa_status` regardless of which tab is
  open, and a template editor panel (toggle, like calling's script editor).
- **`templates/modals/import_wa_leads.html`** — CSV upload or paste, country
  picker (UAE/Qatar; a `country` column on a row overrides the picker).
- **`tests/test_whatsapp.py`** — 17 sections. Signal detection is tested
  against a local `http.server` (same pattern as `test_resilience.py`),
  **never live internet** — don't add a test that depends on a real
  website's content staying the same.

### `db.py` additions (all in one block, search for `# ── WhatsApp`)

- `format_whatsapp_number(raw, country)` / `classify_number_type(raw,
  country)` — deliberately separate from the NANP-only `normalize_phone`.
  `WA_COUNTRY_CODES` currently has `AE` and `QA`; add more by extending that
  dict (UAE strips a leading trunk `0`, Qatar has no trunk prefix — check
  the dialling convention before assuming the same stripping rule applies).
- `get_wa_templates()` / `save_wa_templates()` — three editable templates
  (`gap`, `no_gap`, `followup`) stored as `settings` keys, seeded with real
  starting copy (unlike the call script, which starts empty on purpose —
  see the comment on `_DEFAULT_WA_TEMPLATE_GAP` for why these two cases
  differ).
- `upsert_wa_leads(rows, default_country)` — resolves/creates the business
  via `find_or_create_business` (so a lead already known from email/calling
  is recognised, not duplicated), attaches an email if the row has one, then
  upserts the `wa_leads` row with fill-blanks-never-overwrite merge.
- Lifecycle: `get_wa_leads_pending_signal`, `set_wa_signal`,
  `confirm_wa_signal`, `get_wa_leads_ready_to_draft`, `save_wa_draft`,
  `update_wa_message`, `mark_wa_sent`, `correct_wa_sent_date`,
  `mark_wa_replied`, `set_wa_paused`, `move_wa_lead`.
- `get_wa_followups_due(days, limit)` — **a live SQL query against
  `wa_leads_due_idx`, not a scheduled job.** No follow-up cap: a lead stays
  in this list forever until replied or paused. This was an explicit
  override from the operator partway through planning — don't reintroduce
  an auto-dormant-after-N rule without checking first.

### `app.py` additions

- `_call_claude_text` / `_call_gemini_text` / `_call_openai_text` +
  `_call_configured_ai_text(prompt)` — generic text completion through the
  same three providers and the same `_ai_http_post` transport as AI Review,
  but not locked to the review JSON shape. Added because the paraphrase step
  needed "give me text back," and duplicating three providers' HTTP-call
  code a second time would have been the wrong call.
- `/api/wa/import` — mirrors `/api/contacts/import`; CSV or JSON rows, a
  `country` field, `confirm_conflicts` for the cross-channel resubmission.
- `/api/wa/leads` (list, `?status=`), `/api/wa/summary`,
  `/api/wa/followups-due` (includes a server-rendered `followup_draft` per
  lead, so the frontend never re-implements template substitution).
- `/api/wa/leads/<id>/confirm`, `/api/wa/draft-batch`,
  `/api/wa/leads/<id>/message`, `/api/wa/leads/<id>/sent`,
  `/api/wa/leads/<id>/sent-date`, `/api/wa/leads/<id>/replied`,
  `/api/wa/leads/<id>/pause`, `/api/wa/leads/<id>/move`,
  `/api/wa/templates` (GET/PUT).
- **`api_wa_draft_batch`** is the one route worth re-reading before touching:
  it builds every confirmed lead's templated message first, then makes
  **one** AI call for the whole batch (not one per lead), and falls back to
  the plain template — never blocking, never erroring the whole batch — if
  AI is off, unconfigured, or the call fails for any reason. `tests/
  test_whatsapp.py` §7–8 pin both the fallback and that the route never
  references `wa.me`/whatsapp.com anywhere in its source.

### `scheduler.py` addition

- `run_wa_signal_scan()` — the **only** WhatsApp code that runs in the
  background loop, and deliberately the only one that ever will: it reads a
  homepage and records what it saw, nothing more. Runs every ~60s tick
  alongside `process_queue()`, batch size `WA_SIGNAL_BATCH_SIZE = 3` (small
  on purpose — it shares a thread with email sending; worst case ~18s added
  per tick if all three time out). The follow-up cadence is *not* a job
  here — see `get_wa_followups_due` above.

### Sidebar / templates

- Added a `data-section="whatsapp"` entry to `sidebar.html` (💬 icon) and
  wired `loadWhatsApp()` into `showSection()` in `utils.js`. **Did not**
  rename "Contacts" → "Email" or "Cold Calling" → "Calling" — see §6.

---

## 4. Current repo state

Phases 1–2 are committed (`9784233`, `18a2306`). **Phase 3 is not.**
`git status` should show, beyond the pre-existing untouched files
(`shoutreach-internal.html`, `demo-dashboard.html`,
`shoutreach-portfolio.html` — not part of any phase, leave them alone):

```
db.py  app.py  scheduler.py                    [modified — WhatsApp additions]
templates/sections/sidebar.html                [modified — nav entry]
templates/index.html                           [modified — includes + script tag]
static/js/utils.js                             [modified — showSection dispatch]
wa_signal.py                                    [new]
templates/sections/whatsapp.html                [new]
templates/modals/import_wa_leads.html           [new]
static/js/whatsapp.js                           [new]
tests/test_whatsapp.py                          [new]
docs/WhatsApp Module Handover.md                [modified — this file]
```

All test files pass (`for f in tests/test_*.py; do python "$f"; done`) as of
this writing — 15 files, including the new `test_whatsapp.py`.

---

## 5. Verification performed (and what wasn't)

Verified: every db.py function via direct calls; every route via the Flask
test client (login, CSRF, multipart CSV, JSON); the full page renders
(`GET /` returns 200 with the WhatsApp section, sidebar link, modal, and
script tag all present); every `onclick` in the new templates resolves to a
defined JS function; signal detection against a local HTTP server covering
keyword match, widget match, thin-content, and connection-refused cases;
the background scheduler job end-to-end; the entire confirm → draft →
edit → send → follow-up → replied/paused → move lifecycle; cross-channel
conflict detection specifically for `channel="whatsapp"`.

**Not performed: clicking through the UI in an actual browser.** No browser
automation was available in this session. The JS was checked for syntax
(`node --check`) and cross-referenced against the templates for undefined
function calls, but a layout bug, a CSS issue, or something that only shows
up under real user interaction (e.g. the inline textareas in the table)
would not have been caught. Worth an actual click-through before relying on
this in front of real leads.

---

## 6. Deferred / not done on purpose

- **Contacts → Email, Cold Calling → Calling rename**, and the matching
  `/api/contacts/*` → `/api/email-leads/*` URL rename. Still deliberately
  deferred (see Phase 1's notes) — doing it now would touch every template
  a second time and would require the operator to re-pull
  `scraper_worker.py` on their own machine, which felt like the wrong thing
  to do without being asked.
- **Excel (`.xlsx`) import for WhatsApp leads.** The importer is CSV/paste
  only, matching `/api/contacts/import`'s existing precedent exactly.
  `openpyxl` is already an optional dependency (used for *export*, not
  read) — adding `.xlsx` read support is a small, separable addition if
  actually needed, not attempted here since the operator's own scraper
  workflow already produces CSV.
- **A local rendered-browser worker for "unclear" signals.** Explicitly
  decided against for v1 (see §3, `wa_signal.py`). If the `unclear` bucket
  turns out to be a large fraction of real imports once run against actual
  Gulf clinic websites, that's the trigger to revisit this, not a schedule.
- **The production migration has still never run against the real
  `outreach.db`.** True since Phase 1; still true now. Low-risk given the
  operator's own estimate of ~20 contacts, but worth a deliberate,
  attended run rather than assuming it'll be fine on autopilot.
- **A number-validity pre-check** (is this number even on WhatsApp) —
  researched and explicitly decided against earlier in this project's
  history. Not revisited in Phase 3; the reasoning (no clean free way
  exists, a wasted `wa.me` click already tells the operator for free) still
  holds.

---

## 7. Orientation pointers

- `db.py` — WhatsApp block is one contiguous section, `grep -n "# ── WhatsApp" db.py`.
- `wa_signal.py` — the entire signal-detection surface, deliberately small.
- `app.py` — WhatsApp routes are one contiguous section, `grep -n "# ── API: WhatsApp" app.py`; the generic AI text-completion helpers sit just above it.
- `scheduler.py` — `run_wa_signal_scan` plus its own comment on why it's the only WhatsApp code allowed in that file.
- `tests/test_whatsapp.py` — read this before changing any WhatsApp behavior; it's the most complete description of intended behavior that exists, more so than this document.
