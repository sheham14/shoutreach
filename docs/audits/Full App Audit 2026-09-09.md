# ShoutReach — full application audit

**Date:** 2026-09-09
**Scope:** security, performance, GCP cost on e2-micro, UX and onboarding, simplification, feature ideas.
**Status:** untracked working note. Nothing here is committed to git.

Three parallel audits (security, performance/cost, usability) plus direct
inspection. The security findings marked **reproduced** were demonstrated with a
real two-user Flask test client against a scratch database — they are not theory.

---

## Status — mostly NOT actioned

Last checked against the code: 2026-09-09, after commit `a5e985d`.

**Done (9 items)** — and only because they overlapped with the multi-user work
that was already underway, not because the audit was worked through:

- The six cross-owner security holes and two missing indexes in §0.
- §1.4 password floor: 8 → 12 at account creation.
- §6.1 per-operator WhatsApp templates (plus A/B versions, which the audit
  didn't ask for), and the calling section walled in §7.

**Open (everything else)** — including all of §2 (performance), all of §3 (cost),
all of §4 (onboarding), all of §5 (simplification), §6.2–6.6, and the two
biggest single items: **§1.1 secrets in plaintext** and **§2.1 `/api/calls/queue`**.

Spot-checked while writing this line, all still present: `limit=100000` at
[db.py:3898](db.py#L3898), no `sends(email_lead_id)` index, the uncapped bounce
fetch at [sender.py:640](sender.py#L640), the scraper interval re-armed at
[scraper.js:123](static/js/scraper.js#L123), and `"contacts"` still listed in
`_VIEWER_TABLES` at [app.py:2479](app.py#L2479).

Nothing here is urgent enough to block a deploy. The performance items bite at
scale you don't have yet; the onboarding items bite the day your colleague logs
in.

---

## 0. Read this first

**Already fixed today**, during the multi-user build — listed so you don't chase
them:

| Was | Now |
|---|---|
| Any operator could enrol another's lead into their own campaign, and the scheduler would genuinely mail them | Lead ids from a request body are filtered to the caller |
| "Enrol All" selected every active lead in the database, not just yours | Scoped to the caller |
| Bulk-delete hard-deleted another operator's leads by id | Scoped to the caller |
| Logging a call against another's business set `do_not_contact`, killing their lead on every channel | `require_owned("business", …)` |
| Business search returned everyone's names, phones and addresses | Scoped to the caller |
| Scrape status/stop exposed and could halt the other operator's job | Scoped to the caller |
| Owner filtering meant a full table scan on nearly every read | Indexes on `businesses(owner_id)`, `email_leads(owner_id)` |

Commits `86bd4af`, `8517c64`, `5e15173`, `61ae78f`. All 18 test files pass.

**The three things I would do next, in order:**

1. **Encrypt secrets at rest** (§1.1) — the single largest remaining risk.
2. **Fix `/api/calls/queue`** (§2.1) — the worst performance problem, and it is
   on your most-used page.
3. **Give the app a first-run path** (§4.1) — your colleague currently lands on
   an empty dashboard with nothing telling him what to do.

---

## 1. Security

### 1.1 Secrets are plaintext in the database — highest remaining risk

`smtp_pass` and `imap_pass` in `smtp_accounts`, the AI provider keys, the worker
API key, and **the Flask `SECRET_KEY` itself** (`db.py:1010`) are all stored
unencrypted in `outreach.db`.

Anyone who reads that one file gets: your mail credentials, the ability to forge
session cookies for any user, and the ability to forge unsubscribe tokens. A
stray backup, a snapshot copied to the wrong bucket, or read access to the VM is
enough. This was flagged in the earlier Fable audit (§5) and is still open.

Minimum viable fix: derive a key from an environment variable set on the VM
(never stored in the database) and encrypt the credential columns with it. That
way a leaked `.db` file alone is not enough. The `SECRET_KEY` in particular
should move to the environment outright — it does not belong in a table the app
itself hands out over an API.

### 1.2 Admin is very powerful, and there is no audit trail of admin actions

An admin can read the worker key (`app.py:578`) and reset any other user's
password without knowing the current one (`app.py:810` — the guard only checks
`is_self` for non-admins). With two admins that means either can silently take
over the other's account. Now that a real non-admin role exists, **make the
colleague a non-admin** and this stops mattering. If you ever want two admins,
log password resets and key reveals.

### 1.3 Things that are correct and should not be "fixed"

The auditor confirmed these are sound; noting them so they don't get churned:

- **No SQL injection reachable.** Every f-string in SQL interpolates a fixed
  literal, a whitelisted key, or a dict-looked-up column name. Values are always
  bound.
- **No template injection.** No `render_template_string`, no `|safe`. `/api/preview`
  takes an attacker-supplied template but `sender._render` is regex substitution,
  not Jinja.
- **CSRF, session handling and cookie flags are correct.** Session is cleared and
  rotated on login; `compare_digest` used throughout; the `X-API-Key` CSRF
  exemption for the worker is properly reasoned.
- **`/unsubscribe` is HMAC-gated**, with no enumeration.
- **Suppression deliberately ignores the multi-user wall.** `unsubscribe_contact`,
  `mark_bounced`, `increment_soft_bounce` fan out across every owner on purpose —
  you both send from the same domain, so a half-honoured opt-out would be a real
  CASL/CAN-SPAM breach. Do not "fix" this by adding an owner filter.

### 1.4 Deployment

- `requirements.txt` uses `>=` only — a breaking upstream release can land on a
  deploy. Pin exact versions.
- `.github/workflows/deploy.yml` deploys on every push to master **with no test
  gate**. You have 18 passing test files; run them in CI first. This is cheap and
  would have caught real regressions today.
- `appleboy/ssh-action@v1.0.3` is pinned by tag, not commit SHA. A compromised
  tag would get shell on your VM.
- ~~Password minimum is 8 characters at `app.py:791` but 12 elsewhere.~~ **Fixed**
  (`a5e985d`) — 12 everywhere.
- `/logout` is a CSRF-exempt GET (`app.py:157`) — someone can log you out from a
  link. Annoying rather than dangerous.

---

## 2. Performance and speed

Ordered by what actually bites first on a 1 GB box.

### 2.1 `/api/calls/queue` is three problems stacked — worst in the app

`app.py:1801`. Every time the Calling page loads:

1. **`get_call_queue_counts`** (`db.py:3729`) calls `get_call_queue(limit=100000)`
   four times and takes `len()` of each. Each pass builds full 21-column joined
   dicts. At 5,000 businesses that is ~20,000 dicts (~50 MB transient) per
   request; at 100,000 it will OOM the box. **These should be four
   `SELECT COUNT(*)`.**
2. **N+1**: `get_touch_history` is called per lead (`app.py:1818`), up to 200
   times. Each opens its own connection and runs 4 queries — ~800 queries and
   200 connection opens per page load.
3. **Missing index**: `sends` has no index on `email_lead_id` alone.
   `sends_dedupe_idx` is `(campaign_id, email_lead_id, step_num)` — wrong
   leftmost column, so it can't serve this. `get_touch_history` therefore scans
   `sends` 200× per request.

Fixing (1) alone is the single biggest speed win available.

### 2.2 Whole-table load per import row

`db.py:3095` — `find_existing_business`'s fourth fallback runs
`SELECT * FROM businesses WHERE name!='' AND owner_id=?` with **no LIMIT**,
fetches everything, and normalises names in Python. This is the *common* path for
no-website scraped leads, and the import calls it three times per row.

A 1,000-row import against 20,000 businesses is ~60 million row materialisations
in one request. Fix by storing a normalised name + locality column at write time
and indexing it, or at minimum caching one scan per import batch.

### 2.3 The bounce scanner can OOM the box

`sender.py:630` — unlike the reply scanner (capped at 50, `sender.py:466`), the
bounce scanner's ID list is uncapped, and it fetches `BODY.PEEK[]`, i.e. **entire
messages including attachments**, into memory. Non-bounces are never marked seen,
so the same set is re-downloaded and re-parsed every 5 minutes, forever.

Cap the ID list; fetch headers first and only pull the body when a header
matches. This is the most likely cause of an unexplained restart on a 1 GB VM.

### 2.4 The Scraper tab polls forever

`static/js/scraper.js:8,31,122` — `showSection` has no teardown and line 122
re-arms the interval on every idle poll. Open the Scraper tab once and
`/api/scraper/status` fires every 3 seconds for the life of the tab. That
endpoint does a **write** (`reap_stale_scrape_jobs`) plus three more calls, about
5 fresh connections per poll. On shared-core e2-micro this quietly burns CPU
credits doing nothing. Clear the interval when the section is hidden.

### 2.5 Slower-burning

- **Connection churn** — `get_db()` is called 129 times in `db.py`, each opening a
  new connection and running 3 PRAGMAs. Note `with get_db() as conn` **commits
  but does not close**. A thread-local connection would remove most of this.
- **`/api/campaigns` N+1** — per campaign: `get_steps` + `get_stats`, and
  `get_stats` is 8 COUNTs plus a 10th connection for `get_today_count()`. Same
  shape in `get_call_campaigns` (`db.py:3470`).
- **Blocking the only worker inside requests** — inline MX validation does up to
  500 DNS lookups at 5s each (`app.py:1613`); worst case blows past gunicorn's
  120s timeout, which kills the worker **and the scheduler thread living inside
  it**. Outbound LLM calls (30s) do the same on `/api/ai/review` and
  `/api/wa/draft-batch`.
- **Scheduler idles noisily** — every 60s it runs `get_settings` +
  `get_campaigns` + `get_today_count` + the WhatsApp signal scan even with zero
  active campaigns: ~7,200 connections a day doing nothing, with no back-off.
  `run_wa_signal_scan` deserves the same off-switch `email_checking_enabled` has.
- **Two IMAP logins per account per cycle** — reply check and bounce check log in
  separately (`sender.py:539`, `sender.py:686`) = 576 logins/day/account.
  Consolidate to one.

---

## 3. GCP cost on e2-micro

**The VM is probably already free.** e2-micro is free-tier eligible in
`us-west1`, `us-central1` and `us-east1` with a 30 GB standard persistent disk.
Worth confirming which region you're in — if you're outside those three, moving
is the single biggest saving available and the app is trivially portable
(one VM, one SQLite file).

**Your largest line item may be the static external IPv4 address (~$3/month),
which can cost more than the VM itself.** Check whether you actually need a
static IP or whether an ephemeral one plus DNS would do.

Disk is pennies — don't bother shrinking it. Other levers:

- **Log volume.** `sender.py:484` writes a "Reply check: scanning N…" row every 5
  minutes per account regardless of activity, plus up to 50 "unmatched
  In-Reply-To" rows per scan. At 60-day retention that's hundreds of thousands of
  rows of no value. Demote to `logger.debug`.
- **Disk never shrinks.** `prune_logs`/`clear_logs` DELETE without `VACUUM`, and
  WAL autocheckpoint is untuned. Add a periodic `PRAGMA incremental_vacuum`.
- **journald** is uncapped by default (10% of disk). Set `SystemMaxUse`
  explicitly.
- **No gzip anywhere** (`app.py:359` only sets security headers). Enable it in
  nginx — roughly 4× smaller pages for negligible CPU.
- **Static files**: `templates/index.html:92` loads all 11 JS files (205 KB raw)
  on every page load, and renders all 10 sections plus 10 modals server-side
  (~98 KB, of which `help.html` alone is 25 KB) whether used or not. There is no
  cache-busting and Flask's `SEND_FILE_MAX_AGE_DEFAULT` is None, so you get 11
  revalidation requests per reload hitting the single worker unless nginx serves
  `/static` directly. **Serving `/static` from nginx is the easiest win here.**

**Verdict: you do not need to upgrade from e2-micro.** Fix §2.1 and §2.3 and the
box has plenty of headroom for a few tens of thousands of leads.

---

## 4. Onboarding — your colleague's first week

This is where the app is weakest, and it's the thing you actually asked about.

### 4.1 There is no first-run path at all

A new user logs in and lands on the Dashboard. Above the fold is a checkbox —
"Automatically check for replies & bounces" (`dashboard.html:16`) — a maintenance
setting, presented as the first thing on the page. Below it: six stat tiles all
showing `—`, then a table saying "No campaigns yet" with **no link and no next
step**. Nothing anywhere points at Help.

Help itself is genuinely good, but it opens on "First-Time Setup", whose first
instruction is editing SPF/DKIM/DMARC records at a domain registrar
(`help.html:23`). That is the wrong first screen for a non-technical person on a
phone — and it's work you have already done.

**Fix:** an empty-state block on the Dashboard with three buttons — *Import
leads* / *Start a WhatsApp list* / *How this works* — and a second Help entry
point that assumes the domain is already configured. This is maybe an hour of
work and it is the difference between him getting started and him messaging you.

Also stale: `help.html:380` tells the user to "check the status dot in the
bottom-left sidebar". There is no status dot; it was removed.

### 4.2 Activating a campaign sends real email with no confirmation

`campaigns.js:117` — `activateCampaign()` POSTs immediately. No confirm, no count
of how many people are about to be emailed. Meanwhile *deleting* a campaign gets
a full consequence-stating confirmation (`campaigns.js:88`).

Worse on mobile: that button sits in a row with `Manage →` and a bare red `✕`
delete, in a `<div class="ml-auto flex gap-2">` that **neither** wrap rule in
`main.css:475` reaches — so four controls, one of which starts emailing strangers
and one of which deletes everything, are crammed unwrapped into 375px.

**This is the most likely way your colleague causes real-world damage in week
one.** Add a confirm stating the recipient count, and make that row wrap.

### 4.3 `prompt()` for building call lists

`contacts.js:404` and `calling.js:85` use browser `prompt()` — rendering a
numbered text menu of campaigns and asking the user to *type a number or a new
name* into a native single-line dialog. On a phone that's a cramped OS box with
the list truncated, and it's the main path from Contacts into Calling.

### 4.4 Mobile gaps the media query cannot reach

The mobile work is real but has holes, all the same root cause — inline styles
beat class-based `@media` rules:

- `calling.js:462` — **the outcome buttons**, the most-tapped control on the
  Calling page, are built with inline `padding:6px 12px;font-size:12px` instead of
  `.btn`. ~28px tap targets deciding a lead's fate. `main.css:492` cannot touch
  them.
- `modals/add_call_leads.html:19` — search + status inputs hand-styled with
  `min-width:180px`, bypassing `.search-input`/`.filter-select`.
- `whatsapp.js:141` — signal-type `<select>`, same.
- `modals/step_editor.html:81` — `#copy-target` select, same.
- `main.css:251` — `.modal-close` is a bare 20px glyph with no padding, and it's
  the only way to dismiss every modal.
- **Contacts shows 12 columns by default** — it scrolls, but reading one lead
  means dragging sideways through 14 columns. The `⊞ Columns` control fixes this
  and is invisible as a fix.

**Hover-only explanations** (unreachable on touch): all three Contacts filters
(`contacts.html:21,31,41`), "Also stops any email sequence" (`calling.js:469`),
the No website / No email pills (`utils.js:248` — your strongest lead signal),
and the *only* description of what "Check for replies & send" does
(`sidebar.html:23`). Info dots exist and are used exactly three times.

### 4.5 Jargon that is never defined in the UI

Ranked by how much it will bite: **enroll** (never defined outside Help, and
"Enroll All Active" is a one-tap bulk action with no confirm and no count) ·
**signal / gap found / no gap** (WhatsApp's core concept) · **MX** ("no MX
records… a potential sales signal" means nothing to him) · **variant / A/B** ·
**warmup** · **SMTP/IMAP** (unavoidable, but Settings tells him to "See
README.md", a file he cannot open from a phone).

---

## 5. Simplification

Things a non-technical operator will never touch, currently all first-class UI:

- **Database** section — a raw table browser.
- **Scraper** — requires running `python scraper_worker.py` with environment
  variables on a laptop. Useless on a phone, yet it is a top-level nav item.
- **Worker API Key card** (`settings.html:120`).
- **A/B variants** (`step_editor.html:63`).
- **min/max delay seconds**, **bounce auto-pause threshold**, **include
  unsubscribe header**.

Hiding these behind an "Advanced" toggle — or behind the admin flag, which now
actually means something — would cut the visible surface roughly in half for him
while changing nothing for you. **Gating them on `is_admin` is close to free now
that the role exists.**

Duplication worth removing: the worker setup instructions appear twice, verbatim
(`scraper.html:20` and `settings.html:139`).

---

## 6. Feature ideas, in rough value order

1. ~~**Per-operator WhatsApp templates and follow-up interval.**~~ **Done**
   (`5021136`) — both are now keyed per operator, saving is no longer admin-only,
   and any template can hold up to four A/B versions with reply rates reported
   per version. See §7 for what this does *not* cover.
2. **A recipient count before activating a campaign.** See §4.2 — safety and
   confidence at once.
3. **CI that runs the tests before deploying.** You have the tests; nothing runs
   them.
4. **`.xlsx` import.** Already on the deferred list. Your scraper emits CSV, but
   people send spreadsheets.
5. **A "what changed" digest** — replies, bounces, follow-ups due — so the
   dashboard answers "what needs me today" instead of showing lifetime totals.
6. **Cross-owner overlap on existing data.** The new notice fires at import time
   only, so overlap that already exists between your list and his on day one is
   never flagged. A one-off scan would cover it.

---

## 7. Known limits of the multi-user wall

Worth being explicit, since it is new:

- Cross-owner duplicate detection is **heuristic** — the same phone/domain/name
  matching used everywhere else. Two rows for one real clinic that share no
  matching field will not be flagged. It is a heads-up, not a guarantee.
- The **activity log** (`/api/logs`) has no owner column and carries recipient
  addresses. It is admin-only, which is why this isn't a leak — but don't
  downgrade that route without adding ownership first.
- The **raw database viewer** reads `campaigns`, `enrollments`, `sends`, `steps`,
  `logs` and `settings` unfiltered. Also admin-only. Same warning.
- `_VIEWER_TABLES` still lists `"contacts"`, a table that no longer exists.
- Sending accounts, the daily cap, the AI keys and the domain are **shared by
  design** — you both send as the same company. One person's campaign can eat the
  other's daily quota. That is intended, but it means a busy week for one of you
  throttles the other.
- **Calling is fully walled** as of `11b2325`: the call script is per operator
  (blank for a new one, not inherited), and custom call outcomes belong to
  whoever invented them. Built-in outcomes stay shared because two are
  special-cased in code, and `terminal_outcome_keys` deliberately still spans
  every owner — those keys are matched against `call_status` values already on
  rows, so filtering them would resurrect closed leads.
- **Email step copy is shared only in the sense that campaigns are owned** —
  steps and their A/B variants live on the campaign, so in practice you each
  write your own.
- **A/B versions are dealt round-robin, not weighted**, and nothing declares a
  winner. You read the reply rates and decide. That is deliberate at this volume:
  significance testing on a few dozen sends would imply confidence the numbers
  don't support.
