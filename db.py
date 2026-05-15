"""
db.py — SQLite database layer for the outreach system.
All tables, queries, and helpers live here.
"""

import sqlite3
import json
import logging
import datetime
import secrets
from pathlib import Path

logger = logging.getLogger("db")

import os as _os
DB_PATH = _os.environ.get("DB_PATH", "outreach.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
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

            CREATE TABLE IF NOT EXISTS contacts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT NOT NULL UNIQUE,
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
        """)

        # Schema migrations — safe to run repeatedly on existing databases
        for _col_sql in [
            "ALTER TABLE contacts ADD COLUMN soft_bounce_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE contacts ADD COLUMN website TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE contacts ADD COLUMN address TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                conn.execute(_col_sql)
            except Exception:
                pass  # Column already exists


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
                    min_delay=45, max_delay=120):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns(name,daily_limit,send_start_hour,send_end_hour,"
            "min_delay_secs,max_delay_secs) VALUES(?,?,?,?,?,?)",
            (name, daily_limit, start_hour, end_hour, min_delay, max_delay)
        )
        return cur.lastrowid


def update_campaign(cid, **fields):
    allowed = {"name", "daily_limit", "send_start_hour", "send_end_hour",
               "min_delay_secs", "max_delay_secs", "bounce_pause_pct", "status"}
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

def upsert_contacts(rows):
    """rows: list of dicts — required key: email; optional: first_name, last_name, company, website, address"""
    with get_db() as conn:
        inserted = 0
        for r in rows:
            email = r.get("email", "").strip().lower()
            if not email or "@" not in email:
                continue
            conn.execute("""
                INSERT INTO contacts(email,first_name,last_name,company,website,address,extra)
                VALUES(:email,:first_name,:last_name,:company,:website,:address,:extra)
                ON CONFLICT(email) DO UPDATE SET
                    first_name=COALESCE(NULLIF(excluded.first_name,''), contacts.first_name),
                    last_name=COALESCE(NULLIF(excluded.last_name,''),   contacts.last_name),
                    company=COALESCE(NULLIF(excluded.company,''),       contacts.company),
                    website=COALESCE(NULLIF(excluded.website,''),       contacts.website),
                    address=COALESCE(NULLIF(excluded.address,''),       contacts.address)
            """, {
                "email":      email,
                "first_name": r.get("first_name", ""),
                "last_name":  r.get("last_name", ""),
                "company":    r.get("company", ""),
                "website":    r.get("website", ""),
                "address":    r.get("address", ""),
                "extra":      json.dumps(r.get("extra", {})),
            })
            inserted += 1
        return inserted


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
    with get_db() as conn:
        conn.execute("UPDATE contacts SET status='unsubscribed' WHERE email=?", (email.lower(),))
        conn.execute("""
            UPDATE enrollments SET status='unsubscribed'
            WHERE contact_id=(SELECT id FROM contacts WHERE email=?)
        """, (email.lower(),))


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

def enroll_contacts_bulk(campaign_id, contact_ids):
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        enrolled = 0
        for cid in contact_ids:
            try:
                cur = conn.execute("""
                    INSERT OR IGNORE INTO enrollments
                        (campaign_id,contact_id,current_step,status,next_send_at)
                    VALUES(?,?,1,'queued',?)
                """, (campaign_id, cid, now))
                enrolled += cur.rowcount  # 0 if already enrolled, 1 if new
            except Exception as e:
                logger.warning(f"Failed to enroll contact {cid}: {e}")
        return enrolled


def get_campaign_contacts(campaign_id):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT c.email, c.first_name, c.last_name, c.company, c.status as contact_status,
                   e.id as enroll_id, e.current_step, e.status, e.next_send_at, e.enrolled_at
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
                   e.current_step, e.next_send_at,
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
    with get_db() as conn:
        conn.execute("""
            UPDATE enrollments SET status='replied'
            WHERE campaign_id=? AND contact_id=? AND status='queued'
        """, (campaign_id, contact_id))


# ── Sends & Counts ────────────────────────────────────────────────────────────

def log_send(campaign_id, contact_id, step_num, subject, msg_id):
    today = datetime.date.today().isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sends(campaign_id,contact_id,step_num,subject,msg_id)
            VALUES(?,?,?,?,?)
        """, (campaign_id, contact_id, step_num, subject, msg_id))
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
