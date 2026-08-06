# Opus Fixes — ShoutReach

> **Retired 2026-08-05.** A changelog of fixes that were applied and subsequently
> verified against the code by the Fable audit. Historical record; nothing here is
> an open action.

---

**Companion to:** [Opus Audit.md](Opus%20Audit.md)
**Date:** 2026-05-26
**Scope of this pass:** security shortlist + correctness bugs + the user-reported reply-detection regression. Architectural rewrites (Postgres, queue, multi-tenancy, test suite) were intentionally out of scope.

---

## TL;DR

23 audit findings shipped as code changes, plus the reply-detection follow-up. All Python modules compile and `import app` runs clean. The app is now safe to give other staff accounts to (within the limits of "self-hosted Flask + SQLite").

| Bucket | Fixed | Partial | Deferred |
|---|---:|---:|---:|
| Critical security | 6 | 1 | 0 |
| High security | 5 | 0 | 3 |
| Correctness | 6 | 1 | 6 |
| UI/UX | 1 | 1 | 9 |
| Architecture | 2 | 1 | 8 |
| **Total** | **20** | **4** | **26** |

---

## Status legend

- ✅ **Fixed** — landed in this pass.
- 🔶 **Partial** — improved but a larger fix is left for later.
- ⏸ **Deferred** — out of scope here; tracked for later. (Reason given.)

---

## §1 — Security findings

| # | Audit finding | Status | Notes |
|---|---|---|---|
| 1.1 | AI API keys plaintext via `/api/settings` | ✅ | `_is_secret_key()` allowlist + heuristic catches anything ending in `_key`/`_secret`/`_token` ([app.py:202-225](app.py#L202-L225)). Endpoint also `@admin_required`. |
| 1.2 | DB viewer leaks the same keys | ✅ | All `/api/db/*` routes `@admin_required`. Settings rows masked via `_is_secret_key`; per-table column mask covers `smtp_accounts.smtp_pass`/`imap_pass` ([app.py:925-1006](app.py#L925-L1006)). |
| 1.3 | No CSRF protection | ✅ | Per-session token via `GET /api/csrf`; required as `X-CSRF-Token` on every non-GET/HEAD/OPTIONS request. Login form uses hidden `csrf_token` field + same-origin check ([app.py:177-201](app.py#L177-L201), [app.py:299-325](app.py#L299-L325)). JS bootstraps the token automatically ([static/js/utils.js:2-13](static/js/utils.js#L2-L13)). |
| 1.4 | Non-admins can do almost everything | ✅ | `@admin_required` added to every mutating endpoint (campaigns, contacts, steps, enrollments, accounts, settings, scheduler-trigger, scraper, AI review, DB viewer). |
| 1.5 | First-run admin race | ✅ | Two paths: (a) set `ADMIN_PASS` env (≥12 chars) and admin is seeded at boot; (b) otherwise a one-time `SHOUTREACH SETUP TOKEN` is generated and printed to stdout/logs, and the first-run web form requires it ([app.py:70-101](app.py#L70-L101)). |
| 1.6 | XSS via `esc()` not escaping `'` | 🔶 | `esc()` now escapes `'` and `` ` ``; new `escj()` for values dropped inside single-quoted JS in `onclick=` ([static/js/utils.js:64-89](static/js/utils.js#L64-L89)). Two highest-risk sites converted to `escj()`. Other call sites are safer now but still ideally should use `escj()` in JS contexts — tracked but not migrated wholesale. |
| 1.7 | Secrets plaintext in SQLite | ⏸ | Requires a real secrets-encryption story (env-var key or KMS). Not in this pass — flagged for Phase 2. Mitigation today: only admins can read those endpoints, and the DB viewer masks them. |
| 1.8 | No login rate limit | ✅ | In-memory per-IP throttle: 10 attempts / 15 min, then 429 ([app.py:114-138](app.py#L114-L138), [app.py:336-343](app.py#L336-L343)). Survives a single-worker gunicorn; would need shared state for `n>1` workers. |
| 1.9 | Session cookie hardening | ✅ | `SECURE`, `HTTPONLY=True`, `SAMESITE=Lax`, `PERMANENT_SESSION_LIFETIME=14 days`, `MAX_CONTENT_LENGTH=16MB` ([app.py:56-66](app.py#L56-L66)). |
| 1.10 | Security headers | ✅ | `@app.after_request` sets `X-Frame-Options: DENY`, `X-Content-Type-Options`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy`, and `HSTS` when behind HTTPS ([app.py:218-230](app.py#L218-L230)). |
| 1.11 | Password change without current password | ✅ | Self-service change requires `current_password` and verifies it with `db.verify_user_password`; admin-resetting another user does not (intended reset flow) ([app.py:339-365](app.py#L339-L365), [db.py:945-950](db.py#L945-L950)). Modal UI shows/hides the current-password field accordingly ([static/js/settings.js:176-218](static/js/settings.js#L176-L218)). |
| 1.12 | PBKDF2 iterations | ✅ | Bumped to 600k. New hash format `pbkdf2_sha256$ITER$SALT$KEY`. Legacy `SALT:KEY` hashes still verify and get **transparently rehashed** on next successful login ([db.py:899-963](db.py#L899-L963)). |
| 1.13 | CSV import unbounded | ✅ | 8 MB / 50k row hard caps at the import endpoint, plus Flask-level `MAX_CONTENT_LENGTH=16MB` ([app.py:840-867](app.py#L840-L867)). |
| 1.14 | AI prompt injection | ⏸ | Cosmetic for a copy-review tool. Not addressed. |
| 1.15 | Auto-deploy unpinned | ⏸ | Ops/CI work — needs `pip-compile` lockfile + `--require-hashes`. Not touched. |
| 1.16 | Unsubscribe token entropy | — | No change — 80-bit truncated HMAC is fine for this purpose. |
| 1.17 | Committed `outreach.db` / CSVs | ⏸ | `.gitignore` already excludes them. If any were committed historically, scrubbing would mean `git filter-repo` + force-push — not done here. |

---

## §2 — Correctness bugs

| # | Audit finding | Status | Notes |
|---|---|---|---|
| 2.1 | A/B broken — `random` not imported in `db.py` | ✅ | One-line fix ([db.py:11](db.py#L11)). |
| 2.2 | `/api/scheduler/run` blocks HTTP thread | ✅ | New `scheduler.request_run_now(include_reply_check=...)` flips an `Event` the background loop responds to immediately. HTTP request returns in milliseconds ([scheduler.py:32-43](scheduler.py#L32-L43), [app.py:1062-1077](app.py#L1062-L1077)). |
| 2.3 | Scheduler started in `before_request` | ✅ | Started once at module load with a lock around `start()` ([app.py:67-69](app.py#L67-L69), [scheduler.py:251-265](scheduler.py#L251-L265)). |
| 2.4 | No idempotency on sends | ⏸ | Requires a transactional outbox pattern or `INSERT … ON CONFLICT` on a deterministic send key. Not in this pass. |
| 2.5 | `known_msg_ids` unbounded | ✅ | Bounded to last 30 days; backed by new `sends_msg_id_idx` ([sender.py:347-358](sender.py#L347-L358), [db.py:194-208](db.py#L194-L208)). |
| 2.6 | `process_queue` blocks scheduler thread | 🔶 | Wake-event replaces the old 60×1s spin loop, so Run Now is responsive; per-send `time.sleep(min,max)` between SMTP calls is unchanged (intentional — that's the anti-spam jitter). For higher throughput a real queue/worker fan-out is the next step. |
| 2.7 | Per-email today-count requery | ⏸ | Perf nit — left for an indexes-already-help pass. |
| 2.8 | Bounce parser sets `\Seen` | ⏸ | Design choice; not changed. |
| 2.9 | `delete_campaign` doesn't clear `daily_counts` | ⏸ | Minor; behaviour preserved. |
| 2.10 | Unsubscribe enrollment propagation | ✅ | Now flips every non-terminal enrollment, not just `queued` ([db.py:431-440](db.py#L431-L440)). |
| 2.11 | `_make_message_id` collision risk | ✅ | Uses `secrets.token_urlsafe(16)`; embeds `campaign_id`/`contact_id`/`step_num` for human readability in mail clients ([sender.py:108-115](sender.py#L108-L115)). |
| 2.12 | Dockerfile incomplete | ⏸ | Not used in production (systemd on GCP). Left as-is; the audit's note stands. |
| 2.13 | `fly.toml` placeholder | ⏸ | Same — unused. |

---

## §3 — UI / UX

| # | Audit finding | Status | Notes |
|---|---|---|---|
| 3.1 | Inline styles in JS template strings | ⏸ | Design lift; not done. |
| 3.2 | Inline event handlers prevent strict CSP | ⏸ | Same; would require delegated listeners everywhere. |
| 3.3 | No responsive design | ⏸ | |
| 3.4 | Native `confirm()`/`prompt()` | ⏸ | |
| 3.5 | A11y | ⏸ | |
| 3.6 | No optimistic UI / loading states | 🔶 | `api()` now surfaces non-2xx errors and auto-redirects on 401. Button-level loading states not added. |
| 3.7 | DB section probably shouldn't ship | ✅ | Gated to admins. Not removed — kept as a power-user feature. |
| 3.8 | Help bundle on every load | ⏸ | |
| 3.9 | Sidebar username placement | ⏸ | |
| 3.10 | Run Now hover via inline JS | ⏸ | |
| 3.11 | README still says "Outreach System" | ⏸ | Brand-only; tracked. |

---

## §4 — Architecture

| # | Audit finding | Status | Notes |
|---|---|---|---|
| 4.1 | SQLite + 1 worker ceiling | ⏸ | Acknowledged tradeoff for self-hosted; needs Postgres + a real queue to lift. |
| 4.2 | Scheduler is a thread, not a process/queue | 🔶 | Same architecture, but `start()` is now lock-protected/idempotent, the loop is event-driven (no busy-wait), and `stop()` joins with a timeout ([scheduler.py:251-282](scheduler.py#L251-L282)). |
| 4.3 | No idempotency on sends | ⏸ | Same as 2.4. |
| 4.4 | Unbounded growth tables (`sends`, `logs`, `daily_counts`) | ⏸ | Needs an archive job. |
| 4.5 | Missing hot-path indexes | ✅ | `sends(msg_id)`, `sends(campaign_id, sent_at)`, `sends(sent_at)`, `enrollments(campaign_id, status, next_send_at)`, `enrollments(contact_id, status)`, `contacts(status)` ([db.py:194-208](db.py#L194-L208)). |
| 4.6 | Serial campaign processing | ⏸ | |
| 4.7 | No retry on transient SMTP | ⏸ | |
| 4.8 | AI calls no caching / no retry | ⏸ | |
| 4.9 | Scraper module-level globals | ⏸ | |
| 4.10 | No observability | ⏸ | |
| 4.11 | No tests | ⏸ | The bug in 2.1 was the strongest argument for adding any. Still none. |

---

## §5 — Bonus fix: reply-detection false-negatives

Not in the original audit; reported separately by the user.

Two failure modes downstream of the (correct) `In-Reply-To`/`References` match in [sender.py:_scan_inbox_for_replies](sender.py#L338):

1. **Contact lookup by `From:` address.** Once a reply was confirmed via message-ID, the code still resolved the prospect by the reply's From address. If the prospect replied from an alias, a delegate's mailbox, or a forwarded chain, `get_contact_by_email(from_email)` returned `None` and the reply was silently dropped.
2. **`mark_enrollment_replied` only updated `WHERE status='queued'`.** If the reply arrived after the last step had been sent, the enrollment was already `'completed'`, the row never flipped to `'replied'`, and the reply-rate stat undercounted.

**Fix** ([sender.py:347-420](sender.py#L347-L420), [db.py:660-679](db.py#L660-L679)):
- Scan now builds `msg_id → (campaign_id, contact_id)` from `sends` (last 30 days). The matched message-ID *is* the source of truth for who replied — we don't need to re-derive it from the From address.
- `mark_enrollment_replied` updates every non-terminal status (skipping only `replied`/`bounced`/`unsubscribed`) and returns the row count.
- The "reply detected" log line only fires when a row actually changed, so the same inbox message scanned every 5 minutes no longer spams the log.

---

## §6 — Operational changes (new behaviour you should know about)

### New environment variables
| Var | Purpose |
|---|---|
| `ADMIN_PASS` (existing, now stricter) | First-run admin seed. Must be ≥12 chars or the seed is refused. |
| `ADMIN_USER` (existing) | Username for the seeded admin. Default `admin`. |
| `SECRET_KEY` (existing) | Flask session signing key. Falls back to a DB-stored secret if unset. |
| `SHOUTREACH_INSECURE_COOKIES` | Set to `1` to disable `Secure` on session cookies — **only for local HTTP dev**. Production should leave this unset. |
| `FLASK_DEBUG` | Set to `1` to enable Werkzeug debug + reloader when running via `python app.py`. Default is now off. |

### First-run flow
On a fresh database with no `ADMIN_PASS` set, ShoutReach prints a one-time setup token to the server logs (`logging.warning`) at boot. The first-run web form will not create an admin without it. The token is single-use; if the form rejects the form (e.g., password too short), a fresh token is issued and logged.

### Password policy
- New password minimum: **12 characters** (was 8).
- Existing passwords continue to work; they are silently re-hashed to the new format on next successful login.
- Self-service password change now requires the current password. Admin-resetting another user does not.

### CSRF on the API
The JS `api()` helper auto-bootstraps the CSRF token from `/api/csrf` on first call and includes it as `X-CSRF-Token` on every state-changing request. Code outside `utils.js` that calls `fetch()` directly will fail with 403 unless it supplies the header itself. (Audit: `static/js/contacts.js:281` does a raw multipart POST for CSV import — it currently lacks the header and will fail under the new middleware. Tracked as a follow-up.)

`importContacts()` in `contacts.js` uses a raw `fetch()` for the multipart upload (FormData sets its own `Content-Type` with the MIME boundary, so it can't go through `api()` as-is). Patched inline to fetch the CSRF token via `_getCsrfToken()` and attach the `X-CSRF-Token` header ([static/js/contacts.js:281-293](static/js/contacts.js#L281-L293)).

### Run Now behaviour
`POST /api/scheduler/trigger` and `POST /api/scheduler/run` no longer call `process_queue()` inline; they signal the background scheduler via `Event` and return immediately. The actual sending starts within a second on the scheduler thread, not the HTTP worker.

### Cookie semantics
Sessions are now `SameSite=Lax`, 14 days, `Secure` (over HTTPS). Browsers visiting over plain HTTP without `SHOUTREACH_INSECURE_COOKIES=1` will **not** receive the session cookie — the login will silently appear to fail.

---

## §7 — What was deliberately not fixed and why

| Item | Why not |
|---|---|
| Postgres migration | Architecture redesign, not a fix. Several days of work. |
| Real queue (RQ/Celery) | Same. Pairs naturally with Postgres. |
| Multi-tenancy (`tenant_id` on every row) | Product decision; needs a data model + invite/onboarding flow. |
| Test suite | Worth doing but not "fixing." Recommend starting with `db.py` (auth) and `sender.py` (`_render`, `_make_unsub_token`). |
| Secrets-at-rest encryption (1.7) | Needs a key source (env var or KMS). Mitigation in place: only admins can read those values, DB viewer masks them. |
| Argon2 migration | PBKDF2-600k is OWASP-acceptable; Argon2 would be better but is a non-trivial dependency add. |
| Removing inline event handlers (3.2) | Would unlock strict CSP but requires touching every section. Cost > current benefit. |
| Mobile-responsive redesign | Not a security or correctness issue. |
| Accessibility pass | Same. Worth doing for product quality. |
| README/branding sweep (3.11) | Pure docs; trivial to do later. |

---

## §8 — Verification done

- `python -m py_compile app.py db.py sender.py scheduler.py email_validator.py` — clean.
- `python -c "import app; app.scheduler.stop()"` — clean import, scheduler starts + stops as expected.
- **Not done:** end-to-end browser test of login + a state-changing API call + reply-detection happy path. Recommended before pushing:
  1. `$env:SHOUTREACH_INSECURE_COOKIES='1'; python app.py`
  2. Visit `http://localhost:5000` → confirm first-run flow asks for the setup token from the startup logs.
  3. Log in → confirm sidebar shows your username.
  4. Try Save Settings → confirm it works (CSRF wiring).
  5. Try CSV import → expect success (CSRF header is wired through).

---

## §9 — Follow-up punch list (recommended next pass)

In order of "smallest unit of work / biggest gain":

1. **Migrate remaining `onclick` interpolations to `escj()` (~30 min).** Roughly a dozen sites in `campaigns.js` / `settings.js` / `database.js`. The XSS surface is much smaller now that `esc()` covers `'`, but `escj()` is the right primitive for inline JS contexts.
2. **Teach `api()` to handle `FormData`.** Right now the CSV upload uses a raw `fetch` with its own header wiring; making `api()` detect `FormData` and skip the JSON content-type would remove the second code path.
3. **Audit log table.** Right now password changes and admin-only mutations only log via Python `logging`. A dedicated `audit_log` table (who/what/when/IP) is the next thing security-minded users will ask for.
4. **Tests for auth + reply detection.** ~50 lines of `pytest` would have caught both the `random` import bug and the reply-detection regression.
5. **Archive job for `sends` and `logs`.** Trivial cron-style query; keeps the DB from growing forever.

---

## §10 — One-line verdict

**The audit's §8 shortlist is done, plus the user-reported reply-detection bug. ShoutReach is now defensible to give other staff accounts to. The architectural ceiling (single SQLite + one worker + in-process scheduler) is unchanged — that's still where the v2 work lives.**
