# ShoutReach Handover

**Last updated:** 2026-09-16 · **Branch:** `master` · **Live:** https://shoutreach.hexiv.co

Read this before touching code. It's written for a session with no memory of
how the app got here. Where it and the code disagree, trust the code — and
fix this file.

---

## 0. Read this first

- **Not everything is deployed.** Everything after `076b20c` is committed but
  **not pushed** (`git log origin/master..HEAD` lists it) — most importantly
  `bfe08c7`, each account getting its own scrape worker. The operator wanted to
  run a test scrape first. Pushing deploys it (§6) and moves the old shared
  worker key onto the founding admin account, so the worker already running on
  their laptop keeps working.
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
| `bfe08c7` | 09-15 | Each account gets its own scrape worker | **No — not pushed** |
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

- **Email** (sidebar: *Contacts*, *Campaigns*) — multi-step sequences, A/B
  variants per step, rotation across sending accounts, IMAP reply and bounce
  detection, HMAC-signed unsubscribe links. Contacts are `email_leads` rows.
- **Calling** (sidebar: *Cold Calling*) — a call queue by bucket, call
  campaigns, operator-defined outcomes, a per-operator call script, `.ics`
  invites for booked meetings.
- **WhatsApp** — import → background "booking gap" check on the clinic's
  website → the operator confirms the signal → a batch-drafted opener → the
  operator taps *Open in WhatsApp* and sends it themselves → follow-ups forever
  until replied or paused. Per-operator templates with up to four A/B versions.
  Design history: `docs/WhatsApp Module Handover.md`.
- **Lead Scraper** — Google Maps, run by a worker on an operator's own laptop
  (the server has no screen to show CAPTCHAs on). Each scrape targets Email or
  WhatsApp.
- **Settings** — admins: email accounts, sending rules, AI keys, users.
  Everyone: their own scrape worker key.

The sidebar still says *Contacts* and *Cold Calling*, and `/api/contacts/*`
serves email leads. Renaming was deliberately deferred.

---

## 3. The multi-operator model

Two operators — the owner and his cofounder — share one install. They share
the sending infrastructure. They do **not** share leads.

**Walled — every row belongs to one operator:** `businesses`, `email_leads`
(owner copied onto the row, because the per-owner unique email index needs
it), `campaigns`, `call_campaigns`, `scrape_jobs`, `call_scripts`,
`call_outcome_types` (owner 0 = built-in and shared), `worker_keys`.
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
  body are not** — filter them with `db._own_business_ids` or
  `require_owned(...)`. That gap was a real, exploitable hole (fixed in
  `61ae78f`).
- **Refuse another operator's row with 404, not 403**, so ids can't be probed.
- **These deliberately span every operator — never add an owner filter:**
  - `unsubscribe_contact`, `mark_bounced`, `increment_soft_bounce`. They use
    `IN (SELECT …)` because an address can exist once per operator, and an
    opt-out binds everyone sending from the shared domain.
  - The send loop (`get_campaigns(all_owners=True)`) and `get_due_enrollments`.
  - `terminal_outcome_keys` — it's matched against `call_status` values already
    written to rows.
  - `get_wa_leads_pending_signal` and `reap_stale_scrape_jobs`.
- **Overlap between operators is advisory only** (`find_cross_owner_matches`).
  The import goes ahead; the notice shows business name, channel and date,
  never contact details; nothing is merged. Matching is heuristic
  (phone / domain / email / name + locality).
- **A user who still owns rows can't be deleted** (`delete_user` refuses).
- **Per-operator settings are suffixed keys** — `wa_template_gap:<uid>`,
  `wa_followup_days:<uid>` and so on. **Message copy never falls back to
  another operator's**: the two lead with different services.

`tests/test_ownership.py` (16 sections) is the most precise statement of all
of this.

---

## 4. Scraper and workers

### Per-account workers — `bfe08c7`, not yet deployed

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

**Until it's pushed, production still has one shared key, and any running
worker takes the next scrape no matter who started it.**

### Destinations — `076b20c`, live

A scrape targets `email` or `whatsapp` (`scrape_jobs.destination`, plus a
required `country` — `AE` or `QA` — for WhatsApp). The **server** decides
where rows land, from the job, and only for worker requests: a CSV imported by
hand through Contacts still goes to Contacts. A WhatsApp scrape drops any
emails rather than filing them as email leads. The updated worker skips the
email search and the per-site delay for WhatsApp scrapes — the slow part — and
leaves sites it didn't search unlabelled rather than marking them "no email".

**Adding leads you already have to WhatsApp:** WhatsApp → *+ Add leads* →
*From my leads* (`POST /api/wa/add-existing`). Anything already on another
channel is held for confirmation. Leads with no phone, numbers already ruled
out as not on WhatsApp, and opted-out businesses are counted and reported
rather than silently dropped.

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

Do this after `bfe08c7` is live:

1. Create his account: Settings → Users, **Admin off**.
2. On his laptop: install Python and Chrome. Copy `scraper_worker.py`,
   `gmaps_email_scraper.py`, `email_validator.py` and
   `requirements-worker.txt` into one folder. Run
   `pip install -r requirements-worker.txt`, then
   `playwright install chromium`.
3. He signs in on his laptop and copies **his own** worker key.
4. Start the worker once with the server and key. Optionally add a `.bat`
   shortcut to his Startup folder (`shell:startup`) so it launches at login.

**Worker updates are copied to him by hand.** When any of those four files
change, send him the new copies — and remind the operator that his copy won't
update itself. This was chosen over cloning the repo onto his laptop (it would
put the whole private codebase there) and over the worker updating itself (his
laptop would run whatever the server sent it).

**Gotcha:** the worker keeps a CSV per machine named `<city>_<niche>.csv` and
skips businesses already listed in it, so an interrupted run can resume.
Scraping the same niche and city again on the same machine skips what that
CSV already holds.

---

## 5. WhatsApp additions since the module handover

- **Templates and the follow-up interval are per operator.** A new operator
  starts from the factory copy. The pre-multi-user shared copy was moved to
  the founding admin by `_migrate_wa_settings`.
- **A/B versions:** each template holds 1–4 versions, labelled A–D
  (`WA_ARM_LABELS`). New leads are dealt out round-robin *per signal type*,
  not randomly — on small batches, random assignment routinely puts every lead
  on one side. A lead keeps its version through its follow-ups
  (`_wa_arm_position`).
- `wa_leads.template_variant` holds the version label (`''` when a template
  has only one). `wa_leads.paraphrased` and `wa_log.paraphrased` record
  whether the AI reworded it — kept separate so a rewrite isn't credited to a
  version.
- **Reply rates** come from `get_wa_variant_stats`, counted per **lead** rather
  than per message, and split by paraphrased. Nothing declares a winner; the
  operator decides.

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
- **Signal detection stays lean:** one plain HTTP fetch per lead, no per-lead
  agent loops or web searches. If a review-based signal is ever added, it must
  quote verbatim text or say nothing.
- **Suppression spans every operator** (§3).
- **Don't push without asking** — it deploys.

---

## 8. Tests

No pytest. Each `tests/test_*.py` is a standalone script; exit code 0 means
pass.

```
for f in tests/test_*.py; do python "$f"; done
```

19 files, all passing as of `bfe08c7`.

- **Create any user you fake a session for.** A fixture that sets
  `sess["user_id"] = 1` without creating user 1 breaks: rows get owner 0
  (invisible to everyone), and `worker_keys` has a real foreign key.
- **With two or more users, pass `owner_id` to every db write in a test**, or
  `_resolve_owner_id` raises — by design.
- **Never hit the live internet.** Signal detection is tested against a local
  `http.server`; set `mx_valid` on import rows so the importer skips DNS.
- **The Windows console is cp1252.** Printing an em dash or emoji can raise
  `UnicodeEncodeError`; print ASCII-safe.
- **A check can pass without testing anything.** Read what new checks print,
  not just the exit code.

---

## 9. What's next

**Immediately:**

1. The operator's test scrape — WhatsApp destination, about 10 results.
   Confirm leads land in WhatsApp with correctly formatted UAE or Qatar numbers.
2. Push `bfe08c7` and verify it per §6.
3. Create the cofounder's account and set up his laptop (§4).

**From the full audit** — `docs/audits/Full App Audit 2026-09-09.md`, local and
untracked on purpose; its status block says what's done. The biggest open
items:

- Secrets stored in plaintext: SMTP passwords, AI keys, the Flask `SECRET_KEY`.
- `/api/calls/queue` performance: `limit=100000` four times, an N+1 loop, and
  no index on `sends(email_lead_id)`.
- The bounce scanner downloads whole messages with no cap — an out-of-memory
  risk on a 1 GB server.
- No first-run guidance for a new user.
- Activating a campaign has no confirmation, though it starts real sending.

**Still deliberately deferred** (reasons in the WhatsApp handover §6): renaming
Contacts / Cold Calling, `.xlsx` import, a rendered-browser worker for
"unclear" signals, a WhatsApp number pre-check.

---

## 10. Where things are

- `db.py` — all SQL. Ownership helpers under `# ── Ownership`; WhatsApp under
  `# ── WhatsApp`.
- `app.py` — routes. `me()`, `require_owned`, `@owned`, `import_owner` and
  `worker_owner` are near the top; the worker's routes are under
  `# ── API: scrape worker`.
- `scheduler.py` — the single background thread: sends, reply and bounce
  checks, the WhatsApp signal scan.
- `scraper_worker.py`, `gmaps_email_scraper.py`, `email_validator.py` — the
  laptop worker.
- `wa_signal.py` — booking-gap detection.
- `reset_password.py` — shell password reset.
- `tests/test_ownership.py`, `tests/test_whatsapp.py`,
  `tests/test_worker_api.py` — the best description of intended behaviour.
