# Fable Deep-Dive — Progress Checkpoint

> **Retired 2026-08-05.** A session checkpoint file that declares itself complete
> at the Status line below. It was scaffolding for writing the audit and study
> guide, both of which exist. Kept rather than deleted only because it is a useful
> record of what was examined; nothing here is an open action.

---

> Purpose: if this session is cut off, a fresh session reads this file first and resumes exactly where work stopped.
> Task: (1) fresh audit of current code verifying Opus Audit fixes + new findings → `Fable Audit.md`;
> (2) beginner-friendly study guide explaining every concept & architectural decision for interview prep → `Fable Study Guide.md`.
> User decisions: two separate files; full scope (core app + templates + scraper + deployment); committed data (outreach.db, WAL, CSVs) = FLAG ONLY, do not modify repo.
> Assume user has very basic coding knowledge — explain concepts from the ground up, framed as interview talking points.

## Status: COMPLETE — both docs written (Fable Study Guide.md + Fable Audit.md). Safe to delete this progress file.

### Phase checklist
- [x] Decisions gathered from user
- [x] Read Opus Audit.md + Opus Fixes.md (map of prior findings/fixes)
- [x] Read app.py
- [x] Read db.py
- [x] Read sender.py
- [x] Read scheduler.py
- [x] Read email_validator.py
- [x] Read gmaps_email_scraper.py
- [x] Read frontend (utils.js verified escj/api; onclick sites all safe; contacts.js deleted-toggle exists)
- [x] Read Dockerfile, fly.toml, requirements.txt, .github/deploy.yml, .gitignore, memory/project_outreach.md
- [x] Verified: DB/CSV/cookies NEVER committed to git (history clean). shoutreach-internal.html + ssh-linux-reference.html ARE tracked (server username/paths/layout disclosure if repo public; no live secrets).
- [x] Verified on Python 3.13.7: smtplib.SMTP_SSL/starttls + imaplib.IMAP4_SSL default context = CERT_NONE, check_hostname=False → NEW HIGH finding (TLS MITM on mail credentials). Fix: pass ssl.create_default_context().
- [ ] Write Fable Audit.md (verify prior fixes → new findings → data flags)
- [ ] Write Fable Study Guide.md (section by section; each section complete when written)
- [ ] Final summary to user

**Additional final notes for

### Notes so far
- Repo root contains committed `outreach.db` (184KB), `outreach.db-wal` (2.1MB!), `outreach.db-shm`, and two CSVs of scraped dentist contacts — privacy/portfolio concern, flag in audit.
- Last commit: "Security hardening pass + reply detection fix + audit docs" — Opus fixes were applied; audit findings must be verified against CURRENT code, not assumed open.
- README describes: Flask app, SQLite, scheduler thread, SMTP send + IMAP reply/bounce detection, sequences, warmup guidance, unsubscribe + List-Unsubscribe.

**Opus-fix verification (against current code) — CONFIRMED APPLIED:**
CSRF hook (app.py:202), admin gates on mutating routes, secret masking via `_is_secret_key` (app.py:397), setup-token first-run (app.py:86), login rate limiter (app.py:127), session cookie hardening (app.py:64), security headers (app.py:218), PBKDF2-600k + legacy rehash (db.py:916+), `import random` fix (db.py:10), indexes (db.py:200), run-now via Event (scheduler.py:35), stop() joins, reply-detection via msg_id map (sender.py:339+), unsubscribe propagation (db.py:440), MAX_CONTENT_LENGTH+CSV caps, msg-id secrets.token_urlsafe. DB viewer table list no longer includes smtp_accounts/users.

**NEW findings (draft for Fable Audit.md):**
1. **One-Click unsubscribe broken**: sender.py:278 sets `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058) but app.py:369 `/unsubscribe/<token>` route only accepts GET → Gmail/Yahoo POST gets 405. Deliverability + compliance issue. (HIGH)
2. **Bounce scanner marks ALL unseen mail read**: sender.py:526 `M.fetch(num, "(RFC822)")` on non-readonly SELECT implicitly sets \Seen on every unseen message in last 7 days, not just bounces (BODY[] side effect). Use BODY.PEEK[]. (MEDIUM-HIGH if shared inbox)
3. **Rate-limit bypass via X-Forwarded-For**: app.py:121 `_client_ip()` trusts first XFF value; attacker rotating XFF headers (esp. if nginx appends rather than overwrites) bypasses login throttle. (MEDIUM)
4. **Excel/CSV formula injection**: campaign xlsx export (app.py:900) and db CSV export write contact-controlled strings (scraped/imported names/companies); openpyxl treats "=..." as formulas; CSV opens in Excel evaluate =/+/-/@. (MEDIUM)
5. **JSON import path uncapped rows**: app.py:1050 `rows` from JSON body has no _CSV_MAX_ROWS check (only multipart does); 16MB JSON of rows → synchronous per-row DNS MX check in request thread. (MEDIUM)
6. **Password min inconsistency**: admin create user = 8 chars (app.py:552) vs change/first-run = 12. (LOW)
7. **Logout CSRF still possible**: GET /logout, CSRF-exempt. (LOW)
8. **Migration bug**: db.py:169 contacts_new rebuild omits mx_valid column → data loss + column dropped on legacy-DB upgrade path. Also migration loop still swallows all exceptions. (LOW)
9. **delete_contact soft-deletes but delete_contacts (bulk) hard-deletes** — inconsistent; also get_contacts() doesn't filter status='deleted' (check frontend). (LOW)
10. **Failed sends retry forever**: scheduler.py — non-bounce send failure leaves enrollment due → retried every cycle, no backoff/cap. (MEDIUM)
11. **Multi-worker foot-gun**: scheduler.start() at module import — gunicorn workers>1 → N schedulers → double sends. Only documented by convention (workers 1). (NOTE)
12. Successful logins count toward the login rate limit window (app.py:135 appends before auth outcome known). (NIT)
13. smtplib/imaplib default TLS cert verification depends on Python version; no explicit ssl.create_default_context(). (NOTE — verify claim before writing)
14. `with get_db() as conn` = transaction scope, NOT close; new connection per call; rely on GC to close. Concept to explain; fine at this scale. (NOTE)
15. Deferred-by-Opus items still open (confirm in audit): secrets plaintext in SQLite, prompt injection, deploy pipeline unpinned, no tests, no outbox idempotency, serial campaigns, Dockerfile broken/fly.toml placeholder, XSS escj() migration incomplete (check JS), inline handlers/CSP.

### Resume instructions for next session
1. Read this file.
2. Continue at first unchecked item.
3. Findings accumulate in "Notes so far" until docs are written; once a doc section is written it is durable — do not rewrite completed sections.
