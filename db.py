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

            -- Every call attempt, append-only. The contact's current state is
            -- denormalized onto contacts (call_status, next_call_at,
            -- call_attempts) so the queue query stays a single indexed scan,
            -- but the history is what makes "no answer Tue, voicemail Thu,
            -- booked Mon" visible -- and that sequence is the thing you want
            -- in front of you before dialling someone a fourth time.
            CREATE TABLE IF NOT EXISTS call_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                outcome      TEXT    NOT NULL,
                notes        TEXT    NOT NULL DEFAULT '',
                next_call_at TEXT,
                called_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- Sections are JSON [{title, body}] rather than columns: the parts
            -- of a call script are the operator's to name and reorder, and a
            -- fixed schema would decide that for them.
            CREATE TABLE IF NOT EXISTS call_scripts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL DEFAULT 'Default script',
                sections   TEXT    NOT NULL DEFAULT '[]',
                is_active  INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
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
            # Which weekdays this campaign may send on, as Python weekday
            # numbers (Monday=0 ... Sunday=6). The gate used to hardcode
            # "weekday() >= 5", which is only the Western weekend -- a campaign
            # aimed at the Gulf or the Levant, where the working week is
            # Sunday to Thursday, would sit idle on its two busiest days and
            # send on the two nobody is working. Defaults to Mon-Fri so
            # existing campaigns keep behaving exactly as before.
            "ALTER TABLE campaigns ADD COLUMN send_days TEXT NOT NULL DEFAULT '0,1,2,3,4'",
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
            # Promoted out of the `extra` JSON blob: the scraper always reads
            # these off the Maps detail panel, and they're what make a lead
            # worth qualifying by hand (a bad phone number or a 2-star rating
            # says more than the email address does).
            "ALTER TABLE contacts ADD COLUMN phone TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE contacts ADD COLUMN category TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE contacts ADD COLUMN rating REAL DEFAULT NULL",
            "ALTER TABLE contacts ADD COLUMN review_count INTEGER DEFAULT NULL",
            # Which scrape found this lead -- powers the "lead list" filter in
            # Contacts. Deliberately NOT a foreign key: leads must outlive the
            # scrape_jobs row that produced them, and a pruned job should
            # degrade to an "unknown source" label, not block the delete or
            # orphan the contact. NULL means manually added or CSV-imported.
            "ALTER TABLE contacts ADD COLUMN source_job_id INTEGER DEFAULT NULL",
            # Comparable form of `phone`. Maps hands the same number back as
            # "+1 709-555-0123", "(709) 555-0123" and "709.555.0123", so the
            # raw column can never answer "have I already dialled this
            # business". Kept beside the original rather than replacing it --
            # the display value is what you want on screen.
            "ALTER TABLE contacts ADD COLUMN phone_normalized TEXT NOT NULL DEFAULT ''",
            # Current calling state, denormalized from call_log so the queue is
            # one indexed scan rather than a correlated subquery per contact.
            # '' means never called, which is what puts a lead in the new pile.
            "ALTER TABLE contacts ADD COLUMN call_status TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE contacts ADD COLUMN next_call_at TEXT DEFAULT NULL",
            "ALTER TABLE contacts ADD COLUMN call_attempts INTEGER NOT NULL DEFAULT 0",
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
                    duplicate_of      INTEGER DEFAULT NULL,
                    phone             TEXT NOT NULL DEFAULT '',
                    category          TEXT NOT NULL DEFAULT '',
                    rating            REAL DEFAULT NULL,
                    review_count      INTEGER DEFAULT NULL,
                    source_job_id     INTEGER DEFAULT NULL,
                    phone_normalized  TEXT NOT NULL DEFAULT '',
                    call_status       TEXT NOT NULL DEFAULT '',
                    next_call_at      TEXT DEFAULT NULL,
                    call_attempts     INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO contacts_new
                    SELECT id, email, first_name, last_name, company, extra, status,
                           created_at, COALESCE(website,''), COALESCE(address,''),
                           COALESCE(soft_bounce_count, 0), mx_valid,
                           COALESCE(domain,''), duplicate_of,
                           COALESCE(phone,''), COALESCE(category,''),
                           rating, review_count, source_job_id,
                           COALESCE(phone_normalized,''), COALESCE(call_status,''),
                           next_call_at, COALESCE(call_attempts, 0)
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
            "CREATE INDEX IF NOT EXISTS logs_created_at_idx     ON logs(created_at)",
            "CREATE INDEX IF NOT EXISTS contacts_source_job_idx ON contacts(source_job_id) WHERE source_job_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS contacts_created_at_idx ON contacts(created_at)",
            # Backs has_sent_step, which runs once per email before sending.
            "CREATE INDEX IF NOT EXISTS sends_dedupe_idx ON sends(campaign_id, contact_id, step_num)",
            "CREATE INDEX IF NOT EXISTS contacts_phone_idx ON contacts(phone_normalized) WHERE phone_normalized != ''",
            "CREATE INDEX IF NOT EXISTS contacts_next_call_idx ON contacts(next_call_at) WHERE next_call_at IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS contacts_call_status_idx ON contacts(call_status)",
            "CREATE INDEX IF NOT EXISTS call_log_contact_idx ON call_log(contact_id, called_at)",
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

        # Repair leads suppressed by treating a freemail domain as a business.
        # Contacts with no website fell back to the address's own domain, so
        # every gmail.com lead was arbitrated against every other one and all
        # but the first were marked duplicate_of and refused at enrollment.
        # Clear the domain (they dedupe on the exact address instead) and lift
        # the suppression, or those leads stay silently unmailable.
        try:
            placeholders = ",".join("?" * len(FREEMAIL_DOMAINS))
            freed = conn.execute(f"""
                UPDATE contacts
                   SET domain = '', duplicate_of = NULL
                 WHERE domain IN ({placeholders})
            """, tuple(FREEMAIL_DOMAINS)).rowcount
            if freed:
                logger.info(
                    "Freed %d contact(s) that were suppressed as freemail-domain "
                    "duplicates", freed,
                )
        except Exception as exc:
            logger.warning("Freemail suppression repair skipped: %s", exc)

        # Comparable phone for rows scraped before the column existed, so
        # "have I already dialled this business" works on the list you already
        # have rather than only on the next scrape.
        try:
            todo = conn.execute(
                "SELECT id, phone FROM contacts WHERE phone_normalized = '' AND phone != ''"
            ).fetchall()
            for row in todo:
                key = normalize_phone(row["phone"])
                if key:
                    conn.execute(
                        "UPDATE contacts SET phone_normalized=? WHERE id=?", (key, row["id"])
                    )
            if todo:
                logger.info("Normalized phone for %d contact(s)", len(todo))
        except Exception as exc:
            logger.warning("Phone normalization backfill skipped: %s", exc)

        # Every step owns at least one variant, and its copy lives there.
        #
        # Copy used to live in two places at once: steps.subject/body_html plus
        # an optional set of variants. A two-arm test therefore showed three
        # editors, and the reporting showed three arms -- A, B and a phantom
        # 'default' holding whoever was enrolled before the variants existed.
        # Promoting the base copy to variant A makes the arms and the editors
        # the same set of things. The base columns stay populated and in sync
        # (see save_step_variants) so any send that cannot resolve a label
        # still has copy to fall back on.
        try:
            orphans = conn.execute("""
                SELECT s.id, s.subject, s.body_html
                  FROM steps s
                  LEFT JOIN step_variants v ON v.step_id = s.id
                 WHERE v.id IS NULL
            """).fetchall()
            for s in orphans:
                conn.execute("""
                    INSERT INTO step_variants(step_id, label, subject, body_html, weight)
                    VALUES(?, 'A', ?, ?, 100)
                """, (s["id"], s["subject"] or "", s["body_html"] or ""))
            if orphans:
                logger.info("Promoted base copy to variant A for %d step(s)", len(orphans))
        except Exception as exc:
            logger.warning("Step variant promotion skipped: %s", exc)

        # Backfill phone/category/rating/review_count for rows imported before
        # these had their own columns -- they're sitting in `extra` from a CSV
        # import (the scraper's CSV writes a "reviews" column; the DB column is
        # review_count to read better next to rating).
        try:
            todo = conn.execute("""
                SELECT id, extra FROM contacts
                WHERE phone = '' AND category = '' AND rating IS NULL
                  AND review_count IS NULL AND extra != '{}'
            """).fetchall()
            backfilled = 0
            for row in todo:
                try:
                    extra = json.loads(row["extra"] or "{}")
                except Exception:
                    continue
                if not any(k in extra for k in ("phone", "category", "rating", "reviews")):
                    continue
                rating = None
                try:
                    rating = float(extra["rating"]) if extra.get("rating") not in (None, "") else None
                except (TypeError, ValueError):
                    pass
                review_count = None
                try:
                    review_count = int(extra["reviews"]) if extra.get("reviews") not in (None, "") else None
                except (TypeError, ValueError):
                    pass
                conn.execute(
                    "UPDATE contacts SET phone=?, category=?, rating=?, review_count=? WHERE id=?",
                    (extra.get("phone", ""), extra.get("category", ""),
                     rating, review_count, row["id"]),
                )
                backfilled += 1
            if backfilled:
                logger.info("Backfilled phone/category/rating for %d contact(s)", backfilled)
        except Exception as exc:
            logger.warning("Phone/category/rating backfill skipped: %s", exc)


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


def _seconds_since(ts_str):
    """Seconds between now (UTC) and a 'YYYY-MM-DD HH:MM:SS' timestamp, or None."""
    if not ts_str:
        return None
    try:
        then = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    return max(0.0, (datetime.datetime.utcnow() - then).total_seconds())


def worker_seconds_since_seen():
    """Seconds since any worker last checked in, or None if never."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='_worker_last_seen'"
        ).fetchone()
    return _seconds_since(row["value"] if row else None)


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
                    min_delay=45, max_delay=120, timezone=None, variables='{}',
                    send_days='0,1,2,3,4'):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns(name,daily_limit,send_start_hour,send_end_hour,"
            "min_delay_secs,max_delay_secs,timezone,variables,send_days) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, daily_limit, start_hour, end_hour, min_delay, max_delay,
             timezone, variables, send_days)
        )
        return cur.lastrowid


def update_campaign(cid, **fields):
    allowed = {"name", "daily_limit", "send_start_hour", "send_end_hour",
               "min_delay_secs", "max_delay_secs", "bounce_pause_pct", "status",
               "timezone", "variables", "send_days"}
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
    """
    Create or update a step, keeping its single-arm variant in step.

    A step always owns at least one variant: copy lives there, so the editor
    and the reporting describe the same set of arms. The startup migration only
    covers steps that already existed, so creating one here has to establish
    the same invariant -- and editing a one-arm step's copy has to update that
    arm too, or the step and its only variant drift apart and which text goes
    out depends on whether a label happens to resolve.

    Steps with a real A/B split are left alone; save_step_variants owns those.
    """
    with get_db() as conn:
        conn.execute("""
            INSERT INTO steps(campaign_id,step_num,subject,body_html,delay_days)
            VALUES(?,?,?,?,?)
            ON CONFLICT(campaign_id,step_num) DO UPDATE SET
                subject=excluded.subject,
                body_html=excluded.body_html,
                delay_days=excluded.delay_days
        """, (campaign_id, step_num, subject, body_html, delay_days))

        step = conn.execute(
            "SELECT id FROM steps WHERE campaign_id=? AND step_num=?",
            (campaign_id, step_num),
        ).fetchone()
        if not step:
            return

        existing = conn.execute(
            "SELECT id FROM step_variants WHERE step_id=? ORDER BY label", (step["id"],)
        ).fetchall()
        if not existing:
            conn.execute("""
                INSERT INTO step_variants(step_id, label, subject, body_html, weight)
                VALUES(?, 'A', ?, ?, 100)
            """, (step["id"], subject, body_html))
        elif len(existing) == 1:
            conn.execute(
                "UPDATE step_variants SET subject=?, body_html=? WHERE id=?",
                (subject, body_html, existing[0]["id"]),
            )


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


# Consumer mailbox providers. The domain of an address is used as a stand-in
# for "which business is this?", which holds for smithdental.ca and collapses
# badly for gmail.com: every Gmail lead would be treated as one business and
# all but one silently suppressed.
#
# That is worst exactly where it hurts most -- a business with no website is
# the strongest lead for a web-design offer, and it is also the one with no
# domain of its own to identify it by, so it falls back to whatever freemail
# address it publishes.
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com",
    "yahoo.com", "yahoo.co.uk", "yahoo.ca", "ymail.com", "rocketmail.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "proton.me", "pm.me",
    "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com",
    "fastmail.com", "hushmail.com", "tutanota.com", "tuta.io",
    "bell.net", "rogers.com", "shaw.ca", "telus.net", "sympatico.ca",
    "nf.aibn.com", "bellaliant.com", "bellaliant.net",
})


def is_freemail(domain: str) -> bool:
    """True when a domain identifies a mailbox provider, not a business."""
    return (domain or "").strip().lower() in FREEMAIL_DOMAINS


# Legal suffixes and punctuation carry no identity: "Paradise Dental Care Inc."
# and "Paradise Dental Care" are one business, and a scrape will produce both.
_COMPANY_NOISE = {
    "inc", "inc.", "incorporated", "ltd", "ltd.", "limited", "llc", "llp",
    "corp", "corp.", "corporation", "co", "co.", "company", "plc", "pc",
    "professional", "the", "and", "&",
}


def normalize_phone(raw: str) -> str:
    """
    Reduce a phone number to something comparable.

    Google Maps returns the same number as "+1 709-555-0123", "(709) 555-0123"
    and "709.555.0123" depending on the listing, so the raw string can never
    answer "have I already dialled this business". Digits only, and for North
    American numbers the trailing ten -- which drops a leading 1 country code
    so the two forms of the same number match.

    Deliberately not a full E.164 parser: that needs a phone-number library and
    a country to resolve against, and this is aimed at NANP lists. Numbers
    shorter than seven digits are treated as unusable rather than guessed at.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 7:
        return ""
    return digits[-10:] if len(digits) >= 10 else digits


def normalize_company(raw: str) -> str:
    """
    Comparable form of a business name, for leads with no domain to match on.

    Weaker than a phone or a domain and never used alone -- see
    find_existing_business, which pairs it with locality. On its own it would
    merge every "Main Street Dental" in the country.
    """
    cleaned = re.sub(r"[^a-z0-9\s]", " ", (raw or "").lower())
    words = [w for w in cleaned.split() if w and w not in _COMPANY_NOISE]
    return " ".join(words)


def _locality_key(address: str) -> str:
    """
    A rough locality from a scraped address, for disambiguating company names.

    Maps addresses are unstructured, so this takes the longest alphabetic
    fragment after the street line -- usually the city. Crude, but it only has
    to separate St John's from Toronto, not parse an address properly.
    """
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) < 2:
        return ""
    candidates = [re.sub(r"[^a-z\s]", "", p.lower()).strip() for p in parts[1:]]
    candidates = [c for c in candidates if len(c) > 2]
    return max(candidates, key=len) if candidates else ""


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
    if not domain or is_freemail(domain):
        # Freemail is never a business identity -- arbitrating a "winner"
        # across gmail.com would suppress every Gmail lead but one. Guarded
        # here as well as at the call site so legacy rows that already carry a
        # freemail domain cannot resurrect the bug.
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
                    #
                    # Freemail is excluded: gmail.com identifies a mailbox
                    # provider, not a business, and treating it as an identity
                    # collapsed every Gmail lead into one enrollable contact.
                    # Such rows keep an empty domain and dedupe on the exact
                    # address instead, via the contacts_email_unique index.
                    candidate = email.split("@")[-1]
                    if not is_freemail(candidate):
                        domain = candidate
                        touched_domains.add(domain)
                mx_valid = r.get("mx_valid")  # None = unchecked, 1 = valid, 0 = invalid
                conn.execute("""
                    INSERT INTO contacts(email,first_name,last_name,company,website,address,extra,status,mx_valid,domain,phone,phone_normalized,category,rating,review_count,source_job_id)
                    VALUES(:email,:first_name,:last_name,:company,:website,:address,:extra,:status,:mx_valid,:domain,:phone,:phone_normalized,:category,:rating,:review_count,:source_job_id)
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
                        mx_valid=COALESCE(excluded.mx_valid,                contacts.mx_valid),
                        phone=COALESCE(NULLIF(contacts.phone,''),           excluded.phone),
                        phone_normalized=COALESCE(NULLIF(contacts.phone_normalized,''), excluded.phone_normalized),
                        category=COALESCE(NULLIF(contacts.category,''),     excluded.category),
                        rating=COALESCE(excluded.rating,                    contacts.rating),
                        review_count=COALESCE(excluded.review_count,        contacts.review_count),
                        -- First scrape that found this lead wins, so a business
                        -- turning up again in a later search stays filed under
                        -- the list you originally built.
                        source_job_id=COALESCE(contacts.source_job_id,      excluded.source_job_id)
                """, {
                    "email":         email,
                    "first_name":    r.get("first_name", ""),
                    "last_name":     r.get("last_name", ""),
                    "company":       r.get("company", ""),
                    "website":       website,
                    "address":       r.get("address", ""),
                    "extra":         json.dumps(r.get("extra", {})),
                    "status":        status,
                    "mx_valid":      mx_valid,
                    "domain":        domain,
                    "phone":         r.get("phone", ""),
                    "phone_normalized": normalize_phone(r.get("phone", "")),
                    "category":      r.get("category", ""),
                    "rating":        r.get("rating") or None,
                    "review_count":  r.get("review_count") or None,
                    "source_job_id": r.get("source_job_id") or None,
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
                        INSERT INTO contacts(company,website,address,status,extra,domain,phone,phone_normalized,category,rating,review_count,source_job_id)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        r.get("company", ""), website,
                        r.get("address", ""), status,
                        json.dumps(r.get("extra", {})), domain,
                        r.get("phone", ""), normalize_phone(r.get("phone", "")),
                        r.get("category", ""),
                        r.get("rating") or None, r.get("review_count") or None,
                        r.get("source_job_id") or None,
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


# ── Contacts: server-side paging, filtering and the lead-list ("cabinet") view ─
#
# The Contacts tab used to pull every row and filter in the browser, hard-capped
# at 500. Past that it silently showed only the newest 500 -- which a per-scrape
# filter would then narrow further, under-reporting a list with no warning. All
# filtering therefore happens in SQL now, against the whole table.

# Whitelist: sort_col is interpolated into the SQL string, so it can never come
# straight from the query string.
_CONTACT_SORT_COLUMNS = frozenset({
    "id", "email", "first_name", "last_name", "company", "website", "address",
    "status", "created_at", "phone", "category", "rating", "review_count",
    "domain", "mx_valid",
})

# Search covers what someone would plausibly type looking for a lead.
_CONTACT_SEARCH_COLUMNS = (
    "email", "first_name", "last_name", "company",
    "website", "address", "phone", "category",
)

# Sentinel for "added by hand or CSV import, not by any scrape".
SOURCE_MANUAL = "manual"


def _contact_filters(q="", source_job_id=None, status=None, include_deleted=False):
    """Build the shared WHERE clause for the contact list views."""
    clauses, params = [], []

    if not include_deleted:
        clauses.append("status != 'deleted'")

    if status:
        clauses.append("status = ?")
        params.append(status)

    if source_job_id is not None and source_job_id != "":
        if str(source_job_id) == SOURCE_MANUAL:
            clauses.append("source_job_id IS NULL")
        else:
            clauses.append("source_job_id = ?")
            params.append(int(source_job_id))

    q = (q or "").strip()
    if q:
        like = " OR ".join(f'COALESCE("{c}",\'\') LIKE ?' for c in _CONTACT_SEARCH_COLUMNS)
        clauses.append(f"({like})")
        params.extend([f"%{q}%"] * len(_CONTACT_SEARCH_COLUMNS))

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_contacts_page(page=1, per_page=50, q="", source_job_id=None, status=None,
                      include_deleted=False, sort_col="", sort_dir="desc"):
    """One page of contacts plus the total matching the same filter."""
    page     = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 50), 500))
    offset   = (page - 1) * per_page

    sort_dir = "asc" if str(sort_dir).lower() == "asc" else "desc"
    if sort_col in _CONTACT_SORT_COLUMNS:
        # NULLs and '' sort last either way, so an empty phone column doesn't
        # push the rows you actually want to the top of an ascending sort.
        order_by = f'NULLIF("{sort_col}", \'\') IS NULL, "{sort_col}" {sort_dir.upper()}'
    else:
        sort_col = ""
        order_by = "created_at DESC, id DESC"

    where, params = _contact_filters(q, source_job_id, status, include_deleted)

    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM contacts {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM contacts {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()

    return {
        "rows":     [dict(r) for r in rows],
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "sort_col": sort_col,
        "sort_dir": sort_dir,
    }


def get_contact_ids_matching(q="", source_job_id=None, status=None, include_deleted=False):
    """
    Every contact id matching a filter, ignoring paging.

    Backs "select all N matching" -- without it, select-all could only ever
    reach the rows on screen, so a bulk delete over a filtered list would
    silently act on one page's worth.
    """
    where, params = _contact_filters(q, source_job_id, status, include_deleted)
    with get_db() as conn:
        return [r["id"] for r in conn.execute(
            f"SELECT id FROM contacts {where}", params
        ).fetchall()]


def get_contact_sources():
    """
    The lead lists: one entry per scrape that produced contacts, newest first,
    plus a 'manual' bucket for hand-added and CSV-imported rows.

    LEFT JOIN, not a foreign key -- a contact whose scrape_jobs row has gone
    still counts, it just shows as an unknown source rather than vanishing
    from the filter.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT c.source_job_id           AS job_id,
                   j.niche                   AS niche,
                   j.city                    AS city,
                   j.created_at              AS scraped_at,
                   COUNT(*)                  AS count
              FROM contacts c
              LEFT JOIN scrape_jobs j ON j.id = c.source_job_id
             WHERE c.status != 'deleted'
             GROUP BY c.source_job_id
             ORDER BY (c.source_job_id IS NULL), c.source_job_id DESC
        """).fetchall()

    out = []
    for r in rows:
        if r["job_id"] is None:
            label = "Added manually / CSV"
        elif r["niche"] or r["city"]:
            date = (r["scraped_at"] or "")[:10]
            label = f"{r['niche']} — {r['city']}" + (f" · {date}" if date else "")
        else:
            label = f"Scrape #{r['job_id']} (details deleted)"
        out.append({
            "job_id": r["job_id"] if r["job_id"] is not None else SOURCE_MANUAL,
            "label":  label,
            "count":  r["count"],
        })
    return out


def get_contact(contact_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        return dict(row) if row else None


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
    """
    Replace all variants for a step.

    A step always keeps at least one variant: passing an empty list leaves the
    step's own subject/body as variant A rather than deleting the only copy the
    step has. The first variant is also mirrored back onto steps.subject /
    steps.body_html, so the fallback used when a label cannot be resolved is
    real copy and not a stale earlier draft.
    """
    with get_db() as conn:
        if not variants:
            row = conn.execute(
                "SELECT subject, body_html FROM steps WHERE id=?", (step_id,)
            ).fetchone()
            variants = [{
                "label":     "A",
                "subject":   (row["subject"] if row else "") or "",
                "body_html": (row["body_html"] if row else "") or "",
                "weight":    100,
            }]

        conn.execute("DELETE FROM step_variants WHERE step_id=?", (step_id,))
        for v in variants:
            conn.execute("""
                INSERT INTO step_variants(step_id, label, subject, body_html, weight)
                VALUES(?,?,?,?,?)
            """, (step_id, v["label"], v["subject"], v["body_html"], int(v.get("weight", 50))))

        first = variants[0]
        conn.execute(
            "UPDATE steps SET subject=?, body_html=? WHERE id=?",
            (first.get("subject", ""), first.get("body_html", ""), step_id),
        )


def get_campaign_variants(campaign_id: int):
    """
    The arms of this campaign: every label any step defines, weighted by the
    earliest step that defines it.

    Enrollment used to draw only from step 1, so a variant added to a later
    step was inert -- no contact ever carried its label, so it could never be
    sent and the test quietly measured nothing. Taking the union lets a later
    step be tested on its own: those contacts just receive the default copy for
    the steps that do not define their label.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT v.label, v.weight, s.step_num
              FROM step_variants v
              JOIN steps s ON s.id = v.step_id
             WHERE s.campaign_id = ?
             ORDER BY s.step_num ASC, v.label ASC
        """, (campaign_id,)).fetchall()

    by_step = {}
    for r in rows:
        by_step.setdefault(r["step_num"], {})[r["label"]] = r["weight"]
    if not by_step:
        return []

    # Weights come from the step that defines the most arms, because that is
    # where the split was actually configured. Taking each label's first
    # appearance instead let a single-arm step 1 contribute A at weight 100
    # against a B of 50 defined on step 2 -- a 50/50 the operator set up would
    # have run at 67/33.
    widest = min(by_step.items(), key=lambda kv: (-len(kv[1]), kv[0]))[1]

    arms, seen = [], set()
    for label, weight in sorted(widest.items()):
        arms.append({"label": label, "weight": weight})
        seen.add(label)
    # A label defined only on some other step still counts as an arm; it keeps
    # its own weight rather than being dropped and made unreachable.
    for step_num in sorted(by_step):
        for label, weight in sorted(by_step[step_num].items()):
            if label not in seen:
                arms.append({"label": label, "weight": weight})
                seen.add(label)
    return arms


def assign_missing_variants(campaign_id: int) -> int:
    """
    Give a variant to enrollments that never got one. Returns how many.

    A variant is drawn at enrollment, so contacts enrolled before the variants
    existed carry no label and would receive the fallback copy for the whole
    sequence -- silently excluded from the test they appear to be part of.
    Activation is when the campaign's copy is final, so fill the gaps there.

    Two deliberate limits:

    * The draw is weighted, not "everyone defaults to A". Dropping every
      unassigned contact into one arm would load it with all the pre-existing
      contacts and the arms would stop being comparable.
    * Only untouched enrollments are eligible. Someone already mid-sequence
      keeps whatever they have: switching arms between steps of one thread
      changes the voice or offer mid-conversation, and it would file their
      earlier sends under the wrong arm.
    """
    variants = get_campaign_variants(campaign_id)
    if len(variants) < 2:
        # One arm is not a test; leaving the label NULL keeps the fallback path
        # and avoids writing a label that means nothing.
        return 0

    with get_db() as conn:
        rows = conn.execute("""
            SELECT e.id FROM enrollments e
             WHERE e.campaign_id = ?
               AND (e.variant_label IS NULL OR e.variant_label = '')
               AND e.status = 'queued'
               AND NOT EXISTS (
                     SELECT 1 FROM sends s
                      WHERE s.campaign_id = e.campaign_id
                        AND s.contact_id  = e.contact_id
               )
        """, (campaign_id,)).fetchall()

        for r in rows:
            conn.execute(
                "UPDATE enrollments SET variant_label=? WHERE id=?",
                (_pick_variant(variants), r["id"]),
            )
        return len(rows)


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


# Contact columns usable as {{variables}}, in the order they are most likely to
# be wanted. Must stay in step with what get_due_enrollments selects and what
# sender._render exposes -- a name here that the send query does not fetch would
# advertise a variable that renders as nothing.
TEMPLATE_VARIABLES = [
    ("first_name",   "First name"),
    ("last_name",    "Last name"),
    ("full_name",    "First + last"),
    ("company",      "Company / business name"),
    ("email",        "Email address"),
    ("phone",        "Phone number"),
    ("website",      "Website"),
    ("category",     "Business category"),
    ("rating",       "Google rating"),
    ("review_count", "Number of reviews"),
    ("address",      "Street address"),
]


def get_variable_coverage(campaign_id: int = None):
    """
    How many contacts actually have a value for each template variable.

    Scoped to a campaign's enrolled contacts when given one, because that is
    the population the copy will reach: a database that is 21% first-name on
    the strength of a few hand-added rows is still 0% for a campaign built
    entirely from a scrape. Falls back to every active contact when the
    campaign has nobody enrolled yet, so the panel is useful while drafting.

    Returns rows of {key, label, filled, total, scope}.
    """
    scope = "campaign"
    where = """
        WHERE c.id IN (SELECT contact_id FROM enrollments WHERE campaign_id = ?)
    """
    params = [campaign_id]

    with get_db() as conn:
        if campaign_id is not None:
            total = conn.execute(
                f"SELECT COUNT(*) FROM contacts c {where}", params
            ).fetchone()[0]
        else:
            total = 0

        if not total:
            scope  = "all"
            where  = "WHERE c.status NOT IN ('deleted','unsubscribed','bounced')"
            params = []
            total  = conn.execute(
                f"SELECT COUNT(*) FROM contacts c {where}", params
            ).fetchone()[0]

        if not total:
            return {"scope": scope, "total": 0, "variables": [
                {"key": k, "label": lbl, "filled": 0, "total": 0}
                for k, lbl in TEMPLATE_VARIABLES
            ]}

        # full_name is derived rather than stored, so it counts as present when
        # either half is.
        pieces = []
        for key, _ in TEMPLATE_VARIABLES:
            if key == "full_name":
                expr = ("(COALESCE(NULLIF(TRIM(c.first_name),''),"
                        " NULLIF(TRIM(c.last_name),'')) IS NOT NULL)")
            elif key in ("rating", "review_count"):
                expr = f"(c.{key} IS NOT NULL)"
            else:
                expr = f"(NULLIF(TRIM(COALESCE(c.{key},'')),'') IS NOT NULL)"
            pieces.append(f"SUM(CASE WHEN {expr} THEN 1 ELSE 0 END) AS {key}")

        row = conn.execute(
            f"SELECT {', '.join(pieces)} FROM contacts c {where}", params
        ).fetchone()

    return {
        "scope": scope,
        "total": total,
        "variables": [
            {"key": key, "label": label, "filled": row[key] or 0, "total": total}
            for key, label in TEMPLATE_VARIABLES
        ],
    }


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

    # Every label the campaign defines, not just step 1's -- see
    # get_campaign_variants. One arm is not a test, so leave the label NULL and
    # let the send fall back to the step's own copy.
    variants = get_campaign_variants(campaign_id)
    if len(variants) < 2:
        variants = []

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
                # Freemail is exempt: "one per domain" across gmail.com would
                # mean one Gmail lead in flight at a time, across the whole
                # database.
                if one_per_domain and row["domain"] and not is_freemail(row["domain"]):
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
                   c.email, c.first_name, c.last_name, c.company, c.extra,
                   -- Available as {{phone}}, {{category}} and so on. These
                   -- used to ride along inside `extra`; promoting them to real
                   -- columns emptied that blob, so leaving them out here would
                   -- silently retire template variables that already worked.
                   c.phone, c.category, c.rating, c.review_count,
                   c.website, c.address
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


def find_existing_business(conn, phone="", website="", company="", address="",
                           exclude_id=None):
    """
    Find a contact already representing this business. Returns a row or None.

    Three keys, strongest first, because no single field covers the list:

      1. Normalized phone -- the right key for calling. Two rows that dial the
         same number are one conversation, whoever they claim to be.
      2. Canonical domain -- the right key for email, and already how contacts
         with a website are deduped.
      3. Normalized company AND locality -- last resort, for the no-website
         leads that have neither of the above. Never company alone: that would
         merge "Main Street Dental" in St John's with the one in Toronto.
    """
    phone_key = normalize_phone(phone)
    if phone_key:
        row = conn.execute(
            "SELECT * FROM contacts WHERE phone_normalized=? AND phone_normalized!='' "
            "AND status != 'deleted' AND (? IS NULL OR id != ?) LIMIT 1",
            (phone_key, exclude_id, exclude_id or -1),
        ).fetchone()
        if row:
            return row

    domain = canonical_domain(website)
    if domain and not is_freemail(domain):
        row = conn.execute(
            "SELECT * FROM contacts WHERE domain=? AND domain!='' "
            "AND status != 'deleted' AND (? IS NULL OR id != ?) LIMIT 1",
            (domain, exclude_id, exclude_id or -1),
        ).fetchone()
        if row:
            return row

    name_key = normalize_company(company)
    place    = _locality_key(address)
    if name_key and place:
        for row in conn.execute(
            "SELECT * FROM contacts WHERE company!='' AND status != 'deleted' "
            "AND (? IS NULL OR id != ?)",
            (exclude_id, exclude_id or -1),
        ).fetchall():
            if (normalize_company(row["company"]) == name_key
                    and _locality_key(row["address"]) == place):
                return row
    return None


def get_touch_history(contact_id: int) -> dict:
    """
    How this business has already been contacted, across every channel.

    Distinct from duplicate detection: "is this the same row" and "have I
    already worked this lead" are different questions, and only the second one
    decides whether to dial. A previously-emailed lead with no reply is still
    worth a call; one that already said no is not -- so this reports rather
    than hides.
    """
    with get_db() as conn:
        emails = conn.execute(
            "SELECT COUNT(*) AS n, MAX(sent_at) AS last FROM sends WHERE contact_id=?",
            (contact_id,),
        ).fetchone()
        enrolled = conn.execute("""
            SELECT c.name AS campaign, e.status
              FROM enrollments e JOIN campaigns c ON c.id = e.campaign_id
             WHERE e.contact_id = ?
             ORDER BY e.enrolled_at DESC
        """, (contact_id,)).fetchall()

    return {
        "emails_sent":   emails["n"] or 0,
        "last_email_at": emails["last"],
        "campaigns":     [dict(r) for r in enrolled],
        # Terminal states mean the prospect has already answered -- surfaced so
        # the call list can warn rather than silently re-work them.
        "closed":        any(r["status"] in ("replied", "unsubscribed", "bounced")
                             for r in enrolled),
    }


def get_campaign_today_count(campaign_id):
    """
    Emails this campaign has sent today, counted from the sends table.

    Lifted out of scheduler._get_campaign_today_count so the campaign page can
    report "daily limit reached" using the same number the scheduler enforces
    -- two implementations of the same count would eventually disagree about
    why nothing is going out.
    """
    today = datetime.date.today().isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM sends WHERE campaign_id=? AND DATE(sent_at)=?",
            (campaign_id, today),
        ).fetchone()
        return row[0] if row else 0


def has_sent_step(campaign_id, contact_id, step_num) -> bool:
    """
    Has this exact step already gone to this contact?

    Sending is three separate writes -- deliver over SMTP, log the send,
    advance the enrollment -- and a restart between the first and the last
    leaves the enrollment still queued on a step the contact has already
    received. Without this check the scheduler simply sends it again. That
    window is small, but a deploy lands in it eventually, and the cost is a
    duplicate cold email to a prospect.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM sends WHERE campaign_id=? AND contact_id=? AND step_num=? LIMIT 1",
            (campaign_id, contact_id, step_num),
        ).fetchone()
        return row is not None


# Below this many sends a bounce rate is noise, not signal: one bad address in
# a list of twelve reads as 8% and trips a 5% threshold, pausing the whole
# campaign on its first typo'd scrape. The operator experiences that as the
# campaign stopping for no visible reason.
BOUNCE_RATE_MIN_SENDS = 20


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


def bounce_breaker_should_pause(campaign_id, threshold_pct) -> tuple:
    """
    Whether the bounce circuit-breaker should fire. Returns (should_pause, rate, sends).

    Gated on volume as well as rate. The rate alone is meaningless early on --
    a single bounce out of the first handful of sends exceeds any sane
    threshold -- so the breaker waits until there is enough traffic for the
    percentage to mean something.
    """
    rate  = get_bounce_rate(campaign_id)
    sends = get_campaign_send_total(campaign_id)
    return (sends >= BOUNCE_RATE_MIN_SENDS and rate >= threshold_pct), rate, sends


def get_campaign_send_total(campaign_id) -> int:
    """Every email this campaign has ever sent, across all days."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM sends WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        return row[0] if row else 0


# ── Cold calling ──────────────────────────────────────────────────────────────
#
# Calling is kept separate from enrollments on purpose. An email sequence is a
# schedule the machine runs; a call list is a pile you work through in a
# sitting, and its states ("no answer, try again", "booked") do not map onto
# the enrollment lifecycle. Overloading one status field with both would go
# wrong the first time a contact was mid-sequence and also mid-callback.

# outcome -> (label, is_terminal, stops_email, wants_next_call)
CALL_OUTCOMES = {
    "no_answer":      ("No answer",        False, False, False),
    "voicemail":      ("Left voicemail",   False, False, False),
    "callback":       ("Callback booked",  False, False, True),
    "interested":     ("Interested",       False, False, True),
    "proposal_sent":  ("Proposal sent",    False, False, True),
    "booked":         ("Meeting booked",   True,  True,  True),
    "not_interested": ("Not interested",   True,  True,  False),
    "wrong_number":   ("Wrong number",     True,  True,  False),
    "do_not_call":    ("Do not call",      True,  True,  False),
}

# Attempts past this without reaching anyone: the lead is spending your time.
CALL_ATTEMPT_LIMIT = 6

_DEFAULT_SCRIPT_SECTIONS = [
    {"title": "Opening",            "body": ""},
    {"title": "Qualifying questions", "body": ""},
    {"title": "Common objections",  "body": ""},
    {"title": "Discovery call",     "body": ""},
    {"title": "Close",              "body": ""},
    {"title": "Voicemail",          "body": ""},
]


def log_call(contact_id: int, outcome: str, notes: str = "", next_call_at: str = None) -> dict:
    """
    Record a call attempt and move the contact's calling state forward.

    Terminal outcomes also stop any live email sequence. Telling someone "not
    interested" on the phone and then having the scheduler send them a cheerful
    follow-up two days later is the specific embarrassment this prevents --
    the two channels have to share the same answer.
    """
    if outcome not in CALL_OUTCOMES:
        raise ValueError(f"Unknown call outcome: {outcome}")

    _label, is_terminal, stops_email, wants_next = CALL_OUTCOMES[outcome]

    # "Booked" is both terminal and dated: the lead leaves the queue, but the
    # meeting time is the most valuable thing on the record and the calendar
    # invite is generated from it. Clearing the date for every terminal outcome
    # threw that away. A date is dropped only when the outcome has no use for
    # one -- otherwise a dead lead would carry a stale callback forever.
    keeps_date = wants_next or not is_terminal

    with get_db() as conn:
        conn.execute("""
            INSERT INTO call_log(contact_id, outcome, notes, next_call_at)
            VALUES(?,?,?,?)
        """, (contact_id, outcome, notes or "", next_call_at or None))

        # Retirement from the queue comes from call_status being terminal, not
        # from clearing the date, so keeping a booked meeting's time cannot put
        # the lead back in tomorrow's list.
        conn.execute("""
            UPDATE contacts
               SET call_status   = ?,
                   next_call_at  = ?,
                   call_attempts = call_attempts + 1
             WHERE id = ?
        """, (outcome, (next_call_at or None) if keeps_date else None, contact_id))

        if stops_email:
            conn.execute("""
                UPDATE enrollments SET status='completed'
                 WHERE contact_id = ?
                   AND status NOT IN ('completed','replied','unsubscribed','bounced')
            """, (contact_id,))

        # "Do not call" is a request about contact, not about the phone. It has
        # to suppress email too, or honouring it is only half true.
        if outcome == "do_not_call":
            conn.execute(
                "UPDATE contacts SET status='unsubscribed' WHERE id=? AND status != 'deleted'",
                (contact_id,),
            )

    return {"outcome": outcome, "terminal": is_terminal, "stopped_email": stops_email}


def get_call_queue(bucket="today", limit=200, source_job_id=None, only_no_website=False):
    """
    The leads to work right now.

      today    -- callbacks due (including overdue), soonest first
      new      -- never called, freshest leads first
      upcoming -- callbacks scheduled beyond today
      all      -- everything still callable

    Terminal outcomes and unsubscribed contacts are excluded everywhere: a
    finished lead should never reappear in a queue, whichever bucket is open.
    """
    terminal = [k for k, v in CALL_OUTCOMES.items() if v[1]]
    placeholders = ",".join("?" * len(terminal))

    where = [
        f"(c.call_status = '' OR c.call_status NOT IN ({placeholders}))",
        "c.status NOT IN ('deleted','unsubscribed')",
        "COALESCE(c.phone,'') != ''",
    ]
    params = list(terminal)

    if bucket == "today":
        where.append("c.next_call_at IS NOT NULL AND datetime(c.next_call_at) <= datetime('now')")
        order = "c.next_call_at ASC"
    elif bucket == "new":
        where.append("c.call_status = ''")
        order = "c.created_at DESC"
    elif bucket == "upcoming":
        where.append("c.next_call_at IS NOT NULL AND datetime(c.next_call_at) > datetime('now')")
        order = "c.next_call_at ASC"
    else:
        order = "c.next_call_at IS NULL, c.next_call_at ASC, c.created_at DESC"

    if source_job_id:
        where.append("c.source_job_id = ?")
        params.append(int(source_job_id))
    if only_no_website:
        where.append("c.status = 'no_website'")

    params.append(int(limit))
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT c.* FROM contacts c
             WHERE {' AND '.join(where)}
             ORDER BY {order}
             LIMIT ?
        """, params).fetchall()
        return [dict(r) for r in rows]


def get_call_queue_counts(source_job_id=None, only_no_website=False):
    """Bucket sizes, so the tabs can show what is waiting without loading it."""
    return {
        b: len(get_call_queue(b, limit=100000, source_job_id=source_job_id,
                              only_no_website=only_no_website))
        for b in ("today", "new", "upcoming")
    }


def get_call_history(contact_id: int):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM call_log WHERE contact_id=? ORDER BY called_at DESC, id DESC",
            (contact_id,),
        ).fetchall()]


def get_active_call_script() -> dict:
    """
    The script shown beside the dialler, creating an empty one on first use.

    Seeded with section headings and no content: the words are the operator's,
    and inventing a script for them would put language in their mouth that
    they have to notice and delete mid-call.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM call_scripts WHERE is_active=1 ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            cur = conn.execute(
                "INSERT INTO call_scripts(name, sections, is_active) VALUES(?,?,1)",
                ("Default script", json.dumps(_DEFAULT_SCRIPT_SECTIONS)),
            )
            row = conn.execute(
                "SELECT * FROM call_scripts WHERE id=?", (cur.lastrowid,)
            ).fetchone()

    out = dict(row)
    try:
        out["sections"] = json.loads(out["sections"] or "[]")
    except Exception:
        out["sections"] = list(_DEFAULT_SCRIPT_SECTIONS)
    return out


def save_call_script(script_id: int, name: str, sections: list):
    with get_db() as conn:
        conn.execute("""
            UPDATE call_scripts
               SET name=?, sections=?, updated_at=datetime('now')
             WHERE id=?
        """, (name or "Default script", json.dumps(sections or []), script_id))


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


def prune_logs(retention_days=60) -> int:
    """Delete app log rows older than retention_days. Called daily by the scheduler."""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM logs WHERE created_at < datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        )
        return cur.rowcount


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
