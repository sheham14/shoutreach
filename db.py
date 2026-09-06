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

            -- ─── Identity ───────────────────────────────────────────────────
            --
            -- One row per real-world business, independent of how we reach it.
            -- Everything channel-specific lives in email_leads / call_leads /
            -- wa_leads, which reference this.
            --
            -- Splitting identity out of the channel is what makes "is this
            -- clinic already being emailed" a foreign-key lookup instead of a
            -- fuzzy match on phone or domain. The domain-arbitration code this
            -- replaced existed only because `domain` was standing in for
            -- business identity, which broke on freemail and on group
            -- practices sharing one address.
            CREATE TABLE IF NOT EXISTS businesses (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT    NOT NULL DEFAULT '',
                phone            TEXT    NOT NULL DEFAULT '',
                phone_normalized TEXT    NOT NULL DEFAULT '',
                website          TEXT    NOT NULL DEFAULT '',
                domain           TEXT    NOT NULL DEFAULT '',
                address          TEXT    NOT NULL DEFAULT '',
                city             TEXT    NOT NULL DEFAULT '',
                country          TEXT    NOT NULL DEFAULT '',
                category         TEXT    NOT NULL DEFAULT '',
                rating           REAL    DEFAULT NULL,
                review_count     INTEGER DEFAULT NULL,
                -- What the scraper could establish about reaching them on the
                -- web: '' unknown, 'no_website', 'form_only', 'has_email'.
                -- Distinct from email_leads.status, which tracks the lifecycle
                -- of one address rather than a fact about the business.
                web_status       TEXT    NOT NULL DEFAULT '',
                source_job_id    INTEGER DEFAULT NULL,
                extra            TEXT    NOT NULL DEFAULT '{}',
                -- Suppresses this business on every channel at once. A clinic
                -- that says "stop contacting us" on WhatsApp must not keep
                -- receiving email, and one flag here is the only way to be
                -- sure of that.
                do_not_contact   INTEGER NOT NULL DEFAULT 0,
                notes            TEXT    NOT NULL DEFAULT '',
                created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- ─── Per-channel lead state ─────────────────────────────────────

            -- One row per email address. A business legitimately has several
            -- (info@, the owner, a billing address), so this is many-to-one
            -- against businesses -- which is also how "several addresses at
            -- one clinic, only email the best" is expressed now.
            --
            -- email is nullable and uniqueness comes from the partial index
            -- email_leads_email_unique below, not a table constraint, so
            -- prospect rows with no address found yet are allowed to repeat.
            CREATE TABLE IF NOT EXISTS email_leads (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id       INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
                email             TEXT    DEFAULT NULL,
                first_name        TEXT    NOT NULL DEFAULT '',
                last_name         TEXT    NOT NULL DEFAULT '',
                status            TEXT    NOT NULL DEFAULT 'active',
                mx_valid          INTEGER DEFAULT NULL,
                soft_bounce_count INTEGER NOT NULL DEFAULT 0,
                -- Points at the address chosen to actually receive mail when a
                -- business has several. Kept out of `status` because
                -- get_due_enrollments filters on status and a mid-sequence
                -- lead flipped here would silently lose its follow-ups.
                duplicate_of      INTEGER DEFAULT NULL,
                created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS call_leads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id   INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
                call_status   TEXT    NOT NULL DEFAULT '',
                next_call_at  TEXT    DEFAULT NULL,
                call_attempts INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(business_id)
            );

            -- WhatsApp. Sending is manual by design: this table stages a
            -- message and records that the operator opened the wa.me link.
            -- Nothing in this schema is driven by a scheduler, and there is no
            -- send path -- see wa_log's comment.
            CREATE TABLE IF NOT EXISTS wa_leads (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id      INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
                -- Digits only, full international form, ready to drop into a
                -- wa.me link. Computed once at import against the lead's
                -- country rather than at click time, so a number that cannot
                -- be formatted is visible in the table before it wastes a tap.
                wa_number        TEXT    NOT NULL DEFAULT '',
                country          TEXT    NOT NULL DEFAULT '',
                -- 'mobile' / 'landline' / 'unknown', from dialling-prefix
                -- rules. A landline is less likely to be on WhatsApp, but
                -- WhatsApp Business does run on them, so this sorts the queue
                -- rather than filtering it.
                number_type      TEXT    NOT NULL DEFAULT 'unknown',
                wa_status        TEXT    NOT NULL DEFAULT '',
                signal_type      TEXT    DEFAULT NULL,
                signal_detail    TEXT    NOT NULL DEFAULT '',
                signal_confirmed INTEGER NOT NULL DEFAULT 0,
                draft_message    TEXT    NOT NULL DEFAULT '',
                template_variant TEXT    NOT NULL DEFAULT '',
                sent_date        TEXT    DEFAULT NULL,
                replied          INTEGER NOT NULL DEFAULT 0,
                followup_count   INTEGER NOT NULL DEFAULT 0,
                paused           INTEGER NOT NULL DEFAULT 0,
                -- Where a lead went when its number turned out not to be on
                -- WhatsApp, e.g. 'call' or 'email'. Kept rather than deleted
                -- so a later scrape cannot quietly re-queue a number already
                -- ruled out here.
                moved_to         TEXT    NOT NULL DEFAULT '',
                notes            TEXT    NOT NULL DEFAULT '',
                created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(business_id)
            );

            -- Every WhatsApp message the operator actually opened in WhatsApp,
            -- append-only. "Sent" here means the wa.me link was opened, not
            -- that WhatsApp confirmed delivery or that a message left the
            -- phone -- there is no way to observe either from outside the app,
            -- and treating this as delivery would be a lie the follow-up
            -- cadence then acts on.
            CREATE TABLE IF NOT EXISTS wa_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                wa_lead_id       INTEGER NOT NULL REFERENCES wa_leads(id) ON DELETE CASCADE,
                kind             TEXT    NOT NULL DEFAULT 'opener',
                message          TEXT    NOT NULL DEFAULT '',
                template_variant TEXT    NOT NULL DEFAULT '',
                sent_at          TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS enrollments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id   INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                email_lead_id INTEGER NOT NULL REFERENCES email_leads(id) ON DELETE CASCADE,
                current_step  INTEGER NOT NULL DEFAULT 1,
                status        TEXT    NOT NULL DEFAULT 'queued',
                next_send_at  TEXT,
                enrolled_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(campaign_id, email_lead_id)
            );

            CREATE TABLE IF NOT EXISTS sends (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id   INTEGER,
                email_lead_id INTEGER,
                step_num      INTEGER,
                subject       TEXT,
                msg_id        TEXT,
                status        TEXT NOT NULL DEFAULT 'sent',
                sent_at       TEXT NOT NULL DEFAULT (datetime('now'))
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
                call_lead_id INTEGER NOT NULL REFERENCES call_leads(id) ON DELETE CASCADE,
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

            -- A named batch of leads to work: "10 dental clinics, St John's".
            -- Distinct from the scrape that found them, which groups by when
            -- you happened to scrape rather than by any decision you made.
            -- What a call can result in. A table rather than a constant
            -- because the useful vocabulary is the operator's, not the app's:
            -- "callback booked" and "follow up sometime" are different things,
            -- and forcing the second into the first puts a date in the system
            -- that was never actually agreed with anyone.
            --
            -- requires_date is separate from "has a date": any outcome may
            -- carry one, this only marks the ones that make no sense without.
            CREATE TABLE IF NOT EXISTS call_outcome_types (
                key           TEXT PRIMARY KEY,
                label         TEXT    NOT NULL,
                is_terminal   INTEGER NOT NULL DEFAULT 0,
                stops_email   INTEGER NOT NULL DEFAULT 0,
                requires_date INTEGER NOT NULL DEFAULT 0,
                tone          TEXT    NOT NULL DEFAULT 'neutral',
                sort_order    INTEGER NOT NULL DEFAULT 100,
                is_builtin    INTEGER NOT NULL DEFAULT 0,
                archived      INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS call_campaigns (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                notes      TEXT    NOT NULL DEFAULT '',
                status     TEXT    NOT NULL DEFAULT 'active',
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- Membership is many-to-many on purpose: the same clinic can be
            -- worked again months later under a different offer, and the
            -- earlier campaign's record of what happened should survive that.
            CREATE TABLE IF NOT EXISTS call_campaign_members (
                call_campaign_id INTEGER NOT NULL REFERENCES call_campaigns(id) ON DELETE CASCADE,
                call_lead_id     INTEGER NOT NULL REFERENCES call_leads(id) ON DELETE CASCADE,
                added_at         TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (call_campaign_id, call_lead_id)
            );

            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)

        # Schema migrations — safe to run repeatedly on existing databases.
        #
        # Columns for the retired `contacts` table are gone from this list:
        # businesses / email_leads / call_leads declare their own columns in
        # full above, and the one-shot split below is what carries old data
        # across. Adding a column to a new table means editing its CREATE and
        # adding one line here, nothing else.
        for _col_sql in [
            "ALTER TABLE sends ADD COLUMN account_id INTEGER",
            "ALTER TABLE enrollments ADD COLUMN variant_label TEXT",
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
            # Which batch a call was made under. Without it, a lead worked in
            # two campaigns would have its calls counted against both and
            # neither campaign's numbers would mean anything.
            "ALTER TABLE call_log ADD COLUMN call_campaign_id INTEGER DEFAULT NULL",
            # Where a scrape was aimed. The WhatsApp module needs a country to
            # turn a locally-formatted Gulf number into something a wa.me link
            # will accept, and the scrape already knows it -- "dental clinics
            # Doha" is a country fact the operator should not have to restate
            # at import time.
            "ALTER TABLE scrape_jobs ADD COLUMN country TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                conn.execute(_col_sql)
            except Exception as exc:
                # Almost always "duplicate column name" on an already-migrated DB,
                # but log it — a genuine migration failure must not be invisible.
                logger.debug("Column migration skipped: %s (%s)", _col_sql, exc)

        # One-shot split of the old single `contacts` table into a business
        # identity plus per-channel leads. No-ops on a database that has
        # already been split, and on a fresh one that never had `contacts`.
        try:
            _split_contacts_into_channels(conn)
        except Exception as exc:
            logger.exception("Contact split migration failed: %s", exc)
            raise

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS email_leads_email_unique
            ON email_leads(email) WHERE email IS NOT NULL AND email != ''
        """)

        # Hot-path indexes — used by the scheduler / reply-detection loops.
        # Without these every cycle full-scans the sends and enrollments tables.
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS sends_msg_id_idx        ON sends(msg_id) WHERE msg_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS sends_campaign_sent_at  ON sends(campaign_id, sent_at)",
            "CREATE INDEX IF NOT EXISTS sends_sent_at_idx       ON sends(sent_at)",
            "CREATE INDEX IF NOT EXISTS enrollments_due_idx     ON enrollments(campaign_id, status, next_send_at)",
            "CREATE INDEX IF NOT EXISTS enrollments_lead_idx    ON enrollments(email_lead_id, status)",
            "CREATE INDEX IF NOT EXISTS logs_created_at_idx     ON logs(created_at)",
            # Backs has_sent_step, which runs once per email before sending.
            "CREATE INDEX IF NOT EXISTS sends_dedupe_idx ON sends(campaign_id, email_lead_id, step_num)",

            # Identity. phone and domain are how an inbound scrape row is
            # matched to a business we already know about, so both are on the
            # hot path of every import.
            "CREATE INDEX IF NOT EXISTS businesses_phone_idx      ON businesses(phone_normalized) WHERE phone_normalized != ''",
            "CREATE INDEX IF NOT EXISTS businesses_domain_idx     ON businesses(domain) WHERE domain != ''",
            "CREATE INDEX IF NOT EXISTS businesses_source_job_idx ON businesses(source_job_id) WHERE source_job_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS businesses_created_at_idx ON businesses(created_at)",
            "CREATE INDEX IF NOT EXISTS businesses_dnc_idx        ON businesses(do_not_contact) WHERE do_not_contact = 1",

            # Per-channel. business_id carries the cross-channel lookups
            # ("is this clinic already being called"), which used to be a
            # fuzzy phone match across one shared table.
            "CREATE INDEX IF NOT EXISTS email_leads_business_idx ON email_leads(business_id)",
            "CREATE INDEX IF NOT EXISTS email_leads_status_idx   ON email_leads(status)",
            "CREATE INDEX IF NOT EXISTS call_leads_business_idx  ON call_leads(business_id)",
            "CREATE INDEX IF NOT EXISTS call_leads_next_call_idx ON call_leads(next_call_at) WHERE next_call_at IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS call_leads_status_idx    ON call_leads(call_status)",
            "CREATE INDEX IF NOT EXISTS call_log_lead_idx        ON call_log(call_lead_id, called_at)",
            "CREATE INDEX IF NOT EXISTS wa_leads_business_idx    ON wa_leads(business_id)",
            "CREATE INDEX IF NOT EXISTS wa_leads_status_idx      ON wa_leads(wa_status)",
            # Backs the follow-up-due query, which is a live read on every
            # load of the WhatsApp section rather than a scheduled job.
            "CREATE INDEX IF NOT EXISTS wa_leads_due_idx         ON wa_leads(sent_date) WHERE replied = 0 AND paused = 0",
            "CREATE INDEX IF NOT EXISTS wa_log_lead_idx          ON wa_log(wa_lead_id, sent_at)",
        ):
            try:
                conn.execute(idx_sql)
            except Exception as exc:
                logger.debug("Index create skipped: %s (%s)", idx_sql, exc)

        # The domain backfill, the freemail-suppression repair and the phone
        # normalization backfill that used to run here are gone: all three
        # patched rows in `contacts`, and the split migration above computes
        # domain and phone_normalized as it writes each business. The freemail
        # bug they worked around cannot recur -- identity is business_id now,
        # not a domain string, so gmail.com is never mistaken for a business.

        try:
            _seed_call_outcomes(conn)
        except Exception as exc:
            logger.warning("Call outcome seeding skipped: %s", exc)

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

        # The phone/category/rating backfill that used to close this function
        # is gone with the rest: it pulled those values out of `extra` on old
        # `contacts` rows, and the split migration reads the same keys while
        # building each business.


# ── One-shot migration: contacts → businesses + per-channel leads ────────────

def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _identity_keys(row: dict) -> list:
    """
    Every handle we have on who this business is, best first.

    A single old contact row often carries more than one -- a phone and a
    website -- and two rows for the same clinic may each carry a different
    one. Returning all of them lets the caller merge rows that agree on any
    single handle, which is what stops "info@ and the owner's address at the
    same practice" from becoming two businesses.
    """
    keys = []
    phone = row.get("phone_normalized") or normalize_phone(row.get("phone") or "")
    if phone:
        keys.append(("phone", phone))

    domain = row.get("domain") or canonical_domain(row.get("website") or "")
    # Freemail is a mailbox provider, not a business. Keying on it is the exact
    # bug the old schema kept having to repair.
    if domain and not is_freemail(domain):
        keys.append(("domain", domain))

    company = normalize_company(row.get("company") or "")
    if company:
        keys.append(("company", company))
    return keys


def _split_contacts_into_channels(conn):
    """
    Split the old single `contacts` table into `businesses` plus the
    per-channel lead tables, then repoint everything that referenced it.

    Runs once. A database that never had `contacts` (a fresh install) and one
    that has already been split both fall straight through.
    """
    if not _table_exists(conn, "contacts"):
        return
    if conn.execute("SELECT 1 FROM businesses LIMIT 1").fetchone():
        logger.warning(
            "Both `contacts` and a populated `businesses` exist -- refusing to "
            "re-run the split. Drop `contacts` by hand once you have checked it."
        )
        return

    old = [dict(r) for r in conn.execute("SELECT * FROM contacts")]
    if not old:
        conn.executescript("PRAGMA foreign_keys=OFF; DROP TABLE contacts; PRAGMA foreign_keys=ON;")
        logger.info("Split migration: no contacts to move, dropped the empty table")
        return

    # Which old contact ids ever had calling activity. Checked against the log
    # and campaign membership as well as the denormalized columns, because a
    # lead can have been called under a campaign without call_status surviving.
    called = set()
    for tbl, col in (("call_log", "contact_id"), ("call_campaign_members", "contact_id")):
        if _table_exists(conn, tbl):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})")]
            if col in cols:
                called.update(
                    r[0] for r in conn.execute(f"SELECT DISTINCT {col} FROM {tbl}")
                )

    index = {}            # identity key -> business id
    email_map = {}        # old contact id -> email_lead id
    call_map = {}         # old contact id -> call_lead id
    merged = 0

    for row in old:
        # Values that older rows kept in the `extra` blob rather than columns.
        try:
            extra = json.loads(row.get("extra") or "{}")
        except Exception:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}

        def _num(val, cast):
            try:
                return cast(val) if val not in (None, "") else None
            except (TypeError, ValueError):
                return None

        phone = row.get("phone") or extra.get("phone", "") or ""
        website = row.get("website") or ""
        company = row.get("company") or ""
        rating = row.get("rating") if row.get("rating") is not None else _num(extra.get("rating"), float)
        reviews = row.get("review_count") if row.get("review_count") is not None else _num(extra.get("reviews"), int)

        keys = _identity_keys({**row, "phone": phone, "website": website, "company": company})
        biz_id = next((index[k] for k in keys if k in index), None)

        if biz_id is None:
            status = (row.get("status") or "").strip()
            biz_id = conn.execute("""
                INSERT INTO businesses(
                    name, phone, phone_normalized, website, domain, address,
                    category, rating, review_count, web_status, source_job_id,
                    extra, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                company,
                phone,
                row.get("phone_normalized") or normalize_phone(phone),
                website,
                row.get("domain") or canonical_domain(website),
                row.get("address") or "",
                row.get("category") or extra.get("category", "") or "",
                rating,
                reviews,
                status if status in ("no_website", "form_only", "no_email") else "",
                row.get("source_job_id"),
                json.dumps(extra),
                row.get("created_at") or datetime.datetime.now().isoformat(" ", "seconds"),
            )).lastrowid
        else:
            merged += 1
            # Fill blanks on the business from this row without overwriting
            # anything already established by an earlier one.
            conn.execute("""
                UPDATE businesses SET
                    phone            = COALESCE(NULLIF(phone,''), ?),
                    phone_normalized = COALESCE(NULLIF(phone_normalized,''), ?),
                    website          = COALESCE(NULLIF(website,''), ?),
                    domain           = COALESCE(NULLIF(domain,''), ?),
                    address          = COALESCE(NULLIF(address,''), ?),
                    category         = COALESCE(NULLIF(category,''), ?),
                    rating           = COALESCE(rating, ?),
                    review_count     = COALESCE(review_count, ?),
                    source_job_id    = COALESCE(source_job_id, ?)
                WHERE id = ?
            """, (
                phone, row.get("phone_normalized") or normalize_phone(phone),
                website, row.get("domain") or canonical_domain(website),
                row.get("address") or "", row.get("category") or "",
                rating, reviews, row.get("source_job_id"), biz_id,
            ))

        for k in keys:
            index.setdefault(k, biz_id)

        # An email address becomes an email_lead. Rows with no address were
        # prospects; the business itself now carries that fact in web_status,
        # so there is nothing left to represent and no empty row to carry.
        email = (row.get("email") or "").strip().lower()
        if email:
            status = (row.get("status") or "active").strip()
            if status in ("no_website", "form_only", "no_email", ""):
                status = "active"
            email_map[row["id"]] = conn.execute("""
                INSERT INTO email_leads(
                    business_id, email, first_name, last_name, status,
                    mx_valid, soft_bounce_count, created_at
                ) VALUES(?,?,?,?,?,?,?,?)
            """, (
                biz_id, email,
                row.get("first_name") or "", row.get("last_name") or "",
                status, row.get("mx_valid"), row.get("soft_bounce_count") or 0,
                row.get("created_at") or datetime.datetime.now().isoformat(" ", "seconds"),
            )).lastrowid
            conn.execute(
                "UPDATE businesses SET web_status='has_email' WHERE id=? AND web_status=''",
                (biz_id,),
            )

        has_call_state = (
            (row.get("call_status") or "") != ""
            or (row.get("call_attempts") or 0) > 0
            or row.get("next_call_at")
            or row["id"] in called
        )
        if has_call_state:
            existing = conn.execute(
                "SELECT id, call_status, call_attempts, next_call_at FROM call_leads WHERE business_id=?",
                (biz_id,),
            ).fetchone()
            if existing:
                # Two old rows for one clinic, both called. Keep the larger
                # attempt count and whichever status is actually set.
                call_map[row["id"]] = existing["id"]
                conn.execute("""
                    UPDATE call_leads SET
                        call_status   = COALESCE(NULLIF(call_status,''), ?),
                        call_attempts = MAX(call_attempts, ?),
                        next_call_at  = COALESCE(next_call_at, ?)
                    WHERE id = ?
                """, (
                    row.get("call_status") or "",
                    row.get("call_attempts") or 0,
                    row.get("next_call_at"),
                    existing["id"],
                ))
            else:
                call_map[row["id"]] = conn.execute("""
                    INSERT INTO call_leads(
                        business_id, call_status, next_call_at, call_attempts, created_at
                    ) VALUES(?,?,?,?,?)
                """, (
                    biz_id,
                    row.get("call_status") or "",
                    row.get("next_call_at"),
                    row.get("call_attempts") or 0,
                    row.get("created_at") or datetime.datetime.now().isoformat(" ", "seconds"),
                )).lastrowid

    _repoint_channel_refs(conn, email_map, call_map)

    conn.executescript("PRAGMA foreign_keys=OFF; DROP TABLE contacts; PRAGMA foreign_keys=ON;")
    logger.info(
        "Split migration: %d contact row(s) -> %d business(es) "
        "(%d merged), %d email lead(s), %d call lead(s)",
        len(old), len(set(index.values())), merged, len(email_map), len(call_map),
    )


def _repoint_channel_refs(conn, email_map: dict, call_map: dict):
    """
    Rebuild the four tables that referenced contacts(id) so they point at the
    channel table that now owns that relationship.

    Rebuilt rather than renamed: SQLite stores the REFERENCES clause as text,
    so a renamed column would still point at a `contacts` table that is about
    to be dropped, and every later insert would fail the foreign-key check.

    Each CREATE below must match the one in init_db, including columns added
    by the ALTER loop -- a column missing here is silently dropped.
    """
    def rebuild(table, ddl, old_col, new_col, mapping, extra_cols):
        if not _table_exists(conn, table):
            return
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        if old_col not in cols:
            return          # already repointed
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
        conn.executescript(f"""
            PRAGMA foreign_keys=OFF;
            DROP TABLE {table};
            {ddl}
            PRAGMA foreign_keys=ON;
        """)
        kept = dropped = 0
        target = [new_col] + extra_cols
        placeholders = ",".join("?" * len(target))
        for r in rows:
            mapped = mapping.get(r.get(old_col))
            if mapped is None:
                dropped += 1
                continue
            conn.execute(
                f"INSERT INTO {table}({','.join(target)}) VALUES({placeholders})",
                [mapped] + [r.get(c) for c in extra_cols],
            )
            kept += 1
        if dropped:
            # Almost always history against a prospect row that never had an
            # address, so there is no email lead for it to belong to.
            logger.info("Split migration: dropped %d orphaned %s row(s)", dropped, table)
        logger.info("Split migration: repointed %d %s row(s)", kept, table)

    rebuild(
        "enrollments",
        """CREATE TABLE enrollments (
               id            INTEGER PRIMARY KEY AUTOINCREMENT,
               campaign_id   INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
               email_lead_id INTEGER NOT NULL REFERENCES email_leads(id) ON DELETE CASCADE,
               current_step  INTEGER NOT NULL DEFAULT 1,
               status        TEXT    NOT NULL DEFAULT 'queued',
               next_send_at  TEXT,
               enrolled_at   TEXT    NOT NULL DEFAULT (datetime('now')),
               variant_label TEXT,
               UNIQUE(campaign_id, email_lead_id)
           );""",
        "contact_id", "email_lead_id", email_map,
        ["campaign_id", "current_step", "status", "next_send_at", "enrolled_at", "variant_label"],
    )
    rebuild(
        "sends",
        """CREATE TABLE sends (
               id            INTEGER PRIMARY KEY AUTOINCREMENT,
               campaign_id   INTEGER,
               email_lead_id INTEGER,
               step_num      INTEGER,
               subject       TEXT,
               msg_id        TEXT,
               status        TEXT NOT NULL DEFAULT 'sent',
               sent_at       TEXT NOT NULL DEFAULT (datetime('now')),
               account_id    INTEGER
           );""",
        "contact_id", "email_lead_id", email_map,
        ["campaign_id", "step_num", "subject", "msg_id", "status", "sent_at", "account_id"],
    )
    rebuild(
        "call_log",
        """CREATE TABLE call_log (
               id               INTEGER PRIMARY KEY AUTOINCREMENT,
               call_lead_id     INTEGER NOT NULL REFERENCES call_leads(id) ON DELETE CASCADE,
               outcome          TEXT    NOT NULL,
               notes            TEXT    NOT NULL DEFAULT '',
               next_call_at     TEXT,
               called_at        TEXT    NOT NULL DEFAULT (datetime('now')),
               call_campaign_id INTEGER DEFAULT NULL
           );""",
        "contact_id", "call_lead_id", call_map,
        ["outcome", "notes", "next_call_at", "called_at", "call_campaign_id"],
    )
    rebuild(
        "call_campaign_members",
        """CREATE TABLE call_campaign_members (
               call_campaign_id INTEGER NOT NULL REFERENCES call_campaigns(id) ON DELETE CASCADE,
               call_lead_id     INTEGER NOT NULL REFERENCES call_leads(id) ON DELETE CASCADE,
               added_at         TEXT    NOT NULL DEFAULT (datetime('now')),
               PRIMARY KEY (call_campaign_id, call_lead_id)
           );""",
        "contact_id", "call_lead_id", call_map,
        ["call_campaign_id", "added_at"],
    )


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


def _has_live_enrollment(conn, email_lead_id: int) -> bool:
    """True if this address is mid-sequence and must not be suppressed."""
    row = conn.execute("""
        SELECT 1 FROM enrollments
         WHERE email_lead_id=?
           AND status NOT IN ('completed','replied','unsubscribed','bounced')
         LIMIT 1
    """, (email_lead_id,)).fetchone()
    return row is not None


def _pick_business_winner(conn, business_id: int):
    """
    Decide which of a business's addresses is the sendable one, and link the
    rest to it.

    Two rules, in order:
      1. An address already mid-sequence always wins. Demoting it would strand
         the prospect after step 1 -- they would never receive the follow-ups,
         with no error anywhere.
      2. Otherwise the best-ranked address wins, oldest as the tiebreak.

    This replaced an arbitration keyed on `domain`, which needed a freemail
    exception because gmail.com is a mailbox provider rather than a business,
    and every Gmail lead but one was being suppressed as a "duplicate" of a
    business it had nothing to do with. Scoping to business_id removes the
    guesswork: two addresses share a winner only when they genuinely belong to
    the same business, so no domain can be mistaken for an identity.
    """
    rows = conn.execute("""
        SELECT id, email FROM email_leads
         WHERE business_id=? AND email IS NOT NULL AND email != ''
           AND status NOT IN ('deleted','unsubscribed','bounced')
         ORDER BY id ASC
    """, (business_id,)).fetchall()
    if len(rows) < 2:
        # Nothing to arbitrate; make sure a lone address is not left suppressed.
        for row in rows:
            conn.execute(
                "UPDATE email_leads SET duplicate_of=NULL WHERE id=?", (row["id"],)
            )
        return

    enrolled = [r for r in rows if _has_live_enrollment(conn, r["id"])]
    if enrolled:
        winner = enrolled[0]["id"]
    else:
        winner = sorted(rows, key=lambda r: (email_rank(r["email"]), r["id"]))[0]["id"]

    for row in rows:
        if row["id"] == winner:
            conn.execute("UPDATE email_leads SET duplicate_of=NULL WHERE id=?", (winner,))
        elif _has_live_enrollment(conn, row["id"]):
            # Already being emailed. Leave it alone rather than cutting a live
            # sequence short; the operator can unenroll it deliberately.
            conn.execute("UPDATE email_leads SET duplicate_of=NULL WHERE id=?", (row["id"],))
        else:
            conn.execute(
                "UPDATE email_leads SET duplicate_of=? WHERE id=?", (winner, row["id"])
            )


def find_or_create_business(conn, r: dict) -> int:
    """
    Resolve one import row to a business, creating it if genuinely new.

    The lookup itself is find_existing_business -- this just adds the
    create-if-missing step on top, so there is exactly one place that decides
    what counts as "the same business" rather than two copies that can drift
    apart (which is exactly how the email-domain fallback below went missing
    from this function's very first version).

    A match fills blanks but never overwrites. Whatever is already stored
    arrived first and has usually been looked at by a human since; a later
    scrape returning a truncated name or a redirect URL must not quietly
    degrade it. The one exception is `address`, which Maps does genuinely
    correct over time.

    Note what this function cannot touch: every channel's state lives in its
    own table, so re-importing a lead can never reset a call outcome, a
    WhatsApp follow-up count, or an email enrolment. That used to be a rule
    the import code had to remember; it is now a property of the schema.
    """
    phone = (r.get("phone") or "").strip()
    website = (r.get("website") or "").strip()
    name = (r.get("company") or r.get("name") or "").strip()
    address = (r.get("address") or "").strip()
    email = (r.get("email") or "").strip().lower()

    row = find_existing_business(conn, email=email, phone=phone, website=website,
                                 company=name, address=address)

    phone_norm = normalize_phone(phone)
    domain = canonical_domain(website)
    if not domain and email and "@" in email:
        # Same fallback find_existing_business uses for matching -- repeated
        # here because a genuinely new business still needs a domain stored,
        # not just matched against.
        candidate = email.split("@")[-1]
        if not is_freemail(candidate):
            domain = candidate

    def _num(val, cast):
        try:
            return cast(val) if val not in (None, "") else None
        except (TypeError, ValueError):
            return None

    rating = _num(r.get("rating"), float)
    reviews = _num(r.get("review_count"), int)
    web_status = r.get("status") if r.get("status") in ("no_website", "form_only", "no_email") else ""

    if row:
        conn.execute("""
            UPDATE businesses SET
                name             = COALESCE(NULLIF(name,''), ?),
                phone            = COALESCE(NULLIF(phone,''), ?),
                phone_normalized = COALESCE(NULLIF(phone_normalized,''), ?),
                website          = COALESCE(NULLIF(website,''), ?),
                domain           = COALESCE(NULLIF(domain,''), ?),
                -- Maps does correct a listing's address, so a non-empty
                -- incoming value wins here where it would not elsewhere.
                address          = COALESCE(NULLIF(?,''), address),
                city             = COALESCE(NULLIF(city,''), ?),
                country          = COALESCE(NULLIF(country,''), ?),
                category         = COALESCE(NULLIF(category,''), ?),
                rating           = COALESCE(?, rating),
                review_count     = COALESCE(?, review_count),
                web_status       = COALESCE(NULLIF(web_status,''), ?),
                -- First scrape that found this lead keeps it, so a business
                -- turning up again later stays filed under the list you
                -- originally built.
                source_job_id    = COALESCE(source_job_id, ?)
            WHERE id = ?
        """, (
            name, phone, phone_norm, website, domain, address,
            r.get("city", ""), r.get("country", ""), r.get("category", ""),
            rating, reviews, web_status, r.get("source_job_id") or None, row["id"],
        ))
        _note_alternate_name(conn, row["id"], name)
        return row["id"]

    return conn.execute("""
        INSERT INTO businesses(
            name, phone, phone_normalized, website, domain, address, city,
            country, category, rating, review_count, web_status,
            source_job_id, extra
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        name, phone, phone_norm, website, domain, address,
        r.get("city", ""), r.get("country", ""), r.get("category", ""),
        rating, reviews, web_status, r.get("source_job_id") or None,
        json.dumps(r.get("extra", {}) if isinstance(r.get("extra"), dict) else {}),
    )).lastrowid


def upsert_businesses(rows):
    """
    Import scraped or pasted rows as businesses, plus an email lead per address.

    rows: list of dicts. `email` is optional -- a row without one is a real
    lead, not a failure. For a web-design agency "this clinic has no website"
    is the strongest qualifying signal there is, and for WhatsApp or calling
    the phone number is all that was ever needed.

    Returns (accepted, business_ids) -- the count, and every business the rows
    resolved to (creates and updates alike, in row order with duplicates from
    repeat rows removed). The CSV/manual "add to a call campaign" flow needs
    the ids directly: it used to guess by re-querying "whatever was created
    most recently", which silently mismatched on a second import within the
    same minute.
    """
    with get_db() as conn:
        accepted = 0
        touched = set()
        ordered_ids = []

        for r in rows:
            email = (r.get("email") or "").strip().lower()
            name = (r.get("company") or r.get("name") or "").strip()
            # Nothing to file it under and nothing to reach it on.
            if not any((email, name, (r.get("phone") or "").strip(), (r.get("website") or "").strip())):
                continue

            business_id = find_or_create_business(conn, r)
            touched.add(business_id)
            if business_id not in ordered_ids:
                ordered_ids.append(business_id)

            if email and "@" in email:
                status = r.get("status", "active")
                if status in ("no_website", "form_only", "no_email", ""):
                    status = "active"
                conn.execute("""
                    INSERT INTO email_leads(
                        business_id, email, first_name, last_name, status, mx_valid
                    ) VALUES(:business_id,:email,:first_name,:last_name,:status,:mx_valid)
                    ON CONFLICT(email) WHERE email IS NOT NULL AND email != '' DO UPDATE SET
                        first_name = COALESCE(NULLIF(excluded.first_name,''), email_leads.first_name),
                        last_name  = COALESCE(NULLIF(excluded.last_name,''),  email_leads.last_name),
                        mx_valid   = COALESCE(excluded.mx_valid,              email_leads.mx_valid)
                """, {
                    "business_id": business_id,
                    "email":       email,
                    "first_name":  r.get("first_name", ""),
                    "last_name":   r.get("last_name", ""),
                    "status":      status,
                    "mx_valid":    r.get("mx_valid"),
                })
                conn.execute(
                    "UPDATE businesses SET web_status='has_email' WHERE id=? AND web_status=''",
                    (business_id,),
                )
            accepted += 1

        for business_id in touched:
            _pick_business_winner(conn, business_id)

        return accepted, ordered_ids


def _note_alternate_name(conn, business_id: int, name: str):
    """
    Keep a record when a business turns up under a different listing name.

    Group practices share a phone line and a building, so the same business
    legitimately appears as "Smile Dental" and "Smile Dental - Downtown". The
    first name stays; this stops the others from being silently discarded.
    """
    name = (name or "").strip()
    if not name:
        return
    row = conn.execute(
        "SELECT name, extra FROM businesses WHERE id=?", (business_id,)
    ).fetchone()
    if not row or row["name"] == name:
        return
    try:
        extra = json.loads(row["extra"] or "{}")
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    seen = extra.get("also_seen_as") or []
    if name not in seen:
        seen.append(name)
        extra["also_seen_as"] = seen[:10]
        conn.execute(
            "UPDATE businesses SET extra=? WHERE id=?", (json.dumps(extra), business_id)
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
            r["name"] for r in conn.execute(
                "SELECT DISTINCT name FROM businesses WHERE name != ''"
            ).fetchall()
        }


def get_email_leads(limit=200, offset=0):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(f"""
            SELECT {_EMAIL_LEAD_COLUMNS} {_EMAIL_LEAD_JOIN}
             ORDER BY el.created_at DESC LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()]


# ── Email leads: server-side paging, filtering and the lead-list view ────────
#
# This tab used to pull every row and filter in the browser, hard-capped at
# 500. Past that it silently showed only the newest 500 -- which a per-scrape
# filter would then narrow further, under-reporting a list with no warning. All
# filtering therefore happens in SQL now, against the whole table.

# Every list view returns the address joined to its business, because a bare
# address is not something anyone can act on -- the clinic's name, phone and
# site are what make it a lead. call_status/call_attempts come along too: the
# Contacts table has always shown whether a lead was also called, and that
# now lives on a different table's row instead of a column on this one.
_EMAIL_LEAD_COLUMNS = """
    el.id, el.business_id, el.email, el.first_name, el.last_name,
    el.status, el.mx_valid, el.soft_bounce_count, el.duplicate_of,
    el.created_at,
    b.name AS company, b.website, b.domain, b.address, b.city, b.country,
    b.phone, b.category, b.rating, b.review_count, b.source_job_id,
    b.web_status, b.do_not_contact, b.notes AS business_notes,
    COALESCE(cl.call_status,'') AS call_status,
    COALESCE(cl.call_attempts,0) AS call_attempts, cl.next_call_at
"""
_EMAIL_LEAD_JOIN = """
    FROM email_leads el
    JOIN businesses  b  ON b.id  = el.business_id
    LEFT JOIN call_leads cl ON cl.business_id = b.id
"""

# Whitelist: sort_col is interpolated into the SQL string, so it can never come
# straight from the query string. Values are qualified because the view is a
# join and `created_at` alone would be ambiguous.
_EMAIL_LEAD_SORT_COLUMNS = {
    "id": "el.id", "email": "el.email", "first_name": "el.first_name",
    "last_name": "el.last_name", "status": "el.status",
    "mx_valid": "el.mx_valid", "created_at": "el.created_at",
    "company": "b.name", "website": "b.website", "address": "b.address",
    "phone": "b.phone", "category": "b.category", "rating": "b.rating",
    "review_count": "b.review_count", "domain": "b.domain",
}

# Search covers what someone would plausibly type looking for a lead.
_EMAIL_LEAD_SEARCH_COLUMNS = (
    "el.email", "el.first_name", "el.last_name", "b.name",
    "b.website", "b.address", "b.phone", "b.category",
)

# Sentinel for "added by hand or CSV import, not by any scrape".
SOURCE_MANUAL = "manual"


def _email_lead_filters(q="", source_job_id=None, status=None, include_deleted=False,
                        call_status=None):
    """Build the shared WHERE clause for the email lead list views."""
    clauses, params = [], []

    if not include_deleted:
        clauses.append("el.status != 'deleted'")

    if status:
        clauses.append("el.status = ?")
        params.append(status)

    # Calling state lives on the business's call lead now, so this filter is a
    # cross-channel question: "show me addresses at clinics I have already
    # phoned". It used to read a column on the contact itself.
    if call_status:
        if call_status == "none":
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM call_leads cl "
                "WHERE cl.business_id = b.id AND COALESCE(cl.call_status,'') != '')"
            )
        elif call_status == "any":
            clauses.append(
                "EXISTS (SELECT 1 FROM call_leads cl "
                "WHERE cl.business_id = b.id AND COALESCE(cl.call_status,'') != '')"
            )
        else:
            clauses.append(
                "EXISTS (SELECT 1 FROM call_leads cl "
                "WHERE cl.business_id = b.id AND cl.call_status = ?)"
            )
            params.append(call_status)

    if source_job_id is not None and source_job_id != "":
        if str(source_job_id) == SOURCE_MANUAL:
            clauses.append("b.source_job_id IS NULL")
        else:
            clauses.append("b.source_job_id = ?")
            params.append(int(source_job_id))

    q = (q or "").strip()
    if q:
        like = " OR ".join(f"COALESCE({c},'') LIKE ?" for c in _EMAIL_LEAD_SEARCH_COLUMNS)
        clauses.append(f"({like})")
        params.extend([f"%{q}%"] * len(_EMAIL_LEAD_SEARCH_COLUMNS))

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_email_leads_page(page=1, per_page=50, q="", source_job_id=None, status=None,
                         include_deleted=False, sort_col="", sort_dir="desc",
                         call_status=None):
    """One page of email leads plus the total matching the same filter."""
    page     = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 50), 500))
    offset   = (page - 1) * per_page

    sort_dir = "asc" if str(sort_dir).lower() == "asc" else "desc"
    if sort_col in _EMAIL_LEAD_SORT_COLUMNS:
        col = _EMAIL_LEAD_SORT_COLUMNS[sort_col]
        # NULLs and '' sort last either way, so an empty phone column doesn't
        # push the rows you actually want to the top of an ascending sort.
        order_by = f"NULLIF({col}, '') IS NULL, {col} {sort_dir.upper()}"
    else:
        sort_col = ""
        order_by = "el.created_at DESC, el.id DESC"

    where, params = _email_lead_filters(q, source_job_id, status, include_deleted, call_status)
    join = _EMAIL_LEAD_JOIN

    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) {join} {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_EMAIL_LEAD_COLUMNS} {join} {where} "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?",
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


def get_email_lead_ids_matching(q="", source_job_id=None, status=None, include_deleted=False,
                                call_status=None):
    """
    Every email lead id matching a filter, ignoring paging.

    Backs "select all N matching" -- without it, select-all could only ever
    reach the rows on screen, so a bulk delete over a filtered list would
    silently act on one page's worth.
    """
    where, params = _email_lead_filters(q, source_job_id, status, include_deleted, call_status)
    with get_db() as conn:
        return [r["id"] for r in conn.execute(
            f"SELECT el.id FROM email_leads el "
            f"JOIN businesses b ON b.id = el.business_id {where}", params
        ).fetchall()]


def get_lead_sources():
    """
    The lead lists: one entry per scrape that produced businesses, newest
    first, plus a 'manual' bucket for hand-added and CSV-imported rows.

    Counts businesses rather than addresses: a scrape of 40 clinics that
    happened to find three addresses at one of them found 40 leads, and
    reporting 42 would misdescribe the list.

    LEFT JOIN, not a foreign key -- a business whose scrape_jobs row has gone
    still counts, it just shows as an unknown source rather than vanishing
    from the filter.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT b.source_job_id           AS job_id,
                   j.niche                   AS niche,
                   j.city                    AS city,
                   j.created_at              AS scraped_at,
                   COUNT(*)                  AS count
              FROM businesses b
              LEFT JOIN scrape_jobs j ON j.id = b.source_job_id
             GROUP BY b.source_job_id
             ORDER BY (b.source_job_id IS NULL), b.source_job_id DESC
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


def get_email_lead(email_lead_id: int):
    with get_db() as conn:
        row = conn.execute(f"""
            SELECT {_EMAIL_LEAD_COLUMNS} {_EMAIL_LEAD_JOIN}
             WHERE el.id=?
        """, (email_lead_id,)).fetchone()
        return dict(row) if row else None


def get_email_lead_by_email(email_addr: str):
    with get_db() as conn:
        row = conn.execute(f"""
            SELECT {_EMAIL_LEAD_COLUMNS} {_EMAIL_LEAD_JOIN}
             WHERE el.email=?
        """, (email_addr.lower(),)).fetchone()
        return dict(row) if row else None


def get_business(business_id: int):
    """A business plus which channels it is already being worked on.

    The per-channel flags are what the cross-channel duplicate warning reads:
    adding a clinic to WhatsApp when it is mid-sequence on email is a decision
    the operator should make deliberately, not discover afterwards.
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["channels"] = _business_channels(conn, business_id)
        return out


def _business_channels(conn, business_id: int) -> dict:
    """Which channels this business already exists on."""
    return get_channel_presence(conn, [business_id]).get(
        business_id, {"email": False, "call": False, "whatsapp": False}
    )


def get_channel_presence(conn, business_ids: list) -> dict:
    """
    Which of email / call / whatsapp each of these businesses already has a
    row on. {business_id: {"email": bool, "call": bool, "whatsapp": bool}}

    Batched rather than one query per business: this backs the cross-channel
    duplicate check at import time, where the whole point is not to run N+1
    queries against a 200-row CSV.
    """
    ids = list({int(b) for b in business_ids})
    out = {b: {"email": False, "call": False, "whatsapp": False} for b in ids}
    if not ids:
        return out
    placeholders = ",".join("?" * len(ids))
    for bid, in conn.execute(
        f"SELECT DISTINCT business_id FROM email_leads "
        f"WHERE business_id IN ({placeholders}) AND status != 'deleted'", ids,
    ):
        out[bid]["email"] = True
    for bid, in conn.execute(
        f"SELECT DISTINCT business_id FROM call_leads WHERE business_id IN ({placeholders})", ids,
    ):
        out[bid]["call"] = True
    for bid, in conn.execute(
        f"SELECT DISTINCT business_id FROM wa_leads WHERE business_id IN ({placeholders})", ids,
    ):
        out[bid]["whatsapp"] = True
    return out


_CHANNEL_LABELS = {"email": "Email", "call": "Calling", "whatsapp": "WhatsApp"}


def find_cross_channel_conflicts(rows: list, channel: str) -> list:
    """
    Which of these import rows resolve to a business already active on a
    DIFFERENT channel than the one they're about to be added to.

    Read-only -- this never creates or attaches anything, so it is safe to
    call before the operator has decided whether to proceed. A row whose
    business doesn't exist yet, or already exists only on `channel` itself
    (a normal re-import), is not a conflict.

    Returns a list of {row, business_id, business_name, channels} -- `row` is
    the original dict, `channels` the OTHER channels already present, in the
    stable order email/call/whatsapp regardless of lookup order, and labelled
    for direct display.
    """
    if not rows:
        return []
    with get_db() as conn:
        resolved = []   # (row, business_row) for rows that matched something
        for r in rows:
            existing = find_existing_business(
                conn,
                email=(r.get("email") or ""),
                phone=(r.get("phone") or ""),
                website=(r.get("website") or ""),
                company=(r.get("company") or r.get("name") or ""),
                address=(r.get("address") or ""),
            )
            if existing:
                resolved.append((r, existing))

        if not resolved:
            return []
        presence = get_channel_presence(conn, [b["id"] for _, b in resolved])

    conflicts = []
    for r, biz in resolved:
        other = [c for c in ("email", "call", "whatsapp")
                 if c != channel and presence[biz["id"]][c]]
        if other:
            conflicts.append({
                "row": r,
                "business_id": biz["id"],
                "business_name": biz["name"],
                "channels": other,
                "channel_labels": [_CHANNEL_LABELS[c] for c in other],
            })
    return conflicts


def channel_conflicts_for_businesses(business_ids: list, channel: str) -> list:
    """
    Which of these businesses already have a presence on a channel other than
    `channel`. Same idea as find_cross_channel_conflicts, for a caller that
    already has resolved business ids rather than raw import rows -- adding
    to a call campaign, for instance, where the picker already deals in
    business ids.
    """
    business_ids = list({int(b) for b in business_ids})
    if not business_ids:
        return []
    placeholders = ",".join("?" * len(business_ids))
    with get_db() as conn:
        presence = get_channel_presence(conn, business_ids)
        names = {r["id"]: r["name"] for r in conn.execute(
            f"SELECT id, name FROM businesses WHERE id IN ({placeholders})", business_ids,
        )}
    out = []
    for bid in business_ids:
        other = [c for c in ("email", "call", "whatsapp")
                 if c != channel and presence.get(bid, {}).get(c)]
        if other:
            out.append({
                "business_id": bid, "business_name": names.get(bid, ""),
                "channels": other, "channel_labels": [_CHANNEL_LABELS[c] for c in other],
            })
    return out


def delete_campaign(campaign_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM sends WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))


def delete_email_leads(ids: list):
    if not ids:
        return
    placeholders = ','.join('?' for _ in ids)
    with get_db() as conn:
        conn.execute(f"DELETE FROM email_leads WHERE id IN ({placeholders})", ids)


def create_email_lead(email: str, first_name='', last_name='', company='',
                      website='', address='', status='active'):
    """
    Add one address by hand, resolving it to a business the same way an import
    would -- so typing in an address for a clinic already on the list attaches
    it to that clinic instead of creating a second one.
    """
    email = email.strip().lower()
    if not email or '@' not in email:
        return None, 'Invalid email address'
    try:
        with get_db() as conn:
            business_id = find_or_create_business(conn, {
                "company": company, "website": website, "address": address,
            })
            cur = conn.execute(
                "INSERT INTO email_leads(business_id,email,first_name,last_name,status) "
                "VALUES(?,?,?,?,?)",
                (business_id, email, first_name, last_name, status)
            )
            conn.execute(
                "UPDATE businesses SET web_status='has_email' WHERE id=? AND web_status=''",
                (business_id,),
            )
            _pick_business_winner(conn, business_id)
            return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, 'A lead with that email already exists'


def update_email_lead(email_lead_id: int, fields: dict):
    """
    Edit one address, and the identity fields of the business behind it.

    Split by destination: `company`, `website` and `address` describe the
    business and are shared with every other channel, so writing them to the
    address row would leave the calling and WhatsApp views showing stale
    details for the same clinic.
    """
    lead_cols = {'email', 'first_name', 'last_name', 'status'}
    biz_cols  = {'company': 'name', 'website': 'website', 'address': 'address'}

    lead_updates = {k: v for k, v in fields.items() if k in lead_cols}
    biz_updates  = {biz_cols[k]: v for k, v in fields.items() if k in biz_cols}
    if not lead_updates and not biz_updates:
        return True, None
    if 'email' in lead_updates:
        lead_updates['email'] = lead_updates['email'].strip().lower()

    try:
        with get_db() as conn:
            if lead_updates:
                set_clause = ', '.join(f'{k}=?' for k in lead_updates)
                conn.execute(
                    f'UPDATE email_leads SET {set_clause} WHERE id=?',
                    (*lead_updates.values(), email_lead_id)
                )
            if biz_updates:
                if 'website' in biz_updates:
                    biz_updates['domain'] = canonical_domain(biz_updates['website'])
                set_clause = ', '.join(f'{k}=?' for k in biz_updates)
                conn.execute(
                    f'UPDATE businesses SET {set_clause} '
                    f'WHERE id=(SELECT business_id FROM email_leads WHERE id=?)',
                    (*biz_updates.values(), email_lead_id)
                )
        return True, None
    except sqlite3.IntegrityError:
        return False, 'A lead with that email already exists'


def delete_email_lead(email_lead_id: int):
    with get_db() as conn:
        conn.execute("UPDATE email_leads SET status='deleted' WHERE id=?", (email_lead_id,))


def unsubscribe_contact(email):
    """
    Mark an address unsubscribed and propagate to ALL its enrollments
    regardless of current status (a paused or replied enrollment must also
    stop sending if they opt out later).

    An unsubscribe is also recorded on the business, which suppresses the
    clinic on calling and WhatsApp too. Someone who asked to be left alone
    did not mean "by email only", and the old schema had no way to express
    that.
    """
    email_lc = email.lower()
    with get_db() as conn:
        conn.execute("UPDATE email_leads SET status='unsubscribed' WHERE email=?", (email_lc,))
        conn.execute("""
            UPDATE businesses SET do_not_contact=1
             WHERE id=(SELECT business_id FROM email_leads WHERE email=?)
        """, (email_lc,))
        conn.execute("""
            UPDATE enrollments SET status='unsubscribed'
            WHERE email_lead_id=(SELECT id FROM email_leads WHERE email=?)
              AND status NOT IN ('unsubscribed','bounced','completed','replied')
        """, (email_lc,))


def get_unsubscribed_contacts():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT el.email, el.first_name, el.last_name,
                   b.name AS company, el.created_at
              FROM email_leads el JOIN businesses b ON b.id = el.business_id
             WHERE el.status = 'unsubscribed'
             ORDER BY el.created_at DESC
        """).fetchall()]


def get_invalid_mx_contacts():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT el.email, b.name AS company, b.website, b.address, el.created_at
              FROM email_leads el JOIN businesses b ON b.id = el.business_id
             WHERE el.mx_valid = 0
             ORDER BY el.created_at DESC
        """).fetchall()]


def mark_bounced(email):
    with get_db() as conn:
        conn.execute("UPDATE email_leads SET status='bounced' WHERE email=?", (email.lower(),))
        conn.execute("""
            UPDATE enrollments SET status='bounced'
            WHERE email_lead_id=(SELECT id FROM email_leads WHERE email=?)
              AND status='queued'
        """, (email.lower(),))


def increment_soft_bounce(email: str, threshold: int = 3):
    """
    Increment soft-bounce counter for an address.
    Once the counter hits threshold, treat it as a hard bounce.
    Everything runs in one transaction to avoid deadlocks.
    """
    email = email.lower()
    with get_db() as conn:
        conn.execute(
            "UPDATE email_leads SET soft_bounce_count = soft_bounce_count + 1 WHERE email=?",
            (email,)
        )
        row = conn.execute(
            "SELECT soft_bounce_count FROM email_leads WHERE email=?", (email,)
        ).fetchone()
        count = row["soft_bounce_count"] if row else 0

        if count >= threshold:
            conn.execute("UPDATE email_leads SET status='bounced' WHERE email=?", (email,))
            conn.execute("""
                UPDATE enrollments SET status='bounced'
                WHERE email_lead_id=(SELECT id FROM email_leads WHERE email=?)
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
                      WHERE s.campaign_id   = e.campaign_id
                        AND s.email_lead_id = e.email_lead_id
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
    # Which table each variable actually comes from. Names the operator types
    # in a template do not change, but half of them describe the business and
    # half the person at it, and the join has to know which is which.
    _VAR_SOURCE = {
        "first_name": "el", "last_name": "el", "email": "el",
        "company": "b", "phone": "b", "website": "b", "category": "b",
        "rating": "b", "review_count": "b", "address": "b",
    }
    _VAR_COLUMN = {"company": "name"}

    join = ("FROM email_leads el JOIN businesses b ON b.id = el.business_id")
    scope = "campaign"
    where = """
        WHERE el.id IN (SELECT email_lead_id FROM enrollments WHERE campaign_id = ?)
    """
    params = [campaign_id]

    with get_db() as conn:
        if campaign_id is not None:
            total = conn.execute(f"SELECT COUNT(*) {join} {where}", params).fetchone()[0]
        else:
            total = 0

        if not total:
            scope  = "all"
            where  = "WHERE el.status NOT IN ('deleted','unsubscribed','bounced')"
            params = []
            total  = conn.execute(f"SELECT COUNT(*) {join} {where}", params).fetchone()[0]

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
                expr = ("(COALESCE(NULLIF(TRIM(el.first_name),''),"
                        " NULLIF(TRIM(el.last_name),'')) IS NOT NULL)")
            else:
                col = f"{_VAR_SOURCE[key]}.{_VAR_COLUMN.get(key, key)}"
                if key in ("rating", "review_count"):
                    expr = f"({col} IS NOT NULL)"
                else:
                    expr = f"(NULLIF(TRIM(COALESCE({col},'')),'') IS NOT NULL)"
            pieces.append(f"SUM(CASE WHEN {expr} THEN 1 ELSE 0 END) AS {key}")

        row = conn.execute(
            f"SELECT {', '.join(pieces)} {join} {where}", params
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
            LEFT JOIN sends s ON s.campaign_id=e.campaign_id AND s.email_lead_id=e.email_lead_id
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
                el.email,
                el.first_name,
                el.last_name,
                b.name AS company,
                e.variant_label,
                e.status,
                e.current_step,
                e.next_send_at,
                e.enrolled_at,
                COUNT(s.id) AS steps_sent
            FROM enrollments e
            JOIN email_leads el ON el.id = e.email_lead_id
            JOIN businesses  b  ON b.id  = el.business_id
            LEFT JOIN sends s
                   ON s.campaign_id   = e.campaign_id
                  AND s.email_lead_id = e.email_lead_id
            WHERE e.campaign_id = ?
            GROUP BY e.id
            ORDER BY e.enrolled_at DESC
        """, (campaign_id,)).fetchall()
        return [dict(r) for r in rows]


def enroll_contacts_bulk(campaign_id, email_lead_ids):
    """
    Enroll addresses, skipping any that would produce a duplicate approach.

    Returns (enrolled, skipped) where skipped explains why. The UNIQUE
    constraint only stops re-enrolling in the SAME campaign; nothing stopped an
    address sitting in two campaigns at once and receiving two different cold
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

    one_per_business = get_settings().get("one_sequence_per_domain", "0") == "1"

    with get_db() as conn:
        enrolled = 0
        skipped = {"other_campaign": 0, "duplicate_address": 0,
                   "same_domain": 0, "do_not_contact": 0}

        for lead_id in email_lead_ids:
            try:
                row = conn.execute("""
                    SELECT el.id, el.business_id, el.duplicate_of, b.do_not_contact
                      FROM email_leads el JOIN businesses b ON b.id = el.business_id
                     WHERE el.id=?
                """, (lead_id,)).fetchone()
                if not row:
                    continue

                # Opted out on any channel. Checked here rather than trusted to
                # the address's own status, because the request may have come
                # in over WhatsApp or on a call.
                if row["do_not_contact"]:
                    skipped["do_not_contact"] += 1
                    continue

                # Suppressed as a duplicate address at a business we already
                # have a better address for.
                if row["duplicate_of"] is not None:
                    skipped["duplicate_address"] += 1
                    continue

                # Already being worked by another campaign.
                busy = conn.execute("""
                    SELECT 1 FROM enrollments
                     WHERE email_lead_id=? AND campaign_id != ?
                       AND status NOT IN ('completed','replied','unsubscribed','bounced')
                     LIMIT 1
                """, (lead_id, campaign_id)).fetchone()
                if busy:
                    skipped["other_campaign"] += 1
                    continue

                # Optional stricter rule: one live sequence per business rather
                # than per address, for operators who would rather
                # under-contact. This used to compare `domain` strings and
                # needed a freemail exemption to avoid treating gmail.com as
                # one enormous business; comparing business_id needs no such
                # exception because it is the identity, not a proxy for it.
                if one_per_business:
                    same_business = conn.execute("""
                        SELECT 1 FROM enrollments e
                          JOIN email_leads el ON el.id = e.email_lead_id
                         WHERE el.business_id=? AND e.email_lead_id != ?
                           AND e.status NOT IN ('completed','replied','unsubscribed','bounced')
                         LIMIT 1
                    """, (row["business_id"], lead_id)).fetchone()
                    if same_business:
                        skipped["same_domain"] += 1
                        continue

                variant_label = _pick_variant(variants) if variants else None
                cur = conn.execute("""
                    INSERT OR IGNORE INTO enrollments
                        (campaign_id,email_lead_id,current_step,status,next_send_at,variant_label)
                    VALUES(?,?,1,'queued',?,?)
                """, (campaign_id, lead_id, now, variant_label))
                enrolled += cur.rowcount
            except Exception as e:
                logger.warning(f"Failed to enroll email lead {lead_id}: {e}")

        return enrolled, skipped


def unenroll_contact(enroll_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM enrollments WHERE id=?", (enroll_id,))


def get_campaign_contacts(campaign_id):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT el.email, el.first_name, el.last_name, b.name AS company,
                   el.status as contact_status,
                   e.id as enroll_id, e.current_step, e.status, e.next_send_at, e.enrolled_at,
                   e.variant_label
            FROM email_leads el
            JOIN businesses  b ON b.id = el.business_id
            JOIN enrollments e ON e.email_lead_id = el.id
            WHERE e.campaign_id=?
            ORDER BY e.enrolled_at DESC
        """, (campaign_id,)).fetchall()]


def get_due_enrollments(campaign_id, limit=20):
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT e.id as enroll_id, e.campaign_id, e.email_lead_id,
                   e.current_step, e.next_send_at, e.variant_label,
                   el.email, el.first_name, el.last_name,
                   b.name AS company, b.extra,
                   -- Available as {{phone}}, {{category}} and so on. These
                   -- used to ride along inside `extra`; promoting them to real
                   -- columns emptied that blob, so leaving them out here would
                   -- silently retire template variables that already worked.
                   b.phone, b.category, b.rating, b.review_count,
                   b.website, b.address
            FROM enrollments e
            JOIN email_leads el ON el.id = e.email_lead_id
            JOIN businesses  b  ON b.id  = el.business_id
            WHERE e.campaign_id=?
              AND e.status='queued'
              AND el.status='active'
              -- An opt-out on any channel stops the send here, not just an
              -- unsubscribe on this address.
              AND b.do_not_contact = 0
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


def mark_enrollment_replied(campaign_id, email_lead_id):
    """Mark a (campaign, address) enrollment as replied.

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
             WHERE campaign_id=? AND email_lead_id=?
               AND status NOT IN ('replied','bounced','unsubscribed')
        """, (campaign_id, email_lead_id))
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


def log_send(campaign_id, email_lead_id, step_num, subject, msg_id, account_id=None):
    today = datetime.date.today().isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sends(campaign_id,email_lead_id,step_num,subject,msg_id,account_id)
            VALUES(?,?,?,?,?,?)
        """, (campaign_id, email_lead_id, step_num, subject, msg_id, account_id))
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


def find_existing_business(conn, email="", phone="", website="", company="", address="",
                           exclude_id=None):
    """
    Find the business row matching these details. Returns a row or None.
    Read-only -- never creates a row; find_or_create_business wraps this with
    the create-if-missing step.

    Four keys, strongest first, because no single field covers the list:

      1. An address already on file -- more authoritative than re-deriving
         identity from whatever website or company name this row happens to
         declare. Without this, the same address reappearing under a
         different claimed company (a shared billing inbox, a re-scrape with
         a typo'd site) would resolve to a second, wrong business.
      2. Normalized phone -- the right key for calling. Two rows that dial the
         same number are one conversation, whoever they claim to be.
      3. Canonical domain -- the right key for email, falling back to the
         email's own domain when no separate website is given (a business
         pasted in as just "name, email"). Freemail is excluded either way:
         gmail.com identifies a mailbox provider, not a business, and treating
         it as an identity would collapse every Gmail lead into one.
      4. Normalized company AND locality -- last resort, for the no-website
         leads that have neither of the above. Never company alone: that would
         merge "Main Street Dental" in St John's with the one in Toronto.
    """
    email = (email or "").strip().lower()
    if email and "@" in email:
        row = conn.execute("""
            SELECT b.* FROM businesses b JOIN email_leads el ON el.business_id=b.id
             WHERE el.email=? AND (? IS NULL OR b.id != ?) LIMIT 1
        """, (email, exclude_id, exclude_id or -1)).fetchone()
        if row:
            return row

    phone_key = normalize_phone(phone)
    if phone_key:
        row = conn.execute(
            "SELECT * FROM businesses WHERE phone_normalized=? AND phone_normalized!='' "
            "AND (? IS NULL OR id != ?) LIMIT 1",
            (phone_key, exclude_id, exclude_id or -1),
        ).fetchone()
        if row:
            return row

    domain = canonical_domain(website)
    if not domain and email and "@" in email:
        candidate = email.split("@")[-1]
        if not is_freemail(candidate):
            domain = candidate
    if domain and not is_freemail(domain):
        row = conn.execute(
            "SELECT * FROM businesses WHERE domain=? AND domain!='' "
            "AND (? IS NULL OR id != ?) LIMIT 1",
            (domain, exclude_id, exclude_id or -1),
        ).fetchone()
        if row:
            return row

    name_key = normalize_company(company)
    place    = _locality_key(address)
    if name_key and place:
        for row in conn.execute(
            "SELECT * FROM businesses WHERE name!='' AND (? IS NULL OR id != ?)",
            (exclude_id, exclude_id or -1),
        ).fetchall():
            if (normalize_company(row["name"]) == name_key
                    and _locality_key(row["address"]) == place):
                return row
    return None


def get_touch_history(business_id: int) -> dict:
    """
    How this business has already been contacted, across every channel.

    Distinct from duplicate detection: "is this the same row" and "have I
    already worked this lead" are different questions, and only the second one
    decides whether to dial. A previously-emailed lead with no reply is still
    worth a call; one that already said no is not -- so this reports rather
    than hides.

    Now genuinely cross-channel: it reports WhatsApp alongside email, which
    the old contact-scoped version could not see at all.
    """
    with get_db() as conn:
        emails = conn.execute("""
            SELECT COUNT(*) AS n, MAX(s.sent_at) AS last
              FROM sends s JOIN email_leads el ON el.id = s.email_lead_id
             WHERE el.business_id = ?
        """, (business_id,)).fetchone()
        enrolled = conn.execute("""
            SELECT c.name AS campaign, e.status
              FROM enrollments e
              JOIN campaigns   c  ON c.id  = e.campaign_id
              JOIN email_leads el ON el.id = e.email_lead_id
             WHERE el.business_id = ?
             ORDER BY e.enrolled_at DESC
        """, (business_id,)).fetchall()
        wa = conn.execute("""
            SELECT COUNT(*) AS n, MAX(l.sent_at) AS last
              FROM wa_log l JOIN wa_leads w ON w.id = l.wa_lead_id
             WHERE w.business_id = ?
        """, (business_id,)).fetchone()
        wa_replied = conn.execute(
            "SELECT COALESCE(MAX(replied),0) FROM wa_leads WHERE business_id=?",
            (business_id,),
        ).fetchone()[0]

    return {
        "emails_sent":   emails["n"] or 0,
        "last_email_at": emails["last"],
        "campaigns":     [dict(r) for r in enrolled],
        "wa_sent":       wa["n"] or 0,
        "last_wa_at":    wa["last"],
        # Terminal states mean the prospect has already answered -- surfaced so
        # the call list can warn rather than silently re-work them.
        "closed":        bool(wa_replied) or any(
            r["status"] in ("replied", "unsubscribed", "bounced") for r in enrolled
        ),
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


def has_sent_step(campaign_id, email_lead_id, step_num) -> bool:
    """
    Has this exact step already gone to this address?

    Sending is three separate writes -- deliver over SMTP, log the send,
    advance the enrollment -- and a restart between the first and the last
    leaves the enrollment still queued on a step the recipient has already
    received. Without this check the scheduler simply sends it again. That
    window is small, but a deploy lands in it eventually, and the cost is a
    duplicate cold email to a prospect.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM sends WHERE campaign_id=? AND email_lead_id=? AND step_num=? LIMIT 1",
            (campaign_id, email_lead_id, step_num),
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

# The outcomes every install starts with. Seeded into call_outcome_types on
# first run; the operator adds their own alongside them.
#
# key: (label, is_terminal, stops_email, requires_date, tone, sort_order)
_BUILTIN_CALL_OUTCOMES = {
    "no_answer":      ("No answer",        False, False, False, "neutral", 10),
    "voicemail":      ("Left voicemail",   False, False, False, "neutral", 20),
    "callback":       ("Callback booked",  False, False, True,  "info",    30),
    "interested":     ("Interested",       False, False, False, "good",    40),
    "proposal_sent":  ("Proposal sent",    False, False, False, "good",    50),
    "booked":         ("Meeting booked",   True,  True,  True,  "good",    60),
    "not_interested": ("Not interested",   True,  True,  False, "bad",     70),
    "wrong_number":   ("Wrong number",     True,  True,  False, "bad",     80),
    "do_not_call":    ("Do not call",      True,  True,  False, "bad",     90),
}


def _seed_call_outcomes(conn):
    """Insert the builtins once. Never updates them -- an operator who renamed
    'Interested' to something that suits their pitch should keep that."""
    for key, (label, term, stops, needs_date, tone, order) in _BUILTIN_CALL_OUTCOMES.items():
        conn.execute("""
            INSERT OR IGNORE INTO call_outcome_types
                (key, label, is_terminal, stops_email, requires_date, tone,
                 sort_order, is_builtin)
            VALUES(?,?,?,?,?,?,?,1)
        """, (key, label, int(term), int(stops), int(needs_date), tone, order))


def get_call_outcomes(include_archived=False):
    """Every outcome the operator can pick, keyed for lookup."""
    where = "" if include_archived else "WHERE archived = 0"
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM call_outcome_types {where} ORDER BY sort_order, label"
        ).fetchall()
    return {r["key"]: dict(r) for r in rows}


def get_call_outcome(key: str):
    """One outcome, archived ones included -- historical rows still name them."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM call_outcome_types WHERE key=?", (key,)
        ).fetchone()
        return dict(row) if row else None


def terminal_outcome_keys():
    with get_db() as conn:
        return [r["key"] for r in conn.execute(
            "SELECT key FROM call_outcome_types WHERE is_terminal = 1"
        ).fetchall()]


def _slugify_outcome(label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
    return base[:40] or "outcome"


def create_call_outcome(label, is_terminal=False, stops_email=False,
                        requires_date=False, tone="neutral") -> str:
    """
    Add an outcome. Returns its key.

    The key is derived from the label and then kept for good, because call_log
    rows point at it -- renaming the label later changes what you see
    everywhere, including on old calls, while the key underneath stays put.
    """
    with get_db() as conn:
        base = _slugify_outcome(label)
        key, n = base, 2
        while conn.execute("SELECT 1 FROM call_outcome_types WHERE key=?", (key,)).fetchone():
            key, n = f"{base}_{n}", n + 1
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 10 FROM call_outcome_types"
        ).fetchone()[0]
        conn.execute("""
            INSERT INTO call_outcome_types
                (key, label, is_terminal, stops_email, requires_date, tone,
                 sort_order, is_builtin)
            VALUES(?,?,?,?,?,?,?,0)
        """, (key, (label or "").strip()[:60] or key, int(bool(is_terminal)),
              int(bool(stops_email)), int(bool(requires_date)),
              tone if tone in ("neutral", "good", "bad", "info") else "neutral", nxt))
        return key


def update_call_outcome(key: str, **fields):
    """
    Edit an outcome. The key is never editable.

    Builtins can be relabelled and recoloured but keep their behaviour: the
    code special-cases 'do_not_call' for unsubscribing and 'booked' for the
    calendar file, so letting those flags be flipped would quietly break both.
    """
    row = get_call_outcome(key)
    if not row:
        return False
    allowed = {"label", "tone", "sort_order"}
    if not row["is_builtin"]:
        allowed |= {"is_terminal", "stops_email", "requires_date", "archived"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    sets = ", ".join(f"{k}=?" for k in updates)
    with get_db() as conn:
        conn.execute(f"UPDATE call_outcome_types SET {sets} WHERE key=?",
                     (*updates.values(), key))
    return True


def delete_call_outcome(key: str):
    """
    Remove a custom outcome, or archive it if calls already used it.

    Returns ('deleted'|'archived'|'refused'). Archiving rather than deleting a
    used outcome keeps old call records readable -- a history row saying
    'outcome: follow_up_later' is useless once nothing can resolve that name.
    """
    row = get_call_outcome(key)
    if not row or row["is_builtin"]:
        return "refused"
    with get_db() as conn:
        used = conn.execute(
            "SELECT 1 FROM call_log WHERE outcome=? LIMIT 1", (key,)
        ).fetchone()
        if used:
            conn.execute("UPDATE call_outcome_types SET archived=1 WHERE key=?", (key,))
            return "archived"
        conn.execute("DELETE FROM call_outcome_types WHERE key=?", (key,))
        return "deleted"

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


def create_call_campaign(name: str, notes: str = "") -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO call_campaigns(name, notes) VALUES(?,?)",
            (name.strip() or "Untitled call campaign", notes or ""),
        )
        return cur.lastrowid


def update_call_campaign(cid: int, **fields):
    allowed = {"name", "notes", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    with get_db() as conn:
        conn.execute(f"UPDATE call_campaigns SET {sets} WHERE id=?",
                     (*updates.values(), cid))


def delete_call_campaign(cid: int):
    """Removes the campaign and its membership. Contacts and calls are kept."""
    with get_db() as conn:
        conn.execute("DELETE FROM call_campaign_members WHERE call_campaign_id=?", (cid,))
        conn.execute("DELETE FROM call_campaigns WHERE id=?", (cid,))


def add_to_call_campaign(cid: int, business_ids) -> int:
    """
    Add businesses to a call campaign, ignoring any already in it.

    Takes business ids rather than call-lead ids because that is what the
    operator is choosing from -- a clinic that has never been dialled has no
    call lead yet, and requiring one before it could be added would make the
    never-called leads the only ones you could not put in a campaign.
    """
    added = 0
    with get_db() as conn:
        for business_id in business_ids:
            call_lead_id = get_or_create_call_lead(conn, int(business_id))
            cur = conn.execute("""
                INSERT OR IGNORE INTO call_campaign_members(call_campaign_id, call_lead_id)
                VALUES(?,?)
            """, (cid, call_lead_id))
            added += cur.rowcount
    return added


def remove_from_call_campaign(cid: int, business_ids) -> int:
    with get_db() as conn:
        removed = 0
        for business_id in business_ids:
            cur = conn.execute("""
                DELETE FROM call_campaign_members
                 WHERE call_campaign_id=?
                   AND call_lead_id IN (SELECT id FROM call_leads WHERE business_id=?)
            """, (cid, int(business_id)))
            removed += cur.rowcount
        return removed


def get_or_create_call_lead(conn, business_id: int) -> int:
    """
    The call lead for a business, created on first use.

    Calling a clinic for the first time is what brings it onto the calling
    list; there is no separate import step, and a business can be worked on
    email or WhatsApp without ever appearing here.
    """
    row = conn.execute(
        "SELECT id FROM call_leads WHERE business_id=?", (business_id,)
    ).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO call_leads(business_id) VALUES(?)", (business_id,)
    ).lastrowid


def get_call_campaigns():
    """
    Every campaign with its progress. One query per campaign is fine at this
    scale and keeps the counting rules in one readable place rather than a
    lattice of correlated subqueries.
    """
    terminal = terminal_outcome_keys() or ["__none__"]
    placeholders = ",".join("?" * len(terminal))

    with get_db() as conn:
        campaigns = [dict(r) for r in conn.execute(
            "SELECT * FROM call_campaigns ORDER BY id DESC"
        ).fetchall()]

        for c in campaigns:
            row = conn.execute(f"""
                SELECT
                  COUNT(*)                                                        AS total,
                  SUM(CASE WHEN COALESCE(cl.call_status,'') = '' THEN 1 ELSE 0 END) AS uncalled,
                  SUM(CASE WHEN cl.call_status IN ({placeholders}) THEN 1 ELSE 0 END) AS closed,
                  SUM(CASE WHEN cl.call_status = 'booked' THEN 1 ELSE 0 END)      AS booked,
                  SUM(CASE WHEN cl.call_status = 'not_interested' THEN 1 ELSE 0 END) AS not_interested,
                  SUM(CASE WHEN cl.next_call_at IS NOT NULL
                            AND datetime(cl.next_call_at) <= datetime('now')
                            AND cl.call_status NOT IN ({placeholders})
                           THEN 1 ELSE 0 END)                                     AS due
                  FROM call_campaign_members m
                  JOIN call_leads cl ON cl.id = m.call_lead_id
                  JOIN businesses b  ON b.id  = cl.business_id
                 WHERE m.call_campaign_id = ?
            """, (*terminal, *terminal, c["id"])).fetchone()

            c.update({k: (row[k] or 0) for k in
                      ("total", "uncalled", "closed", "booked", "not_interested", "due")})
            # What is left to work, which is the number you actually plan by.
            c["remaining"] = c["total"] - c["closed"]
    return campaigns


def log_call(call_lead_id: int, outcome: str, notes: str = "", next_call_at: str = None,
             call_campaign_id: int = None) -> dict:
    """
    Record a call attempt and move the lead's calling state forward.

    Terminal outcomes also stop any live email sequence. Telling someone "not
    interested" on the phone and then having the scheduler send them a cheerful
    follow-up two days later is the specific embarrassment this prevents --
    the two channels have to share the same answer.
    """
    spec = get_call_outcome(outcome)
    if not spec:
        raise ValueError(f"Unknown call outcome: {outcome}")

    is_terminal = bool(spec["is_terminal"])
    stops_email = bool(spec["stops_email"])
    # Any outcome may carry a date; requires_date only marks the ones that make
    # no sense without one. That distinction is what lets "follow up later"
    # exist without inventing a commitment nobody made.
    wants_next  = bool(spec["requires_date"])

    # "Booked" is both terminal and dated: the lead leaves the queue, but the
    # meeting time is the most valuable thing on the record and the calendar
    # invite is generated from it. Clearing the date for every terminal outcome
    # threw that away. A date is dropped only when the outcome has no use for
    # one -- otherwise a dead lead would carry a stale callback forever.
    keeps_date = wants_next or not is_terminal

    with get_db() as conn:
        conn.execute("""
            INSERT INTO call_log(call_lead_id, outcome, notes, next_call_at, call_campaign_id)
            VALUES(?,?,?,?,?)
        """, (call_lead_id, outcome, notes or "", next_call_at or None,
              int(call_campaign_id) if call_campaign_id else None))

        # Retirement from the queue comes from call_status being terminal, not
        # from clearing the date, so keeping a booked meeting's time cannot put
        # the lead back in tomorrow's list.
        conn.execute("""
            UPDATE call_leads
               SET call_status   = ?,
                   next_call_at  = ?,
                   call_attempts = call_attempts + 1
             WHERE id = ?
        """, (outcome, (next_call_at or None) if keeps_date else None, call_lead_id))

        if stops_email:
            conn.execute("""
                UPDATE enrollments SET status='completed'
                 WHERE email_lead_id IN (
                        SELECT el.id FROM email_leads el
                         WHERE el.business_id = (SELECT business_id FROM call_leads WHERE id=?)
                       )
                   AND status NOT IN ('completed','replied','unsubscribed','bounced')
            """, (call_lead_id,))

        # "Do not call" is a request about contact, not about the phone. Set on
        # the business so it suppresses email and WhatsApp too -- honouring it
        # on one channel only is not honouring it.
        if outcome == "do_not_call":
            conn.execute("""
                UPDATE businesses SET do_not_contact=1
                 WHERE id = (SELECT business_id FROM call_leads WHERE id=?)
            """, (call_lead_id,))

    return {"outcome": outcome, "terminal": is_terminal, "stopped_email": stops_email}


# A business plus its calling state, shaped to match what the old `contacts`
# row looked like to the calling UI: `company` for the name, `status` for what
# the scraper found (web_status), the rest passed through. Every calling view
# reads through this so the frontend needed no changes for the identity split.
_CALL_LEAD_COLUMNS = """
    b.id, b.name AS company, b.phone, b.phone_normalized, b.website, b.domain,
    b.address, b.city, b.country, b.category, b.rating, b.review_count,
    b.web_status AS status, b.source_job_id, b.do_not_contact,
    b.notes AS business_notes, b.created_at,
    cl.id AS call_lead_id, COALESCE(cl.call_status,'') AS call_status,
    cl.next_call_at, COALESCE(cl.call_attempts,0) AS call_attempts
"""
_CALL_LEAD_JOIN = "FROM businesses b LEFT JOIN call_leads cl ON cl.business_id = b.id"


def get_call_lead_view(business_id: int):
    """One business plus its calling state, or None. Backs the call detail card."""
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {_CALL_LEAD_COLUMNS} {_CALL_LEAD_JOIN} WHERE b.id=?", (business_id,)
        ).fetchone()
        return dict(row) if row else None


def search_businesses(q="", status=None, call_status=None, limit=100):
    """
    Businesses matching a filter, for the "add existing leads" pickers (call
    campaigns today; WhatsApp will use the same query).

    Separate from the email-lead search: this looks at businesses directly, so
    a clinic the scraper found with no email at all is still findable here,
    which is the entire point of the calling "from contacts" tab.
    """
    where, params = ["1=1"], []
    if status:
        where.append("b.web_status = ?")
        params.append(status)
    if call_status == "none":
        where.append("COALESCE(cl.call_status,'') = ''")
    elif call_status == "any":
        where.append("COALESCE(cl.call_status,'') != ''")
    elif call_status:
        where.append("cl.call_status = ?")
        params.append(call_status)
    q = (q or "").strip()
    if q:
        where.append("(b.name LIKE ? OR b.phone LIKE ? OR b.website LIKE ? OR b.address LIKE ?)")
        params.extend([f"%{q}%"] * 4)

    params.append(int(limit))
    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) {_CALL_LEAD_JOIN} WHERE {' AND '.join(where)}", params[:-1]
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_CALL_LEAD_COLUMNS} {_CALL_LEAD_JOIN} "
            f"WHERE {' AND '.join(where)} ORDER BY b.created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return {"rows": [dict(r) for r in rows], "total": total}


def get_call_queue(bucket="today", limit=200, source_job_id=None, only_no_website=False,
                   call_campaign_id=None):
    """
    The leads to work right now.

      today    -- callbacks due (including overdue), soonest first
      new      -- never called, freshest leads first
      upcoming -- callbacks scheduled beyond today
      all      -- everything still callable

    Terminal outcomes and opted-out businesses are excluded everywhere: a
    finished lead should never reappear in a queue, whichever bucket is open.

    Selects from businesses rather than call_leads, LEFT JOINing the calling
    state. A clinic that has never been dialled has no call_leads row yet --
    one is created the moment a call is logged -- and drawing only from that
    table would make the never-called leads, which are the whole point of the
    "new" bucket, invisible.
    """
    terminal = terminal_outcome_keys() or ["__none__"]
    placeholders = ",".join("?" * len(terminal))

    cols = _CALL_LEAD_COLUMNS
    join = _CALL_LEAD_JOIN

    where = [
        f"(COALESCE(cl.call_status,'') = '' OR cl.call_status NOT IN ({placeholders}))",
        "b.do_not_contact = 0",
        "COALESCE(b.phone,'') != ''",
    ]
    params = list(terminal)

    def _apply_common(where, params):
        if source_job_id:
            where.append("b.source_job_id = ?")
            params.append(int(source_job_id))
        if only_no_website:
            where.append("b.web_status = 'no_website'")
        if call_campaign_id:
            where.append(
                "cl.id IN (SELECT call_lead_id FROM call_campaign_members "
                "WHERE call_campaign_id = ?)"
            )
            params.append(int(call_campaign_id))

    # "worked" is the opposite of every other bucket: it exists precisely to
    # show the leads the others hide, so a lead closed out by mistake can be
    # found and reopened instead of disappearing.
    if bucket == "worked":
        where = [f"cl.call_status IN ({placeholders})"]
        params = list(terminal)
        _apply_common(where, params)
        params.append(int(limit))
        with get_db() as conn:
            return [dict(r) for r in conn.execute(f"""
                SELECT {cols} {join}
                 WHERE {' AND '.join(where)}
                 ORDER BY b.id DESC LIMIT ?
            """, params).fetchall()]

    if bucket == "today":
        where.append("cl.next_call_at IS NOT NULL AND datetime(cl.next_call_at) <= datetime('now')")
        order = "cl.next_call_at ASC"
    elif bucket == "new":
        where.append("COALESCE(cl.call_status,'') = ''")
        order = "b.created_at DESC"
    elif bucket == "upcoming":
        where.append("cl.next_call_at IS NOT NULL AND datetime(cl.next_call_at) > datetime('now')")
        order = "cl.next_call_at ASC"
    else:
        order = "cl.next_call_at IS NULL, cl.next_call_at ASC, b.created_at DESC"

    _apply_common(where, params)

    params.append(int(limit))
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT {cols} {join}
             WHERE {' AND '.join(where)}
             ORDER BY {order}
             LIMIT ?
        """, params).fetchall()
        return [dict(r) for r in rows]


def get_call_queue_counts(source_job_id=None, only_no_website=False, call_campaign_id=None):
    """Bucket sizes, so the tabs can show what is waiting without loading it."""
    return {
        b: len(get_call_queue(b, limit=100000, source_job_id=source_job_id,
                              only_no_website=only_no_website,
                              call_campaign_id=call_campaign_id))
        for b in ("today", "new", "upcoming", "worked")
    }


def reopen_call_lead(call_lead_id: int) -> bool:
    """
    Put a closed-out lead back in the queue.

    Marking the wrong row terminal during a calling session is easy, and
    without this the only remedy is editing the database by hand. The call
    history is left intact -- what happened still happened, this only says the
    lead is workable again. A do-not-contact flag is deliberately not undone
    here: that was a request from the prospect, not a misclick.
    """
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE call_leads SET call_status='', next_call_at=NULL WHERE id=?",
            (call_lead_id,),
        )
        return cur.rowcount > 0


def get_call_summary(call_campaign_id=None) -> dict:
    """
    Totals across calling, scoped to a campaign when one is selected so the
    numbers agree with the list underneath rather than describing some wider
    population the operator is not looking at.
    """
    terminal = terminal_outcome_keys() or ["__none__"]
    tph = ",".join("?" * len(terminal))

    scope, scope_params = "", []
    if call_campaign_id:
        scope = ("AND cl.id IN (SELECT call_lead_id FROM call_campaign_members "
                 "WHERE call_campaign_id = ?)")
        scope_params = [int(call_campaign_id)]

    with get_db() as conn:
        row = conn.execute(f"""
            SELECT
              COUNT(*)                                                          AS leads,
              SUM(CASE WHEN cl.call_status = 'booked' THEN 1 ELSE 0 END)        AS booked,
              SUM(CASE WHEN cl.call_status = 'not_interested' THEN 1 ELSE 0 END) AS not_interested,
              SUM(CASE WHEN cl.next_call_at IS NOT NULL
                        AND datetime(cl.next_call_at) <= datetime('now')
                        AND cl.call_status NOT IN ({tph}) THEN 1 ELSE 0 END)    AS due
              FROM businesses b LEFT JOIN call_leads cl ON cl.business_id = b.id
             WHERE b.do_not_contact = 0 AND COALESCE(b.phone,'') != '' {scope}
        """, (*terminal, *scope_params)).fetchone()

        # Calls, not leads: one clinic rung four times is four calls, and that
        # is the number that reflects a day's work.
        call_scope, call_params = "", []
        if call_campaign_id:
            call_scope = "WHERE call_campaign_id = ?"
            call_params = [int(call_campaign_id)]
        made = conn.execute(
            f"SELECT COUNT(*) FROM call_log {call_scope}", call_params
        ).fetchone()[0]
        today_where = "WHERE DATE(called_at) = DATE('now')"
        if call_campaign_id:
            today_where += " AND call_campaign_id = ?"
        today = conn.execute(
            f"SELECT COUNT(*) FROM call_log {today_where}", call_params
        ).fetchone()[0]

    return {
        "leads":          row["leads"] or 0,
        "booked":         row["booked"] or 0,
        "not_interested": row["not_interested"] or 0,
        "due":            row["due"] or 0,
        "calls_made":     made or 0,
        "calls_today":    today or 0,
    }


def get_call_history(business_id: int):
    """Every call to this business, newest first, across all its call leads."""
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT l.* FROM call_log l
              JOIN call_leads cl ON cl.id = l.call_lead_id
             WHERE cl.business_id=?
             ORDER BY l.called_at DESC, l.id DESC
        """, (business_id,)).fetchall()]


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


# ── WhatsApp ──────────────────────────────────────────────────────────────────
#
# Sending is manual by design -- see wa_leads' own comment in init_db. Nothing
# below ever transmits anything; it stages a business as a lead, detects and
# records a confirmable signal, drafts a message, and tracks that the
# operator clicked Open in WhatsApp. The follow-up cadence is a live query
# (get_wa_followups_due), not a scheduled job, on the same principle: no
# background code path in this module touches the network unattended.

# Dialling codes for the numbers this module actually needs to format. Not a
# general phone library -- normalize_phone is NANP-only for the same reason,
# and Gulf numbers need a different rule (no NANP-style "drop everything but
# the last 10 digits"; a UAE or Qatar number has no fixed total length once
# the country code is included). Extend this as new countries come up.
WA_COUNTRY_CODES = {"AE": "971", "QA": "974"}


def format_whatsapp_number(raw: str, country: str = "") -> str:
    """
    Digits only, full international form, ready to drop straight into a
    wa.me link. wa.me rejects a leading + or a local trunk 0, and Google
    Maps shows Gulf numbers in local format ("050 123 4567") with no country
    code at all, so this has to add what Maps left out rather than just
    stripping punctuation the way normalize_phone does for NANP numbers.

    Returns '' if there are no digits to work with. If `country` is
    unrecognized, returns the digits as-is rather than guessing a country --
    a wrong guess produces a wa.me link that silently opens the wrong chat,
    which is worse than a lead the operator has to fix by hand.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    country = (country or "").strip().upper()
    code = WA_COUNTRY_CODES.get(country)
    if not code:
        for c in WA_COUNTRY_CODES.values():
            if digits.startswith(c):
                return digits
        return digits

    if digits.startswith(code):
        return digits
    if digits.startswith("00" + code):
        return digits[2:]
    # UAE numbers are dialled with a leading trunk 0 ("050 123 4567") that
    # the country code replaces; Qatar has no trunk prefix to strip at all.
    if country == "AE" and digits.startswith("0"):
        digits = digits[1:]
    return code + digits


def classify_number_type(raw: str, country: str = "") -> str:
    """
    'mobile' / 'landline' / 'unknown' from the dialling prefix.

    A soft signal, not a filter: WhatsApp Business does run on landlines, so
    this sorts the queue (mobiles first, since they're the likelier hit)
    rather than hiding anything. Unrecognized country or prefix both fall
    through to 'unknown' rather than a guess.
    """
    digits = re.sub(r"\D", "", raw or "")
    country = (country or "").strip().upper()
    if not digits:
        return "unknown"

    if country == "AE":
        local = digits
        if local.startswith("971"):
            local = local[3:]
        elif local.startswith("0"):
            local = local[1:]
        if local[:1] == "5":
            return "mobile"
        if local[:1] in "234679":
            return "landline"
        return "unknown"

    if country == "QA":
        local = digits[3:] if digits.startswith("974") else digits
        if local[:1] in "3567":
            return "mobile"
        if local[:1] == "4":
            return "landline"
        return "unknown"

    return "unknown"


# Filled in by the operator, not invented for them -- but unlike the call
# script (which starts empty because a phone conversation is entirely the
# operator's words), a WhatsApp opener is drafted by the system from a
# confirmed signal, so starting with real, editable copy is the actual
# feature rather than words put in anyone's mouth.
_DEFAULT_WA_TEMPLATE_GAP = (
    "Hi! I noticed {{business_name}}'s website doesn't have an online "
    "booking option — {{signal_detail}}. I help clinics add simple online "
    "booking so patients can book without calling. Worth a quick chat?"
)
_DEFAULT_WA_TEMPLATE_NO_GAP = (
    "Hi! I came across {{business_name}} and noticed you've already got "
    "online booking set up nicely — {{signal_detail}}. Just wanted to say "
    "it's great to see, wishing you all the best!"
)
_DEFAULT_WA_TEMPLATE_FOLLOWUP = (
    "Hi again! Just following up on my last message to {{business_name}} — "
    "no worries if now isn't a good time, happy to check back later."
)

_WA_TEMPLATE_SETTINGS_KEYS = {
    "gap":      ("wa_template_gap", _DEFAULT_WA_TEMPLATE_GAP),
    "no_gap":   ("wa_template_no_gap", _DEFAULT_WA_TEMPLATE_NO_GAP),
    "followup": ("wa_template_followup", _DEFAULT_WA_TEMPLATE_FOLLOWUP),
}


def get_wa_templates() -> dict:
    """The three editable templates, seeded with real starting copy on first read."""
    settings = get_settings()
    return {
        key: settings.get(setting_key, default)
        for key, (setting_key, default) in _WA_TEMPLATE_SETTINGS_KEYS.items()
    }


def save_wa_templates(templates: dict):
    updates = {}
    for key, (setting_key, _default) in _WA_TEMPLATE_SETTINGS_KEYS.items():
        if key in templates:
            updates[setting_key] = templates[key]
    if updates:
        save_settings(updates)


def _render_wa_template(template: str, business: dict, signal_detail: str) -> str:
    return (template
            .replace("{{business_name}}", business.get("name") or "there")
            .replace("{{signal_detail}}", signal_detail or ""))


def upsert_wa_leads(rows: list, default_country: str = "") -> tuple:
    """
    Import WhatsApp leads: resolve/create the business the same way any
    channel's import does (find_or_create_business, so a clinic already
    known from email or calling is recognised rather than duplicated), then
    attach or update its wa_leads row.

    `country` on a row overrides `default_country` -- a CSV can carry a
    country column of its own; the picker in the import UI is the fallback
    for one that doesn't.

    Returns (accepted, business_ids), same shape as upsert_businesses.
    """
    with get_db() as conn:
        accepted = 0
        touched = set()
        ordered_ids = []

        for r in rows:
            name = (r.get("company") or r.get("name") or "").strip()
            phone = (r.get("phone") or "").strip()
            email = (r.get("email") or "").strip().lower()
            website = (r.get("website") or "").strip()
            if not any((email, name, phone, website)):
                continue

            business_id = find_or_create_business(conn, r)
            touched.add(business_id)
            if business_id not in ordered_ids:
                ordered_ids.append(business_id)

            if email and "@" in email:
                status = r.get("status", "active")
                if status in ("no_website", "form_only", "no_email", ""):
                    status = "active"
                conn.execute("""
                    INSERT INTO email_leads(business_id, email, first_name, last_name, status, mx_valid)
                    VALUES(:business_id,:email,:first_name,:last_name,:status,:mx_valid)
                    ON CONFLICT(email) WHERE email IS NOT NULL AND email != '' DO UPDATE SET
                        first_name = COALESCE(NULLIF(excluded.first_name,''), email_leads.first_name),
                        last_name  = COALESCE(NULLIF(excluded.last_name,''),  email_leads.last_name),
                        mx_valid   = COALESCE(excluded.mx_valid,              email_leads.mx_valid)
                """, {
                    "business_id": business_id, "email": email,
                    "first_name": r.get("first_name", ""), "last_name": r.get("last_name", ""),
                    "status": status, "mx_valid": r.get("mx_valid"),
                })
                conn.execute(
                    "UPDATE businesses SET web_status='has_email' WHERE id=? AND web_status=''",
                    (business_id,),
                )

            country = (r.get("country") or default_country or "").strip().upper()
            wa_number = format_whatsapp_number(phone, country) if phone else ""
            number_type = classify_number_type(phone, country) if phone else "unknown"

            existing = conn.execute(
                "SELECT id, wa_number, country FROM wa_leads WHERE business_id=?", (business_id,)
            ).fetchone()
            if existing:
                conn.execute("""
                    UPDATE wa_leads SET
                        wa_number   = COALESCE(NULLIF(wa_number,''), ?),
                        country     = COALESCE(NULLIF(country,''), ?),
                        number_type = CASE WHEN COALESCE(NULLIF(wa_number,''), ?) != wa_number
                                           THEN ? ELSE number_type END
                    WHERE id=?
                """, (wa_number, country, wa_number, number_type, existing["id"]))
            else:
                conn.execute("""
                    INSERT INTO wa_leads(business_id, wa_number, country, number_type)
                    VALUES(?,?,?,?)
                """, (business_id, wa_number, country, number_type))
            accepted += 1

        for business_id in touched:
            _pick_business_winner(conn, business_id)

        return accepted, ordered_ids


# Every row a WhatsApp list view needs, business joined in the same shape the
# other two channels use -- `company` for the name, so the UI needs no
# special-casing per channel.
_WA_LEAD_COLUMNS = """
    w.id, w.business_id, w.wa_number, w.country, w.number_type, w.wa_status,
    w.signal_type, w.signal_detail, w.signal_confirmed, w.draft_message,
    w.template_variant, w.sent_date, w.replied, w.followup_count, w.paused,
    w.moved_to, w.notes, w.created_at,
    b.name AS company, b.website, b.address, b.city, b.phone, b.category,
    b.rating, b.review_count, b.do_not_contact
"""
_WA_LEAD_JOIN = "FROM wa_leads w JOIN businesses b ON b.id = w.business_id"


def get_wa_lead(wa_lead_id: int):
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {_WA_LEAD_COLUMNS} {_WA_LEAD_JOIN} WHERE w.id=?", (wa_lead_id,)
        ).fetchone()
        return dict(row) if row else None


def get_wa_leads(status: str = None, limit: int = 200) -> list:
    """
    The WhatsApp list, optionally scoped to one lifecycle stage:
    '' (imported, awaiting signal), 'signal_ready' (needs operator review),
    'confirmed' (signal locked in, awaiting drafting), 'drafted' (ready to
    open), 'sent' (in the cadence), 'replied' / 'moved' (terminal).
    """
    where, params = "", []
    if status is not None:
        where = "WHERE w.wa_status = ?"
        params.append(status)
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {_WA_LEAD_COLUMNS} {_WA_LEAD_JOIN} {where} "
            f"ORDER BY w.created_at DESC LIMIT ?", params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_wa_summary() -> dict:
    with get_db() as conn:
        row = conn.execute("""
            SELECT
              COUNT(*)                                                     AS total,
              SUM(CASE WHEN wa_status=''            THEN 1 ELSE 0 END)     AS pending_signal,
              SUM(CASE WHEN wa_status='signal_ready' THEN 1 ELSE 0 END)    AS awaiting_review,
              SUM(CASE WHEN wa_status='confirmed'   THEN 1 ELSE 0 END)     AS awaiting_draft,
              SUM(CASE WHEN wa_status='drafted'     THEN 1 ELSE 0 END)     AS ready_to_send,
              SUM(CASE WHEN wa_status='sent'        THEN 1 ELSE 0 END)     AS in_cadence,
              SUM(replied)                                                 AS replied,
              SUM(CASE WHEN moved_to != ''          THEN 1 ELSE 0 END)     AS moved
              FROM wa_leads
        """).fetchone()
        return {k: (row[k] or 0) for k in row.keys()}


def get_wa_leads_pending_signal(limit: int = 5) -> list:
    """
    WhatsApp leads awaiting their first (and only) signal check. Consumed by
    the background scan in scheduler.py, never by a request -- see that
    module for why this can't run inline.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT w.id, b.website FROM wa_leads w JOIN businesses b ON b.id = w.business_id
             WHERE w.wa_status = ''
             ORDER BY w.created_at ASC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def set_wa_signal(wa_lead_id: int, signal_type: str, signal_detail: str):
    """Records a detected (not yet operator-confirmed) signal."""
    with get_db() as conn:
        conn.execute("""
            UPDATE wa_leads SET signal_type=?, signal_detail=?, wa_status='signal_ready'
             WHERE id=?
        """, (signal_type, signal_detail, wa_lead_id))


def confirm_wa_signal(wa_lead_id: int, signal_type: str, signal_detail: str):
    """
    The operator locks in a signal -- either as detected, or corrected by
    hand. Nothing drafts from this lead until this has been called; that
    gate is signal_confirmed, checked by get_wa_leads_ready_to_draft.
    """
    if signal_type not in ("gap_found", "no_gap", "unclear"):
        raise ValueError(f"Unknown signal type: {signal_type}")
    with get_db() as conn:
        conn.execute("""
            UPDATE wa_leads SET
                signal_type=?, signal_detail=?, signal_confirmed=1, wa_status='confirmed'
             WHERE id=?
        """, (signal_type, signal_detail, wa_lead_id))


def get_wa_leads_ready_to_draft(limit: int = 200) -> list:
    """Confirmed leads with no draft yet -- what the batch draft step processes."""
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT {_WA_LEAD_COLUMNS} {_WA_LEAD_JOIN}
             WHERE w.wa_status='confirmed' AND w.signal_confirmed=1
             ORDER BY w.created_at ASC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def save_wa_draft(wa_lead_id: int, message: str, template_variant: str = ""):
    with get_db() as conn:
        conn.execute("""
            UPDATE wa_leads SET draft_message=?, template_variant=?, wa_status='drafted'
             WHERE id=?
        """, (message, template_variant, wa_lead_id))


def update_wa_message(wa_lead_id: int, message: str):
    """Inline edits to the draft before it's sent -- doesn't touch wa_status."""
    with get_db() as conn:
        conn.execute("UPDATE wa_leads SET draft_message=? WHERE id=?", (message, wa_lead_id))


def mark_wa_sent(wa_lead_id: int, message: str, kind: str = "opener", template_variant: str = ""):
    """
    Records that the operator clicked Open in WhatsApp -- an approximation,
    not delivery confirmation; see wa_log's comment in init_db. Follow-ups
    call this too, incrementing followup_count so the cadence knows how many
    have gone out; the opener does not count as a follow-up.
    """
    with get_db() as conn:
        conn.execute("""
            INSERT INTO wa_log(wa_lead_id, kind, message, template_variant)
            VALUES(?,?,?,?)
        """, (wa_lead_id, kind, message, template_variant))
        if kind == "followup":
            conn.execute("""
                UPDATE wa_leads SET
                    sent_date=datetime('now'), wa_status='sent',
                    followup_count = followup_count + 1
                 WHERE id=?
            """, (wa_lead_id,))
        else:
            conn.execute(
                "UPDATE wa_leads SET sent_date=datetime('now'), wa_status='sent' WHERE id=?",
                (wa_lead_id,),
            )


def correct_wa_sent_date(wa_lead_id: int, sent_date: str = None):
    """
    Manual fix for "I opened the link but didn't actually send." sent_date is
    never verified against WhatsApp itself -- there's no way to -- so this is
    the one correction the operator has. sent_date=None clears it, which also
    drops the lead out of the follow-up-due query until it's sent again.
    """
    with get_db() as conn:
        conn.execute("UPDATE wa_leads SET sent_date=? WHERE id=?", (sent_date, wa_lead_id))


def mark_wa_replied(wa_lead_id: int, replied: bool = True):
    """Marking replied removes the lead from the cadence immediately, at any
    follow-up count -- see get_wa_followups_due, which excludes replied=1."""
    with get_db() as conn:
        conn.execute("""
            UPDATE wa_leads SET replied=?, wa_status=? WHERE id=?
        """, (1 if replied else 0, "replied" if replied else "sent", wa_lead_id))


def set_wa_paused(wa_lead_id: int, paused: bool = True):
    """The only way to stop the cadence short of a reply -- follow-ups are
    otherwise infinite by design; there is no auto-dormant-after-N here."""
    with get_db() as conn:
        conn.execute("UPDATE wa_leads SET paused=? WHERE id=?", (1 if paused else 0, wa_lead_id))


def get_wa_followups_due(days: int = 3, limit: int = 200) -> list:
    """
    Leads due for a follow-up: sent at least `days` ago, not replied, not
    paused, not moved to another channel. A live query, not a scheduled job
    -- nothing about surfacing "this is due" should touch the network, and
    computing it on read means there is no background code path here at all
    for anyone auditing the manual-send constraint to have to trust.
    """
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT {_WA_LEAD_COLUMNS} {_WA_LEAD_JOIN}
             WHERE w.wa_status = 'sent'
               AND w.replied = 0 AND w.paused = 0 AND w.moved_to = ''
               AND w.sent_date IS NOT NULL
               AND datetime(w.sent_date) <= datetime('now', ?)
             ORDER BY w.sent_date ASC LIMIT ?
        """, (f"-{int(days)} days", limit)).fetchall()
        return [dict(r) for r in rows]


def move_wa_lead(wa_lead_id: int, destination: str) -> dict:
    """
    The number turned out not to be on WhatsApp (discovered by the operator,
    not this app -- see the module handover for why there's no automatic
    check). Files the business under Calling or Email instead of losing the
    lead, and marks moved_to so it drops out of the WhatsApp cadence and a
    later re-scrape can't quietly re-queue a number already ruled out here.

    'call' creates/reuses a call_leads row and returns its id so the caller
    can offer adding it straight to a campaign. 'email' has nowhere to
    enroll a business with no address on file, so it only ensures the
    business exists as a prospect -- ready to pick up once an email surfaces.
    """
    if destination not in ("call", "email"):
        raise ValueError(f"Unknown destination: {destination}")
    lead = get_wa_lead(wa_lead_id)
    if not lead:
        raise ValueError("WhatsApp lead not found")

    with get_db() as conn:
        conn.execute(
            "UPDATE wa_leads SET moved_to=?, paused=1 WHERE id=?",
            (destination, wa_lead_id),
        )
        if destination == "call":
            call_lead_id = get_or_create_call_lead(conn, lead["business_id"])
            return {"destination": "call", "call_lead_id": call_lead_id}
        else:
            conn.execute(
                "UPDATE businesses SET web_status=COALESCE(NULLIF(web_status,''),'no_email') "
                "WHERE id=?", (lead["business_id"],),
            )
            return {"destination": "email", "business_id": lead["business_id"]}


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
                "SELECT COUNT(DISTINCT email_lead_id) FROM sends WHERE campaign_id=?",
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
                "SELECT COUNT(DISTINCT email_lead_id) FROM sends"
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


def clear_logs() -> int:
    """
    Delete every row in the activity log, on request rather than by age.
    Distinct from prune_logs (the automatic 60-day retention job) -- this is
    "wipe it now", for an operator who just wants a clean slate rather than
    waiting out the retention window. Logs are an operational trail, not
    outreach data: clearing them can't lose a contact, a send, or a reply.
    """
    with get_db() as conn:
        return conn.execute("DELETE FROM logs").rowcount


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
