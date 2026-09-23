# ShoutReach Handover

**Last updated:** 2026-09-20 (bespoke per-lead WhatsApp copy; live) · **Branch:** `master` · **Live:** https://shoutreach.hexiv.co

Read this before touching code. It's written for a session with no memory of
how the app got here. Where it and the code disagree, trust the code — and
fix this file.

---

## 0. Read this first

- **Everything is deployed** as of 2026-09-16 (`bed7f2a`): per-account scrape
  workers, and the **channel redesign** — Contacts as every business, a Leads
  table and tabs on every channel, WhatsApp campaigns, explicit Calling
  membership, a three-channel Dashboard (§2, §2a). That deploy ran three
  one-shot migrations on the live database: the worker key moved to the
  founding admin; every lead that was implicitly on Calling got a real row,
  minus WhatsApp leads (`_make_calling_explicit`); each operator's WhatsApp
  leads and templates moved into a campaign named "My first campaign"
  (`_migrate_wa_campaigns`). Before pushing, the upgrade was rehearsed on a
  database built by the previously-live code (`076b20c`) — templates, leads,
  call history and campaigns all carried over. No fresh backup was taken for
  this deploy; the newest on the VM is `~/outreach.db.bak-2026-09-15`.
- **Live since 2026-09-16 18:51 UTC: the lean WhatsApp flow** (`cf49426`,
  deployed with `be439b5`, §5). No website check or review step — leads land
  ready to send; an optional, on-click audit; every country; a side panel on
  every Leads tab; "Add all to…" for a whole scrape. The restart ran
  `_migrate_wa_no_review`. No backup was taken: the operator said the
  WhatsApp leads were only a test run and free to lose.
- **Live since 2026-09-16 21:35 UTC (`9354245`): a send is only recorded when
  the operator says so** (§5). Opening a chat asks "Did it send?" — Sent, Not
  on WhatsApp, Didn't send. "Not on WhatsApp" marks a lead and keeps it on
  WhatsApp for a bulk move-off; landlines sort last. It added two nullable
  columns (`opened_at`, `no_whatsapp_at`) and changed no data. Prompted by a
  real send: a number not on WhatsApp was marked sent, and the operator saw
  other leads marked too — most likely extra clicks while the reused
  WhatsApp tab stayed in the background (the code can only mark the lead
  clicked).
- **Live since 2026-09-20 08:04 UTC (`62f9118`): bespoke per-lead WhatsApp
  copy** (§5). Leads can arrive from a JSON or CSV import carrying their own
  opener and three follow-ups; anything they don't carry falls back to the
  campaign's templates, and so does every follow-up past the third. The
  restart added two defaulted columns to `wa_leads` and ran
  `_migrate_wa_message_source`. **No backup was taken for this deploy.** That
  one push also carried three commits that had been sitting unpushed since
  09-16 — the phone layouts, the per-row WhatsApp button and row-menu fix,
  and a docs pass — so the VM went `9354245..62f9118` in one restart, 26
  files. All 21 test files passed before the push, and `/login` returns 200
  after it, which is what rules out a failed `init_db`.
- **Built 2026-09-22, NOT deployed: pipeline stages** (§5a). One stage per
  business, shared by every channel, with your own stages allowed; plus the
  "who have I sent to" filter and a list of what actually went out. Four
  defaulted columns on `businesses` and one new table, no backfill.
- **Two people use this install, walled off from each other.** Almost every
  query is scoped to an owner, and a handful deliberately aren't. Read §3
  before adding a query, a route, or a background job.
- **Pushing to `master` deploys to production immediately** and re-runs
  `init_db()` migrations against the live database. Confirm with the operator
  before every push.
- **WhatsApp sending is manual, always.** §7 lists the hard constraints.

---

## 1. What's deployed

| Commit | Date | What | Live? |
|---|---|---|---|
| `62f9118` | 09-20 | A lead can bring its own opener and three follow-ups (JSON/CSV import); follow-ups editable per lead | Yes |
| `f5c10ce` | 09-16 | Lead lists and lead panels work on a phone | Yes |
| `ddfd25e` | 09-16 | WhatsApp button on each To do row; row menus no longer clipped | Yes |
| `06f1c3f` | 09-16 | Docs brought up to date with the confirmed-send flow | Yes |
| `9354245` | 09-16 | Confirm a send after opening WhatsApp; mark "not on WhatsApp" and move off in bulk; landlines last | Yes |
| `a534086` | 09-16 | Handover: lean flow recorded as live | Yes |
| `be439b5` | 09-16 | Operator's own files (reference pages, AGENTS.md, the full audit doc) | Yes |
| `cf49426` | 09-16 | Lean WhatsApp flow, lead audit, every country, lead side panels, add a whole scrape | Yes |
| `bed7f2a` | 09-16 | Contacts hub, Leads tabs on every channel, WhatsApp campaigns, explicit Calling, Dashboard | Yes |
| `bfe08c7` | 09-15 | Each account gets its own scrape worker | Yes |
| `076b20c` | 09-15 | Scrapes can feed WhatsApp; add existing leads to WhatsApp | Yes |
| `364857a` | 09-15 | `reset_password.py` for a locked-out admin | Yes |
| `a5e985d` | 09-09 | WhatsApp copy never inherited across accounts; user-admin fixes | Yes |
| `11b2325` | 09-09 | Calling walled: per-operator call script and custom outcomes | Yes |
| `5021136` | 09-09 | Per-operator WhatsApp templates, with A/B versions | Yes |
| `61ae78f` | 09-09 | Ownership holes in body-id routes closed; admin/non-admin split | Yes |
| `5e15173` | 09-09 | "Someone else already has this lead" notice on import | Yes |
| `8517c64` | 09-09 | Every list and id-taking route scoped to its owner | Yes |
| `86bd4af` | 09-09 | Owner on every lead, campaign and scrape job | Yes |
| `792537f` and earlier | 09-06 | WhatsApp module, `contacts` → `businesses` split, mobile work | Yes |

**The big production migration has run.** Deployed 2026-09-15 around 11:01
UTC: the `contacts` → `businesses` split *and* the ownership backfill, in one
restart. A backup was taken first: `~/outreach.db.bak-2026-09-15` on the VM.
The app came up healthy. The operator was asked to check that their leads,
calling list and WhatsApp templates all appear under their own account; that
wasn't explicitly confirmed back in the session.

---

## 2. What the app does now

- **Contacts** — every business (`businesses`), whichever channel it's on, with
  a Channels column, tabs for **All / Unassigned / Do not contact**, bulk
  "send to a channel", "Add all in this list to…" when a scrape is picked, and
  the lead side panel. Served by `/api/businesses*` (`db.get_businesses_page`
  and friends).
- **The lead side panel** (`static/js/lead_panel.js`, `openLeadPanel`) — one
  business, opened beside the table on Contacts and on every channel's Leads
  tab: that channel's controls, where else the business is, notes, the audit
  and the history.
- **Email** (sidebar *Email*, formerly *Campaigns*) — multi-step sequences, A/B
  variants per step, rotation across sending accounts, IMAP reply and bounce
  detection, HMAC-signed unsubscribe links. Tabs: Campaigns, Leads (every
  `email_leads` row — what the old Contacts page was), Unsubscribed & bad
  addresses. `/api/contacts/*` still serves email leads; the URLs were kept
  because the scrape worker posts to `/api/contacts/import`.
- **Calling** — tabs: To do (the dialler: buckets, lead card, script), Leads
  (`/api/calls/leads`), Campaigns, Script & outcomes. `.ics` invites for booked
  meetings.
- **WhatsApp** — import into a **campaign** → the lead lands in **Ready to
  send**, its message read live from the campaign's template → the operator
  taps *Open in WhatsApp*, sends it themselves, and comes back to say *Sent*,
  *Not on WhatsApp* or *Didn't send* → follow-ups at the
  campaign's gap, forever, until replied or paused. The audit is optional and
  never in the way. Tabs: To do (Ready to send, Follow-up due, "sent today"),
  Leads, Campaigns, Templates. Design history:
  `docs/WhatsApp Module Handover.md` (its booking-gap check is retired).
- **Lead Scraper** — Google Maps, run by a worker on an operator's own laptop
  (the server has no screen to show CAPTCHAs on). Each scrape targets Email,
  Calling or WhatsApp, optionally into a campaign (required for WhatsApp).
  "Your scrapes" lists past scrapes with "Add all to…".
- **Dashboard** — today's to-do across channels, each channel's numbers, every
  campaign in one table (`/api/dashboard`, `db.get_dashboard`).
- **Settings** — admins: email accounts, sending rules (including the automatic
  reply-check switch, moved here from the Dashboard), AI keys, the optional
  Google API key for the audit, users. Everyone: their own scrape worker key
  and their own audit links.

## 2a. Channel membership — what "on a channel" means

| Channel | On it when | Taken off by |
|---|---|---|
| Email | an `email_leads` row with `status != 'deleted'` | deleting the address |
| Calling | a `call_leads` row with `removed_at IS NULL` | `remove_from_calling` (history kept; adding back clears `removed_at`) |
| WhatsApp | a `wa_leads` row with `moved_to = ''` and `removed_at IS NULL` | `move_wa_lead` / `move_wa_leads` (rules the number out for good) or `remove_wa_leads` (can be re-added) |

A WhatsApp lead **marked not on WhatsApp** (`no_whatsapp_at`) is still on the
channel — it isn't Unassigned and the add-leads picker won't offer it — but
it's in no queue and no ready/due count (`_WA_WORKABLE`) until it's moved off
or the mark is taken back.

A business on none of them, and not `do_not_contact`, is **Unassigned**; the
reason is derived in `_business_rows_sql`. Rules that are easy to break:

- **Calling is explicit.** The queue reads `call_leads`, not every business with
  a phone. Nothing but an operator's action (or a Calling scrape) puts a lead
  on Calling. `get_or_create_call_lead` re-activates a removed lead, because
  logging a call against it is as explicit as adding it.
- **Every WhatsApp lead belongs to a campaign** (`wa_leads.wa_campaign_id`).
  NULL only happens when a campaign is deleted; such a lead has no message
  until it's moved into one.
- **Templates live on `wa_campaigns`** (JSON `templates`, `followup_days`,
  `variables`). The old per-operator settings keys (`wa_template_gap:<uid>`…)
  are read only by `_migrate_wa_campaigns`, and `GET /api/settings` no longer
  returns any `:`-suffixed or `_`-prefixed key.
- **A new lead gets whichever version has fewest leads** in its campaign
  (`_deal_wa_label`), so versions alternate A, B, A, B however small the batch.
- **Deleting a business keeps anyone who opted out, unsubscribed or bounced**
  (`delete_businesses`), because that row is what suppresses a re-import.
- The one-shot migrations are guarded by settings markers
  (`_migrated_calling_explicit`, `_migrated_wa_campaigns`,
  `_migrated_wa_no_review`), not by the absence of rows — re-running them would
  undo operators' removals and edits.

## 3. The multi-operator model

Two operators — the owner and his cofounder — share one install. They share
the sending infrastructure. They do **not** share leads.

**Walled — every row belongs to one operator:** `businesses`, `email_leads`
(owner copied onto the row, because the per-owner unique email index needs
it), `campaigns`, `call_campaigns`, `wa_campaigns`, `scrape_jobs`,
`call_scripts`, `call_outcome_types` (owner 0 = built-in and shared),
`worker_keys`.
`wa_leads` and `call_leads` inherit ownership from their business. Two
operators working the same clinic hold two separate `businesses` rows.

**Shared on purpose:** SMTP accounts, the global daily sending cap, AI provider
keys, sending rules, the domain. One person's campaigns can use up the shared
daily cap.

**Admin is not a back door.** `is_admin` gates the shared setup — email
accounts, `POST /api/settings`, users, the raw database viewer, running the
scheduler by hand, clearing logs, editing built-in call outcomes. It grants no
visibility into anyone else's leads.

**Rules that are easy to break:**

- **Scope every list or summary query by `owner_id`.** `db._resolve_owner_id`
  **raises** once more than one user exists and no owner was passed. That's
  deliberate: a forgotten owner errors loudly instead of filing one person's
  leads under the other.
- **Ids in the URL** are guarded by `@owned(kind, param)`. **Ids in the request
  body are not** — filter them with `db._own_business_ids`,
  `db._own_wa_lead_ids` or `require_owned(...)`, and check a body campaign id
  with `db.owns("wa_campaign"|"call_campaign"|"campaign", id, me())`. That gap
  was a real, exploitable hole (fixed in `61ae78f`).
- **`call_log` has no owner.** Anything counting calls joins through
  `call_leads` → `businesses` (`get_call_summary` used to count both
  operators' calls).
- **Refuse another operator's row with 404, not 403**, so ids can't be probed.
- **These deliberately span every operator — never add an owner filter:**
  - `unsubscribe_contact`, `mark_bounced`, `increment_soft_bounce`. They use
    `IN (SELECT …)` because an address can exist once per operator, and an
    opt-out binds everyone sending from the shared domain.
  - The send loop (`get_campaigns(all_owners=True)`) and `get_due_enrollments`.
  - `terminal_outcome_keys` — it's matched against `call_status` values already
    written to rows.
  - `reap_stale_scrape_jobs`.
- **Overlap between operators is advisory only** (`find_cross_owner_matches`).
  The import goes ahead; the notice shows business name, channel and date,
  never contact details; nothing is merged. Matching is heuristic
  (phone / domain / email / name + locality).
- **A user who still owns rows can't be deleted** (`delete_user` refuses).
- **Per-operator settings are suffixed keys** — `audit_links:<uid>`, and the
  retired `wa_template_gap:<uid>` family. **Message copy never falls back to
  another operator's**: the two lead with different services.
- **The audit's checks and the shared Google API key** are the one audit
  piece that isn't per operator; results live on the business, so they're
  walled like it.

`tests/test_ownership.py` (16 sections) is the most precise statement of all
of this.

---

## 4. Scraper and workers

### Per-account workers — `bfe08c7`, live

Each account has its own key in `worker_keys`. `worker_owner_for_key` maps the
`X-API-Key` a worker presents to an account (timing-safe; compares against
every key without stopping early). From there:

- `claim_scrape_job(owner)` only hands a worker its own account's scrapes.
- A worker can't post progress into someone else's scrape (404).
- Imports are attributed to the key's owner, which outranks any
  `source_job_id` in the rows.
- One active scrape per operator, and *Worker connected* shows only your own.
- Rotating a key disconnects only that person's worker.
- `_migrate_worker_key` moves the old install-wide `_worker_api_key` setting
  onto the founding admin at startup.

### Destinations

A scrape targets `email`, `calling` or `whatsapp` (`scrape_jobs.destination`;
WhatsApp also needs a `country` — any region Google's `phonenumbers` library
knows (`db.WA_COUNTRY_CODES`) — and a `campaign_id`; Calling takes an optional
one). The **server** decides where rows land, from the job,
and only for worker requests (`_scrape_job_for_import`): a CSV imported by hand
still goes where the person importing it sent it. Calling and WhatsApp scrapes
drop any emails rather than filing them as email leads; a Calling scrape's
leads with no phone stay in Contacts and the count goes into the scrape log.
The worker skips the email search and the per-site delay for anything that
isn't an Email scrape — **the `calling` destination needs the updated
`scraper_worker.py`** (an older worker would still work, just slowly, since the
server does the routing).

**Adding leads you already have to a channel:** from Contacts (bulk or the
detail panel), or each channel's *+ Add leads* (`POST /api/calls/add`,
`POST /api/wa/add-existing` with a `wa_campaign_id`,
`POST /api/businesses/enroll`), or a whole scrape at once from the Scraper's
"Your scrapes" or Contacts (`POST /api/lists/add-to` with `source_job_id`,
`channel`, `campaign_id`, and `country` for WhatsApp). Anything already on
another channel is held for confirmation. Leads with no phone, numbers already ruled out as not on
WhatsApp, and opted-out businesses are counted and reported rather than
silently dropped.

### Running a worker

Full steps are in README → *0. Get Leads*.

```
pip install -r requirements-worker.txt
playwright install chromium
python scraper_worker.py --server https://shoutreach.hexiv.co --api-key <key>
```

The first successful start saves the server and key to `.worker_config.json`
next to the script (git-ignored). After that it's just
`python scraper_worker.py`. The window has to stay open while scraping. Copy
the key from **Settings → Lead Scraper Worker** while signed in as the person
whose scrapes that machine should run.

### The cofounder's laptop — planned, not done

Not started (per-account workers are live, so nothing blocks it):

1. Create his account: Settings → Users, **Admin off**.
2. On his laptop: install Python and Chrome. Copy `scraper_worker.py`,
   `gmaps_email_scraper.py`, `email_validator.py` and
   `requirements-worker.txt` into one folder. Run
   `pip install -r requirements-worker.txt`, then
   `playwright install chromium`.
3. He signs in on his laptop and copies **his own** worker key.
4. Start the worker once with the server and key. Optionally add a `.bat`
   shortcut to his Startup folder (`shell:startup`) so it launches at login.

**Worker updates are copied to him by hand.** `scraper_worker.py` changed in
the redesign (Calling scrapes skip the email search), so the copy he gets must
be current. When any of those four files change, send him the new copies — and remind the operator that his copy won't
update itself. This was chosen over cloning the repo onto his laptop (it would
put the whole private codebase there) and over the worker updating itself (his
laptop would run whatever the server sent it).

**Gotcha:** the worker keeps a CSV per machine named `<city>_<niche>.csv` and
skips businesses already listed in it, so an interrupted run can resume.
Scraping the same niche and city again on the same machine skips what that
CSV already holds.

---

## 5. WhatsApp additions since the module handover

**The lean flow (live 2026-09-16).** The booking-gap check, the review step
and the "write messages" batch are gone (`wa_signal.py` deleted, its scheduler
job removed). The focus is volume.

- **Leads land ready to send** (`wa_status = 'drafted'`) from every route in:
  scrape, CSV/paste, add existing, add a whole list. A lead with no phone isn't
  put on WhatsApp at all.
- **Messages are live.** `wa_message_for(lead, campaign, kind)` renders the
  campaign's *current* template for the lead's version each time it's shown,
  so a template change reaches every unsent lead. A hand edit or AI rewording
  is stored in `draft_message` with `message_edited = 1` and kept until
  "Reset to template" (`reset_wa_message`). What was sent is in `wa_log`
  and never changes.
- **Versions** (`opener`, `followup`; up to 4 each, labelled A–D): a new lead is
  dealt the version with fewest leads (`_deal_wa_label`). When the editor
  removes or reorders opener versions it sends `opener_from` (each saved
  version's old label, or null), and `_remap_wa_versions` points unsent leads
  at their version's new position — or deals a lead whose version was deleted
  to another. Sent leads keep their label; it records what they got. A lead's
  follow-up uses the same letter as its opener.
- **Rendering** is `db.render_wa_message`: `{{key|fallback}}` like email copy;
  fields are the business's (`business_name`, `city`, `category`, `rating`,
  `review_count`, `website`, `address`) and the campaign's variables (lowest
  priority). `{{signal_detail}}` still fills from an old lead's stored value
  and is empty for new ones; the editor flags a template that uses it. The
  browser preview (`fillPlaceholders` in `tables.js`) follows the same rules.
- **Migration** `_migrate_wa_no_review`: each campaign's "no booking" opener
  becomes its opener; the "has booking" copy is kept, unread, under
  `retired_no_gap` in the templates JSON. Leads waiting on a check, review or
  drafting move to Ready to send. A lead that already had written text keeps
  it (as edited) only if it differs from what the template gives now.
- **Countries:** `phonenumbers` (in `requirements.txt`) formats any country's
  number (`format_whatsapp_number`) and tells mobile from landline, with a
  prefix fallback for AE/QA. `GET /api/countries` lists every region and the
  ones this operator uses; the picker is a searchable `<datalist>`.
- **Open in WhatsApp** (`openWhatsAppChat` in `lead_panel.js`): on a computer,
  `web.whatsapp.com/send?...` in one named tab reused for every lead (focused,
  since a reused tab often stays in the background); on a phone, or when the
  operator picks "desktop app", `whatsapp://send`. It's a link the operator
  opens — nothing reads or drives that page.
- **Nothing is sent until the operator says so.** Opening a chat only sets
  `wa_leads.opened_at` (`POST /api/wa/leads/<id>/opened`); the lead stays put
  and asks "Did it send?" (`waConfirmHtml`): **Sent** (`POST .../sent`, the
  only thing that writes `wa_log` and moves the lead on), **Not on WhatsApp**
  (marks it), **Didn't send** (`DELETE .../opened`). A lead opened and not
  answered keeps asking, with the time, so it isn't messaged twice. This
  replaced marking sent on click, which let a second click on a tab that
  hadn't come to the front mark the next lead. "Sent today" counts `wa_log`
  rows since the browser's local midnight (`since`).
- **Ready to send lists landlines last** (`number_type`), oldest first within
  each. Each To do row has its own **WhatsApp ↗** button (`openWaFromRow`),
  which opens the chat and selects that lead in one click.
- **Reword with AI** is an optional button per lead (`POST .../reword`), saved
  like a hand edit. `wa_leads.paraphrased` and `wa_log.paraphrased` keep a
  rewrite from being credited to a version.
- **"Not on WhatsApp" marks, then you move them off in bulk.** Marking
  (`/api/wa/leads/bulk` `no_whatsapp`; `on_whatsapp` undoes it) sets
  `no_whatsapp_at` and takes the lead out of every queue (stage
  `no_whatsapp`). The Leads tab filters to them, and **Move off WhatsApp…**
  (bulk `move`, `move_wa_leads`) sends them to `call` (optional call
  campaign), `email` (needs an active address; optional enrolment) or `none`.
  Each goes through `move_wa_lead`, which sets up the other channel first: a
  lead that can't go (no address, no phone, opted out) stays marked and is
  counted by why (`WaMoveRefused.reason`). The single
  `POST /api/wa/leads/<id>/move` route still works.
- **Reply rates** come from `get_wa_variant_stats`, counted per **lead** rather
  than per message, and split by paraphrased. Nothing declares a winner.

**Bespoke per-lead copy — live 2026-09-20 (`62f9118`).** A lead can
arrive with its own opener and up to `WA_MAX_LEAD_FOLLOWUPS` (3) follow-ups,
written per lead rather than from the campaign's templates. This is the
volume play: the copy is generated elsewhere (a chat that researches each
business) and imported in one pass.

- **Two columns on `wa_leads`.** `draft_followups` is a JSON array of up to 3
  strings, '' when there are none (`parse_lead_followups` /
  `serialize_lead_followups` are the only things that read and write it).
  `message_source` is '' / `import` / `manual` / `ai` — it says whether the
  lead's copy can be reproduced, which is what decides if an import may
  replace it. `_migrate_wa_message_source` backfills it from
  `message_edited`/`paraphrased`, guarded by `_migrated_wa_message_source`.
- **`wa_message_for` picks the follow-up by `followup_count`** (how many have
  gone, so it indexes the next). An empty slot, and *every follow-up past the
  third*, falls back to the campaign's follow-up template — follow-ups are
  still infinite (§7), so the template remains the floor forever. Bespoke copy
  now goes through `render_wa_message` too, so a placeholder in it fills
  instead of reaching a phone as literal braces.
- **Import keys are `message` and `followup_1..3`**, matched exactly like every
  other column — nothing lowercases or aliases a header. `POST /api/wa/import`
  also accepts a bare JSON array, and refuses rows that aren't objects.
  `upsert_wa_leads` now returns **`(accepted, business_ids, drafts)`**; the
  third is `new_draft_report()` — per-slot coverage plus `kept_edits`,
  `no_phone` and `too_long`. Over `WA_MAX_DRAFT_CHARS` (4000) a message is
  dropped, not truncated, and the lead still imports.
- **The overwrite rule** (`_apply_imported_drafts`): an import replaces copy
  whose source is '' or `import`, and never `manual` or `ai`. Any hand edit —
  opener *or* follow-up (`set_wa_followup_draft` sets `manual` too) — protects
  the whole lead, deliberately: replacing an opener that a hand-written
  follow-up was written to follow on from is worse than skipping the lead and
  saying so. Nothing about a draft re-queues a sent lead; only `_put_on_wa`
  moves status, and only when `sent_date IS NULL`.
- **Follow-ups are editable now.** `PUT /api/wa/leads/<id>/followup/<index>`,
  wired to the To do box and the side panel. The box was previously read-only
  (no `onchange`), and there was nowhere per-lead to put follow-up text.
  Past slot 3 the box shows shared template copy, so saving is refused.
- **`get_wa_variant_stats` groups by `own_copy`** as well as arm and
  paraphrased. Every lead carries an arm label whether or not it was ever sent
  that arm's words, so a bespoke lead's reply would otherwise be credited to
  version A. The template editor shows what share of a campaign never used its
  versions. There is no per-lead measurement beyond that and deliberately so:
  with one message per lead there is no repeated copy to compare (the operator
  considered an `angle` tag and rejected it — n=1 per message either way).
- `tests/test_wa_drafts.py` is the precise statement of all of this.

## 5a. Pipeline stages — built 2026-09-22, not yet deployed

Where a business is up to as a deal. Prompted by the cofounder starting to
use the app and not finding: which leads he'd sent to, what went out today,
or anywhere to record that someone booked.

**One stage per business, not per channel** (`businesses.pipeline_stage`,
`pipeline_channel`, `pipeline_at`, `next_action_at` — four defaulted columns,
no backfill). Booked over WhatsApp reads as booked from Email and Calling,
because it's a fact about the prospect, not the channel that reached them.
`pipeline_channel` records where it was set so a channel can say "booked ·
via whatsapp" instead of leaving you to guess.

- **The vocabulary is editable**, shaped exactly like `call_outcome_types`:
  `pipeline_stages` holds shared built-ins (owner 0) plus stages an operator
  invents, which the other never sees. Built-ins: replied, proposal due,
  proposal sent, meeting booked, won, not interested. `wants_date` makes a
  stage ask for one (`next_action_at`); `is_terminal` drops it off the board.
- **Three rules that are load-bearing** (`set_business_pipeline`):
  setting a stage marks that business's WhatsApp lead replied, so the cadence
  stops; **clearing a stage does not un-reply**, because a mis-click must
  never resume messaging a live conversation; and a terminal stage does
  **not** set `do_not_contact`, which spans every operator — "not interested
  in this offer" is not "never contact us".
- **The channel stage and the deal stage are one chain, not two.** Once
  `replied = 1`, `_wa_stage_sql` returns 'replied' forever, so the channel
  stage says nothing after a reply and the deal stage says nothing before
  one. The Leads filter is therefore a single list: ready → messaged →
  follow-up due → replied → the deal stages, with paused / not on WhatsApp /
  taken off as exits. Don't re-split these into two columns.
- **`stage='messaged'`** in `get_wa_leads_page` means `waiting` + `due` — the
  question "who have I sent to" that those two split by a queue detail. A
  value of `deal:<key>` filters on the pipeline stage instead.
- **`get_wa_sent_log`** finally displays `wa_log`, which has recorded every
  send since the module existed and was only ever counted. The "N sent today"
  chip opens it.
- **The import confirmation now names the stage** — "already on whatsapp ·
  Meeting booked" — which is the concrete reason the stage is on the business.
- Also here, from the same feedback: the follow-up gap moved out of Templates
  into campaign Settings, the Version column reads "—" rather than a bare
  letter when there's nothing to say, and lead search covers email and notes.
- `tests/test_pipeline.py` is the precise statement of all of this.

**Not done, deliberately:** Calling and Email still keep their own per-channel
state (`call_leads.call_status`, enrollments) — the pipeline sits alongside
rather than replacing them. An email reply detected over IMAP does **not** set
`pipeline_stage`, so a business that answered your email reads as untouched in
the pipeline; wiring that in means touching the scheduler's scan loop and was
left for its own change. There is no stage-change history, only `pipeline_at`.

**The lead audit** (`audit.py`), on any business with a website, only when the
operator clicks *Run checks* on its panel:

- `run_checks` runs, in parallel: a homepage scan (built with, analytics and ad
  pixels, chat/booking widgets, socials, title/description, structured data,
  phone viewport, footer year), Google PageSpeed v5 mobile scores and
  screenshot (the `google_api_key` setting lifts the unauthenticated quota),
  the SSL certificate, MX records → email provider, the Wayback Machine's
  first capture, and RDAP domain dates. One failing never stops the rest.
- `POST /api/businesses/<id>/audit` starts it on a background thread (single
  gunicorn worker; at most `_AUDIT_MAX_RUNNING` = 3 at once) and the page polls
  `GET`. Results are saved as JSON on `businesses.audit`. Tests set
  `AUDIT_INLINE` to run it in the request.
- `get_rating_context` compares the Google rating and review count with the
  operator's own leads of the same category and city (needs 3 peers).
- The one-click links (Meta Ad Library, Google Ads Transparency, Maps and
  competitors, ChatGPT/Perplexity, BuiltWith, and so on) are built in the
  browser (`auditLinkGroups`). Each operator adds their own under Settings →
  Lead audit (`GET/PUT /api/audit-links`, https only, up to 40).

---

## 6. Deploys and operations

- **Server:** GCP e2-micro, Debian 12, app in `~/shoutreach`, gunicorn with
  `--workers 1` (the scheduler runs in-process — never raise this), nginx, the
  systemd unit `shoutreach`, SQLite `outreach.db`.
- **Deploying = pushing to `master`.** GitHub Actions SSHes in and runs
  `git pull`, `pip install -r requirements.txt` and
  `sudo systemctl restart shoutreach`. `init_db()` runs on restart, so
  migrations hit the live database. CI runs no tests — run them first.
- **A green deploy run doesn't prove the app is up.** `systemctl restart`
  succeeds even if the app crashes a moment later. Check that
  `https://shoutreach.hexiv.co/login` returns **200** and `/api/users/me`
  returns **401**; a **502** means startup failed, most likely in `init_db`.
  `/static/*` needs a login (302), so it can't prove new code shipped — the
  run log's `git pull` line (`Updating <old>..<new>`) can.
- **Deploy SSH key:** ed25519. The public half is in the Cloud Console under
  **VM → Edit → SSH Keys** (username `sheham_shahid`); the private half exists
  only in the GitHub secret `SSH_PRIVATE_KEY`. On 2026-09-15 the old,
  hand-added key vanished from `~/.ssh/authorized_keys` and deploys failed with
  `ssh: unable to authenticate`. The cause is unconfirmed; it coincided with a
  browser-SSH session's temporary keys expiring. **Never add a deploy key by
  hand to `authorized_keys`.**
- **Back up before any risky migration:**
  `cp ~/shoutreach/outreach.db ~/outreach.db.bak-$(date +%F)`.
- **Users:** Settings → Users (admins only). Passwords need 12+ characters. An
  admin can reset someone else's password without knowing it; changing your
  own needs your current one. If the only admin is locked out, on the VM:
  `cd ~/shoutreach && source venv/bin/activate && python reset_password.py`.
- **The first admin** is created from the login page, and only while there
  are no users at all, using a setup token printed to the server log at boot.

---

## 7. Hard constraints — don't break these

- **WhatsApp is never sent automatically.** No unofficial WhatsApp library, no
  browser driving a WhatsApp session (not even a read-only number check), no
  scheduled or background send. "Sent" only means the operator opened the
  `wa.me` link, and it must stay correctable by hand.
- **WhatsApp follow-ups are infinite** until replied or paused. No
  auto-dormant cap.
- **The audit runs on click, one lead at a time.** Nothing audits leads in the
  background or in bulk, and nothing blocks sending on it.
- **Suppression spans every operator** (§3).
- **Don't push without asking** — it deploys.

---

## 8. Tests

No pytest. Each `tests/test_*.py` is a standalone script; exit code 0 means
pass.

```
for f in tests/test_*.py; do python "$f"; done
```

22 files. 21 pass; `tests/test_send_window.py` has been failing since
2026-09-22 and **not because of any change here** — it fails identically on a
clean checkout. Its two failures are a UTC-versus-local date boundary: the
operator's machine is UTC-2:30, so between about 21:30 and midnight local the
daily-cap count and the test disagree about which day "today" is. Worth
treating as a real bug in the email daily cap rather than a flaky test, since
the cap is a sending-safety control. Not investigated yet.

`tests/test_pipeline.py` covers stages (§5a): the built-ins and inventing your
own, one stage read from all three channels, the cadence stopping on set and
NOT restarting on clear, a terminal stage leaving `do_not_contact` alone, the
board's ordering, the messaged filter, the send log, widened search, the
stage in the import confirmation, and the per-operator walls.
`tests/test_wa_drafts.py` covers imported openers and follow-ups, the order
they're used in, the fallback to the template past the third, the rule that an
import never replaces a hand edit, the guarantee that a re-import can't
re-queue a lead already messaged, per-slot coverage counts, ownership, and the
`message_source` backfill. `tests/test_whatsapp.py` covers live
messages, edits and reset, version dealing and removal, countries, sent today,
opening versus confirming a send, marking not on WhatsApp and bulk move-off,
and landlines last; `tests/test_contacts_hub.py` covers Contacts, Unassigned,
deletion guards, the Dashboard, adding a whole list, and the audit.

**Browser smoke test.** There's no UI test in the repo, but the redesign was
checked by serving the app against a seeded throwaway database and driving
every page and tab with Playwright (installed locally for the worker),
watching for JS errors and failed API calls. Import `scheduler` and replace
`scheduler.start` with a no-op first, or the app starts sending. Replace
`audit.run_checks` with a canned result, and `window.open` with a recorder
(`page.add_init_script`), so the test never reaches the internet or loads
WhatsApp. On a phone-sized context (390×844, `is_mobile`, iPhone user
agent), replace `openWhatsAppChat` after load instead — a phone opens
`whatsapp://` in the current page, not `window.open` — and wrap the
assignment in `() => { ... }`, or Playwright calls the function it returns.
**Never import `app` without `DB_PATH` pointing somewhere disposable** —
importing runs `init_db()` against `./outreach.db`.

- **Create any user you fake a session for.** A fixture that sets
  `sess["user_id"] = 1` without creating user 1 breaks: rows get owner 0
  (invisible to everyone), and `worker_keys` has a real foreign key.
- **With two or more users, pass `owner_id` to every db write in a test**, or
  `_resolve_owner_id` raises — by design.
- **Never hit the live internet.** The audit's site scan is tested against a
  local `http.server` with PageSpeed faked; set `mx_valid` on import rows so
  the importer skips DNS.
- **The Windows console is cp1252.** Printing an em dash or emoji can raise
  `UnicodeEncodeError`; print ASCII-safe.
- **A check can pass without testing anything.** Read what new checks print,
  not just the exit code.

---

## 9. What's next

**Immediately:**

00. **Bespoke per-lead copy is live** (§5), and so are the three commits that
    rode along with it. Still to check by hand, in the app: *+ Add leads →
    Paste JSON* accepts a small batch and the result line reports per-slot
    coverage; a lead with imported copy shows **own copy** instead of a
    version letter on the Leads tab; its second follow-up can be edited and
    saved from To do; and — from the commits that shipped alongside — the
    phone layouts and the per-row WhatsApp button, which were written on
    09-16 and have never run against the live database. The **post-reply
    pipeline** is the agreed next piece of
    design, not yet started: a replied lead currently leaves every queue with
    nothing but a binary `replied` flag and free-text notes on its business,
    so there is no "conversations in progress" surface and no sales stage.
    Calling already has the model (`call_outcome_types`, `is_terminal`,
    `requires_date`, `call_log`, `.ics`); the open decision is whether to
    generalise that table to serve both channels or fork it. Keep the channel
    stage (`ready`/`due`/`waiting`/`replied`) separate from any sales stage —
    the queues read the former directly.
0. Both WhatsApp deploys are live and the app is up (`/login` 200,
   `/api/users/me` 401; run logs `Updating 4f7ea0f..be439b5` and
   `be439b5..9354245`). Still to check by hand, in the app: open a chat and
   answer "Did it send?" each way; mark a lead not on WhatsApp and move it off
   from Leads → Not on WhatsApp; each campaign's Templates tab shows one
   opening message (take `{{signal_detail}}` out of it); Run checks on a lead
   with a website returns scores. Leads wrongly marked sent before `9354245`
   can be put back with ⋯ → "Didn't actually send?" (or deleted — they were
   test leads). A Google API key (Settings → Lead audit) is optional.
1. Deployed and verified per §6 on 2026-09-16. Still to do by hand, in the
   app: WhatsApp → Campaigns shows "My first campaign"
   holding the existing leads and the operator's own templates (rename it);
   Calling → Leads still has the old call list minus WhatsApp leads; Contacts →
   Unassigned looks sensible.
2. A test scrape to each destination, with a campaign picked.
3. Create the cofounder's account and set up his laptop (§4), with the current
   `scraper_worker.py`.

**From the full audit** — `docs/audits/Full App Audit 2026-09-09.md` (kept
local until the operator committed it in `be439b5`; the repo is private); its
status block says what's done. The biggest open
items:

- Secrets stored in plaintext: SMTP passwords, AI keys, the Flask `SECRET_KEY`.
- `/api/calls/queue` performance: `limit=100000` four times, an N+1 loop, and
  no index on `sends(email_lead_id)`.
- The bounce scanner downloads whole messages with no cap — an out-of-memory
  risk on a 1 GB server.
- No first-run guidance for a new user.
- Activating a campaign has no confirmation, though it starts real sending.

**Still deliberately deferred** (reasons in the WhatsApp handover §6): renaming
the `/api/contacts/*` URLs (the worker posts to them), `.xlsx` import, a
WhatsApp number pre-check, auditing leads in bulk.

**Wanted later, not now — "the other operator is already talking to this
business."** Asked for on 2026-09-22, explicitly deferred. Today
`find_cross_owner_matches` is advisory and fires only at import: it names the
business, the channel and the date, never contact details (§3). Showing that
someone is *in conversation* — once the pipeline stage of §9's phase 2
exists — would mean surfacing one operator's live progress on another's
screen, which is a deliberate hole in the wall rather than an extension of
the existing notice. Worth doing, but decide first how much it may say: a
bare "someone else is in conversation with this business" leaks far less than
a stage and a date, and the two operators lead with different services, so
the answer isn't obvious. Don't let it arrive by accident as a side effect of
putting the pipeline on `businesses`.

**Noticed, not fixed:** the admin Database viewer's table list still names the
retired `contacts` table.

---

## 10. Where things are

- `db.py` — all SQL. Ownership helpers under `# ── Ownership`; WhatsApp under
  `# ── WhatsApp`.
- `app.py` — routes. `me()`, `require_owned`, `@owned`, `import_owner` and
  `worker_owner` are near the top; the worker's routes are under
  `# ── API: scrape worker`.
- `scheduler.py` — the single background thread: sends, reply and bounce
  checks.
- `scraper_worker.py`, `gmaps_email_scraper.py`, `email_validator.py` — the
  laptop worker.
- `static/js/tables.js` — shared frontend pieces: tabs (`setTab`/`onTab`),
  `createLeadTable` (every leads table), `chooseDialog` (replaces `prompt()`),
  pills, `fillPlaceholders`. Row ⋯ menus are `position: fixed`, placed by
  `toggleRowMenu` against the window (opening upward when there's no room
  below) and re-placed on scroll, closing once their button leaves the
  screen: inside a table's `overflow-x: auto` wrapper an absolute menu was
  clipped into a scrolling strip. Don't put one under a `transform`ed
  ancestor, which would break fixed positioning.
- **Phones (≤768px)** — all in the last `@media` block of `main.css` plus a
  few helpers in `tables.js`; the computer layout is unchanged.
  - *Rows, not tables:* a table with class `m-table` hides its headings and
    every cell except `td.check`, `td.m-card` and `td.m-keep`. `m-card` is a
    phone-only cell built with `mCard(name, ...lines)`; `createLeadTable`
    adds it from a `mobile(r)` config and shows "Select all on this page"
    above the table (inside it, Chrome widened the checkbox column).
  - *A lead opens full screen:* panels with class `sheet` (`wa-panel`,
    `wl-panel`, `cl-panel`, `el-panel`, `ct-detail`, and Calling's
    `#cq-work`) are hidden on a phone until `openSheet(id)` adds
    `sheet-open`; each render starts with `sheetBarHtml(...)` (← Back to
    list). Opening pushes a history entry, so the phone's back gesture closes
    it (`popstate` → `_dropSheet`, which runs the panel's own close). Only a
    tap opens a sheet — `openWaLead(id, true)`, `openCallLead(id, true)`,
    `openLeadPanel` — never a list picking its first lead as it loads. After
    Sent / Save & next, the next lead shows in the same sheet. `setTab` (on a
    real tab change) and `showSection` close any open sheet.
  - *Calling's dialler:* the lead card and script share `#cq-work`, which is
    `display: contents` on a computer (grid placement in the
    `min-width: 769px` block) and one scrolling sheet on a phone.
  `static/js/lead_panel.js` — the lead side panel,
  the country picker, opening WhatsApp, the audit section. One JS file per
  page: `contacts.js` (businesses, "Add all to…"), `email_leads.js` (Email →
  Leads and suppression), `calling.js`, `whatsapp.js`, `dashboard.js`.
- `audit.py` — the lead audit's automatic checks.
- `reset_password.py` — shell password reset.
- `tests/test_ownership.py`, `tests/test_whatsapp.py`,
  `tests/test_worker_api.py` — the best description of intended behaviour.
