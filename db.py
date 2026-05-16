"""
db.py — SQLite database layer for the outreach system.
All tables, queries, and helpers live here.
"""

import sqlite3
import json
import logging
import datetime
import secrets
import hashlib
import hmac as _hmac
import os as _os_auth
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
        ]:
            try:
                conn.execute(_col_sql)
            except Exception:
                pass  # Column already exists

        # Make email nullable so the scraper can store no-email prospects
        _col_info = conn.execute("PRAGMA table_info(contacts)").fetchall()
        _email_col = next((r for r in _col_info if r['name'] == 'email'), None)
        if _email_col and _email_col['notnull']:
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
                    soft_bounce_count INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO contacts_new
                    SELECT id, email, first_name, last_name, company, extra, status,
                           created_at, COALESCE(website,''), COALESCE(address,''),
                           COALESCE(soft_bounce_count, 0)
                    FROM contacts;
                DROP TABLE contacts;
                ALTER TABLE contacts_new RENAME TO contacts;
                PRAGMA foreign_keys = ON;
            """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS contacts_email_unique
            ON contacts(email) WHERE email IS NOT NULL AND email != ''
        """)


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
    """rows: list of dicts — email optional; required for active contacts, omit for prospects."""
    with get_db() as conn:
        inserted = 0
        for r in rows:
            email = (r.get("email") or "").strip().lower()
            website = r.get("website", "")
            status = r.get("status", "active")

            if email and "@" in email:
                conn.execute("""
                    INSERT INTO contacts(email,first_name,last_name,company,website,address,extra,status)
                    VALUES(:email,:first_name,:last_name,:company,:website,:address,:extra,:status)
                    ON CONFLICT(email) WHERE email IS NOT NULL AND email != '' DO UPDATE SET
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
                    "website":    website,
                    "address":    r.get("address", ""),
                    "extra":      json.dumps(r.get("extra", {})),
                    "status":     status,
                })
                inserted += 1
            elif website and status in ("form_only", "no_email"):
                # Prospect record — no email found; skip if same website already stored
                exists = conn.execute(
                    "SELECT id FROM contacts WHERE website=? AND email IS NULL",
                    (website,)
                ).fetchone()
                if not exists:
                    conn.execute("""
                        INSERT INTO contacts(company,website,address,status,extra)
                        VALUES(?,?,?,?,?)
                    """, (
                        r.get("company", ""), website,
                        r.get("address", ""), status,
                        json.dumps(r.get("extra", {})),
                    ))
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
    with get_db() as conn:
        conn.execute("UPDATE contacts SET status='unsubscribed' WHERE email=?", (email.lower(),))
        conn.execute("""
            UPDATE enrollments SET status='unsubscribed'
            WHERE contact_id=(SELECT id FROM contacts WHERE email=?)
        """, (email.lower(),))


def get_unsubscribed_contacts():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT email, first_name, last_name, company, created_at
            FROM contacts
            WHERE status = 'unsubscribed'
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
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Determine variant weights from step 1 (if A/B is configured)
    with get_db() as conn:
        step1 = conn.execute(
            "SELECT id FROM steps WHERE campaign_id=? AND step_num=1", (campaign_id,)
        ).fetchone()
    variants = get_step_variants(step1["id"]) if step1 else []

    with get_db() as conn:
        enrolled = 0
        for cid in contact_ids:
            try:
                variant_label = _pick_variant(variants) if variants else None
                cur = conn.execute("""
                    INSERT OR IGNORE INTO enrollments
                        (campaign_id,contact_id,current_step,status,next_send_at,variant_label)
                    VALUES(?,?,1,'queued',?,?)
                """, (campaign_id, cid, now, variant_label))
                enrolled += cur.rowcount
            except Exception as e:
                logger.warning(f"Failed to enroll contact {cid}: {e}")
        return enrolled


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
    with get_db() as conn:
        conn.execute("""
            UPDATE enrollments SET status='replied'
            WHERE campaign_id=? AND contact_id=? AND status='queued'
        """, (campaign_id, contact_id))


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

def _hash_password(password: str) -> str:
    salt = _os_auth.urandom(32)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return salt.hex() + ":" + key.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
        return _hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


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


def authenticate(username: str, password: str):
    """Return user dict if credentials valid, else None."""
    user = get_user_by_username(username)
    if user and _verify_password(password, user["password_hash"]):
        return user
    return None


def user_count() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def seed_admin_from_env():
    """On first run, create an admin account from ADMIN_PASS env var if set."""
    if user_count() > 0:
        return
    password = _os_auth.environ.get("ADMIN_PASS", "")
    username  = _os_auth.environ.get("ADMIN_USER", "admin")
    if password:
        create_user(username, password, is_admin=True)
        logger.info(f"Created admin user '{username}' from ADMIN_PASS env var")
