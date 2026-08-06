"""
db.py — SQLite database layer for the outreach system.
All tables, queries, and helpers live here.
"""

import sqlite3
import json
import logging
import datetime
import random
import re
import secrets
from urllib.parse import urlsplit
import hashlib
import hmac as _hmac
import os as _os
from pathlib import Path

logger = logging.getLogger("db")

DB_PATH = _os.environ.get("DB_PATH", "outreach.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL lets readers run alongside a writer but still serialises writers.
    # The scrape worker posts progress while the scheduler is sending, so a
    # collision is expected -- wait it out instead of raising "database is
    # locked" at whichever one loses the race.
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            -- ─── Core tables ────────────────────────────────────────────────

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT    NOT NULL,
                status           TEXT    NOT NULL DEFAULT 'draft',
                daily_limit      INTEGER NOT NULL DEFAULT 30,
                send_start_hour  INTEGER NOT NULL DEFAULT 9,
                send_end_hour    INTEGER NOT NULL DEFAULT 17,
                min_delay_secs   INTEGER NOT NULL DEFAULT 45,
                max_delay_secs   INTEGER NOT NULL DEFAULT 120,
                bounce_pause_pct REAL    NOT NULL DEFAULT 5.0,
                created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS steps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                step_num    INTEGER NOT NULL,
                subject     TEXT    NOT NULL DEFAULT '',
                body_html   TEXT    NOT NULL DEFAULT '',
                delay_days  INTEGER NOT NULL DEFAULT 0,
                UNIQUE(campaign_id, step_num)
            );

            -- email is nullable: the scraper stores no-email prospects.
            -- Uniqueness comes from the partial index contacts_email_unique
            -- below, not a table constraint, so NULLs are allowed to repeat.
            CREATE TABLE IF NOT EXISTS contacts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT DEFAULT NULL,
                first_name TEXT NOT NULL DEFAULT '',
                last_name  TEXT NOT NULL DEFAULT '',
                company    TEXT NOT NULL DEFAULT '',
                extra      TEXT NOT NULL DEFAULT '{}',
                status     TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS enrollments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                current_step INTEGER NOT NULL DEFAULT 1,
                status       TEXT    NOT NULL DEFAULT 'queued',
                next_send_at TEXT,
                enrolled_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(campaign_id, contact_id)
            );

            CREATE TABLE IF NOT EXISTS sends (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER,
                contact_id  INTEGER,
                step_num    INTEGER,
                subject     TEXT,
                msg_id      TEXT,
                status      TEXT NOT NULL DEFAULT 'sent',
                sent_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_counts (
                date  TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                level      TEXT NOT NULL DEFAULT 'INFO',
                message    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS smtp_accounts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                email       TEXT    NOT NULL,
                from_name   TEXT    NOT NULL DEFAULT '',
                smtp_host   TEXT    NOT NULL DEFAULT '',
                smtp_port   INTEGER NOT NULL DEFAULT 587,
                smtp_user   TEXT    NOT NULL DEFAULT '',
                smtp_pass   TEXT    NOT NULL DEFAULT '',
                imap_host   TEXT    NOT NULL DEFAULT '',
                imap_user   TEXT    NOT NULL DEFAULT '',
                imap_pass   TEXT    NOT NULL DEFAULT '',
                status      TEXT    NOT NULL DEFAULT 'active',
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS campaign_accounts (
                campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                account_id  INTEGER NOT NULL REFERENCES smtp_accounts(id) ON DELETE CASCADE,
                PRIMARY KEY (campaign_id, account_id)
            );

            CREATE TABLE IF NOT EXISTS step_variants (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                step_id   INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
                label     TEXT    NOT NULL,
                subject   TEXT    NOT NULL DEFAULT '',
                body_html TEXT    NOT NULL DEFAULT '',
                weight    INTEGER NOT NULL DEFAULT 50,
                UNIQUE(step_id, label)
            );

            -- Scrape jobs are queued here by the web UI and claimed by a
            -- worker running on the operator's own machine. The server never
            -- launches a browser: the scraper needs a visible Chrome window
            -- for CAPTCHA solving, which a headless VM cannot provide.
            CREATE TABLE IF NOT EXISTS scrape_jobs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                niche        TEXT    NOT NULL,
                city         TEXT    NOT NULL,
                max_results  INTEGER NOT NULL DEFAULT 50,
                auto_import  INTEGER NOT NULL DEFAULT 1,
                status       TEXT    NOT NULL DEFAULT 'queued',
                progress     INTEGER NOT NULL DEFAULT 0,
                total        INTEGER NOT NULL DEFAULT 0,
                found        INTEGER NOT NULL DEFAULT 0,
                imported     INTEGER NOT NULL DEFAULT 0,
                logs         TEXT    NOT NULL DEFAULT '[]',
                stop_flag    INTEGER NOT NULL DEFAULT 0,
                resume_flag  INTEGER NOT NULL DEFAULT 0,
                error        TEXT    NOT NULL DEFAULT '',
                created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                claimed_at   TEXT,
                heartbeat_at TEXT,
                finished_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)

        # Schema migrations — safe to run repeatedly on existing databases
        for _col_sql in [
            "ALTER TABLE contacts ADD COLUMN soft_bounce_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE contacts ADD COLUMN website TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE contacts ADD COLUMN address TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sends ADD COLUMN account_id INTEGER",
            "ALTER TABLE enrollments ADD COLUMN variant_label TEXT",
            "ALTER TABLE contacts ADD COLUMN mx_valid INTEGER DEFAULT NULL",
            "ALTER TABLE campaigns ADD COLUMN timezone TEXT DEFAULT NULL",
            "ALTER TABLE campaigns ADD COLUMN variables TEXT DEFAULT '{}'",
            # Canonical host for a contact's website. Deduping on the raw
            # website string failed constantly because Maps hands out
            # http://x.ca, https://www.x.ca/ and http://x.ca/?utm_source=gmb
            # for the same business.
            "ALTER TABLE contacts ADD COLUMN domain TEXT NOT NULL DEFAULT ''",
            # Points at the contact that won for this domain. Suppression is
            # kept OUT of `status` on purpose: get_due_enrollments filters
            # status='active', and follow-ups re-enter through that same
            # query, so flipping a mid-sequence contact's status would
            # silently cancel steps 2 and 3.
            "ALTER TABLE contacts ADD COLUMN duplicate_of INTEGER DEFAULT NULL",
        ]:
            try:
                conn.execute(_col_sql)
            except Exception as exc:
                # Almost always "duplicate column name" on an already-migrated DB,
                # but log it — a genuine migration failure must not be invisible.
                logger.debug("Column migration skipped: %s (%s)", _col_sql, exc)

        # Make email nullable on legacy databases created before the scraper
        # needed to store no-email prospects. Fresh databases are already
        # nullable (see CREATE TABLE above), so this never fires for them.
        #
        # This rebuild must list EVERY column added by the ALTER loop above --
        # anything omitted here is silently dropped. That is exactly how
        # mx_valid went missing (Fable Audit 2.1). Add new columns in both places.
        _col_info = conn.execute("PRAGMA table_info(contacts)").fetchall()
        _email_col = next((r for r in _col_info if r['name'] == 'email'), None)
        if _email_col and _email_col['notnull']:
            logger.info("Migrating contacts.email to nullable (legacy schema)")
            conn.executescript("""
                PRAGMA foreign_keys = OFF;
                CREATE TABLE contacts_new (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    email             TEXT DEFAULT NULL,
                    first_name        TEXT NOT NULL DEFAULT '',
                    last_name         TEXT NOT NULL DEFAULT '',
                    company           TEXT NOT NULL DEFAULT '',
                    extra             TEXT NOT NULL DEFAULT '{}',
                    status            TEXT NOT NULL DEFAULT 'active',
                    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
                    website           TEXT NOT NULL DEFAULT '',
                    address           TEXT NOT NULL DEFAULT '',
                    soft_bounce_count INTEGER NOT NULL DEFAULT 0,
                    mx_valid          INTEGER DEFAULT NULL,
                    domain            TEXT NOT NULL DEFAULT '',
                    duplicate_of      INTEGER DEFAULT NULL
                );
                INSERT INTO contacts_new
                    SELECT id, email, first_name, last_name, company, extra, status,
                           created_at, COALESCE(website,''), COALESCE(address,''),
                           COALESCE(soft_bounce_count, 0), mx_valid,
                           COALESCE(domain,''), duplicate_of
                    FROM contacts;
                DROP TABLE contacts;
                ALTER TABLE contacts_new RENAME TO contacts;
                PRAGMA foreign_keys = ON;
            """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS contacts_email_unique
            ON contacts(email) WHERE email IS NOT NULL AND email != ''
        """)

        # Hot-path indexes — used by the scheduler / reply-detection loops.
        # Without these every cycle full-scans the sends and enrollments tables.
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS sends_msg_id_idx        ON sends(msg_id) WHERE msg_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS sends_campaign_sent_at  ON sends(campaign_id, sent_at)",
            "CREATE INDEX IF NOT EXISTS sends_sent_at_idx       ON sends(sent_at)",
            "CREATE INDEX IF NOT EXISTS enrollments_due_idx     ON enrollments(campaign_id, status, next_send_at)",
            "CREATE INDEX IF NOT EXISTS enrollments_contact_idx ON enrollments(contact_id, status)",
            "CREATE INDEX IF NOT EXISTS contacts_status_idx     ON contacts(status)",
            "CREATE INDEX IF NOT EXISTS contacts_domain_idx     ON contacts(domain) WHERE domain != ''",
        ):
            try:
                conn.execute(idx_sql)
            except Exception as exc:
                logger.debug("Index create skipped: %s (%s)", idx_sql, exc)

        # Backfill domain for rows that predate the column.
        try:
            todo = conn.execute(
                "SELECT id, website FROM contacts "
                "WHERE domain = '' AND website != ''"
            ).fetchall()
            for row in todo:
                conn.execute(
                    "UPDATE contacts SET domain=? WHERE id=?",
                    (canonical_domain(row["website"]), row["id"]),
                )
            if todo:
                logger.info("Backfilled domain for %d contact(s)", len(todo))
        except Exception as exc:
            logger.warning("Domain backfill skipped: %s", exc)


# ── Settings ──────────────────────────────────────────────────────────────────

def get_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


def save_settings(data: dict):
    with get_db() as conn:
        for k, v in data.items():
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v))
            )


def get_or_create_secret() -> str:
    """Return the persistent HMAC signing secret, creating it on first call."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='_secret_key'"
        ).fetchone()
        if row:
            return row["value"]
        key = secrets.token_hex(32)
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('_secret_key',?)", (key,)
        )
        return key


def get_or_create_worker_api_key() -> str:
    """
    Shared secret the local scrape worker uses to authenticate.

    The worker is not a browser and has no session cookie, so it presents this
    as an X-API-Key header instead. Custom headers are not attached
    cross-origin by browsers, so token auth on these routes is not exposed to
    CSRF the way a cookie-authenticated route would be.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='_worker_api_key'"
        ).fetchone()
        if row and row["value"]:
            return row["value"]
        key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('_worker_api_key',?)",
            (key,),
        )
        return key


def rotate_worker_api_key() -> str:
    with get_db() as conn:
        key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('_worker_api_key',?)",
            (key,),
        )
        return key


# ── Scrape jobs ───────────────────────────────────────────────────────────────

# Statuses a job can sit in while it is still someone's responsibility.
SCRAPE_ACTIVE_STATUSES = ("queued", "claimed", "running", "captcha")

# A worker that has not checked in for this long is treated as gone. It has to
# comfortably exceed the worker's own post interval, or a busy scrape that goes
# quiet during a slow page load would flap the UI to "offline".
WORKER_STALE_SECONDS = 45


def create_scrape_job(niche, city, max_results=50, auto_import=True) -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO scrape_jobs(niche, city, max_results, auto_import, logs)
            VALUES(?,?,?,?,'[]')
        """, (niche, city, int(max_results), 1 if auto_import else 0))
        return cur.lastrowid


def get_scrape_job(job_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM scrape_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_latest_scrape_job():
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM scrape_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_active_scrape_job():
    placeholders = ",".join("?" * len(SCRAPE_ACTIVE_STATUSES))
    with get_db() as conn:
        row = conn.execute(
            f"SELECT * FROM scrape_jobs WHERE status IN ({placeholders}) "
            "ORDER BY id ASC LIMIT 1",
            SCRAPE_ACTIVE_STATUSES,
        ).fetchone()
        return dict(row) if row else None


def claim_scrape_job() -> dict:
    """
    Hand the oldest queued job to a worker, atomically.

    The UPDATE ... WHERE status='queued' is the lock: if two workers race, only
    one gets a rowcount of 1, so the job cannot be run twice.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM scrape_jobs WHERE status='queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cur = conn.execute("""
            UPDATE scrape_jobs
               SET status='claimed',
                   claimed_at=datetime('now'),
                   heartbeat_at=datetime('now')
             WHERE id=? AND status='queued'
        """, (row["id"],))
        if cur.rowcount != 1:
            return None          # another worker won the race
        return dict(conn.execute(
            "SELECT * FROM scrape_jobs WHERE id=?", (row["id"],)
        ).fetchone())


def update_scrape_job(job_id: int, *, status=None, progress=None, total=None,
                      found=None, imported=None, error=None, new_logs=None,
                      finished=False) -> dict:
    """
    Apply a worker's progress report and return the current control flags.

    Returning stop/resume in the same round trip is deliberate: this one call
    is the heartbeat, the log upload, and the control channel, so the worker
    learns about a Stop or Resume press without a second request.
    """
    with get_db() as conn:
        sets, params = ["heartbeat_at=datetime('now')"], []
        for column, value in (("status", status), ("progress", progress),
                              ("total", total), ("found", found),
                              ("imported", imported), ("error", error)):
            if value is not None:
                sets.append(f"{column}=?")
                params.append(value)
        if finished:
            sets.append("finished_at=datetime('now')")

        if new_logs:
            row = conn.execute(
                "SELECT logs FROM scrape_jobs WHERE id=?", (job_id,)
            ).fetchone()
            try:
                existing = json.loads(row["logs"]) if row else []
            except Exception:
                existing = []
            existing.extend(new_logs)
            # Bounded so a long scrape cannot grow the row without limit.
            sets.append("logs=?")
            params.append(json.dumps(existing[-300:]))

        params.append(job_id)
        conn.execute(f"UPDATE scrape_jobs SET {','.join(sets)} WHERE id=?", params)

        row = conn.execute(
            "SELECT stop_flag, resume_flag, status FROM scrape_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if not row:
            return {"stop": True, "resume": False, "status": "gone"}
        # Resume is a one-shot edge: clear it once the worker has been told,
        # or a single click would unblock every later CAPTCHA too.
        if row["resume_flag"]:
            conn.execute(
                "UPDATE scrape_jobs SET resume_flag=0 WHERE id=?", (job_id,)
            )
        return {
            "stop":   bool(row["stop_flag"]),
            "resume": bool(row["resume_flag"]),
            "status": row["status"],
        }


def flag_scrape_job(job_id: int, *, stop=False, resume=False):
    column = "stop_flag" if stop else "resume_flag"
    with get_db() as conn:
        conn.execute(f"UPDATE scrape_jobs SET {column}=1 WHERE id=?", (job_id,))


def touch_worker_seen():
    """
    Record that a worker just checked in.

    Kept in settings rather than on the job row because the worker polls for
    work when no job exists -- the UI still needs to show it as connected so
    pressing Start is not a shot in the dark.
    """
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) "
            "VALUES('_worker_last_seen', datetime('now'))"
        )


def worker_seconds_since_seen():
    """Seconds since any worker last checked in, or None if never."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='_worker_last_seen'"
        ).fetchone()
    if not row or not row["value"]:
        return None
    try:
        seen = datetime.datetime.strptime(row["value"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    return max(0.0, (datetime.datetime.utcnow() - seen).total_seconds())


def reap_stale_scrape_jobs():
    """
    Fail jobs whose worker vanished mid-run.

    Without this a killed worker leaves a job stuck in 'running' forever, and
    the UI refuses to start a new one because something is already active.
    """
    with get_db() as conn:
        conn.execute(f"""
            UPDATE scrape_jobs
               SET status='error',
                   error='Worker stopped reporting',
                   finished_at=datetime('now')
             WHERE status IN ('claimed','running','captcha')
               AND heartbeat_at IS NOT NULL
               AND (julianday('now') - julianday(heartbeat_at)) * 86400 > ?
        """, (WORKER_STALE_SECONDS * 4,))


# ── Campaigns ─────────────────────────────────────────────────────────────────

def get_campaigns():
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC"
        ).fetchall()]


def get_campaign(cid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None


def create_campaign(name, daily_limit=30, start_hour=9, end_hour=17,
                    min_delay=45, max_delay=120, timezone=None, variables='{}'):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns(name,daily_limit,send_start_hour,send_end_hour,"
            "min_delay_secs,max_delay_secs,timezone,variables) VALUES(?,?,?,?,?,?,?,?)",
            (name, daily_limit, start_hour, end_hour, min_delay, max_delay, timezone, variables)
        )
        return cur.lastrowid


def update_campaign(cid, **fields):
    allowed = {"name", "daily_limit", "send_start_hour", "send_end_hour",
               "min_delay_secs", "max_delay_secs", "bounce_pause_pct", "status", "timezone", "variables"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with get_db() as conn:
        conn.execute(f"UPDATE campaigns SET {set_clause} WHERE id=?",
                     (*updates.values(), cid))


# ── Steps ─────────────────────────────────────────────────────────────────────

def get_steps(campaign_id):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM steps WHERE campaign_id=? ORDER BY step_num",
            (campaign_id,)
        ).fetchall()]


def upsert_step(campaign_id, step_num, subject, body_html, delay_days):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO steps(campaign_id,step_num,subject,body_html,delay_days)
            VALUES(?,?,?,?,?)
            ON CONFLICT(campaign_id,step_num) DO UPDATE SET
                subject=excluded.subject,
                body_html=excluded.body_html,
                delay_days=excluded.delay_days
        """, (campaign_id, step_num, subject, body_html, delay_days))


def delete_step(campaign_id, step_num):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM steps WHERE campaign_id=? AND step_num=?",
            (campaign_id, step_num)
        )


# ── Contacts ──────────────────────────────────────────────────────────────────

def canonical_domain(website: str) -> str:
    """
    Reduce a website URL to the host it identifies.

    'http://x.ca', 'https://www.x.ca/contact' and
    'http://x.ca/?utm_source=gmb' are one business, and Google Maps hands out
    all three shapes. Comparing raw strings created a duplicate row per URL
    variant, so everything domain-related keys off this instead.
    """
    website = (website or "").strip().lower()
    if not website:
        return ""
    if "//" not in website:
        website = "https://" + website
    try:
        host = urlsplit(website).netloc
    except Exception:
        return ""
    host = host.split("@")[-1].split(":")[0]      # drop userinfo and port
    return host[4:] if host.startswith("www.") else host


# Addresses that reach a mailbox nobody reads, or the wrong department. The
# README's own advice is not to cold-email role accounts, so when a business
# exposes several the personal one should win.
_ROLE_PREFIXES = {
    "info", "contact", "hello", "enquiries", "enquiry", "inquiries",
    "office", "admin", "reception", "mail", "email", "general",
}
_AVOID_PREFIXES = {
    "billing", "payment", "payments", "accounts", "accounting", "invoice",
    "careers", "jobs", "hr", "recruitment", "noreply", "no-reply",
    "donotreply", "webmaster", "postmaster", "abuse", "privacy", "legal",
    "support", "help", "sales",
}


def email_rank(email: str) -> int:
    """Lower sorts better. Personal < role < billing/careers/no-reply."""
    local = (email or "").split("@")[0].strip().lower()
    base = re.split(r"[.\-_+]", local)[0] if local else ""
    if base in _AVOID_PREFIXES or local in _AVOID_PREFIXES:
        return 2
    if base in _ROLE_PREFIXES or local in _ROLE_PREFIXES:
        return 1
    return 0


def _has_live_enrollment(conn, contact_id: int) -> bool:
    """True if this contact is mid-sequence and must not be suppressed."""
    row = conn.execute("""
        SELECT 1 FROM enrollments
         WHERE contact_id=?
           AND status NOT IN ('completed','replied','unsubscribed','bounced')
         LIMIT 1
    """, (contact_id,)).fetchone()
    return row is not None


def _pick_domain_winner(conn, domain: str):
    """
    Decide which contact at `domain` is the sendable one, and link the rest.

    Two rules, in order:
      1. A contact already mid-sequence always wins. Demoting it would strand
         the prospect after step 1 -- they would never receive the follow-ups,
         with no error anywhere.
      2. Otherwise the best-ranked address wins, oldest as the tiebreak.
    """
    if not domain:
        return
    rows = conn.execute("""
        SELECT id, email FROM contacts
         WHERE domain=? AND email IS NOT NULL AND email != ''
           AND status NOT IN ('deleted','unsubscribed','bounced')
         ORDER BY id ASC
    """, (domain,)).fetchall()
    if len(rows) < 2:
        # Nothing to arbitrate; make sure a lone contact is not left suppressed.
        for row in rows:
            conn.execute(
                "UPDATE contacts SET duplicate_of=NULL WHERE id=?", (row["id"],)
            )
        return

    enrolled = [r for r in rows if _has_live_enrollment(conn, r["id"])]
    if enrolled:
        winner = enrolled[0]["id"]
    else:
        winner = sorted(rows, key=lambda r: (email_rank(r["email"]), r["id"]))[0]["id"]

    for row in rows:
        if row["id"] == winner:
            conn.execute("UPDATE contacts SET duplicate_of=NULL WHERE id=?", (winner,))
        elif _has_live_enrollment(conn, row["id"]):
            # Already being emailed. Leave it alone rather than cutting a live
            # sequence short; the operator can unenroll it deliberately.
            conn.execute("UPDATE contacts SET duplicate_of=NULL WHERE id=?", (row["id"],))
        else:
            conn.execute(
                "UPDATE contacts SET duplicate_of=? WHERE id=?", (winner, row["id"])
            )


def upsert_contacts(rows):
    """rows: list of dicts — email optional; required for active contacts, omit for prospects."""
    with get_db() as conn:
        inserted = 0
        touched_domains = set()

        for r in rows:
            email = (r.get("email") or "").strip().lower()
            website = r.get("website", "")
            status = r.get("status", "active")
            domain = canonical_domain(website)
            if domain:
                touched_domains.add(domain)

            if email and "@" in email:
                if not domain:
                    # Fall back to the address's own domain so contacts pasted
                    # in without a website still participate in deduplication.
                    domain = email.split("@")[-1]
                    touched_domains.add(domain)
                mx_valid = r.get("mx_valid")  # None = unchecked, 1 = valid, 0 = invalid
                conn.execute("""
                    INSERT INTO contacts(email,first_name,last_name,company,website,address,extra,status,mx_valid,domain)
                    VALUES(:email,:first_name,:last_name,:company,:website,:address,:extra,:status,:mx_valid,:domain)
                    ON CONFLICT(email) WHERE email IS NOT NULL AND email != '' DO UPDATE SET
                        first_name=COALESCE(NULLIF(excluded.first_name,''), contacts.first_name),
                        last_name=COALESCE(NULLIF(excluded.last_name,''),   contacts.last_name),
                        -- company is deliberately NOT overwritten when we
                        -- already have one: shared addresses (a dental group's
                        -- payments@ appearing under several practices) would
                        -- otherwise rename the contact to whichever business
                        -- was scraped last.
                        company=COALESCE(NULLIF(contacts.company,''),       excluded.company),
                        website=COALESCE(NULLIF(contacts.website,''),       excluded.website),
                        address=COALESCE(NULLIF(excluded.address,''),       contacts.address),
                        domain=COALESCE(NULLIF(contacts.domain,''),         excluded.domain),
                        mx_valid=COALESCE(excluded.mx_valid,                contacts.mx_valid)
                """, {
                    "email":      email,
                    "first_name": r.get("first_name", ""),
                    "last_name":  r.get("last_name", ""),
                    "company":    r.get("company", ""),
                    "website":    website,
                    "address":    r.get("address", ""),
                    "extra":      json.dumps(r.get("extra", {})),
                    "status":     status,
                    "mx_valid":   mx_valid,
                    "domain":     domain,
                })
                # Record the other business this address turned up under, so
                # the connection is not lost just because company was kept.
                _note_alternate_company(conn, email, r.get("company", ""))
                inserted += 1

            elif status in ("form_only", "no_email", "no_website"):
                # Prospect record — no email found. Match on the canonical
                # domain, not the raw URL, or one business becomes a row per
                # URL variant Maps happens to return.
                #
                # no_website rows have no domain to match on, so they dedupe on
                # the business name instead. They are kept rather than dropped:
                # for a web-design agency, "this business has no website" is
                # the strongest possible qualifying signal.
                if domain:
                    exists = conn.execute(
                        "SELECT id FROM contacts WHERE domain=? AND (email IS NULL OR email='')",
                        (domain,)
                    ).fetchone()
                elif r.get("company"):
                    exists = conn.execute(
                        "SELECT id FROM contacts WHERE company=? AND (email IS NULL OR email='')",
                        (r["company"],)
                    ).fetchone()
                else:
                    continue        # nothing to identify it by; skip

                if not exists:
                    conn.execute("""
                        INSERT INTO contacts(company,website,address,status,extra,domain)
                        VALUES(?,?,?,?,?,?)
                    """, (
                        r.get("company", ""), website,
                        r.get("address", ""), status,
                        json.dumps(r.get("extra", {})), domain,
                    ))
                    inserted += 1

        for domain in touched_domains:
            _pick_domain_winner(conn, domain)

        return inserted


def _note_alternate_company(conn, email: str, company: str):
    """
    Keep a record when one address is seen under a different business name.

    Group practices share a billing address, so the same email legitimately
    turns up under several listings. We keep the first company as the contact's
    name; this stops the others from being silently discarded.
    """
    company = (company or "").strip()
    if not company:
        return
    row = conn.execute(
        "SELECT id, company, extra FROM contacts WHERE email=?", (email,)
    ).fetchone()
    if not row or row["company"] == company:
        return
    try:
        extra = json.loads(row["extra"] or "{}")
    except Exception:
        extra = {}
    seen = extra.get("also_seen_at") or []
    if company not in seen and company != row["company"]:
        seen.append(company)
        extra["also_seen_at"] = seen[:10]
        conn.execute(
            "UPDATE contacts SET extra=? WHERE id=?", (json.dumps(extra), row["id"])
        )


def get_known_company_names() -> set:
    """
    Every company already stored, for the scraper's resume set.

    The Maps scraper dedupes by the business name shown on the listing, so
    this lets a new search skip businesses an earlier search already collected
    -- overlapping niches like "dentists" and "dental clinics" in one city
    otherwise re-scrape the same places from scratch.
    """
    with get_db() as conn:
        return {
            r["company"] for r in conn.execute(
                "SELECT DISTINCT company FROM contacts WHERE company != ''"
            ).fetchall()
        }


def get_contacts(limit=200, offset=0):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM contacts ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()]


def get_contact_by_email(email_addr: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM contacts WHERE email=?", (email_addr.lower(),)
        ).fetchone()
        return dict(row) if row else None


def delete_campaign(campaign_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM sends WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))


def delete_contacts(ids: list):
    if not ids:
        return
    placeholders = ','.join('?' for _ in ids)
    with get_db() as conn:
        conn.execute(f"DELETE FROM contacts WHERE id IN ({placeholders})", ids)


def create_contact(email: str, first_name='', last_name='', company='',
                   website='', address='', status='active'):
    email = email.strip().lower()
    if not email or '@' not in email:
        return None, 'Invalid email address'
    try:
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO contacts(email,first_name,last_name,company,website,address,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (email, first_name, last_name, company, website, address, status)
            )
            return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, 'A contact with that email already exists'


def update_contact(contact_id: int, fields: dict):
    allowed = {'email', 'first_name', 'last_name', 'company', 'website', 'address', 'status'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return True, None
    if 'email' in updates:
        updates['email'] = updates['email'].strip().lower()
    set_clause = ', '.join(f'{k}=?' for k in updates)
    try:
        with get_db() as conn:
            conn.execute(
                f'UPDATE contacts SET {set_clause} WHERE id=?',
                (*updates.values(), contact_id)
            )
        return True, None
    except sqlite3.IntegrityError:
        return False, 'A contact with that email already exists'


def delete_contact(contact_id: int):
    with get_db() as conn:
        conn.execute("UPDATE contacts SET status='deleted' WHERE id=?", (contact_id,))


def unsubscribe_contact(email):
    """Mark a contact unsubscribed and propagate to ALL their enrollments
    regardless of current status (a paused or replied enrollment must also
    stop sending if the contact opts out later)."""
    email_lc = email.lower()
    with get_db() as conn:
        conn.execute("UPDATE contacts SET status='unsubscribed' WHERE email=?", (email_lc,))
        conn.execute("""
            UPDATE enrollments SET status='unsubscribed'
            WHERE contact_id=(SELECT id FROM contacts WHERE email=?)
              AND status NOT IN ('unsubscribed','bounced','completed','replied')
        """, (email_lc,))


def get_unsubscribed_contacts():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT email, first_name, last_name, company, created_at
            FROM contacts
            WHERE status = 'unsubscribed'
            ORDER BY created_at DESC
        """).fetchall()]


def get_invalid_mx_contacts():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT email, company, website, address, created_at
            FROM contacts
            WHERE mx_valid = 0
            ORDER BY created_at DESC
        """).fetchall()]


def mark_bounced(email):
    with get_db() as conn:
        conn.execute("UPDATE contacts SET status='bounced' WHERE email=?", (email.lower(),))
        conn.execute("""
            UPDATE enrollments SET status='bounced'
            WHERE contact_id=(SELECT id FROM contacts WHERE email=?)
              AND status='queued'
        """, (email.lower(),))


def increment_soft_bounce(email: str, threshold: int = 3):
    """
    Increment soft-bounce counter for a contact.
    Once the counter hits threshold, treat it as a hard bounce.
    Everything runs in one transaction to avoid deadlocks.
    """
    email = email.lower()
    with get_db() as conn:
        conn.execute(
            "UPDATE contacts SET soft_bounce_count = soft_bounce_count + 1 WHERE email=?",
            (email,)
        )
        row = conn.execute(
            "SELECT soft_bounce_count FROM contacts WHERE email=?", (email,)
        ).fetchone()
        count = row["soft_bounce_count"] if row else 0

        if count >= threshold:
            conn.execute("UPDATE contacts SET status='bounced' WHERE email=?", (email,))
            conn.execute("""
                UPDATE enrollments SET status='bounced'
                WHERE contact_id=(SELECT id FROM contacts WHERE email=?)
                  AND status='queued'
            """, (email,))
            conn.execute(
                "INSERT INTO logs(level,message) VALUES(?,?)",
                ("WARN", f"⛔ Soft bounce threshold ({threshold}) reached — marked bounced: {email}")
            )
        else:
            conn.execute(
                "INSERT INTO logs(level,message) VALUES(?,?)",
                ("WARN", f"⚠ Soft bounce {count}/{threshold}: {email}")
            )


# ── Enrollments ───────────────────────────────────────────────────────────────

# ── Step Variants ─────────────────────────────────────────────────────────────

def get_step_variants(step_id: int):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM step_variants WHERE step_id=? ORDER BY label",
            (step_id,)
        ).fetchall()]


def save_step_variants(step_id: int, variants: list):
    """Replace all variants for a step. Pass empty list to clear (disable A/B)."""
    with get_db() as conn:
        conn.execute("DELETE FROM step_variants WHERE step_id=?", (step_id,))
        for v in variants:
            conn.execute("""
                INSERT INTO step_variants(step_id, label, subject, body_html, weight)
                VALUES(?,?,?,?,?)
            """, (step_id, v["label"], v["subject"], v["body_html"], int(v.get("weight", 50))))


def _pick_variant(variants: list):
    """Weighted random selection from a list of variant dicts. Returns label."""
    total = sum(v["weight"] for v in variants)
    if total <= 0:
        return variants[0]["label"]
    r = random.uniform(0, total)
    cumulative = 0
    for v in variants:
        cumulative += v["weight"]
        if r <= cumulative:
            return v["label"]
    return variants[-1]["label"]


def get_variant_stats(campaign_id: int):
    """Per-variant breakdown: enrolled, sent, replied, bounced."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                COALESCE(e.variant_label, 'default') as variant_label,
                COUNT(DISTINCT e.id)                  as enrolled,
                COUNT(s.id)                           as sent,
                COUNT(DISTINCT CASE WHEN e.status='replied'  THEN e.id END) as replied,
                COUNT(DISTINCT CASE WHEN e.status='bounced'  THEN e.id END) as bounced
            FROM enrollments e
            LEFT JOIN sends s ON s.campaign_id=e.campaign_id AND s.contact_id=e.contact_id
            WHERE e.campaign_id=?
            GROUP BY e.variant_label
            ORDER BY e.variant_label
        """, (campaign_id,)).fetchall()
        return [dict(r) for r in rows]


def get_campaign_contact_report(campaign_id: int):
    """One row per enrolled contact with send count — used for the report table and Excel export."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                e.id AS enroll_id,
                c.email,
                c.first_name,
                c.last_name,
                c.company,
                e.variant_label,
                e.status,
                e.current_step,
                e.next_send_at,
                e.enrolled_at,
                COUNT(s.id) AS steps_sent
            FROM enrollments e
            JOIN contacts c ON c.id = e.contact_id
            LEFT JOIN sends s
                   ON s.campaign_id = e.campaign_id
                  AND s.contact_id  = e.contact_id
            WHERE e.campaign_id = ?
            GROUP BY e.id
            ORDER BY e.enrolled_at DESC
        """, (campaign_id,)).fetchall()
        return [dict(r) for r in rows]


def enroll_contacts_bulk(campaign_id, contact_ids):
    """
    Enroll contacts, skipping any that would produce a duplicate approach.

    Returns (enrolled, skipped) where skipped explains why. The UNIQUE
    constraint only stops re-enrolling in the SAME campaign; nothing stopped a
    contact sitting in two campaigns at once and receiving two different cold
    pitches in overlapping windows, which reads as spam to the recipient and
    undoes the deliverability discipline the rest of the system maintains.
    """
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Determine variant weights from step 1 (if A/B is configured)
    with get_db() as conn:
        step1 = conn.execute(
            "SELECT id FROM steps WHERE campaign_id=? AND step_num=1", (campaign_id,)
        ).fetchone()
    variants = get_step_variants(step1["id"]) if step1 else []

    one_per_domain = get_settings().get("one_sequence_per_domain", "0") == "1"

    with get_db() as conn:
        enrolled = 0
        skipped = {"other_campaign": 0, "duplicate_address": 0, "same_domain": 0}

        for cid in contact_ids:
            try:
                row = conn.execute(
                    "SELECT id, domain, duplicate_of FROM contacts WHERE id=?", (cid,)
                ).fetchone()
                if not row:
                    continue

                # Suppressed as a duplicate address at a business we already
                # have a better contact for.
                if row["duplicate_of"] is not None:
                    skipped["duplicate_address"] += 1
                    continue

                # Already being worked by another campaign.
                busy = conn.execute("""
                    SELECT 1 FROM enrollments
                     WHERE contact_id=? AND campaign_id != ?
                       AND status NOT IN ('completed','replied','unsubscribed','bounced')
                     LIMIT 1
                """, (cid, campaign_id)).fetchone()
                if busy:
                    skipped["other_campaign"] += 1
                    continue

                # Optional stricter rule: one live sequence per business, not
                # per address, for operators who would rather under-contact.
                if one_per_domain and row["domain"]:
                    same_domain = conn.execute("""
                        SELECT 1 FROM enrollments e
                          JOIN contacts c ON c.id = e.contact_id
                         WHERE c.domain=? AND e.contact_id != ?
                           AND e.status NOT IN ('completed','replied','unsubscribed','bounced')
                         LIMIT 1
                    """, (row["domain"], cid)).fetchone()
                    if same_domain:
                        skipped["same_domain"] += 1
                        continue

                variant_label = _pick_variant(variants) if variants else None
                cur = conn.execute("""
                    INSERT OR IGNORE INTO enrollments
                        (campaign_id,contact_id,current_step,status,next_send_at,variant_label)
                    VALUES(?,?,1,'queued',?,?)
                """, (campaign_id, cid, now, variant_label))
                enrolled += cur.rowcount
            except Exception as e:
                logger.warning(f"Failed to enroll contact {cid}: {e}")

        return enrolled, skipped


def unenroll_contact(enroll_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM enrollments WHERE id=?", (enroll_id,))


def get_campaign_contacts(campaign_id):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT c.email, c.first_name, c.last_name, c.company, c.status as contact_status,
                   e.id as enroll_id, e.current_step, e.status, e.next_send_at, e.enrolled_at,
                   e.variant_label
            FROM contacts c
            JOIN enrollments e ON e.contact_id=c.id
            WHERE e.campaign_id=?
            ORDER BY e.enrolled_at DESC
        """, (campaign_id,)).fetchall()]


def get_due_enrollments(campaign_id, limit=20):
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT e.id as enroll_id, e.campaign_id, e.contact_id,
                   e.current_step, e.next_send_at, e.variant_label,
                   c.email, c.first_name, c.last_name, c.company, c.extra
            FROM enrollments e
            JOIN contacts c ON c.id=e.contact_id
            WHERE e.campaign_id=?
              AND e.status='queued'
              AND c.status='active'
              AND (e.next_send_at IS NULL OR e.next_send_at <= ?)
            ORDER BY e.next_send_at ASC NULLS FIRST
            LIMIT ?
        """, (campaign_id, now, limit)).fetchall()]


def advance_enrollment(enroll_id, next_step, next_send_at):
    with get_db() as conn:
        conn.execute("""
            UPDATE enrollments SET current_step=?, next_send_at=?, status='queued'
            WHERE id=?
        """, (next_step, next_send_at, enroll_id))


def complete_enrollment(enroll_id):
    with get_db() as conn:
        conn.execute("UPDATE enrollments SET status='completed' WHERE id=?", (enroll_id,))


def mark_enrollment_replied(campaign_id, contact_id):
    """Mark a (campaign, contact) enrollment as replied.

    Updates rows in ANY non-terminal state — including 'completed' (last step
    already sent) and 'paused' — so a reply that arrives after the sequence
    finishes still counts in reply-rate stats. Skips rows already in a final
    state ('replied', 'bounced', 'unsubscribed').

    Returns the number of rows actually updated, so callers can avoid logging
    duplicate "reply detected" messages every time the IMAP scan re-walks the
    same inbox message.
    """
    with get_db() as conn:
        cur = conn.execute("""
            UPDATE enrollments
               SET status='replied'
             WHERE campaign_id=? AND contact_id=?
               AND status NOT IN ('replied','bounced','unsubscribed')
        """, (campaign_id, contact_id))
        return cur.rowcount


def set_enrollment_status(enroll_id, status):
    allowed = {"queued", "paused", "replied", "completed"}
    if status not in allowed:
        raise ValueError(f"Invalid status: {status}")
    with get_db() as conn:
        conn.execute("UPDATE enrollments SET status=? WHERE id=?", (status, enroll_id))


# ── Sends & Counts ────────────────────────────────────────────────────────────

# ── SMTP Accounts ─────────────────────────────────────────────────────────────

def get_smtp_accounts():
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM smtp_accounts ORDER BY id"
        ).fetchall()]


def get_smtp_account(account_id: int):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM smtp_accounts WHERE id=?", (account_id,)).fetchone()
        return dict(r) if r else None


def create_smtp_account(data: dict) -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO smtp_accounts
                (name,email,from_name,smtp_host,smtp_port,smtp_user,smtp_pass,
                 imap_host,imap_user,imap_pass)
            VALUES(:name,:email,:from_name,:smtp_host,:smtp_port,:smtp_user,:smtp_pass,
                   :imap_host,:imap_user,:imap_pass)
        """, {
            "name":       data.get("name", ""),
            "email":      data.get("email", ""),
            "from_name":  data.get("from_name", ""),
            "smtp_host":  data.get("smtp_host", ""),
            "smtp_port":  int(data.get("smtp_port", 587)),
            "smtp_user":  data.get("smtp_user", ""),
            "smtp_pass":  data.get("smtp_pass", ""),
            "imap_host":  data.get("imap_host", ""),
            "imap_user":  data.get("imap_user", ""),
            "imap_pass":  data.get("imap_pass", ""),
        })
        return cur.lastrowid


def update_smtp_account(account_id: int, data: dict):
    allowed = {
        "name", "email", "from_name", "smtp_host", "smtp_port",
        "smtp_user", "smtp_pass", "imap_host", "imap_user", "imap_pass", "status",
    }
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with get_db() as conn:
        conn.execute(
            f"UPDATE smtp_accounts SET {set_clause} WHERE id=?",
            (*updates.values(), account_id)
        )


def delete_smtp_account(account_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM smtp_accounts WHERE id=?", (account_id,))


def get_campaign_smtp_accounts(campaign_id: int):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT a.* FROM smtp_accounts a
            JOIN campaign_accounts ca ON ca.account_id = a.id
            WHERE ca.campaign_id = ? ORDER BY a.id
        """, (campaign_id,)).fetchall()]


def set_campaign_smtp_accounts(campaign_id: int, account_ids: list):
    with get_db() as conn:
        conn.execute("DELETE FROM campaign_accounts WHERE campaign_id=?", (campaign_id,))
        for aid in account_ids:
            conn.execute(
                "INSERT INTO campaign_accounts(campaign_id,account_id) VALUES(?,?)",
                (campaign_id, aid)
            )


def get_next_account_for_campaign(campaign_id: int):
    """Round-robin through the accounts assigned to this campaign."""
    with get_db() as conn:
        accounts = conn.execute("""
            SELECT a.* FROM smtp_accounts a
            JOIN campaign_accounts ca ON ca.account_id = a.id
            WHERE ca.campaign_id = ? AND a.status = 'active'
            ORDER BY a.id
        """, (campaign_id,)).fetchall()

        if not accounts:
            return None
        if len(accounts) == 1:
            return dict(accounts[0])

        last = conn.execute("""
            SELECT account_id FROM sends
            WHERE campaign_id = ? AND account_id IS NOT NULL
            ORDER BY sent_at DESC LIMIT 1
        """, (campaign_id,)).fetchone()

        ids = [a["id"] for a in accounts]
        if not last or last["account_id"] not in ids:
            return dict(accounts[0])

        idx = ids.index(last["account_id"])
        return dict(accounts[(idx + 1) % len(ids)])


def log_send(campaign_id, contact_id, step_num, subject, msg_id, account_id=None):
    today = datetime.date.today().isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sends(campaign_id,contact_id,step_num,subject,msg_id,account_id)
            VALUES(?,?,?,?,?,?)
        """, (campaign_id, contact_id, step_num, subject, msg_id, account_id))
        conn.execute("""
            INSERT INTO daily_counts(date,count) VALUES(?,1)
            ON CONFLICT(date) DO UPDATE SET count=count+1
        """, (today,))


def get_today_count():
    today = datetime.date.today().isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT count FROM daily_counts WHERE date=?", (today,)
        ).fetchone()
        return row["count"] if row else 0


def get_bounce_rate(campaign_id):
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM enrollments WHERE campaign_id=?", (campaign_id,)
        ).fetchone()[0]
        bounced = conn.execute(
            "SELECT COUNT(*) FROM enrollments WHERE campaign_id=? AND status='bounced'",
            (campaign_id,)
        ).fetchone()[0]
        return (bounced / total * 100) if total > 0 else 0.0


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats(campaign_id=None):
    with get_db() as conn:
        q = ("WHERE campaign_id=?", (campaign_id,)) if campaign_id else ("", ())
        def cnt(table, cond=""):
            sql = f"SELECT COUNT(*) FROM {table} {q[0]} {cond}"
            return conn.execute(sql, q[1]).fetchone()[0]

        if campaign_id:
            total     = cnt("enrollments")
            sent      = cnt("sends")
            replied   = cnt("enrollments", "AND status='replied'")
            bounced   = cnt("enrollments", "AND status='bounced'")
            completed = cnt("enrollments", "AND status='completed'")
            queued    = cnt("enrollments", "AND status='queued'")
            sent_to   = conn.execute(
                "SELECT COUNT(DISTINCT contact_id) FROM sends WHERE campaign_id=?",
                (campaign_id,)
            ).fetchone()[0]
        else:
            total     = conn.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0]
            sent      = conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0]
            replied   = conn.execute("SELECT COUNT(*) FROM enrollments WHERE status='replied'").fetchone()[0]
            bounced   = conn.execute("SELECT COUNT(*) FROM enrollments WHERE status='bounced'").fetchone()[0]
            completed = conn.execute("SELECT COUNT(*) FROM enrollments WHERE status='completed'").fetchone()[0]
            queued    = conn.execute("SELECT COUNT(*) FROM enrollments WHERE status='queued'").fetchone()[0]
            sent_to   = conn.execute(
                "SELECT COUNT(DISTINCT contact_id) FROM sends"
            ).fetchone()[0]

        # reply_rate = % of people we emailed who replied (industry-standard definition)
        reply_rate = round(replied / sent_to * 100, 1) if sent_to > 0 else 0

        return {
            "total": total, "sent": sent, "replied": replied,
            "bounced": bounced, "completed": completed,
            "queued": queued, "today": get_today_count(),
            "reply_rate": reply_rate,
        }


# ── Activity Log ──────────────────────────────────────────────────────────────

def add_log(message, level="INFO"):
    with get_db() as conn:
        conn.execute("INSERT INTO logs(level,message) VALUES(?,?)", (level, message))


def get_logs(limit=50):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM logs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()]


# ── Users & Auth ──────────────────────────────────────────────────────────────
#
# Password hash format:
#   pbkdf2_sha256$<iterations>$<salt_hex>$<key_hex>     ← current
#   <salt_hex>:<key_hex>                                ← legacy (assume 260k)
#
# Verifying a legacy hash returns success but the caller is expected to call
# maybe_rehash() to upgrade it to the current format.

_PBKDF2_ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-SHA256


def _hash_password(password: str, iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = _os.urandom(32)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${key.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        if stored.startswith("pbkdf2_sha256$"):
            _, iter_str, salt_hex, key_hex = stored.split("$", 3)
            iterations = int(iter_str)
        elif ":" in stored:
            # Legacy two-part format from the initial release.
            salt_hex, key_hex = stored.split(":", 1)
            iterations = 260_000
        else:
            return False
        salt = bytes.fromhex(salt_hex)
        key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return _hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


def _hash_needs_upgrade(stored: str) -> bool:
    """True if the stored hash is from an older format or weaker parameters."""
    if not stored or not stored.startswith("pbkdf2_sha256$"):
        return True
    try:
        _, iter_str, _, _ = stored.split("$", 3)
        return int(iter_str) < _PBKDF2_ITERATIONS
    except Exception:
        return True


def create_user(username: str, password: str, is_admin: bool = False) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES(?,?,?)",
            (username.strip().lower(), _hash_password(password), 1 if is_admin else 0),
        )
        return cur.lastrowid


def get_user_by_username(username: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (username.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(uid: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None


def list_users():
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
        ).fetchall()]


def delete_user(uid: int):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (uid,))


def change_password(uid: int, new_password: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (_hash_password(new_password), uid),
        )


def verify_user_password(user: dict, password: str) -> bool:
    """Check a password against a user row. Constant-time."""
    if not user or not password:
        return False
    return _verify_password(password, user.get("password_hash", ""))


def authenticate(username: str, password: str):
    """Return user dict if credentials valid, else None.
    Transparently re-hashes legacy or weaker-parameter passwords on success."""
    user = get_user_by_username(username)
    if not user:
        return None
    stored = user["password_hash"]
    if not _verify_password(password, stored):
        return None
    if _hash_needs_upgrade(stored):
        try:
            change_password(user["id"], password)
            logger.info("Upgraded password hash for uid=%s", user["id"])
        except Exception as exc:
            logger.warning("Hash upgrade failed for uid=%s: %s", user["id"], exc)
    return user


def user_count() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def seed_admin_from_env():
    """On first run, create an admin account from ADMIN_PASS env var if set."""
    if user_count() > 0:
        return
    password = _os.environ.get("ADMIN_PASS", "")
    username  = _os.environ.get("ADMIN_USER", "admin")
    if password:
        if len(password) < 12:
            logger.warning("ADMIN_PASS is shorter than 12 chars; refusing to seed admin.")
            return
        create_user(username, password, is_admin=True)
        logger.info(f"Created admin user '{username}' from ADMIN_PASS env var")
