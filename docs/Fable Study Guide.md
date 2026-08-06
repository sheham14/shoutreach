# ShoutReach — The Complete Study Guide

**For:** the developer who built this and wants to explain it fluently in interviews and technical conversations.
**Assumes:** basic coding knowledge. Every concept is explained from the ground up.
**Companion file:** [Fable Audit.md](Fable%20Audit.md) — the honest list of flaws and how to fix them.

> How to use this: read it top to bottom once to build the mental model, then come back to the "Interview questions" section (near the end) to rehearse. When a term is introduced in **bold**, that's a concept an interviewer might ask you to define. If you can explain the bolded terms in your own words, you're ready.

---

## Table of contents

1. [The 30-second pitch](#1-the-30-second-pitch)
2. [What problem does it actually solve?](#2-what-problem-does-it-actually-solve)
3. [The big picture: how the pieces fit](#3-the-big-picture-how-the-pieces-fit)
4. [The technology stack, and why each piece was chosen](#4-the-technology-stack-and-why-each-piece-was-chosen)
5. [The request lifecycle: what happens when you click a button](#5-the-request-lifecycle-what-happens-when-you-click-a-button)
6. [Module by module](#6-module-by-module)
7. [The data model (database tables)](#7-the-data-model-database-tables)
8. [The scheduler: the heart of the system](#8-the-scheduler-the-heart-of-the-system)
9. [The email engine and deliverability craft](#9-the-email-engine-and-deliverability-craft)
10. [Security concepts, explained](#10-security-concepts-explained)
11. [The Google Maps scraper](#11-the-google-maps-scraper)
12. [Deployment and operations](#12-deployment-and-operations)
13. [The architectural decisions, defended](#13-the-architectural-decisions-defended)
14. [Known limitations (say these before they ask)](#14-known-limitations-say-these-before-they-ask)
15. [Interview questions and strong answers](#15-interview-questions-and-strong-answers)
16. [Glossary](#16-glossary)

---

## 1. The 30-second pitch

> "ShoutReach is a self-hosted cold-email outreach platform. You import a list of business contacts, write a multi-step email sequence, and it drips those emails out automatically on a human-like schedule while respecting deliverability best practices — daily caps, business-hours-only sending, random delays, automatic pausing if too many emails bounce, and automatic stopping the moment a prospect replies. It's a Flask + SQLite app, deployed on a single Google Cloud VM. I built the whole thing solo — the web app, the background scheduler, the SMTP/IMAP email engine, an AI copy-review feature, A/B testing, and even a Google Maps lead scraper that feeds contacts into it."

That paragraph hits: **what it is**, **who it's for**, **the hard parts**, and **that you built it end-to-end**. Memorize the shape of it, not the words.

---

## 2. What problem does it actually solve?

**Cold email** is sending email to people who haven't heard from you, to start a business conversation (sales, partnerships, recruiting). It's legal in most places if you follow rules (more on that later), but it's *technically* hard to do well because email providers (Gmail, Outlook) aggressively fight spam.

The naive approach — open Gmail, paste 200 addresses, hit send — fails badly:

- Gmail flags the burst as spam and may suspend your account.
- Your domain gets a bad **sender reputation**, so even future legitimate email lands in spam.
- You have no idea who opened, replied, or bounced.
- You can't follow up automatically 3 days later.

Commercial tools that solve this (Instantly, Lemlist, Smartlead) cost $30–$100+/month and hold your data. ShoutReach is the **self-hosted** ("run it on your own server, own your data, no subscription") alternative.

The core value is not "send email" — Python can send email in 5 lines. The value is **sending email in a way that doesn't get you blacklisted, and orchestrating multi-day follow-up sequences across many prospects without human babysitting.** That orchestration is the interesting engineering.

---

## 3. The big picture: how the pieces fit

There are two "engines" running at once inside one program:

```
                    ┌─────────────────────────────────────────────┐
                    │              ONE PYTHON PROCESS               │
                    │                                               │
   Your browser ───▶│  [1] Flask web app (app.py)                   │
   (dashboard)      │      - serves the dashboard HTML              │
                    │      - handles API calls (create campaign,    │
                    │        import contacts, save settings...)     │
                    │      - talks to the database                  │
                    │                                               │
                    │  [2] Background scheduler thread (scheduler.py)│
                    │      - wakes up every ~60 seconds             │
                    │      - "who is due for an email right now?"   │
                    │      - sends via sender.py                     │
                    │      - every 5 min: checks inbox for replies  │
                    │        and bounces via IMAP                    │
                    │                                               │
                    │           both talk to ▼                      │
                    │  [3] SQLite database (outreach.db, via db.py) │
                    └─────────────────────────────────────────────┘
                                        │
                    external services ▼ │ ▲
             SMTP servers (send mail) ──┘ └── IMAP servers (read replies/bounces)
             AI APIs (Claude/Gemini/OpenAI)   DNS (MX record checks)
```

The key insight to say out loud in an interview: **the web app and the sender run in the same process but on different threads.** The web app is *reactive* (it responds to your clicks). The scheduler is *proactive* (it acts on a timer, whether or not anyone is looking at the dashboard). This split is the central design decision, and it has consequences we'll dig into.

---

## 4. The technology stack, and why each piece was chosen

Interviewers love "why did you choose X?" Here's a defensible answer for each. The honest theme: **this is a solo-built, single-tenant tool, so every choice optimizes for simplicity and zero moving parts over scalability.**

### Python + Flask (the web framework)
**Flask** is a **micro-framework**: it gives you URL routing, request/response handling, and templating, and stays out of your way otherwise. Compare to **Django**, which is "batteries included" (built-in admin, ORM, auth, migrations) but heavier.

- **Why Flask:** the app is small and you wanted full control over the database layer and the sending logic. Django's ORM and migration system would be overhead you don't need for ~10 tables. Flask let you write plain SQL (which you understand exactly) instead of learning an ORM's abstractions.
- **What a "framework" even is:** code someone else wrote that handles the boring, universal parts of a web app (parsing HTTP requests, matching URLs to functions) so you only write the parts unique to your app.

### SQLite (the database)
**SQLite** is a database that lives in a single file (`outreach.db`) with no separate server process. Compare to **PostgreSQL/MySQL**, which run as their own always-on server that your app connects to over a network.

- **Why SQLite:** zero setup, zero ops, the entire database is one file you can copy/back up. Perfect for a single-server, single-writer app. "The database is a file" is a genuine feature for self-hosting.
- **The tradeoff (know this cold):** SQLite allows only **one writer at a time**. That's fine here because there's one app process. It becomes a ceiling the moment you want multiple app servers. That's the #1 thing that would need to change to turn this into a multi-customer SaaS.

### Vanilla JavaScript (the frontend)
No React, no Vue, no build step. The dashboard is HTML templates + plain JavaScript files that call the API with `fetch()` and build HTML strings.

- **Why:** no build tooling to maintain, no `npm install`, the files you write are the files that run. For a dashboard this size, a framework would be more machinery than the problem needs.
- **The tradeoff:** building HTML by string concatenation (`` `<button onclick="...">` ``) is error-prone and was the source of the **XSS** risk (explained in the security section). A framework like React escapes output for you automatically.

### Gunicorn (the production web server)
Flask's built-in server (`app.run()`) is for development only — it's single-threaded and not hardened. **Gunicorn** is a production **WSGI server**.

- **WSGI** ("Web Server Gateway Interface") is the standard Python contract between a web server and a Python web app. Gunicorn speaks HTTP to the world and WSGI to your Flask app. Think of it as the professional receptionist that Flask plugs into.
- Run with `--workers 1` here — deliberately, because more workers would mean more scheduler threads (see the scheduler section for why that's dangerous).

### Nginx (the reverse proxy) + a GCP VM
**Nginx** sits in front of Gunicorn. A **reverse proxy** is a server that receives requests from the internet and forwards them to your app running privately on `localhost`. It handles HTTPS/TLS termination, and shields the app from the raw internet.

- Deployed on a **Google Cloud e2-medium VM** (a small always-on Linux virtual machine), managed as a **systemd service** (Linux's standard way to keep a program running and restart it if it crashes).

### The mental model of the whole stack
```
Internet → Nginx (HTTPS, port 443) → Gunicorn (port 8000) → Flask app → SQLite file
```
Each layer has one job. If someone asks "walk me through what happens when a user loads the dashboard," you trace that line left to right.

---

## 5. The request lifecycle: what happens when you click a button

Let's trace "user clicks **Pause campaign**." This shows how the whole web layer works and touches most of the important concepts.

1. **Browser** runs `pauseCampaign(5)` (JavaScript). It calls the helper `api('/api/campaigns/5/pause', 'POST')`.

2. That helper (`static/js/utils.js`) attaches two things automatically:
   - The **session cookie** (the browser sends it on every request to this site).
   - An **`X-CSRF-Token`** header, which it fetched once from `/api/csrf`. (Why? See CSRF below.)

3. The request hits **Nginx**, which forwards it to **Gunicorn**, which hands it to **Flask**.

4. Flask runs **`before_request` hooks** in order — these are functions that run *before every request*:
   - `_require_login`: "is there a `user_id` in the session? No → 401/redirect to login." (This is **authentication** — proving who you are.)
   - `_check_csrf`: "for any non-GET request, does the `X-CSRF-Token` header match the token stored in this user's session? No → 403." (This is **CSRF protection**.)

5. Flask matches the URL `/api/campaigns/5/pause` to the `api_pause` function. That function has an **`@admin_required`** decorator — a wrapper that checks `session["is_admin"]` and returns 403 if not. (This is **authorization** — checking what you're allowed to do.)

6. `api_pause` calls `db.update_campaign(5, status="paused")`, which runs a parameterized SQL `UPDATE`.

7. It returns `jsonify({"ok": True})` — a JSON response.

8. Before the response leaves, an **`after_request` hook** (`_security_headers`) adds security headers (`X-Frame-Options`, etc.).

9. The browser's JavaScript gets `{ok: true}`, shows a toast, and re-fetches the campaign list to update the screen.

**Authentication vs authorization** is a classic interview distinction:
- *Authentication* = "who are you?" (login, the session cookie).
- *Authorization* = "are you allowed to do this?" (the `@admin_required` check).

You have both, and they're separate layers. Good.

---

## 6. Module by module

The codebase is split by **separation of concerns** — each file has one responsibility. This is a genuine strength; say so.

### `app.py` — the web layer (~1400 lines)
Every URL the browser can hit is defined here as a **route** (`@app.route(...)`). It's the only file that knows about HTTP. It does no email sending and owns no SQL of its own beyond a few viewer queries — it delegates storage to `db.py` and sending to `sender.py`. It also holds cross-cutting web concerns: login/session handling, CSRF, the rate limiter, security headers, and the AI-review HTTP calls.

### `db.py` — the storage layer (~1040 lines)
Everything about the database: the schema (`init_db`), and one function per operation (`create_campaign`, `get_due_enrollments`, `mark_bounced`...). The rest of the app never writes SQL directly; it calls these functions. This is a hand-rolled **data access layer** (a DAL) — the same idea an ORM automates, but written by hand so you control every query. Also owns password hashing and the user table.

### `sender.py` — the email engine (~580 lines)
Turns "send step 2 to this contact" into an actual email on the wire. Renders templates (`{{first_name}}` → "John"), builds the **MIME** message (text + HTML parts), talks **SMTP** to send, and talks **IMAP** to read the inbox for replies and bounces. All the deliverability craft lives here.

### `scheduler.py` — the background engine (~280 lines)
The timer-driven loop. Every ~60s it asks "who's due?" and sends; every ~5 min it checks for replies and bounces. It enforces the gates (business hours, daily caps, bounce circuit-breaker). This is the "proactive" engine.

### `email_validator.py` — MX validation (~50 lines)
One job: given an email address, does its domain actually accept mail? It does a **DNS MX-record lookup** (see the deliverability section) and caches results per domain so a bulk import doesn't hammer DNS. **Fails open** — if the DNS library isn't installed or errors, it assumes the address is fine rather than blocking it.

### `gmaps_email_scraper.py` — lead generation (~760 lines)
A standalone tool (also wired into the dashboard) that drives a real Chrome browser to search Google Maps, collect business listings, visit each website, and extract email addresses. This is where the *contacts* come from in the first place. Explained fully in its own section.

**Why this structure is good:** a new developer can find code by *feature* ("email sending? → `sender.py`"), not by hunting. Each file could almost be tested in isolation. The one weakness: `app.py` and `db.py` are large and could each be split further (e.g. by domain: auth, campaigns, contacts).

---

## 7. The data model (database tables)

The database is the backbone. Here are the tables and how they relate. Understanding this is the fastest way to understand the whole app, because the code is mostly moving rows between these tables.

| Table | What it holds | Key relationships |
|---|---|---|
| `contacts` | People/businesses you might email (name, email, company, website). | Referenced by `enrollments`. |
| `campaigns` | A campaign = settings (daily limit, send window, delays) + a sequence. | Has many `steps`, `enrollments`. |
| `steps` | One email in a sequence (subject, body, "send N days after previous"). | Belongs to a campaign; may have `step_variants`. |
| `step_variants` | A/B test versions of a step (variant A vs B with weights). | Belongs to a step. |
| `enrollments` | **The most important table.** One row = "this contact is in this campaign, currently on step N, next send at time T, status = queued/replied/...". | Links a contact to a campaign. |
| `sends` | A log: "we sent step N to contact C at time T with this Message-ID." | The audit trail; also powers reply matching and daily counts. |
| `daily_counts` | A running counter of emails sent per calendar day. | Enforces the daily cap. |
| `smtp_accounts` | Multiple sending mailboxes (for rotating between inboxes). | Linked to campaigns via `campaign_accounts`. |
| `campaign_accounts` | Which mailboxes a campaign is allowed to send from. | A **join table** (many-to-many). |
| `settings` | Global key/value config (SMTP creds, AI keys, base URL, the signing secret). | — |
| `users` | Dashboard login accounts (username, password hash, is_admin). | — |
| `logs` | Human-readable activity feed shown in the dashboard. | — |

**The concept of an "enrollment" is the one to internalize.** A contact isn't "in" a campaign directly. Instead, enrolling a contact creates an *enrollment* row that tracks their independent progress through the sequence — which step they're on, when the next email fires, and whether they've replied/bounced/unsubscribed. The scheduler's whole job is: "find enrollments whose `next_send_at` has passed and whose status is still `queued`, send them their current step, then advance them to the next step (or mark complete)."

**A many-to-many relationship** (say this term): a campaign can use many mailboxes, and a mailbox can serve many campaigns. You can't express that with a column on either table, so you use a **join table** (`campaign_accounts`) whose rows are just `(campaign_id, account_id)` pairs. Classic relational-database pattern.

**Foreign keys and `ON DELETE CASCADE`:** notice `steps` references `campaigns(id) ON DELETE CASCADE`. A **foreign key** is a column that points at another table's primary key, and the database enforces that the target exists. **CASCADE** means "if the campaign is deleted, automatically delete its steps too" — so you don't leave orphaned rows. (Fun detail to mention: SQLite only enforces foreign keys if you turn them on per-connection with `PRAGMA foreign_keys = ON`, which `db.py` does.)

---

## 8. The scheduler: the heart of the system

This is the part most worth being able to explain deeply, because it's where the real engineering decisions live.

### The core loop
`scheduler.py` runs an infinite loop on a **background thread** (explained below). Simplified:

```
loop forever (until told to stop):
    process_queue()          # send any emails that are due right now
    every 5 minutes:
        run_reply_check()    # scan inbox via IMAP, stop sequences for repliers
        run_bounce_check()   # scan inbox for bounce-backs, mark bad addresses
    wait up to 60 seconds (but wake instantly if "Run Now" is clicked)
```

### What "due" means, and the gates
`process_queue()` doesn't just blast everything. For each **active** campaign it checks a series of **gates** — if any fails, it skips:

1. **Global daily cap** — a hard ceiling across all campaigns (default 200/day). Protects the whole domain.
2. **Business-hours gate** — only send Mon–Fri within the configured hour window, in the campaign's timezone. Real people don't send cold email at 3am Sunday; doing so screams "bot."
3. **Per-campaign daily cap** — each campaign has its own limit (part of domain **warmup**).
4. **Bounce circuit-breaker** — if the campaign's bounce rate crosses a threshold (default 5%), auto-pause it. High bounces = you're emailing dead addresses = spam filters punish you. This is a **circuit breaker** pattern (borrowed from electrical engineering / distributed systems): automatically stop a failing operation before it does more damage.
5. Only then: fetch up to ~10 due enrollments and send them, sleeping a random 45–120s between each.

Being able to list those five gates and say *why each protects deliverability* is a genuinely impressive interview moment. It shows domain knowledge, not just coding.

### Threads, the GIL, and why this design has a ceiling

A **thread** is a separate line of execution inside one program. The web app runs on one thread (handling your clicks); the scheduler runs on another (sending on a timer). They share the same memory and the same database file.

The scheduler is a **daemon thread**: a background thread that automatically dies when the main program exits (so it can't keep the process alive after shutdown).

Now the concept interviewers probe: the **GIL (Global Interpreter Lock)**. In standard Python, only *one thread executes Python code at a time* — the GIL is a lock that enforces this. So threads don't give you true parallelism for CPU work. **But** they *do* help when a thread is *waiting* on I/O (network, disk): while the scheduler thread is blocked waiting for an SMTP server to respond, it releases the GIL and the web thread can run. Since this app is almost entirely I/O-bound (talking to mail servers, DNS, the database), threads are a reasonable fit despite the GIL.

**The ceiling this creates (say this proactively):**
- `time.sleep(45–120)` between sends runs *on the scheduler thread*. With 10 due emails, one campaign can occupy the scheduler for ~15 minutes. Multiple active campaigns are processed *serially*, so they queue up behind each other.
- Because there's one scheduler and it runs inside the single Gunicorn worker, you **cannot** run more than one worker — a second worker would start a *second* scheduler, and both would try to send, causing **double-sends**. That's why the Dockerfile hard-codes `--workers 1`.
- To scale past this you'd move the scheduler into a real **job queue** (Celery or RQ backed by Redis) with dedicated worker processes, and switch SQLite → Postgres. That's the "version 2" story.

### How "Run Now" was made safe (a real fix worth explaining)
Originally, clicking "Run Now" called `process_queue()` *directly inside the web request*. Because that function sleeps for minutes, the HTTP request would hang and eventually be killed by Gunicorn's timeout. The fix: "Run Now" now just sets a **threading Event** (a flag one thread can raise and another watches). The button returns instantly; the scheduler thread notices the flag on its next tick and does the work. This is a clean example of **decoupling a slow background job from a fast web response** — a pattern you'll use constantly in real systems.

---

## 9. The email engine and deliverability craft

This section is your differentiator. Most candidates can send an email; few understand *deliverability*. Here's the vocabulary and why each piece exists.

### SMTP and IMAP
- **SMTP** (Simple Mail Transfer Protocol) is the protocol for *sending* mail. `sender.py` opens an SMTP connection to your mail provider, logs in, and hands over the message.
- **IMAP** (Internet Message Access Protocol) is the protocol for *reading* a mailbox. The scheduler uses IMAP to scan your inbox for replies and bounce-backs.
- **Ports & TLS:** port 587 uses **STARTTLS** (connect in plaintext, then upgrade to encrypted); port 465 uses **implicit TLS** (encrypted from the first byte). **TLS** (Transport Layer Security) is the encryption that turns "SMTP" into secure SMTP. The code handles both ports.

### MIME and multipart email
An email isn't just text. A **MIME** (Multipurpose Internet Mail Extensions) message can carry multiple representations of the same content. This app sends **multipart/alternative**: both a plain-text version and an HTML version. The receiving client picks whichever it prefers.

- **Why send both?** HTML-only emails look like marketing blasts and score worse with spam filters. Including a genuine plain-text part signals "this is a personal email," which improves deliverability. `sender.py` builds the HTML from the body, and also strips it back to plain text for the text part.

### Personalization (template rendering)
Steps contain `{{first_name}}`, `{{company}}` placeholders. `_render()` substitutes real contact values. There's a **priority order**: standard contact fields beat the contact's custom `extra` fields, which beat campaign-level variables. Personalization both improves reply rates and reduces spam scoring (identical bulk copy is a spam signal).

### The deliverability protections (know all of these)
- **SPF, DKIM, DMARC** — three DNS records that prove you're allowed to send from your domain:
  - **SPF** (Sender Policy Framework): a DNS record listing which mail servers may send for your domain. Stops others spoofing you.
  - **DKIM** (DomainKeys Identified Mail): your provider cryptographically *signs* each email; the receiver verifies the signature against a public key in your DNS. Proves the message wasn't tampered with and really came from your domain.
  - **DMARC**: a policy that tells receivers what to do if SPF/DKIM fail (monitor, quarantine, or reject), and where to send reports.
  - The app can't set these for you (they live in *your* DNS), but the README walks you through them — and without all three green, cold email lands in spam. Knowing this boundary ("the app handles X, your DNS handles Y") is exactly the systems-thinking interviewers want.
- **List-Unsubscribe header + one-click** — a machine-readable header that puts a native "Unsubscribe" button in Gmail/Yahoo. As of 2024, bulk senders are *required* to support it. The app adds both the header and an HMAC-signed unsubscribe link.
- **Unsubscribe links that can't be forged** — see HMAC in the security section.
- **Random delays + scheduling jitter** — sends are spaced 45–120s apart, and follow-up steps are scheduled with a random ±30 min offset, so the pattern never looks like a robot firing at exact intervals.
- **MX validation before sending** — checks the address's domain can receive mail *before* you send, so you don't waste a send on a dead domain and rack up bounces.

### Reply detection done right (a highlight)
When someone replies, you must stop emailing them — following up after a reply is annoying and hurts your reputation. The naive way is to match the reply's **From** address against your contacts. That's fragile: people reply from aliases, forwards, or delegate mailboxes, and the From address won't match.

This app instead matches on **Message-ID threading headers**. Every email it sends gets a unique **Message-ID**. When a prospect replies, their mail client automatically includes your Message-ID in the reply's **In-Reply-To** and **References** headers (this is how email threads work). The scheduler builds a lookup of "Message-IDs we sent → which contact," and if a reply references *any* of them, it's a confirmed reply — regardless of what From address it came from. This is more accurate than 95% of tools and a great thing to walk through on a whiteboard.

It also filters **auto-replies** (out-of-office, vacation responders) using the RFC 3834 `Auto-Submitted` header plus subject-line heuristics, so an OOO bounce doesn't get mistaken for a real reply.

### Bounce handling
A **bounce** is a "delivery failed" message that comes back to your inbox. The scheduler reads these via IMAP and parses them:
- **Hard bounce** = permanent (address doesn't exist) → mark the contact dead immediately.
- **Soft bounce** = temporary (mailbox full, server down) → increment a counter; only give up after 3 soft bounces.

Parsing bounces is genuinely messy because every mail server formats them differently, so the code tries three strategies in order: the structured RFC 3464 delivery-status part, the `X-Failed-Recipients` header (Outlook), then a keyword/regex scan of the body as a last resort. "Bounce parsing is hard because there's no single standard" is a good real-world observation to share.

---

## 10. Security concepts, explained

This app had a security hardening pass (documented in [Opus Fixes.md](Opus%20Fixes.md)), so you can speak to both the vulnerabilities *and* the fixes — which is exactly the security-aware-engineer story interviewers like. Here's each concept.

### Password hashing (PBKDF2, salt, work factor)
You never store passwords as plain text. You store a **hash** — a one-way scramble. When someone logs in, you hash their input and compare hashes.

- **Salt:** a random value mixed into each password before hashing, unique per user. Stops attackers from using precomputed "rainbow tables" and ensures two users with the same password get different hashes.
- **PBKDF2:** the specific algorithm here. It deliberately runs the hash **600,000 times** (the **work factor** or iteration count). Why slow it down on purpose? Because if an attacker steals the database, a slow hash means they can only test a few thousand password guesses per second instead of billions. 600k is the current OWASP recommendation for PBKDF2-SHA256.
- **Constant-time comparison:** the code compares hashes with `hmac.compare_digest`, not `==`. A normal `==` returns faster when the first characters differ, and an attacker can *measure* those timing differences to guess the value character by character — a **timing attack**. Constant-time comparison always takes the same time regardless of where the mismatch is.
- **Transparent rehashing:** older accounts were hashed with weaker settings. On next successful login, the app silently re-hashes with the stronger 600k settings. Nice touch — you upgrade security without forcing password resets.

### Sessions and cookies
When you log in, the server puts your `user_id` into a **session** and sends the browser a **cookie**. Flask uses **signed cookies**: the session data lives in the cookie itself, but it's cryptographically signed with a **secret key** so the user can't tamper with it (they can't change `is_admin` to true without the signature failing).

Cookie hardening applied here:
- **HttpOnly** — JavaScript can't read the cookie, so an XSS bug can't steal your session.
- **Secure** — the cookie is only sent over HTTPS.
- **SameSite=Lax** — the browser won't send the cookie on most cross-site requests, which blunts CSRF.
- **Lifetime** — sessions expire after 14 days.

### CSRF (Cross-Site Request Forgery)
The attack: you're logged into ShoutReach; you visit a malicious website in another tab; that site secretly submits a form to `shoutreach.hexiv.co/api/campaigns/5/delete`. Your browser *automatically attaches your session cookie*, so the request looks legitimate.

The defense: a **CSRF token** — a secret random value tied to your session that must be included in every state-changing request (as the `X-CSRF-Token` header). The malicious site can't read your token (it's on a different origin), so it can't forge a valid request. The app generates the token per session, the JavaScript fetches it and attaches it to every non-GET call, and a `before_request` hook rejects any mismatch. Login gets an extra **same-origin check** (the `Origin`/`Referer` header must match) because the CSRF token can't protect the login form itself (you don't have a session yet).

### XSS (Cross-Site Scripting)
The attack: an attacker puts JavaScript into a data field — say a campaign named `<script>steal()</script>` — and when the dashboard renders that name, the script runs in *your* browser with *your* session.

The risk here came from building HTML by string concatenation. The fixes: an `esc()` function that converts dangerous characters (`<`, `>`, `"`, `'`, `` ` ``) into harmless HTML entities for text, and a separate `escj()` for values dropped into inline `onclick="..."` JavaScript (a different, trickier context). The lesson to articulate: **you must escape output based on the context it lands in** — HTML text, HTML attribute, and JavaScript string each need different escaping. This is *the* subtle thing about XSS.

### HMAC-signed unsubscribe tokens
The unsubscribe link contains the recipient's email. If it were a plain `?email=bob@x.com`, anyone could unsubscribe anyone by editing the URL. Instead the app appends an **HMAC** signature.

- **HMAC** (Hash-based Message Authentication Code) = a hash of the data combined with a **secret key** only the server knows. The server can verify a token is authentic (recompute the HMAC and compare), but nobody without the secret can forge one. Same primitive that signs the session cookies. Verified with constant-time comparison, again to prevent timing attacks.

### Rate limiting
`/login` is throttled to 10 attempts per IP per 15 minutes. This slows **brute-force** attacks (trying millions of password guesses). It's an in-memory counter — simple and effective for one server, though it resets on restart and wouldn't be shared across multiple servers (a Redis-backed limiter would fix that at scale).

### Secret masking
API keys, SMTP passwords, and the signing secret are never sent back to the browser — a helper (`_is_secret_key`) detects secret-shaped setting names and replaces their values with `●●●●●●`. When you save settings, sending the placeholder back means "keep the existing value," so you never accidentally blank out a secret you couldn't see. Admin-only endpoints gate all of this.

> For the *current* state of what's fixed vs still open, read [Fable Audit.md](Fable%20Audit.md). The short version: the big web-security holes (CSRF, XSS, secret leakage, privilege escalation) were closed; the deeper items (encrypting secrets at rest, unverified mail-server TLS certificates, no automated tests) remain.

---

## 11. The Google Maps scraper

This is a self-contained subsystem and a great "I built the whole pipeline" talking point — it's where the leads come from.

### What it does
Given a niche and city (e.g. "dental clinics in Brampton"), it:
1. Opens a **real, visible Chrome browser** via **Playwright** (a browser-automation library), searches Google Maps, and scrolls the results, clicking each business to grab its website and address.
2. Visits each website with plain HTTP requests (via `requests` + **BeautifulSoup** for HTML parsing) and scans up to 6 pages (homepage, /contact, /about…) for email addresses using a regex.
3. Classifies each result: `found`, `no_website`, `contact_form_only`, `site_blocked`, etc. — so you know *why* an email wasn't found and which leads are worth a manual look.
4. Optionally imports the found contacts straight into the database.

### The interesting engineering
- **Bot detection evasion (and the ethics):** Google Maps actively blocks scrapers. The tool uses **playwright-stealth** to hide ~20 automation fingerprints (the biggest being `navigator.webdriver`, a flag browsers set when automated), a realistic user-agent, and **persisted cookies** so Google sees a "returning user" rather than a fresh bot. Scrolling uses randomized distances and pauses to look human. Be ready to note this is a **gray area** — it likely violates Google's Terms of Service, and for a portfolio you'd frame it as "a technical exercise in browser automation and anti-bot techniques," and note the polite alternative is the official Google Places API (which costs money).
- **CAPTCHA handling:** if Google shows a CAPTCHA, the tool *pauses* and waits for you to solve it in the visible browser, then resume from the dashboard — a pragmatic human-in-the-loop design.
- **Resume support:** it writes each result to a CSV immediately, so a crash mid-run doesn't lose progress, and a re-run skips businesses already processed. **Idempotency** (safe to re-run without duplicating work) is a mature instinct to point out.
- **Threaded + progress reporting:** when launched from the dashboard it runs on a background thread and writes progress/logs to a shared **`ScraperJob`** object that the frontend polls every 2 seconds — the same "slow work off the request thread, poll for status" pattern as the scheduler.

### Fun detail worth mentioning
It strips `<style>` blocks before scanning for emails, because CSS font-license comments contain the font author's email (e.g. for Raleway or Lato) and would produce false positives. That kind of "I hit a real bug and understood the root cause" story lands well.

---

## 12. Deployment and operations

### Where it runs
A single **Google Cloud e2-medium VM** (a small Linux server, ~4GB RAM). Not serverless, not Kubernetes — one always-on box. Correct for a single-tenant tool: predictable, cheap, simple.

### The serving stack
```
Internet → Nginx (terminates HTTPS on 443) → Gunicorn (1 worker on 127.0.0.1:8000) → Flask
```
- **Nginx** as reverse proxy handles TLS and forwards to the app on localhost.
- **Gunicorn** runs the app as a **systemd service** (`shoutreach.service`), so Linux restarts it automatically if it crashes or the box reboots. Config and secrets come from an `.env` file referenced by the service.
- **Secrets** (`SECRET_KEY`, etc.) live in that `.env` file, injected as environment variables — kept out of the code and out of git.

### The deploy pipeline (CI/CD)
There's a **GitHub Actions** workflow (`.github/workflows/deploy.yml`). **CI/CD** = Continuous Integration / Continuous Deployment: automation that ships your code when you push. On every push to `master`, it SSHes into the VM and runs: `git pull`, `pip install -r requirements.txt`, `sudo systemctl restart shoutreach`. So `git push` = live in ~30 seconds.

- Be ready to critique your own pipeline (interviewers love this): there are **no tests gating the deploy**, dependencies are **unpinned** (a `pip install` could pull a different version than you tested), and a restart means a few seconds of **downtime** (no zero-downtime/rolling deploy). All acceptable for a solo tool; all things you'd harden for a team. See the audit for specifics.

### The Docker / Fly.io files
There's a `Dockerfile` and a `fly.toml` (for Fly.io deployment) in the repo, but production runs on the GCP VM via systemd. Those files are **not currently used** and are out of date (the Dockerfile doesn't install all dependencies; `fly.toml` still has a placeholder app name). If asked, be honest: "those were an earlier deployment target I moved away from; I should either fix or delete them." Owning that is better than getting caught by it.

---

## 13. The architectural decisions, defended

A cheat-sheet of "why did you build it this way?" with answers that show you understood the tradeoff, not that you didn't know the alternative.

| Decision | Why (the defense) | The tradeoff you acknowledge |
|---|---|---|
| **SQLite, not Postgres** | Zero ops, one-file backups, perfect for one writer. | One writer only → caps you at a single app server. First thing I'd change to go multi-tenant. |
| **In-process scheduler thread, not a job queue** | No extra infrastructure (no Redis/Celery to run and monitor). Simple to reason about. | Can't run >1 worker (double-send risk); slow sends block the loop; no crash recovery mid-send. A real queue is the v2 upgrade. |
| **Single Gunicorn worker** | Guarantees exactly one scheduler. | No parallelism for web requests; one core. Fine at this traffic. |
| **Vanilla JS, no framework** | No build step, no dependency churn, the code I write is what runs. | Manual DOM/HTML building was the source of XSS risk; a framework would auto-escape. |
| **Hand-written SQL, no ORM** | Full control, I understand every query, no ORM abstraction to fight. | More boilerplate; no automatic migrations (I hand-write `ALTER TABLE` migrations). |
| **Signed cookies for sessions** | Stateless — no server-side session store needed. | Can't instantly revoke a single session server-side; mitigated by short lifetime. |
| **Self-hosted / single-tenant** | Own your data, no SaaS fees, simpler security model. | No `tenant_id` anywhere → becoming multi-customer is a data-model redesign, not a tweak. |

The meta-point to land: **"Every one of these optimizes for a solo-operated tool. If the goal became a multi-customer SaaS, the migration path is clear — Postgres, a real job queue, multiple workers, and tenant isolation — but building that upfront would have been over-engineering for what this needed to be."** That sentence demonstrates senior-level judgment.

---

## 14. Known limitations (say these before they ask)

Volunteering weaknesses reads as confidence and honesty. Keep this list in your back pocket:

1. **It's single-tenant.** "Users" share one global pool of contacts and settings; there's no per-customer data isolation. It's multi-*user*, not multi-*tenant*.
2. **The architecture caps at one server** (SQLite single-writer + in-process scheduler + one worker). Known and deliberate.
3. **No automated tests.** The biggest honest gap. A past bug (A/B testing crashed because a module wasn't imported) shipped precisely because there were no tests. I'd start with tests for auth, template rendering, and reply detection.
4. **Secrets are stored in plaintext in the SQLite file.** Access is gated to admins, but at-rest they're unencrypted. Encrypting them (or using a secrets manager) is the next security step.
5. **Mail-server TLS certificates aren't verified** (Python's `smtplib`/`imaplib` don't verify by default). A network attacker between the app and the mail server could intercept credentials. Small blast radius on a trusted cloud network, but a real gap — the fix is one line (pass a verifying SSL context). *(Found in this review — see the audit.)*
6. **No crash-safe send idempotency.** If the process dies in the instant between sending an email and recording it, a restart could resend it. A "transactional outbox" pattern would fix it.
7. **The deploy pipeline has no test gate and unpinned dependencies.**

Framing tip: pair each limitation with *why it's acceptable here* and *what you'd do differently at scale*. That turns "weakness" into "judgment."

---

## 15. Interview questions and strong answers

Rehearse these out loud. The answers are compressed — expand them in your own voice.

**Q: Walk me through the architecture.**
> One process, two engines. A Flask web app serves the dashboard and API; a background thread runs the scheduler that sends email on a timer. Both share a SQLite database. External I/O is SMTP for sending, IMAP for reading replies and bounces, DNS for validating addresses, and optional AI APIs for copy review. In production it's Nginx → Gunicorn → Flask on a single GCP VM.

**Q: How does an email actually get sent, start to finish?**
> The scheduler wakes every ~60s, finds enrollments whose next-send time has passed, and for each active campaign checks the gates — global cap, business hours, per-campaign cap, bounce rate. For a due contact it picks the current step (or A/B variant), renders the `{{placeholders}}`, builds a multipart text+HTML MIME message with a unique Message-ID and unsubscribe headers, sends it over SMTP, logs the send, then advances the enrollment to the next step or marks it complete — sleeping a random 45–120 seconds before the next one.

**Q: How do you know when someone replies, and why is your approach better than matching the sender's email?**
> I match on Message-ID threading headers, not the From address. Every send has a unique Message-ID; a reply automatically carries it in its In-Reply-To/References headers. I build a map of the Message-IDs I've sent to which contact, and if an inbound message references any of them, it's a confirmed reply — even if it came from an alias, a forward, or a delegate, which would break From-address matching. I also filter out auto-replies using the RFC 3834 header.

**Q: What stops this from getting your domain blacklisted?**
> Layers: SPF/DKIM/DMARC on the domain (DNS side), then in the app a global daily cap, per-campaign caps for warmup, business-hours-only sending, randomized delays and scheduling jitter, MX validation before sending, a bounce-rate circuit breaker that auto-pauses at 5%, multipart text+HTML, and a compliant List-Unsubscribe header with one-click support.

**Q: Why SQLite? Isn't that a toy database?**
> For a single-writer, single-server, self-hosted tool it's ideal — zero ops, the whole database is one file to back up, and it's plenty fast at this scale. Its one real limit is that it allows one writer at a time, which is exactly why I run a single app worker. If I needed multiple app servers or true multi-tenancy I'd move to Postgres — but adopting it upfront would've been over-engineering.

**Q: You run a background thread inside a web server. What can go wrong?**
> Three things. One: if I ran more than one Gunicorn worker I'd get multiple schedulers and double-sends, so I pin it to one worker. Two: the sleep-between-sends blocks the loop, so campaigns are processed serially — a throughput ceiling. Three: there's no crash recovery mid-send. The clean fix for all three is a real job queue with dedicated workers and idempotent tasks; I chose the thread because it needs zero extra infrastructure and the scale doesn't demand more yet.

**Q: Tell me about a bug you found and fixed.**
> A/B testing crashed the first time anyone used it — the variant picker called `random.uniform` but `random` was never imported in that module, so enrolling a contact into a campaign with variants threw a 500. It shipped because there were no tests. That bug is the reason I now argue for at least a smoke-test suite. *(Alternatively, use the mx_valid migration bug from the audit — see below.)*

**Q: What's the biggest security weakness right now?**
> Secrets are stored unencrypted in the SQLite file. It's mitigated — only admins can read them through the app, and the DB viewer masks them — but at rest they're plaintext, so anyone who gets the file gets the credentials. Encrypting them with a key from the environment (or a secrets manager) is the next step. A close second is that the app doesn't verify mail-server TLS certificates, which I found in a recent review.

**Q: If I gave you a week to make this production-grade for real customers, what would you do?**
> First, tests — auth, rendering, reply detection. Then encrypt secrets at rest and verify mail-server TLS certs. Then the architecture: Postgres plus a Redis-backed job queue with dedicated workers, which unlocks multiple web workers and removes the double-send constraint. Then add `tenant_id` across the schema for real multi-tenancy, and put a test gate and pinned dependencies in the deploy pipeline. In that order, because correctness and secrets beat scale when you have no customers yet.

---

## 16. Glossary

Quick definitions to reinforce. If you can teach these, you understand the system.

- **API (Application Programming Interface):** the set of URLs the frontend calls to talk to the backend (e.g. `POST /api/campaigns`).
- **Authentication vs Authorization:** who you are vs what you're allowed to do.
- **Background/daemon thread:** a line of execution that runs alongside the main program and dies with it.
- **Circuit breaker:** auto-stop a failing operation before it causes more harm (here: auto-pause on high bounce rate).
- **CSRF:** tricking a logged-in user's browser into making a request they didn't intend; defended with a per-session token.
- **DKIM / SPF / DMARC:** DNS records that authenticate your outgoing mail so it isn't treated as spam/spoofing.
- **GIL (Global Interpreter Lock):** the lock that lets only one Python thread run code at a time; threads still help for I/O waiting.
- **HMAC:** a keyed signature proving data is authentic and unmodified without exposing the key.
- **Idempotency:** an operation you can safely repeat without changing the result / duplicating work.
- **IMAP / SMTP:** protocols for reading a mailbox / sending mail.
- **Join table:** a table whose rows link two other tables in a many-to-many relationship.
- **MIME / multipart:** the email format that carries both plain-text and HTML versions of a message.
- **MX record:** the DNS record naming the mail server for a domain; its presence means the domain can receive email.
- **ORM:** a library that maps database rows to objects so you don't write SQL. (This app deliberately doesn't use one.)
- **PBKDF2 / salt / work factor:** deliberately-slow password hashing with per-user randomness to resist cracking.
- **Reverse proxy (Nginx):** a front server that receives internet traffic and forwards it to your private app; handles HTTPS.
- **Session cookie:** the signed token in your browser that keeps you logged in.
- **systemd service:** the Linux mechanism that keeps the app running and restarts it on failure/reboot.
- **Threading Event:** a flag one thread sets and another waits on — used to make "Run Now" instant.
- **WSGI:** the standard interface between a Python web server (Gunicorn) and a Python app (Flask).
- **XSS:** injecting malicious JavaScript through data fields; defended by escaping output per context.

---

*This guide describes the system as it stands. For the current, verified list of flaws and their fixes, see [Fable Audit.md](Fable%20Audit.md).*
