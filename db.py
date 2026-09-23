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

import phonenumbers
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
                owner_id         INTEGER NOT NULL DEFAULT 0,
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
                -- Which operator this row belongs to. Two people working the
                -- same clinic get two rows, not a shared one: they should not
                -- see each other's notes, call outcomes or reply history.
                -- Overlap is surfaced at import time instead -- see
                -- find_cross_owner_matches.
                --
                -- No REFERENCES clause on purpose. SQLite cannot ALTER TABLE
                -- ADD a column that is both NOT NULL and a foreign key: the
                -- first requires a non-null default, the second forbids one.
                -- Existing databases get this column by ALTER, so ownership is
                -- enforced in this module (see _resolve_owner_id, delete_user)
                -- rather than by the engine.
                owner_id         INTEGER NOT NULL DEFAULT 0,
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
                -- JSON from the operator's last "Run checks" (audit.py): site
                -- speed and SEO scores, what the site runs on, pixels, email
                -- provider, site age. Only ever filled on request.
                audit            TEXT    NOT NULL DEFAULT '',
                audit_at         TEXT    DEFAULT NULL,
                created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- ─── Per-channel lead state ─────────────────────────────────────

            -- One row per email address. A business legitimately has several
            -- (info@, the owner, a billing address), so this is many-to-one
            -- against businesses -- which is also how "several addresses at
            -- one clinic, only email the best" is expressed now.
            --
            -- email is nullable and uniqueness comes from the partial index
            -- email_leads_owner_email_unique below, not a table constraint, so
            -- prospect rows with no address found yet are allowed to repeat.
            CREATE TABLE IF NOT EXISTS email_leads (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id       INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
                -- Denormalized from businesses.owner_id, which is otherwise
                -- reachable by join. It has to be a real column here because
                -- the import upserts through ON CONFLICT(owner_id, email), and
                -- a conflict target can only name columns of this table.
                -- Without it the index stays globally unique on email, and one
                -- operator importing an address another already holds silently
                -- overwrites the other's row instead of creating their own.
                owner_id          INTEGER NOT NULL DEFAULT 0,
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

            -- Being on Calling is having a row here with removed_at unset.
            -- It used to be implied by having a phone number, which put every
            -- lead scraped for WhatsApp into the "never called" pile too.
            CREATE TABLE IF NOT EXISTS call_leads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id   INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
                call_status   TEXT    NOT NULL DEFAULT '',
                next_call_at  TEXT    DEFAULT NULL,
                call_attempts INTEGER NOT NULL DEFAULT 0,
                -- Set when the operator takes the lead off Calling. The row
                -- stays so its call history does, and adding the lead back
                -- clears this rather than starting a fresh record.
                removed_at    TEXT    DEFAULT NULL,
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
                -- Bespoke follow-ups for this lead, as a JSON array of up to
                -- WA_MAX_LEAD_FOLLOWUPS strings: index 0 is the first
                -- follow-up, index 1 the second, and so on. Written by a
                -- hyper-personalised import (followup_1..3) or by hand.
                -- A missing or empty slot falls through to the campaign's
                -- follow-up template, which is also what every follow-up past
                -- the end of this array uses -- follow-ups are infinite, so
                -- the template stays the floor. See wa_message_for.
                draft_followups  TEXT    NOT NULL DEFAULT '',
                -- Which A/B arm of the template this lead was drafted from
                -- ('A', 'B', ...), or '' when that template has only one arm.
                -- A label rather than an index: arms can be deleted, and a
                -- number would silently re-point old leads at different copy.
                template_variant TEXT    NOT NULL DEFAULT '',
                -- 1 when draft_message holds the operator's own wording (or an
                -- AI rewording). Otherwise the message is written live from
                -- the campaign's current template, so a template edit reaches
                -- every unsent lead -- see wa_message_for.
                message_edited   INTEGER NOT NULL DEFAULT 0,
                -- Where this lead's own copy came from: '' template (none),
                -- 'import' a draft from a CSV/JSON import, 'manual' the
                -- operator typed it here, 'ai' the reword pass wrote it.
                -- message_edited says THAT the lead has its own copy; this
                -- says whether that copy can be reproduced. An import may
                -- overwrite '' and 'import'; 'manual' and 'ai' exist nowhere
                -- else, so a re-import preserves them and reports the count.
                message_source   TEXT    NOT NULL DEFAULT '',
                -- Whether the AI variety pass rewrote it. Kept apart from the
                -- arm because a paraphrase is a different message: folding the
                -- two together would credit an arm for copy it didn't write.
                paraphrased      INTEGER NOT NULL DEFAULT 0,
                sent_date        TEXT    DEFAULT NULL,
                replied          INTEGER NOT NULL DEFAULT 0,
                followup_count   INTEGER NOT NULL DEFAULT 0,
                paused           INTEGER NOT NULL DEFAULT 0,
                -- Where a lead went when its number turned out not to be on
                -- WhatsApp, e.g. 'call' or 'email'. Kept rather than deleted
                -- so a later scrape cannot quietly re-queue a number already
                -- ruled out here.
                moved_to         TEXT    NOT NULL DEFAULT '',
                -- Taken off WhatsApp by hand for some other reason than the
                -- number not being on it (a wrong import, say). Unlike
                -- moved_to, this doesn't rule the number out: adding the
                -- lead again brings it back.
                removed_at       TEXT    DEFAULT NULL,
                -- When the operator last opened this lead's chat in WhatsApp
                -- and hasn't yet said whether it sent. Nothing counts as sent
                -- until they do; this is only so a lead opened and forgotten
                -- asks again rather than looking untouched.
                opened_at        TEXT    DEFAULT NULL,
                -- Marked as not on WhatsApp but kept here, out of every queue,
                -- until the operator moves it off in bulk (move_wa_leads).
                no_whatsapp_at   TEXT    DEFAULT NULL,
                -- The campaign whose templates, follow-up gap and variables
                -- this lead's messages are written from. NULL only when its
                -- campaign was deleted; such a lead can't be drafted until
                -- it's moved into another.
                wa_campaign_id   INTEGER DEFAULT NULL REFERENCES wa_campaigns(id) ON DELETE SET NULL,
                notes            TEXT    NOT NULL DEFAULT '',
                created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(business_id)
            );

            -- A named batch of WhatsApp leads with its own pitch. Different
            -- campaigns lead with different services, so the copy lives
            -- here rather than once per operator: a lead is written to from
            -- its campaign's templates, at its campaign's follow-up gap.
            CREATE TABLE IF NOT EXISTS wa_campaigns (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id      INTEGER NOT NULL DEFAULT 0,
                name          TEXT    NOT NULL,
                notes         TEXT    NOT NULL DEFAULT '',
                -- Preselected when adding leads, so a Doha campaign doesn't
                -- have to be told it's in Qatar every time. Each lead still
                -- keeps its own country.
                country       TEXT    NOT NULL DEFAULT '',
                status        TEXT    NOT NULL DEFAULT 'active',
                -- {"gap": [arms], "no_gap": [arms], "followup": [arms]}
                templates     TEXT    NOT NULL DEFAULT '{}',
                followup_days INTEGER NOT NULL DEFAULT 3,
                -- {"my_name": "Sam", ...} -- filled into {{my_name}} and so on,
                -- the same as an email campaign's variables.
                variables     TEXT    NOT NULL DEFAULT '{}',
                created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
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
                paraphrased      INTEGER NOT NULL DEFAULT 0,
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
                -- Set from the session of whoever queues the job. The worker
                -- authenticates by API key and has no session of its own, so
                -- this is the only thing that can tell the import who the
                -- leads it pushes back belong to.
                owner_id     INTEGER NOT NULL DEFAULT 0,
                -- Which channel the finished leads go to: 'email' (what every
                -- scrape did originally) or 'whatsapp'. Applied by the server
                -- from this row, not by the worker, so where leads land never
                -- depends on which version of the worker someone is running.
                destination  TEXT    NOT NULL DEFAULT 'email',
                -- Optional campaign on that channel to drop the leads into
                -- (a call campaign or a WhatsApp campaign). Never an email
                -- campaign: enrolling there starts real sending.
                campaign_id  INTEGER DEFAULT NULL,
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
                -- A script is the operator's own pitch, so each has their own.
                owner_id   INTEGER NOT NULL DEFAULT 0,
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
                -- 0 for the built-ins, which everyone shares because the code
                -- special-cases two of them. A custom outcome belongs to the
                -- operator who invented it: the vocabulary someone works in
                -- says what they are working on, and one person archiving an
                -- outcome should not remove it from the other's dialler.
                --
                -- `key` stays globally unique even so -- call_log rows point
                -- at it, and history has to stay readable whoever looks.
                owner_id      INTEGER NOT NULL DEFAULT 0,
                label         TEXT    NOT NULL,
                is_terminal   INTEGER NOT NULL DEFAULT 0,
                stops_email   INTEGER NOT NULL DEFAULT 0,
                requires_date INTEGER NOT NULL DEFAULT 0,
                tone          TEXT    NOT NULL DEFAULT 'neutral',
                sort_order    INTEGER NOT NULL DEFAULT 100,
                is_builtin    INTEGER NOT NULL DEFAULT 0,
                archived      INTEGER NOT NULL DEFAULT 0
            );

            -- How far along a business is, as a deal. Deliberately NOT per
            -- channel: "meeting booked" is a fact about the prospect, not
            -- about whichever channel reached them, so importing them onto a
            -- second channel can show what's already happening with them.
            --
            -- Shaped like call_outcome_types, and for the same reason: the
            -- built-ins are shared (owner 0), a stage someone invents belongs
            -- to them, and `key` stays globally unique because businesses
            -- point at it and history has to stay readable whoever looks.
            CREATE TABLE IF NOT EXISTS pipeline_stages (
                key         TEXT PRIMARY KEY,
                owner_id    INTEGER NOT NULL DEFAULT 0,
                label       TEXT    NOT NULL,
                -- Ends the conversation: won or lost. Kept out of "in
                -- progress" without being deleted, since the stage is what
                -- says how it ended.
                is_terminal INTEGER NOT NULL DEFAULT 0,
                -- Asks for a date when set -- "proposal due", "meeting
                -- booked". Stored on businesses.next_action_at.
                wants_date  INTEGER NOT NULL DEFAULT 0,
                tone        TEXT    NOT NULL DEFAULT 'neutral',
                sort_order  INTEGER NOT NULL DEFAULT 100,
                is_builtin  INTEGER NOT NULL DEFAULT 0,
                archived    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS call_campaigns (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   INTEGER NOT NULL DEFAULT 0,
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

            -- One scrape worker key per operator. Its own table rather than a
            -- column on users, so the secret never rides along in a user row
            -- that gets loaded or serialised somewhere else. The key both
            -- authenticates a worker and decides whose scrapes it may run.
            CREATE TABLE IF NOT EXISTS worker_keys (
                owner_id   INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                api_key    TEXT    NOT NULL UNIQUE,
                last_seen  TEXT    DEFAULT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
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
            # Per-operator ownership. DEFAULT 0 means "unassigned": it is not a
            # valid user id, so a row that somehow escapes the backfill below
            # simply stops matching any owner's filter rather than silently
            # showing up in the wrong person's list. See businesses.owner_id
            # for why none of these carry a REFERENCES clause.
            "ALTER TABLE businesses      ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE email_leads     ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE campaigns       ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE call_campaigns  ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE scrape_jobs     ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0",
            # template_variant used to record whether the AI paraphrase ran.
            # It now records which A/B arm was used, so the paraphrase fact
            # needs somewhere of its own. Existing rows report 0: their real
            # value is in template_variant, which the stats view reads as an
            # arm named 'paraphrased' or 'template' rather than pretending to
            # know better.
            "ALTER TABLE wa_leads ADD COLUMN paraphrased INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE wa_log   ADD COLUMN paraphrased INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE call_scripts       ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE call_outcome_types ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE scrape_jobs ADD COLUMN destination TEXT NOT NULL DEFAULT 'email'",
            "ALTER TABLE scrape_jobs ADD COLUMN campaign_id INTEGER DEFAULT NULL",
            "ALTER TABLE call_leads ADD COLUMN removed_at TEXT DEFAULT NULL",
            "ALTER TABLE wa_leads   ADD COLUMN removed_at TEXT DEFAULT NULL",
            # Nullable with a NULL default, which is the one shape SQLite lets
            # ALTER TABLE add with a REFERENCES clause.
            "ALTER TABLE wa_leads ADD COLUMN wa_campaign_id INTEGER DEFAULT NULL "
            "REFERENCES wa_campaigns(id) ON DELETE SET NULL",
            "ALTER TABLE wa_leads   ADD COLUMN message_edited INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE businesses ADD COLUMN audit TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE businesses ADD COLUMN audit_at TEXT DEFAULT NULL",
            "ALTER TABLE wa_leads   ADD COLUMN opened_at TEXT DEFAULT NULL",
            "ALTER TABLE wa_leads   ADD COLUMN no_whatsapp_at TEXT DEFAULT NULL",
            # Per-lead bespoke copy: the follow-ups an import brought with it,
            # and where this lead's opener came from. See the column comments
            # on wa_leads. message_source is backfilled below from the flags
            # that used to carry the same fact between them.
            "ALTER TABLE wa_leads   ADD COLUMN draft_followups TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE wa_leads   ADD COLUMN message_source TEXT NOT NULL DEFAULT ''",
            # How far along this business is as a deal, shared by every
            # channel. '' means nothing has happened yet, so existing rows
            # need no backfill. See the pipeline_stages table.
            "ALTER TABLE businesses ADD COLUMN pipeline_stage TEXT NOT NULL DEFAULT ''",
            # Which channel it was set from, so Email can say "booked -- via
            # WhatsApp" rather than leaving you to guess.
            "ALTER TABLE businesses ADD COLUMN pipeline_channel TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE businesses ADD COLUMN pipeline_at TEXT DEFAULT NULL",
            # When the next thing is owed: a proposal, a booked meeting.
            "ALTER TABLE businesses ADD COLUMN next_action_at TEXT DEFAULT NULL",
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

        # Hand every pre-multi-user row to its rightful owner. Runs after the
        # split above, which creates businesses/email_leads rows of its own.
        _backfill_owner_ids(conn)
        _make_calling_explicit(conn)
        _migrate_wa_campaigns(conn)
        _migrate_wa_no_review(conn)
        _migrate_wa_message_source(conn)

        # Uniqueness is per owner, not global. The old global index is dropped
        # rather than left in place: while it exists, a second operator
        # importing an address the first already holds does not get their own
        # row, it silently edits the first operator's one through the
        # ON CONFLICT clause in upsert_businesses.
        conn.execute("DROP INDEX IF EXISTS email_leads_email_unique")
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS email_leads_owner_email_unique
            ON email_leads(owner_id, email) WHERE email IS NOT NULL AND email != ''
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
            "CREATE INDEX IF NOT EXISTS wa_leads_campaign_idx    ON wa_leads(wa_campaign_id)",
            # Ownership is now a predicate on nearly every read -- the lead
            # list, the call queue, every summary. Without these, filtering to
            # one operator means a full scan of the table it is filtering.
            # email_leads' partial unique index leads on owner_id but only
            # covers rows with an address, so it cannot serve these.
            "CREATE INDEX IF NOT EXISTS businesses_owner_idx     ON businesses(owner_id)",
            "CREATE INDEX IF NOT EXISTS email_leads_owner_idx    ON email_leads(owner_id)",
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

        try:
            _seed_pipeline_stages(conn)
        except Exception as exc:
            logger.warning("Pipeline stage seeding skipped: %s", exc)

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


# ── Ownership ─────────────────────────────────────────────────────────────────
#
# Leads, campaigns and scrape jobs belong to one operator each. The wall is
# real rows, not a filtered view: two people working the same clinic hold two
# separate businesses rows. Overlap between them is reported at import time
# (find_cross_owner_matches) and never merged.
#
# Deliberately NOT owner-scoped, and it must stay that way:
#   unsubscribe_contact / mark_bounced / increment_soft_bounce
# Suppression is a promise made to the person on the other end, not to one
# operator's copy of them. Those functions match on the address across every
# owner, because everyone here sends from the same accounts and the same
# domain -- a "stop emailing me" that only stopped half the senders would
# still be a CASL/CAN-SPAM breach, and would look identical to spam from the
# recipient's side.

OWNER_UNASSIGNED = 0


def _default_owner_id(conn) -> int:
    """The account that inherits everything predating multi-user support."""
    row = conn.execute(
        "SELECT id FROM users ORDER BY is_admin DESC, id ASC LIMIT 1"
    ).fetchone()
    return row["id"] if row else OWNER_UNASSIGNED


def _resolve_owner_id(conn, owner_id=None) -> int:
    """
    Settle which operator a write belongs to.

    An explicit id always wins. Falling back is only safe while the answer is
    unambiguous, so this raises once a second account exists rather than
    guessing: a caller that forgot to pass an owner would otherwise file one
    person's leads under the other, silently, and only in production -- the
    exact failure the wall exists to prevent. Tests and single-operator
    installs never see it.
    """
    if owner_id:
        return int(owner_id)
    users = conn.execute("SELECT id FROM users ORDER BY id").fetchall()
    if len(users) > 1:
        raise ValueError(
            "owner_id is required: this database has more than one user, so "
            "there is no safe default owner for this write"
        )
    return users[0]["id"] if users else OWNER_UNASSIGNED


def _owner_or_default(owner_id=None) -> int:
    """_resolve_owner_id for callers that don't already hold a connection."""
    if owner_id:
        return int(owner_id)
    with get_db() as conn:
        return _resolve_owner_id(conn, None)


def owns(kind: str, row_id, owner_id) -> bool:
    """
    Whether `owner_id` owns this row. The guard behind every route that acts on
    a single record by id.

    wa_leads and call_leads have no owner column of their own -- they are one
    per business and inherit it -- so ownership is asked of the business they
    hang off rather than duplicated onto them.
    """
    sql = {
        "business":      "SELECT 1 FROM businesses WHERE id=? AND owner_id=?",
        "campaign":      "SELECT 1 FROM campaigns WHERE id=? AND owner_id=?",
        "call_campaign": "SELECT 1 FROM call_campaigns WHERE id=? AND owner_id=?",
        "wa_campaign":   "SELECT 1 FROM wa_campaigns WHERE id=? AND owner_id=?",
        "scrape_job":    "SELECT 1 FROM scrape_jobs WHERE id=? AND owner_id=?",
        "email_lead":    "SELECT 1 FROM email_leads WHERE id=? AND owner_id=?",
        "wa_lead":       ("SELECT 1 FROM wa_leads w JOIN businesses b ON b.id=w.business_id "
                          "WHERE w.id=? AND b.owner_id=?"),
        "call_lead":     ("SELECT 1 FROM call_leads c JOIN businesses b ON b.id=c.business_id "
                          "WHERE c.id=? AND b.owner_id=?"),
        "enrollment":    ("SELECT 1 FROM enrollments e JOIN campaigns c ON c.id=e.campaign_id "
                          "WHERE e.id=? AND c.owner_id=?"),
    }[kind]
    with get_db() as conn:
        return conn.execute(sql, (int(row_id), int(owner_id))).fetchone() is not None


def _own_business_ids(conn, business_ids, owner_id=None) -> list:
    """
    Keep only the business ids this operator actually owns.

    Anything taking ids from a request body has to pass through here. Ids are
    sequential, so an unfiltered list is an invitation to act on the other
    operator's leads by guessing -- and unlike a URL id, a body full of them
    is not covered by the @owned route decorator.
    """
    ids = [int(b) for b in business_ids or []]
    if not ids:
        return []
    rows = conn.execute(
        "SELECT id FROM businesses WHERE id IN (%s) AND owner_id = ?"
        % ",".join("?" * len(ids)),
        [*ids, _resolve_owner_id(conn, owner_id)],
    ).fetchall()
    keep = {r["id"] for r in rows}
    return [b for b in ids if b in keep]


def _backfill_owner_ids(conn):
    """
    Assign every ownerless row to the founding account.

    Idempotent: only touches rows still sitting at OWNER_UNASSIGNED, which no
    real user id can equal. On a database with no users yet (a fresh install
    before first-run setup) there is nobody to assign to, so rows stay
    unassigned and are picked up the next time init_db runs.
    """
    owner = _default_owner_id(conn)
    if owner == OWNER_UNASSIGNED:
        return

    # email_leads follows its business rather than the default, so a database
    # that somehow already holds several owners' rows stays consistent.
    conn.execute("""
        UPDATE email_leads SET owner_id = (
            SELECT b.owner_id FROM businesses b WHERE b.id = email_leads.business_id
        )
        WHERE owner_id = ?
          AND (SELECT b.owner_id FROM businesses b WHERE b.id = email_leads.business_id) != ?
    """, (OWNER_UNASSIGNED, OWNER_UNASSIGNED))

    for table in ("businesses", "email_leads", "campaigns", "call_campaigns",
                  "scrape_jobs", "call_scripts", "wa_campaigns"):
        conn.execute(
            f"UPDATE {table} SET owner_id=? WHERE owner_id=?", (owner, OWNER_UNASSIGNED)
        )

    # Built-in outcomes stay unowned on purpose -- they are the shared
    # vocabulary every operator dials against, and two of them are special-cased
    # in code. Only the ones somebody invented get handed over.
    conn.execute(
        "UPDATE call_outcome_types SET owner_id=? WHERE owner_id=? AND is_builtin=0",
        (owner, OWNER_UNASSIGNED),
    )

    _migrate_wa_settings(conn, owner)
    _migrate_worker_key(conn, owner)


def _migrate_worker_key(conn, owner: int):
    """
    Hand the old install-wide worker key to the founding operator.

    Their worker has that key saved on their laptop, so it has to keep working
    through the upgrade rather than silently disconnecting. Moved, not copied:
    left in settings it would be a second live credential with no owner --
    exactly the ambiguity per-operator keys exist to remove.
    """
    shared = conn.execute(
        "SELECT value FROM settings WHERE key='_worker_api_key'"
    ).fetchone()
    if not shared or not shared["value"]:
        return
    if not conn.execute("SELECT 1 FROM worker_keys WHERE owner_id=?", (owner,)).fetchone():
        conn.execute("INSERT INTO worker_keys(owner_id, api_key) VALUES(?,?)",
                     (owner, shared["value"]))
    conn.execute("DELETE FROM settings WHERE key IN ('_worker_api_key', '_worker_last_seen')")


def _migrate_wa_settings(conn, owner: int):
    """
    Hand the pre-multi-user WhatsApp copy to the operator who wrote it.

    Templates and the follow-up interval used to be one shared set. Moved
    rather than copied: a leftover shared value is read by nothing now, and
    the version that did read it as a fallback is exactly the cross-operator
    leak that was removed -- a second operator opening the editor onto
    somebody else's messages.

    Idempotent. Once the shared key is gone there is nothing left to move, and
    an operator who already has their own value keeps it.
    """
    bases = [key for key, _default in _WA_TEMPLATE_SETTINGS_KEYS.values()]
    bases.append(WA_FOLLOWUP_DAYS_KEY)
    for base in bases:
        shared = conn.execute(
            "SELECT value FROM settings WHERE key=?", (base,)
        ).fetchone()
        if not shared or shared["value"] in (None, ""):
            continue
        own_key = _wa_owner_key(base, owner)
        if conn.execute("SELECT 1 FROM settings WHERE key=?", (own_key,)).fetchone():
            continue
        conn.execute("INSERT INTO settings(key, value) VALUES(?,?)",
                     (own_key, shared["value"]))
        conn.execute("DELETE FROM settings WHERE key=?", (base,))


_CALLING_EXPLICIT_MARKER = "_migrated_calling_explicit"


def _make_calling_explicit(conn):
    """
    One-shot: give every lead that was implicitly on Calling a real row.

    The call queue used to be every business with a phone number, so a lead
    scraped for WhatsApp also sat in "never called". Membership is a call_leads
    row now. So the switch changes nothing an operator can see except that
    fix, every business that was showing up gets its row -- minus the ones on
    WhatsApp, which were the bug, and opted-out ones, which never showed.

    Guarded by a marker, not by "has no call_leads row": after the switch a
    business without one is simply not on Calling, and re-running this would
    quietly put back every lead the operator had taken off.
    """
    if conn.execute("SELECT 1 FROM settings WHERE key=?", (_CALLING_EXPLICIT_MARKER,)).fetchone():
        return
    added = conn.execute("""
        INSERT INTO call_leads(business_id)
        SELECT b.id FROM businesses b
         WHERE COALESCE(b.phone,'') != ''
           AND b.do_not_contact = 0
           AND NOT EXISTS (SELECT 1 FROM call_leads cl WHERE cl.business_id = b.id)
           AND NOT EXISTS (SELECT 1 FROM wa_leads w WHERE w.business_id = b.id)
           AND NOT EXISTS (SELECT 1 FROM scrape_jobs j
                            WHERE j.id = b.source_job_id AND j.destination = 'whatsapp')
    """).rowcount
    conn.execute("INSERT INTO settings(key, value) VALUES(?, '1')", (_CALLING_EXPLICIT_MARKER,))
    if added:
        logger.info("Calling is explicit now: kept %d existing lead(s) on the call list", added)


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


def get_or_create_worker_key(owner_id) -> str:
    """
    This operator's scrape worker key, created on first use.

    One per operator rather than one for the install. A worker only ever runs
    the scrapes of whoever owns the key it presents, so two people each running
    a worker on their own laptop never pick up each other's jobs.

    The worker is not a browser and has no session cookie, so it presents this
    as an X-API-Key header instead. Custom headers are not attached
    cross-origin by browsers, so token auth on these routes is not exposed to
    CSRF the way a cookie-authenticated route would be.
    """
    owner_id = int(owner_id)
    with get_db() as conn:
        row = conn.execute(
            "SELECT api_key FROM worker_keys WHERE owner_id=?", (owner_id,)
        ).fetchone()
        if row:
            return row["api_key"]
        key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO worker_keys(owner_id, api_key) VALUES(?,?)", (owner_id, key)
        )
        return key


def rotate_worker_key(owner_id) -> str:
    """A new key for this operator only. Everyone else's worker keeps working."""
    key = secrets.token_urlsafe(32)
    with get_db() as conn:
        conn.execute("""
            INSERT INTO worker_keys(owner_id, api_key) VALUES(?,?)
            ON CONFLICT(owner_id) DO UPDATE SET api_key=excluded.api_key
        """, (int(owner_id), key))
    return key


def worker_owner_for_key(presented: str):
    """
    Which operator a presented worker key belongs to, or None.

    Every stored key is compared with compare_digest and the loop never stops
    early, so how long a wrong guess takes reveals nothing about how close it
    was. The table holds one row per operator, so comparing against all of
    them stays trivially cheap.
    """
    if not presented:
        return None
    with get_db() as conn:
        rows = conn.execute("SELECT owner_id, api_key FROM worker_keys").fetchall()
    offered = presented.encode("utf-8")
    match = None
    for row in rows:
        if _hmac.compare_digest(offered, row["api_key"].encode("utf-8")):
            match = row["owner_id"]
    return match


# ── Scrape jobs ───────────────────────────────────────────────────────────────

# Statuses a job can sit in while it is still someone's responsibility.
SCRAPE_ACTIVE_STATUSES = ("queued", "claimed", "running", "captcha")

# Each channel scrapes for itself. Email hunts each site for an address;
# Calling and WhatsApp only need the phone number Maps already shows.
SCRAPE_DESTINATIONS = ("email", "calling", "whatsapp")

# A worker that has not checked in for this long is treated as gone. It has to
# comfortably exceed the worker's own post interval, or a busy scrape that goes
# quiet during a slow page load would flap the UI to "offline".
WORKER_STALE_SECONDS = 45


def create_scrape_job(niche, city, max_results=50, auto_import=True, owner_id=None,
                      destination="email", country="", campaign_id=None) -> int:
    if destination not in SCRAPE_DESTINATIONS:
        raise ValueError(f"Unknown scrape destination: {destination}")
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO scrape_jobs(niche, city, max_results, auto_import, logs, owner_id,
                                    destination, country, campaign_id)
            VALUES(?,?,?,?,'[]',?,?,?,?)
        """, (niche, city, int(max_results), 1 if auto_import else 0,
              _resolve_owner_id(conn, owner_id), destination,
              (country or "").strip().upper(),
              int(campaign_id) if campaign_id else None))
        return cur.lastrowid


def get_scrape_job(job_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM scrape_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_latest_scrape_job(owner_id=None):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM scrape_jobs WHERE owner_id=? ORDER BY id DESC LIMIT 1",
            (_resolve_owner_id(conn, owner_id),)
        ).fetchone()
        return dict(row) if row else None


def get_active_scrape_job(owner_id=None):
    """
    This operator's queued or running scrape, if any.

    Always one operator's: each worker runs only its own operator's jobs, so
    nothing needs to see -- or be able to stop -- anyone else's.
    """
    placeholders = ",".join("?" * len(SCRAPE_ACTIVE_STATUSES))
    with get_db() as conn:
        row = conn.execute(
            f"SELECT * FROM scrape_jobs WHERE status IN ({placeholders}) AND owner_id = ? "
            "ORDER BY id ASC LIMIT 1",
            [*SCRAPE_ACTIVE_STATUSES, _resolve_owner_id(conn, owner_id)],
        ).fetchone()
        return dict(row) if row else None


def claim_scrape_job(owner_id) -> dict:
    """
    Hand this operator's oldest queued job to their worker, atomically.

    Only their own. Without the owner filter a worker takes whichever job is
    next in line, opening Chrome on one person's laptop to run the other
    person's search -- their business names in its log, their CSV on its disk.

    The UPDATE ... WHERE status='queued' is the lock: if two workers race, only
    one gets a rowcount of 1, so the job cannot be run twice.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM scrape_jobs WHERE status='queued' AND owner_id=? "
            "ORDER BY id ASC LIMIT 1", (int(owner_id),)
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


def touch_worker_seen(owner_id):
    """
    Record that this operator's worker just checked in.

    Kept apart from the job row because the worker polls for work when no job
    exists -- the UI still needs to show it as connected so pressing Start is
    not a shot in the dark. Per operator, because the Scraper page answers "is
    MY worker online": one shared timestamp would turn one person's page green
    because the other's laptop was on, and a Start pressed there would sit
    queued with nothing coming to run it.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE worker_keys SET last_seen=datetime('now') WHERE owner_id=?",
            (int(owner_id),),
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


def worker_seconds_since_seen(owner_id):
    """Seconds since this operator's worker last checked in, or None if never."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT last_seen FROM worker_keys WHERE owner_id=?", (int(owner_id),)
        ).fetchone()
    return _seconds_since(row["last_seen"] if row else None)


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

def get_campaigns(owner_id=None, all_owners=False):
    """
    One operator's campaigns, or everyone's.

    `all_owners` exists for the send loop in scheduler.py, which runs on a
    timer with nobody logged in and has to send for every operator. It is the
    only caller entitled to it -- anything answering a request must pass the
    requesting user instead.
    """
    with get_db() as conn:
        if all_owners:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM campaigns ORDER BY created_at DESC"
            ).fetchall()]
        return [dict(r) for r in conn.execute(
            "SELECT * FROM campaigns WHERE owner_id=? ORDER BY created_at DESC",
            (_resolve_owner_id(conn, owner_id),)
        ).fetchall()]


def get_campaign(cid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None


def create_campaign(name, daily_limit=30, start_hour=9, end_hour=17,
                    min_delay=45, max_delay=120, timezone=None, variables='{}',
                    send_days='0,1,2,3,4', owner_id=None):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns(name,daily_limit,send_start_hour,send_end_hour,"
            "min_delay_secs,max_delay_secs,timezone,variables,send_days,owner_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, daily_limit, start_hour, end_hour, min_delay, max_delay,
             timezone, variables, send_days, _resolve_owner_id(conn, owner_id))
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


def find_or_create_business(conn, r: dict, owner_id=None) -> int:
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

    owner_id = _resolve_owner_id(conn, owner_id)
    row = find_existing_business(conn, email=email, phone=phone, website=website,
                                 company=name, address=address, owner_id=owner_id)

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
            owner_id, name, phone, phone_normalized, website, domain, address, city,
            country, category, rating, review_count, web_status,
            source_job_id, extra
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        owner_id, name, phone, phone_norm, website, domain, address,
        r.get("city", ""), r.get("country", ""), r.get("category", ""),
        rating, reviews, web_status, r.get("source_job_id") or None,
        json.dumps(r.get("extra", {}) if isinstance(r.get("extra"), dict) else {}),
    )).lastrowid


def upsert_businesses(rows, owner_id=None):
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
        owner_id = _resolve_owner_id(conn, owner_id)

        for r in rows:
            email = (r.get("email") or "").strip().lower()
            name = (r.get("company") or r.get("name") or "").strip()
            # Nothing to file it under and nothing to reach it on.
            if not any((email, name, (r.get("phone") or "").strip(), (r.get("website") or "").strip())):
                continue

            business_id = find_or_create_business(conn, r, owner_id=owner_id)
            touched.add(business_id)
            if business_id not in ordered_ids:
                ordered_ids.append(business_id)

            if email and "@" in email:
                status = r.get("status", "active")
                if status in ("no_website", "form_only", "no_email", ""):
                    status = "active"
                conn.execute("""
                    INSERT INTO email_leads(
                        business_id, owner_id, email, first_name, last_name, status, mx_valid
                    ) VALUES(:business_id,:owner_id,:email,:first_name,:last_name,:status,:mx_valid)
                    ON CONFLICT(owner_id, email) WHERE email IS NOT NULL AND email != '' DO UPDATE SET
                        first_name = COALESCE(NULLIF(excluded.first_name,''), email_leads.first_name),
                        last_name  = COALESCE(NULLIF(excluded.last_name,''),  email_leads.last_name),
                        mx_valid   = COALESCE(excluded.mx_valid,              email_leads.mx_valid)
                """, {
                    "business_id": business_id,
                    "owner_id":    owner_id,
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


def get_known_company_names(owner_id=None) -> set:
    """
    Every company already stored, for the scraper's resume set.

    The Maps scraper dedupes by the business name shown on the listing, so
    this lets a new search skip businesses an earlier search already collected
    -- overlapping niches like "dentists" and "dental clinics" in one city
    otherwise re-scrape the same places from scratch.

    Scoped to the operator whose scrape job this is. Skipping a business
    because the OTHER operator already has it would silently deny this one a
    lead they are entitled to work, and leak the shape of the other's list
    through what came back.
    """
    with get_db() as conn:
        return {
            r["name"] for r in conn.execute(
                "SELECT DISTINCT name FROM businesses WHERE name != '' AND owner_id = ?",
                (_resolve_owner_id(conn, owner_id),)
            ).fetchall()
        }


def get_email_leads(limit=200, offset=0, owner_id=None):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(f"""
            SELECT {_EMAIL_LEAD_COLUMNS} {_EMAIL_LEAD_JOIN}
             WHERE b.owner_id = ?
             ORDER BY el.created_at DESC LIMIT ? OFFSET ?
        """, (_resolve_owner_id(conn, owner_id), limit, offset)).fetchall()]


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
                        call_status=None, owner_id=None, campaign_id=None):
    """
    Build the shared WHERE clause for the email lead list views.

    `owner_id` is a resolved id, not None-means-everyone: every caller runs it
    through _resolve_owner_id first, so a list view cannot accidentally be
    built without a wall.
    """
    clauses, params = [], []

    clauses.append("b.owner_id = ?")
    params.append(int(owner_id))

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

    # 'none' is addresses not in any campaign -- the ones still waiting to be
    # used, which is what you look for when building a new one.
    if campaign_id == "none":
        clauses.append("NOT EXISTS (SELECT 1 FROM enrollments en WHERE en.email_lead_id = el.id)")
    elif campaign_id:
        clauses.append("EXISTS (SELECT 1 FROM enrollments en WHERE en.email_lead_id = el.id "
                       "AND en.campaign_id = ?)")
        params.append(int(campaign_id))

    q = (q or "").strip()
    if q:
        like = " OR ".join(f"COALESCE({c},'') LIKE ?" for c in _EMAIL_LEAD_SEARCH_COLUMNS)
        clauses.append(f"({like})")
        params.extend([f"%{q}%"] * len(_EMAIL_LEAD_SEARCH_COLUMNS))

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_email_leads_page(page=1, per_page=50, q="", source_job_id=None, status=None,
                         include_deleted=False, sort_col="", sort_dir="desc",
                         call_status=None, owner_id=None, campaign_id=None):
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

    join = _EMAIL_LEAD_JOIN

    with get_db() as conn:
        where, params = _email_lead_filters(q, source_job_id, status, include_deleted,
                                            call_status, _resolve_owner_id(conn, owner_id),
                                            campaign_id)
        total = conn.execute(f"SELECT COUNT(*) {join} {where}", params).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_EMAIL_LEAD_COLUMNS} {join} {where} "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()]
        # The campaign each address is in, for the Leads table's Campaign
        # column -- one query for the page rather than one per row.
        ids = [r["id"] for r in rows]
        enrolled = {}
        if ids:
            for en in conn.execute(f"""
                SELECT en.id, en.email_lead_id, en.status, en.current_step, en.next_send_at,
                       c.id AS campaign_id, c.name AS campaign
                  FROM enrollments en JOIN campaigns c ON c.id = en.campaign_id
                 WHERE en.email_lead_id IN ({",".join("?" * len(ids))})
                 ORDER BY en.enrolled_at DESC
            """, ids):
                enrolled.setdefault(en["email_lead_id"], []).append(dict(en))
        for r in rows:
            r["enrollments"] = enrolled.get(r["id"], [])

    return {
        "rows":     rows,
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "sort_col": sort_col,
        "sort_dir": sort_dir,
    }


def get_email_lead_ids_matching(q="", source_job_id=None, status=None, include_deleted=False,
                                call_status=None, owner_id=None, campaign_id=None):
    """
    Every email lead id matching a filter, ignoring paging.

    Backs "select all N matching" -- without it, select-all could only ever
    reach the rows on screen, so a bulk delete over a filtered list would
    silently act on one page's worth.
    """
    with get_db() as conn:
        where, params = _email_lead_filters(q, source_job_id, status, include_deleted,
                                            call_status, _resolve_owner_id(conn, owner_id),
                                            campaign_id)
        return [r["id"] for r in conn.execute(
            f"SELECT el.id FROM email_leads el "
            f"JOIN businesses b ON b.id = el.business_id {where}", params
        ).fetchall()]


def get_lead_sources(owner_id=None):
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
                   j.destination             AS destination,
                   j.country                 AS country,
                   j.status                  AS status,
                   COUNT(*)                  AS count
              FROM businesses b
              -- The job has to be this operator's own. A row can name any job
              -- id -- a CSV column, a worker's tag -- and joining on the id
              -- alone would print someone else's niche and city as a list.
              LEFT JOIN scrape_jobs j ON j.id = b.source_job_id AND j.owner_id = b.owner_id
             WHERE b.owner_id = ?
             GROUP BY b.source_job_id
             ORDER BY (b.source_job_id IS NULL), b.source_job_id DESC
        """, (_resolve_owner_id(conn, owner_id),)).fetchall()

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
            "niche": r["niche"] or "",
            "city": r["city"] or "",
            "scraped_at": r["scraped_at"],
            "destination": r["destination"] or "",
            "country": r["country"] or "",
            "status": r["status"] or "",
        })
    return out


def get_list_business_ids(source_job_id, owner_id=None) -> list:
    """Every business of this operator's that one lead list (a scrape, or the
    hand-added bucket) produced -- what "Add all to..." acts on."""
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        if str(source_job_id) == SOURCE_MANUAL:
            rows = conn.execute(
                "SELECT id FROM businesses WHERE owner_id=? AND source_job_id IS NULL ORDER BY id",
                (owner,))
        else:
            rows = conn.execute(
                "SELECT id FROM businesses WHERE owner_id=? AND source_job_id=? ORDER BY id",
                (owner, int(source_job_id)))
        return [r["id"] for r in rows]


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
    # Only leads still on the channel count. One taken off Calling, or ruled out
    # as not on WhatsApp, isn't being worked there, so it's no reason to hold an
    # import back for confirmation.
    for bid, in conn.execute(
        f"SELECT DISTINCT business_id FROM call_leads "
        f"WHERE business_id IN ({placeholders}) AND removed_at IS NULL", ids,
    ):
        out[bid]["call"] = True
    for bid, in conn.execute(
        f"SELECT DISTINCT business_id FROM wa_leads "
        f"WHERE business_id IN ({placeholders}) AND removed_at IS NULL AND moved_to = ''", ids,
    ):
        out[bid]["whatsapp"] = True
    return out


_CHANNEL_LABELS = {"email": "Email", "call": "Calling", "whatsapp": "WhatsApp"}


def find_cross_channel_conflicts(rows: list, channel: str, owner_id=None) -> list:
    """
    Which of these import rows resolve to a business already active on a
    DIFFERENT channel than the one they're about to be added to.

    Read-only -- this never creates or attaches anything, so it is safe to
    call before the operator has decided whether to proceed. A row whose
    business doesn't exist yet, or already exists only on `channel` itself
    (a normal re-import), is not a conflict.

    Returns a list of {row, business_id, business_name, channels, stage} --
    `row` is the original dict, `channels` the OTHER channels already present,
    in the stable order email/call/whatsapp regardless of lookup order, and
    labelled for direct display. `stage` is how far along the business already
    is, so the confirmation can say "already on whatsapp - meeting booked"
    rather than leaving you to go and look before deciding.
    """
    if not rows:
        return []
    with get_db() as conn:
        owner_id = _resolve_owner_id(conn, owner_id)
        resolved = []   # (row, business_row) for rows that matched something
        for r in rows:
            existing = find_existing_business(
                conn,
                email=(r.get("email") or ""),
                phone=(r.get("phone") or ""),
                website=(r.get("website") or ""),
                company=(r.get("company") or r.get("name") or ""),
                address=(r.get("address") or ""),
                owner_id=owner_id,
            )
            if existing:
                resolved.append((r, existing))

        if not resolved:
            return []
        presence = get_channel_presence(conn, [b["id"] for _, b in resolved])
        labels = {r["key"]: r["label"] for r in
                  conn.execute("SELECT key, label FROM pipeline_stages").fetchall()}

    conflicts = []
    for r, biz in resolved:
        other = [c for c in ("email", "call", "whatsapp")
                 if c != channel and presence[biz["id"]][c]]
        if other:
            stage_key = (biz["pipeline_stage"] if "pipeline_stage" in biz.keys() else "") or ""
            conflicts.append({
                "row": r,
                "business_id": biz["id"],
                "business_name": biz["name"],
                "channels": other,
                "channel_labels": [_CHANNEL_LABELS[c] for c in other],
                "stage": labels.get(stage_key, stage_key),
            })
    return conflicts


def channel_conflicts_for_businesses(business_ids: list, channel: str, owner_id=None) -> list:
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
        owner_id = _resolve_owner_id(conn, owner_id)
        presence = get_channel_presence(conn, business_ids)
        names = {r["id"]: r["name"] for r in conn.execute(
            f"SELECT id, name FROM businesses WHERE id IN ({placeholders}) AND owner_id=?",
            (*business_ids, owner_id),
        )}
    out = []
    for bid in business_ids:
        # A business this operator doesn't own is not theirs to be told about,
        # even to the extent of "it exists and is on Calling".
        if bid not in names:
            continue
        other = [c for c in ("email", "call", "whatsapp")
                 if c != channel and presence.get(bid, {}).get(c)]
        if other:
            out.append({
                "business_id": bid, "business_name": names[bid],
                "channels": other, "channel_labels": [_CHANNEL_LABELS[c] for c in other],
            })
    return out


def find_cross_owner_matches(rows: list, owner_id=None) -> list:
    """
    Which of these import rows look like a business somebody ELSE is already
    working. Purely advisory: nothing is merged, nothing is blocked, and the
    importing operator still gets their own row if they go ahead.

    Deliberately thin on detail -- the business name, which channels it is
    being worked on, and when it was added. Not the phone number, not the
    address on file, not any notes or call outcomes. The point is to let two
    people notice they are about to work the same clinic, not to give either
    one a window into the other's list.

    Matching is the same identity resolution the import itself uses, so it
    inherits the same limits: two rows for one real business that share no
    phone, domain, email or name+locality will not be spotted. That makes this
    a heads-up, never a guarantee of no overlap.
    """
    if not rows:
        return []
    with get_db() as conn:
        owner_id = _resolve_owner_id(conn, owner_id)
        resolved = []
        for r in rows:
            other = find_existing_business(
                conn,
                email=(r.get("email") or ""),
                phone=(r.get("phone") or ""),
                website=(r.get("website") or ""),
                company=(r.get("company") or r.get("name") or ""),
                address=(r.get("address") or ""),
                exclude_owner_id=owner_id,
            )
            if other:
                resolved.append((r, other))

        if not resolved:
            return []
        presence = get_channel_presence(conn, [b["id"] for _, b in resolved])
        owners = {u["id"]: u["username"] for u in conn.execute("SELECT id, username FROM users")}

    out = []
    for r, biz in resolved:
        channels = [c for c in ("email", "call", "whatsapp") if presence[biz["id"]][c]]
        out.append({
            "row": r,
            "business_name": biz["name"],
            "owner_id": biz["owner_id"],
            "owner_name": owners.get(biz["owner_id"], "another user"),
            "channels": channels,
            "channel_labels": [_CHANNEL_LABELS[c] for c in channels],
            "since": (biz["created_at"] or "")[:10],
        })
    return out


def delete_campaign(campaign_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM sends WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))


def delete_email_leads(ids: list, owner_id=None):
    """
    Hard-delete addresses. Scoped to one operator: this takes ids straight from
    a request body, so without the owner clause a guessed id would delete
    somebody else's lead outright.
    """
    if not ids:
        return
    placeholders = ','.join('?' for _ in ids)
    with get_db() as conn:
        conn.execute(
            f"DELETE FROM email_leads WHERE id IN ({placeholders}) AND owner_id = ?",
            [*ids, _resolve_owner_id(conn, owner_id)],
        )


def create_email_lead(email: str, first_name='', last_name='', company='',
                      website='', address='', status='active', owner_id=None):
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
            owner_id = _resolve_owner_id(conn, owner_id)
            business_id = find_or_create_business(conn, {
                "company": company, "website": website, "address": address,
            }, owner_id=owner_id)
            cur = conn.execute(
                "INSERT INTO email_leads(business_id,owner_id,email,first_name,last_name,status) "
                "VALUES(?,?,?,?,?,?)",
                (business_id, owner_id, email, first_name, last_name, status)
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
        # IN, not =. An address can now exist once per operator, and a scalar
        # subquery would silently pick whichever row it saw first -- stopping
        # one person's campaign while the other kept mailing someone who had
        # just asked to be left alone, over the same shared accounts.
        conn.execute("""
            UPDATE businesses SET do_not_contact=1
             WHERE id IN (SELECT business_id FROM email_leads WHERE email=?)
        """, (email_lc,))
        conn.execute("""
            UPDATE enrollments SET status='unsubscribed'
            WHERE email_lead_id IN (SELECT id FROM email_leads WHERE email=?)
              AND status NOT IN ('unsubscribed','bounced','completed','replied')
        """, (email_lc,))


def get_unsubscribed_contacts(owner_id=None):
    """
    This operator's own opt-outs, for their suppression list view.

    Only their own: the underlying suppression is global (see
    unsubscribe_contact), but who ELSE has been asked to stop is not something
    one operator needs to read out of another's list.
    """
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT el.email, el.first_name, el.last_name,
                   b.name AS company, el.created_at
              FROM email_leads el JOIN businesses b ON b.id = el.business_id
             WHERE el.status = 'unsubscribed' AND b.owner_id = ?
             ORDER BY el.created_at DESC
        """, (_resolve_owner_id(conn, owner_id),)).fetchall()]


def get_invalid_mx_contacts(owner_id=None):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT el.email, b.name AS company, b.website, b.address, el.created_at
              FROM email_leads el JOIN businesses b ON b.id = el.business_id
             WHERE el.mx_valid = 0 AND b.owner_id = ?
             ORDER BY el.created_at DESC
        """, (_resolve_owner_id(conn, owner_id),)).fetchall()]


def mark_bounced(email):
    with get_db() as conn:
        conn.execute("UPDATE email_leads SET status='bounced' WHERE email=?", (email.lower(),))
        # IN, not = -- see unsubscribe_contact. A dead address is dead for
        # everyone who holds it.
        conn.execute("""
            UPDATE enrollments SET status='bounced'
            WHERE email_lead_id IN (SELECT id FROM email_leads WHERE email=?)
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
                WHERE email_lead_id IN (SELECT id FROM email_leads WHERE email=?)
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


def get_variable_coverage(campaign_id: int = None, owner_id=None):
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
            # The "every contact" fallback is this operator's contacts. Without
            # the owner clause a freshly drafted step would report coverage
            # over the other operator's list.
            scope  = "all"
            where  = ("WHERE el.status NOT IN ('deleted','unsubscribed','bounced') "
                      "AND b.owner_id = ?")
            params = [_resolve_owner_id(conn, owner_id)]
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


def enroll_contacts_bulk(campaign_id, email_lead_ids, owner_id=None):
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

        # Lead ids arrive in a request body. Owning the campaign says nothing
        # about owning the addresses being put into it, and enrolling somebody
        # else's lead would both expose it in the campaign report and actually
        # mail them from this campaign.
        owner_id = _resolve_owner_id(conn, owner_id)
        email_lead_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM email_leads WHERE id IN (%s) AND owner_id = ?"
                % ",".join("?" * len(email_lead_ids)),
                [*email_lead_ids, owner_id],
            ).fetchall()
        ] if email_lead_ids else []

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
    """
    Record one send: a row in `sends`, and a tick on the shared daily cap.

    Both are keyed to the database's day, not Python's. sends.sent_at is
    UTC (datetime('now')), so the cap it feeds has to be too -- keying the
    tally by the server's local date instead meant the two tables disagreed
    about which day a send belonged to on any host not set to UTC.
    """
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sends(campaign_id,email_lead_id,step_num,subject,msg_id,account_id)
            VALUES(?,?,?,?,?,?)
        """, (campaign_id, email_lead_id, step_num, subject, msg_id, account_id))
        conn.execute("""
            INSERT INTO daily_counts(date,count) VALUES(DATE('now'),1)
            ON CONFLICT(date) DO UPDATE SET count=count+1
        """)


def get_today_count():
    """Today's sends across every campaign -- the shared daily cap. UTC, the
    same day log_send writes and sends.sent_at records."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT count FROM daily_counts WHERE date=DATE('now')"
        ).fetchone()
        return row["count"] if row else 0


def find_existing_business(conn, email="", phone="", website="", company="", address="",
                           exclude_id=None, owner_id=None, exclude_owner_id=None):
    """
    Find the business row matching these details. Returns a row or None.
    Read-only -- never creates a row; find_or_create_business wraps this with
    the create-if-missing step.

    Searches one operator's rows at a time. Pass `owner_id` to search that
    operator's own list (the import path: resolve, then merge or create).
    Pass `exclude_owner_id` to search everyone else's instead, which is how
    cross-owner overlap is spotted -- that mode is strictly a lookup, and no
    caller may write to what it returns.

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
    if exclude_owner_id is not None:
        owner_op, owner_arg = "!=", int(exclude_owner_id)
    else:
        owner_op, owner_arg = "=", _resolve_owner_id(conn, owner_id)

    email = (email or "").strip().lower()
    if email and "@" in email:
        row = conn.execute(f"""
            SELECT b.* FROM businesses b JOIN email_leads el ON el.business_id=b.id
             WHERE el.email=? AND b.owner_id {owner_op} ?
               AND (? IS NULL OR b.id != ?) LIMIT 1
        """, (email, owner_arg, exclude_id, exclude_id or -1)).fetchone()
        if row:
            return row

    phone_key = normalize_phone(phone)
    if phone_key:
        row = conn.execute(
            "SELECT * FROM businesses WHERE phone_normalized=? AND phone_normalized!='' "
            f"AND owner_id {owner_op} ? AND (? IS NULL OR id != ?) LIMIT 1",
            (phone_key, owner_arg, exclude_id, exclude_id or -1),
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
            f"AND owner_id {owner_op} ? AND (? IS NULL OR id != ?) LIMIT 1",
            (domain, owner_arg, exclude_id, exclude_id or -1),
        ).fetchone()
        if row:
            return row

    name_key = normalize_company(company)
    place    = _locality_key(address)
    if name_key and place:
        for row in conn.execute(
            f"SELECT * FROM businesses WHERE name!='' AND owner_id {owner_op} ? "
            "AND (? IS NULL OR id != ?)",
            (owner_arg, exclude_id, exclude_id or -1),
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

    "Today" is the database's, not Python's. sends.sent_at is written by
    SQLite's datetime('now'), which is UTC; comparing it against
    date.today(), which is the server's local date, made this return 0 for
    every send once the two dates diverged -- so on any host not set to UTC
    the per-campaign daily cap silently stopped being enforced for the last
    hours of each local day. Both sides are UTC now, matching how the call
    and WhatsApp counts have always done it.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM sends WHERE campaign_id=? AND DATE(sent_at)=DATE('now')",
            (campaign_id,),
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


def get_call_outcomes(include_archived=False, owner_id=None):
    """
    Every outcome this operator can pick: the shared built-ins plus their own.

    Another operator's custom outcomes are not offered and not listed -- what
    someone has invented a name for is a fair description of what they are
    working on.
    """
    clauses = ["(is_builtin = 1 OR owner_id = ?)"]
    if not include_archived:
        clauses.append("archived = 0")
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM call_outcome_types WHERE {' AND '.join(clauses)} "
            "ORDER BY sort_order, label",
            (_resolve_owner_id(conn, owner_id),),
        ).fetchall()
    return {r["key"]: dict(r) for r in rows}


def get_call_outcome(key: str):
    """One outcome, archived ones included -- historical rows still name them."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM call_outcome_types WHERE key=?", (key,)
        ).fetchone()
        return dict(row) if row else None


def terminal_outcome_keys(owner_id=None):
    """
    Which outcomes close a lead, for this operator.

    Deliberately includes every owner's terminal outcomes, not just this one's:
    these keys are matched against call_status values already written to rows,
    and a lead closed under an outcome this operator cannot see is still
    closed. Filtering here would resurrect it into their queue.
    """
    with get_db() as conn:
        return [r["key"] for r in conn.execute(
            "SELECT key FROM call_outcome_types WHERE is_terminal = 1"
        ).fetchall()]


def _slugify_outcome(label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
    return base[:40] or "outcome"


# ── Pipeline stages: how far along a business is, across every channel ───────
#
# One stage per business, not per channel. A meeting booked over WhatsApp is
# booked when you look at that business from Email too, which is the whole
# point: it stops you pitching someone your cofounder -- or you, last week --
# already got somewhere with.
#
# key: (label, is_terminal, wants_date, tone, sort_order)
_BUILTIN_PIPELINE_STAGES = {
    "replied":        ("Replied",        False, False, "info",    10),
    "proposal_due":   ("Proposal due",   False, True,  "amber",   20),
    "proposal_sent":  ("Proposal sent",  False, False, "good",    30),
    "booked":         ("Meeting booked", False, True,  "good",    40),
    "won":            ("Won",            True,  False, "good",    50),
    "not_interested": ("Not interested", True,  False, "bad",     60),
}

PIPELINE_CHANNELS = ("whatsapp", "call", "email")


def _seed_pipeline_stages(conn):
    """Insert the builtins once. Never updates them -- someone who renamed
    'Proposal due' to suit how they sell should keep that."""
    for key, (label, term, dated, tone, order) in _BUILTIN_PIPELINE_STAGES.items():
        conn.execute("""
            INSERT OR IGNORE INTO pipeline_stages
                (key, label, is_terminal, wants_date, tone, sort_order, is_builtin)
            VALUES(?,?,?,?,?,?,1)
        """, (key, label, int(term), int(dated), tone, order))


def get_pipeline_stages(include_archived=False, owner_id=None) -> dict:
    """
    Every stage this operator can pick: the shared built-ins plus their own.

    Another operator's invented stages are not offered and not listed -- what
    someone names their steps says how they sell, which is theirs.
    """
    clauses = ["(is_builtin = 1 OR owner_id = ?)"]
    if not include_archived:
        clauses.append("archived = 0")
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM pipeline_stages WHERE {' AND '.join(clauses)} "
            "ORDER BY sort_order, label", (_resolve_owner_id(conn, owner_id),),
        ).fetchall()
    return {r["key"]: dict(r) for r in rows}


def get_pipeline_stage(key: str):
    """One stage, archived ones included -- businesses still name them."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM pipeline_stages WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None


def create_pipeline_stage(label, is_terminal=False, wants_date=False,
                          tone="neutral", owner_id=None) -> str:
    """
    Add a stage of your own. Returns its key, which is derived from the label
    once and then kept -- businesses point at it, so renaming the label later
    changes what you read everywhere without stranding the rows.
    """
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        base = _slugify_outcome(label)
        key, n = base, 2
        while conn.execute("SELECT 1 FROM pipeline_stages WHERE key=?", (key,)).fetchone():
            key, n = f"{base}_{n}", n + 1
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 10 FROM pipeline_stages"
        ).fetchone()[0]
        conn.execute("""
            INSERT INTO pipeline_stages
                (key, owner_id, label, is_terminal, wants_date, tone, sort_order, is_builtin)
            VALUES(?,?,?,?,?,?,?,0)
        """, (key, owner, (label or "").strip()[:60] or key, int(bool(is_terminal)),
              int(bool(wants_date)), tone, nxt))
    return key


def update_pipeline_stage(key: str, owner_id=None, **fields) -> bool:
    """
    Edit a stage. A built-in can be relabelled but not archived by one
    operator, since the other one is still using it.
    """
    allowed = {"label", "is_terminal", "wants_date", "tone", "sort_order", "archived"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        row = conn.execute("SELECT * FROM pipeline_stages WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        if row["is_builtin"] and "archived" in updates:
            del updates["archived"]
        if not row["is_builtin"] and row["owner_id"] != owner:
            return False
        if "label" in updates:
            updates["label"] = (str(updates["label"]).strip()[:60]) or row["label"]
        for flag in ("is_terminal", "wants_date", "archived"):
            if flag in updates:
                updates[flag] = int(bool(updates[flag]))
        if not updates:
            return False
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE pipeline_stages SET {sets} WHERE key=?",
                     (*updates.values(), key))
    return True


def delete_pipeline_stage(key: str, owner_id=None) -> bool:
    """
    Remove a stage you invented. Built-ins can't go. Businesses sitting on it
    are cleared rather than left pointing at a stage that no longer exists.
    """
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        row = conn.execute("SELECT * FROM pipeline_stages WHERE key=?", (key,)).fetchone()
        if not row or row["is_builtin"] or row["owner_id"] != owner:
            return False
        conn.execute("UPDATE businesses SET pipeline_stage='', pipeline_channel='', "
                     "pipeline_at=NULL WHERE pipeline_stage=? AND owner_id=?", (key, owner))
        conn.execute("DELETE FROM pipeline_stages WHERE key=?", (key,))
    return True


def set_business_pipeline(business_id: int, stage: str, channel: str = "",
                          next_action_at=None, owner_id=None) -> dict:
    """
    Move a business along. `stage` is a key from pipeline_stages, or '' to
    clear it.

    Setting any stage stops that business's WhatsApp cadence -- you can't
    book someone who never answered, and leaving the follow-ups running after
    a reply is the one mistake this whole module exists to avoid. Clearing a
    stage deliberately does NOT restart them: a mis-click would otherwise
    quietly resume messaging someone you are mid-conversation with, so
    un-replying stays a separate, deliberate action.

    A terminal stage ('Not interested') does not set do_not_contact. That flag
    suppresses a business on every channel for every operator, and "not
    interested in this offer" is not "never contact us again".
    """
    stage = (stage or "").strip()
    channel = (channel or "").strip().lower()
    if channel and channel not in PIPELINE_CHANNELS:
        channel = ""
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        biz = conn.execute("SELECT id, owner_id FROM businesses WHERE id=?",
                           (business_id,)).fetchone()
        if not biz or biz["owner_id"] != owner:
            return {"ok": False, "error": "Not found"}
        if stage:
            known = conn.execute(
                "SELECT * FROM pipeline_stages WHERE key=? AND (is_builtin=1 OR owner_id=?)",
                (stage, owner),
            ).fetchone()
            if not known:
                return {"ok": False, "error": "Unknown stage"}
            conn.execute("""
                UPDATE businesses SET pipeline_stage=?, pipeline_channel=?,
                       pipeline_at=datetime('now'), next_action_at=?
                 WHERE id=?
            """, (stage, channel, _clean_date(next_action_at), business_id))
            # Stop the cadence. Scoped to this business's own WhatsApp lead;
            # nothing else on any channel is touched.
            conn.execute("""
                UPDATE wa_leads SET replied=1, wa_status='replied'
                 WHERE business_id=? AND replied=0 AND sent_date IS NOT NULL
            """, (business_id,))
        else:
            conn.execute("""
                UPDATE businesses SET pipeline_stage='', pipeline_channel='',
                       pipeline_at=NULL, next_action_at=NULL
                 WHERE id=?
            """, (business_id,))
        row = conn.execute(
            "SELECT pipeline_stage, pipeline_channel, pipeline_at, next_action_at "
            "FROM businesses WHERE id=?", (business_id,)).fetchone()
    return {"ok": True, **dict(row)}


def _clean_date(value):
    """A date from a form: 'YYYY-MM-DD' or a datetime, else nothing."""
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    return text[:19] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None


def business_ids_for_wa_leads(wa_lead_ids, owner_id=None) -> list:
    """The businesses behind these WhatsApp leads -- this operator's only."""
    with get_db() as conn:
        ids = _own_wa_lead_ids(conn, wa_lead_ids, owner_id)
        if not ids:
            return []
        rows = conn.execute(
            f"SELECT DISTINCT business_id FROM wa_leads WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchall()
    return [r["business_id"] for r in rows]


def set_pipeline_bulk(business_ids, stage: str, channel: str = "", owner_id=None) -> int:
    """The same, for a selection. Rows that aren't this operator's are skipped."""
    done = 0
    for bid in business_ids or []:
        try:
            bid = int(bid)
        except (TypeError, ValueError):
            continue
        if set_business_pipeline(bid, stage, channel, owner_id=owner_id).get("ok"):
            done += 1
    return done


def get_pipeline_board(owner_id=None, include_terminal=False, limit=200) -> list:
    """
    Conversations in progress: every business with a stage set, soonest thing
    owed first, anything overdue at the top. Terminal stages are left out --
    won and lost are not work.
    """
    with get_db() as conn:
        where = ["b.owner_id = ?", "b.pipeline_stage != ''"]
        params = [_resolve_owner_id(conn, owner_id)]
        if not include_terminal:
            where.append("COALESCE(s.is_terminal, 0) = 0")
        rows = conn.execute(f"""
            SELECT b.id AS business_id, b.name AS company, b.phone, b.city,
                   b.pipeline_stage, b.pipeline_channel, b.pipeline_at, b.next_action_at,
                   COALESCE(s.label, b.pipeline_stage) AS stage_label,
                   COALESCE(s.tone, '')                AS stage_tone,
                   COALESCE(s.is_terminal, 0)          AS stage_terminal
              FROM businesses b
              LEFT JOIN pipeline_stages s ON s.key = b.pipeline_stage
             WHERE {' AND '.join(where)}
             ORDER BY b.next_action_at IS NULL, b.next_action_at ASC, b.pipeline_at DESC
             LIMIT ?
        """, (*params, limit)).fetchall()
    return [dict(r) for r in rows]


def create_call_outcome(label, is_terminal=False, stops_email=False,
                        requires_date=False, tone="neutral", owner_id=None) -> str:
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
                 sort_order, is_builtin, owner_id)
            VALUES(?,?,?,?,?,?,?,0,?)
        """, (key, (label or "").strip()[:60] or key, int(bool(is_terminal)),
              int(bool(stops_email)), int(bool(requires_date)),
              tone if tone in ("neutral", "good", "bad", "info") else "neutral", nxt,
              _resolve_owner_id(conn, owner_id)))
        return key


def update_call_outcome(key: str, owner_id=None, **fields):
    """
    Edit an outcome. The key is never editable.

    Builtins can be relabelled and recoloured but keep their behaviour: the
    code special-cases 'do_not_call' for unsubscribing and 'booked' for the
    calendar file, so letting those flags be flipped would quietly break both.
    """
    row = get_call_outcome(key)
    if not row:
        return False
    # Somebody else's custom outcome is not yours to relabel or archive.
    # Built-ins are shared, so editing those stays an admin decision, gated at
    # the route rather than here.
    if not row["is_builtin"] and row["owner_id"] != _owner_or_default(owner_id):
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


def delete_call_outcome(key: str, owner_id=None):
    """
    Remove a custom outcome, or archive it if calls already used it.

    Returns ('deleted'|'archived'|'refused'). Archiving rather than deleting a
    used outcome keeps old call records readable -- a history row saying
    'outcome: follow_up_later' is useless once nothing can resolve that name.
    """
    row = get_call_outcome(key)
    if not row or row["is_builtin"]:
        return "refused"
    if row["owner_id"] != _owner_or_default(owner_id):
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


def create_call_campaign(name: str, notes: str = "", owner_id=None) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO call_campaigns(name, notes, owner_id) VALUES(?,?,?)",
            (name.strip() or "Untitled call campaign", notes or "",
             _resolve_owner_id(conn, owner_id)),
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


def add_to_calling(business_ids, owner_id=None, call_campaign_id=None) -> dict:
    """
    Put businesses this operator owns on Calling, optionally into a campaign.

    A lead taken off Calling earlier comes back with its call history intact.
    Skipped leads are counted rather than dropped silently, so the operator is
    told why fewer landed than they picked:
      no_phone   -- nothing to dial
      opted_out  -- do_not_contact is set; they asked to be left alone
      already    -- already on Calling (still added to the campaign, if given)
    """
    counts = {"added": 0, "already": 0, "no_phone": 0, "opted_out": 0, "in_campaign": 0}
    with get_db() as conn:
        for business_id in _own_business_ids(conn, business_ids, owner_id):
            biz = conn.execute(
                "SELECT phone, do_not_contact FROM businesses WHERE id=?", (business_id,)
            ).fetchone()
            if biz["do_not_contact"]:
                counts["opted_out"] += 1
                continue
            if not (biz["phone"] or "").strip():
                counts["no_phone"] += 1
                continue
            existing = conn.execute(
                "SELECT id, removed_at FROM call_leads WHERE business_id=?", (business_id,)
            ).fetchone()
            if existing and existing["removed_at"] is None:
                counts["already"] += 1
            else:
                counts["added"] += 1
            call_lead_id = get_or_create_call_lead(conn, business_id)
            if call_campaign_id:
                counts["in_campaign"] += conn.execute("""
                    INSERT OR IGNORE INTO call_campaign_members(call_campaign_id, call_lead_id)
                    VALUES(?,?)
                """, (int(call_campaign_id), call_lead_id)).rowcount
    return counts


def remove_from_calling(business_ids, owner_id=None) -> int:
    """
    Take leads off Calling. Their call history stays, and so does the lead in
    Contacts; they just stop appearing in any queue or campaign. Campaign
    membership goes with it -- a campaign is a batch you are working, and a
    lead you took off Calling is no longer part of that work.
    """
    removed = 0
    with get_db() as conn:
        for business_id in _own_business_ids(conn, business_ids, owner_id):
            row = conn.execute(
                "SELECT id FROM call_leads WHERE business_id=? AND removed_at IS NULL",
                (business_id,),
            ).fetchone()
            if not row:
                continue
            conn.execute("UPDATE call_leads SET removed_at=datetime('now') WHERE id=?",
                         (row["id"],))
            conn.execute("DELETE FROM call_campaign_members WHERE call_lead_id=?", (row["id"],))
            removed += 1
    return removed


def add_to_call_campaign(cid: int, business_ids, owner_id=None) -> int:
    """
    Add businesses to a call campaign, putting them on Calling if they aren't.
    Returns how many joined the campaign; see add_to_calling for what is
    skipped and why.

    Takes business ids rather than call-lead ids because that is what the
    operator is choosing from -- a clinic nobody has put on Calling yet has no
    call lead, and requiring one first would make the campaign picker useless
    for exactly the leads you are about to start on.
    """
    return add_to_calling(business_ids, owner_id=owner_id, call_campaign_id=cid)["in_campaign"]


def remove_from_call_campaign(cid: int, business_ids, owner_id=None) -> int:
    with get_db() as conn:
        removed = 0
        for business_id in _own_business_ids(conn, business_ids, owner_id):
            cur = conn.execute("""
                DELETE FROM call_campaign_members
                 WHERE call_campaign_id=?
                   AND call_lead_id IN (SELECT id FROM call_leads WHERE business_id=?)
            """, (cid, int(business_id)))
            removed += cur.rowcount
        return removed


def get_or_create_call_lead(conn, business_id: int) -> int:
    """
    The call lead for a business, created -- or brought back from having been
    taken off Calling -- on use. Logging a call against a lead is as clear a
    statement that it's on Calling as adding it is.
    """
    row = conn.execute(
        "SELECT id, removed_at FROM call_leads WHERE business_id=?", (business_id,)
    ).fetchone()
    if row:
        if row["removed_at"] is not None:
            conn.execute("UPDATE call_leads SET removed_at=NULL WHERE id=?", (row["id"],))
        return row["id"]
    return conn.execute(
        "INSERT INTO call_leads(business_id) VALUES(?)", (business_id,)
    ).lastrowid


def get_call_campaigns(owner_id=None):
    """
    Every campaign with its progress. One query per campaign is fine at this
    scale and keeps the counting rules in one readable place rather than a
    lattice of correlated subqueries.
    """
    terminal = terminal_outcome_keys() or ["__none__"]
    placeholders = ",".join("?" * len(terminal))

    with get_db() as conn:
        campaigns = [dict(r) for r in conn.execute(
            "SELECT * FROM call_campaigns WHERE owner_id=? ORDER BY id DESC",
            (_resolve_owner_id(conn, owner_id),)
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
                 WHERE m.call_campaign_id = ? AND b.owner_id = ?
                   AND cl.removed_at IS NULL
            """, (*terminal, *terminal, c["id"], c["owner_id"])).fetchone()

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


def get_call_lead_view(business_id: int, owner_id=None):
    """One business plus its calling state, or None. Backs the call detail card."""
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {_CALL_LEAD_COLUMNS} {_CALL_LEAD_JOIN} WHERE b.id=? AND b.owner_id=?",
            (business_id, _resolve_owner_id(conn, owner_id)),
        ).fetchone()
        return dict(row) if row else None


def search_businesses(q="", status=None, call_status=None, limit=100, owner_id=None,
                      not_on_channel=None):
    """
    Businesses matching a filter, for the "add existing leads" pickers on every
    channel.

    Separate from the email-lead search: this looks at businesses directly, so
    a clinic the scraper found with no email at all is still findable here.
    Opted-out businesses are left out -- every picker this backs puts a lead
    onto a channel, and they asked not to be on any.
    """
    where, params = ["b.owner_id = ?", "b.do_not_contact = 0"], [_owner_or_default(owner_id)]
    if status:
        where.append("b.web_status = ?")
        params.append(status)
    if not_on_channel == "whatsapp":
        # A number ruled out as not being on WhatsApp is never offered back as
        # a fresh lead. One merely taken off by hand can be.
        where.append("NOT EXISTS (SELECT 1 FROM wa_leads w WHERE w.business_id = b.id "
                     "AND (w.removed_at IS NULL OR w.moved_to != ''))")
    elif not_on_channel == "calling":
        where.append("NOT EXISTS (SELECT 1 FROM call_leads c2 WHERE c2.business_id = b.id "
                     "AND c2.removed_at IS NULL)")
    elif not_on_channel == "email":
        where.append("NOT EXISTS (SELECT 1 FROM email_leads e2 WHERE e2.business_id = b.id "
                     "AND e2.status != 'deleted')")
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


# The queue draws from call_leads, not from every business with a phone. A
# lead is on Calling because somebody put it there -- see _make_calling_explicit.
_CALL_QUEUE_JOIN = "FROM call_leads cl JOIN businesses b ON b.id = cl.business_id"

CALL_BUCKETS = ("today", "new", "upcoming", "all", "worked")


def _call_queue_filter(bucket, owner, source_job_id=None, only_no_website=False,
                       call_campaign_id=None):
    """WHERE clauses, params and ordering for one bucket. Shared by the list and
    the counts so the two can never disagree about what a bucket holds."""
    terminal = terminal_outcome_keys() or ["__none__"]
    tph = ",".join("?" * len(terminal))
    where, params = ["cl.removed_at IS NULL", "b.owner_id = ?"], [owner]

    # "worked" is the opposite of every other bucket: it exists precisely to
    # show the leads the others hide, so a lead closed out by mistake can be
    # found and reopened instead of disappearing.
    if bucket == "worked":
        where.append(f"cl.call_status IN ({tph})")
        params.extend(terminal)
        order = "b.id DESC"
    else:
        where += [f"(cl.call_status = '' OR cl.call_status NOT IN ({tph}))",
                  "b.do_not_contact = 0", "COALESCE(b.phone,'') != ''"]
        params.extend(terminal)
        if bucket == "today":
            where.append("cl.next_call_at IS NOT NULL AND datetime(cl.next_call_at) <= datetime('now')")
            order = "cl.next_call_at ASC"
        elif bucket == "new":
            where.append("cl.call_status = ''")
            order = "cl.created_at DESC, b.created_at DESC"
        elif bucket == "upcoming":
            where.append("cl.next_call_at IS NOT NULL AND datetime(cl.next_call_at) > datetime('now')")
            order = "cl.next_call_at ASC"
        else:
            order = "cl.next_call_at IS NULL, cl.next_call_at ASC, b.created_at DESC"

    if source_job_id:
        where.append("b.source_job_id = ?")
        params.append(int(source_job_id))
    if only_no_website:
        where.append("b.web_status = 'no_website'")
    if call_campaign_id:
        where.append("cl.id IN (SELECT call_lead_id FROM call_campaign_members "
                     "WHERE call_campaign_id = ?)")
        params.append(int(call_campaign_id))
    return where, params, order


def get_call_queue(bucket="today", limit=200, source_job_id=None, only_no_website=False,
                   call_campaign_id=None, owner_id=None):
    """
    The leads to work right now.

      today    -- callbacks due (including overdue), soonest first
      new      -- never called, most recently added to Calling first
      upcoming -- callbacks scheduled beyond today
      all      -- everything still callable
      worked   -- closed out, so a misclick can be found and reopened

    Terminal outcomes and opted-out businesses are excluded from every bucket
    but "worked": a finished lead should never reappear in a queue.
    """
    where, params, order = _call_queue_filter(
        bucket, _owner_or_default(owner_id), source_job_id, only_no_website, call_campaign_id)
    params.append(int(limit))
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT {_CALL_LEAD_COLUMNS} {_CALL_QUEUE_JOIN}
             WHERE {' AND '.join(where)}
             ORDER BY {order}
             LIMIT ?
        """, params).fetchall()
        return [dict(r) for r in rows]


def get_call_queue_counts(source_job_id=None, only_no_website=False, call_campaign_id=None,
                          owner_id=None):
    """Bucket sizes, so the tabs can show what is waiting without loading it."""
    owner = _owner_or_default(owner_id)
    out = {}
    with get_db() as conn:
        for bucket in ("today", "new", "upcoming", "worked"):
            where, params, _order = _call_queue_filter(
                bucket, owner, source_job_id, only_no_website, call_campaign_id)
            out[bucket] = conn.execute(
                f"SELECT COUNT(*) {_CALL_QUEUE_JOIN} WHERE {' AND '.join(where)}", params
            ).fetchone()[0]
    return out


# Whitelist: sort keys are interpolated into SQL.
_CALL_LEAD_SORT = {
    "company": "b.name", "phone": "b.phone", "call_status": "cl.call_status",
    "call_attempts": "cl.call_attempts", "next_call_at": "cl.next_call_at",
    "last_called_at": "last_called_at", "added": "cl.created_at", "city": "b.city",
}


def get_call_leads_page(page=1, per_page=50, q="", outcome="", call_campaign_id=None,
                        source_job_id=None, sort_col="", sort_dir="desc", owner_id=None):
    """
    Every lead on Calling as one table -- the Leads tab. Unlike the queue this
    includes closed-out and opted-out leads: it is where you go to see and
    manage the whole list, not to decide who to ring next.

    outcome: '' any, 'none' never called, or an outcome key.
    """
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 50), 500))
    where, params = ["cl.removed_at IS NULL", "b.owner_id = ?"], [_owner_or_default(owner_id)]
    if outcome == "none":
        where.append("cl.call_status = ''")
    elif outcome:
        where.append("cl.call_status = ?")
        params.append(outcome)
    if call_campaign_id == "none":
        where.append("cl.id NOT IN (SELECT call_lead_id FROM call_campaign_members)")
    elif call_campaign_id:
        where.append("cl.id IN (SELECT call_lead_id FROM call_campaign_members "
                     "WHERE call_campaign_id = ?)")
        params.append(int(call_campaign_id))
    if source_job_id not in (None, ""):
        if str(source_job_id) == SOURCE_MANUAL:
            where.append("b.source_job_id IS NULL")
        else:
            where.append("b.source_job_id = ?")
            params.append(int(source_job_id))
    q = (q or "").strip()
    if q:
        where.append("(b.name LIKE ? OR b.phone LIKE ? OR b.website LIKE ? "
                     "OR b.address LIKE ? OR b.city LIKE ?)")
        params.extend([f"%{q}%"] * 5)

    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    col = _CALL_LEAD_SORT.get(sort_col)
    order = (f"{col} IS NULL, {col} {direction}" if col else "cl.created_at DESC, cl.id DESC")

    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) {_CALL_QUEUE_JOIN} WHERE {' AND '.join(where)}", params
        ).fetchone()[0]
        rows = [dict(r) for r in conn.execute(f"""
            SELECT {_CALL_LEAD_COLUMNS},
                   cl.created_at AS added_at,
                   (SELECT MAX(l.called_at) FROM call_log l WHERE l.call_lead_id = cl.id)
                       AS last_called_at
              {_CALL_QUEUE_JOIN}
             WHERE {' AND '.join(where)}
             ORDER BY {order}
             LIMIT ? OFFSET ?
        """, (*params, per_page, (page - 1) * per_page)).fetchall()]

        ids = [r["call_lead_id"] for r in rows]
        campaigns = {}
        if ids:
            for m in conn.execute(f"""
                SELECT m.call_lead_id, c.id, c.name
                  FROM call_campaign_members m JOIN call_campaigns c ON c.id = m.call_campaign_id
                 WHERE m.call_lead_id IN ({",".join("?" * len(ids))})
                 ORDER BY c.id DESC
            """, ids):
                campaigns.setdefault(m["call_lead_id"], []).append(
                    {"id": m["id"], "name": m["name"]})
    for r in rows:
        r["campaigns"] = campaigns.get(r["call_lead_id"], [])
    return {"rows": rows, "total": total, "page": page, "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page)}


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


def get_call_summary(call_campaign_id=None, owner_id=None) -> dict:
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
        owner = _resolve_owner_id(conn, owner_id)
        row = conn.execute(f"""
            SELECT
              COUNT(*)                                                          AS leads,
              SUM(CASE WHEN cl.call_status = 'booked' THEN 1 ELSE 0 END)        AS booked,
              SUM(CASE WHEN cl.call_status = 'interested' THEN 1 ELSE 0 END)    AS interested,
              SUM(CASE WHEN cl.call_status = 'not_interested' THEN 1 ELSE 0 END) AS not_interested,
              SUM(CASE WHEN cl.call_status = '' THEN 1 ELSE 0 END)              AS uncalled,
              SUM(CASE WHEN cl.next_call_at IS NOT NULL
                        AND datetime(cl.next_call_at) <= datetime('now')
                        AND cl.call_status NOT IN ({tph}) THEN 1 ELSE 0 END)    AS due
              {_CALL_QUEUE_JOIN}
             WHERE cl.removed_at IS NULL AND b.do_not_contact = 0
               AND COALESCE(b.phone,'') != '' AND b.owner_id = ? {scope}
        """, (*terminal, owner, *scope_params)).fetchone()

        # Calls, not leads: one clinic rung four times is four calls, and that
        # is the number that reflects a day's work. Only this operator's calls
        # -- call_log has no owner of its own, so it is reached through the
        # business it was made to.
        call_where = ["b.owner_id = ?"]
        call_params = [owner]
        if call_campaign_id:
            call_where.append("l.call_campaign_id = ?")
            call_params.append(int(call_campaign_id))
        call_join = ("FROM call_log l JOIN call_leads cl ON cl.id = l.call_lead_id "
                     "JOIN businesses b ON b.id = cl.business_id")
        made = conn.execute(
            f"SELECT COUNT(*) {call_join} WHERE {' AND '.join(call_where)}", call_params
        ).fetchone()[0]
        today = conn.execute(
            f"SELECT COUNT(*) {call_join} WHERE {' AND '.join(call_where)} "
            f"AND DATE(l.called_at) = DATE('now')", call_params
        ).fetchone()[0]

    return {
        "leads":          row["leads"] or 0,
        "booked":         row["booked"] or 0,
        "interested":     row["interested"] or 0,
        "not_interested": row["not_interested"] or 0,
        "uncalled":       row["uncalled"] or 0,
        "due":            row["due"] or 0,
        "calls_made":     made or 0,
        "calls_today":    today or 0,
    }


def get_call_history(business_id: int, owner_id=None):
    """Every call to this business, newest first, across all its call leads."""
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT l.* FROM call_log l
              JOIN call_leads cl ON cl.id = l.call_lead_id
              JOIN businesses b  ON b.id  = cl.business_id
             WHERE cl.business_id=? AND b.owner_id=?
             ORDER BY l.called_at DESC, l.id DESC
        """, (business_id, _resolve_owner_id(conn, owner_id))).fetchall()]


def get_active_call_script(owner_id=None) -> dict:
    """
    This operator's script, creating an empty one on first use.

    One per operator: a script is the words someone says on the phone in their
    own voice, and the other person's pitch is neither useful to them nor
    theirs to read. A new operator gets the blank section headings rather than
    an inherited copy -- unlike the WhatsApp templates, where starting from
    working copy helps, a half-remembered script in someone else's voice is
    worse than an empty one.

    Seeded with section headings and no content: the words are the operator's,
    and inventing a script for them would put language in their mouth that
    they have to notice and delete mid-call.
    """
    with get_db() as conn:
        owner_id = _resolve_owner_id(conn, owner_id)
        row = conn.execute(
            "SELECT * FROM call_scripts WHERE is_active=1 AND owner_id=? ORDER BY id LIMIT 1",
            (owner_id,),
        ).fetchone()
        if not row:
            cur = conn.execute(
                "INSERT INTO call_scripts(name, sections, is_active, owner_id) VALUES(?,?,1,?)",
                ("Default script", json.dumps(_DEFAULT_SCRIPT_SECTIONS), owner_id),
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


def save_call_script(script_id: int, name: str, sections: list, owner_id=None):
    with get_db() as conn:
        conn.execute("""
            UPDATE call_scripts
               SET name=?, sections=?, updated_at=datetime('now')
             WHERE id=? AND owner_id=?
        """, (name or "Default script", json.dumps(sections or []), script_id,
              _resolve_owner_id(conn, owner_id)))


# ── WhatsApp ──────────────────────────────────────────────────────────────────
#
# Sending is manual by design -- see wa_leads' own comment in init_db. Nothing
# below ever transmits anything; it stages a business as a lead, keeps the
# message it will be sent (written live from its campaign's template), and
# tracks that the operator clicked Open in WhatsApp. The follow-up cadence is
# a live query (get_wa_followups_due), not a scheduled job, on the same
# principle: no background code path in this module touches the network.

# Every country Google's phone-number metadata knows (the data Android uses).
# Countries write local numbers differently -- the UAE drops a trunk 0, Qatar
# has none, Italy keeps its leading 0 after the country code -- and a
# hand-kept table of dialling codes would quietly build wa.me links that open
# the wrong chat. Region -> dialling code, as a string.
WA_COUNTRY_CODES = {
    region: str(phonenumbers.country_code_for_region(region))
    for region in phonenumbers.SUPPORTED_REGIONS
}


def is_supported_country(code) -> bool:
    return (code or "").strip().upper() in WA_COUNTRY_CODES


def list_countries() -> list:
    """Every country a number can be formatted for. Names come from the
    browser (Intl.DisplayNames), so nothing here needs translating or keeping
    up to date."""
    return [{"code": r, "dial": WA_COUNTRY_CODES[r]} for r in sorted(WA_COUNTRY_CODES)]


def _parse_phone(raw: str, country: str = ""):
    """A parsed number, or None. Without a known country the digits have to
    carry their own country code already."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    country = (country or "").strip().upper()
    try:
        if country in WA_COUNTRY_CODES:
            return phonenumbers.parse(raw, country)
        text = raw.strip()
        if text.startswith("00"):
            digits = digits[2:]
        return phonenumbers.parse("+" + digits, None)
    except phonenumbers.NumberParseException:
        return None


def format_whatsapp_number(raw: str, country: str = "") -> str:
    """
    Digits only, full international form, ready to drop straight into a
    wa.me link. wa.me rejects a leading + or a local trunk prefix, and Google
    Maps shows numbers in local format ("050 123 4567") with no country code
    at all, so this has to add what Maps left out, following that country's
    own rules.

    Accepts any number of a plausible length for the country, not only numbers
    in ranges the metadata already knows are assigned -- a new mobile range
    would otherwise be refused until the library caught up.

    Returns '' if there are no digits to work with. If the country is unknown
    and the digits don't already start with a country code, returns the digits
    as-is rather than guessing: a wrong guess opens the wrong chat, which is
    worse than a lead the operator has to fix by hand.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    number = _parse_phone(raw, country)
    if number is not None and phonenumbers.is_possible_number(number):
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)[1:]
    return digits


def _prefix_number_type(digits: str, country: str) -> str:
    """The old UAE/Qatar prefix rules, for numbers the metadata can't place --
    usually example or not-yet-listed ranges where the prefix still says a lot."""
    if country == "AE":
        local = digits[3:] if digits.startswith("971") else digits.lstrip("0")
        if local[:1] == "5":
            return "mobile"
        if local[:1] in "234679":
            return "landline"
    if country == "QA":
        local = digits[3:] if digits.startswith("974") else digits
        if local[:1] in "3567":
            return "mobile"
        if local[:1] == "4":
            return "landline"
    return "unknown"


def classify_number_type(raw: str, country: str = "") -> str:
    """
    'mobile' / 'landline' / 'unknown'.

    A soft signal, not a filter: WhatsApp Business does run on landlines, so
    this only warns in the list rather than hiding anything.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return "unknown"
    country = (country or "").strip().upper()
    number = _parse_phone(raw, country)
    if number is not None:
        kind = phonenumbers.number_type(number)
        if kind == phonenumbers.PhoneNumberType.MOBILE:
            return "mobile"
        if kind == phonenumbers.PhoneNumberType.FIXED_LINE:
            return "landline"
        if kind == phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE:
            return "unknown"
    return _prefix_number_type(digits, country)


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

WA_FOLLOWUP_DAYS_KEY = "wa_followup_days"
WA_DEFAULT_FOLLOWUP_DAYS = 3
WA_MAX_ARMS = 4

# How many bespoke follow-ups one lead can carry (wa_leads.draft_followups,
# and followup_1..N on an import row). Past the last one a lead falls back to
# its campaign's follow-up template, which then repeats at the campaign gap:
# follow-ups are infinite by design, so the personalised ones are a head start
# on the cadence rather than the whole of it.
WA_MAX_LEAD_FOLLOWUPS = 3

# Long enough for any real WhatsApp opener, short enough that a generation
# which ran away -- a model returning its whole reasoning, or a CSV row that
# swallowed the next column -- is rejected on import instead of sent.
WA_MAX_DRAFT_CHARS = 4000

# A/B arms are labelled, not numbered, because the label is what gets written
# onto every lead and every log row. Renumbering after deleting an arm would
# silently re-attribute history to the wrong copy.
WA_ARM_LABELS = ("A", "B", "C", "D")


def _wa_owner_key(base: str, owner_id: int) -> str:
    """
    Per-operator settings key.

    Suffixed rather than given its own table: these are three strings and a
    number per person, and _migrate_wa_settings moves the old shared value into
    the founding operator's key so nothing else has to change.
    """
    return f"{base}:{int(owner_id)}"


def _wa_setting(settings: dict, base: str, owner_id: int, default):
    """
    This operator's own value, or the factory default.

    Deliberately does NOT fall back to another operator's copy. An earlier
    version fell back to the shared pre-multi-user key, which meant a second
    operator opened the editor onto the first one's messages -- and two people
    here lead with different services, so that copy is both wrong for them and
    not theirs to read. Anyone who wants the other's wording can ask for it.
    """
    own = settings.get(_wa_owner_key(base, owner_id))
    return own if own not in (None, "") else default


def _wa_arms(raw, default: str) -> list:
    """
    Normalise a stored template into its list of arms.

    Stored as JSON when there is more than one arm and as a bare string when
    there is one, so a single-arm template is byte-identical to what the
    pre-A/B version wrote and downgrading loses nothing.
    """
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:
            parsed = json.loads(raw)
            arms = [a for a in parsed if isinstance(a, str) and a.strip()]
            if arms:
                return arms[:WA_MAX_ARMS]
        except (ValueError, TypeError):
            pass
    if isinstance(raw, str) and raw.strip():
        return [raw]
    return [default]


def get_wa_templates(owner_id=None) -> dict:
    """
    This operator's templates, each as a list of A/B arms, plus their follow-up
    interval. A single-arm list is the no-testing case and is the default.
    """
    settings = get_settings()
    owner_id = _owner_or_default(owner_id)
    out = {
        key: _wa_arms(_wa_setting(settings, setting_key, owner_id, default), default)
        for key, (setting_key, default) in _WA_TEMPLATE_SETTINGS_KEYS.items()
    }
    out["followup_days"] = get_wa_followup_days(owner_id, settings)
    return out


def get_wa_followup_days(owner_id=None, settings=None) -> int:
    settings = get_settings() if settings is None else settings
    raw = _wa_setting(settings, WA_FOLLOWUP_DAYS_KEY,
                      _owner_or_default(owner_id), WA_DEFAULT_FOLLOWUP_DAYS)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return WA_DEFAULT_FOLLOWUP_DAYS


def save_wa_templates(templates: dict, owner_id=None, followup_days=None):
    """Write this operator's own copy. Never touches the shared fallback."""
    owner_id = _owner_or_default(owner_id)
    updates = {}
    for key, (setting_key, _default) in _WA_TEMPLATE_SETTINGS_KEYS.items():
        if key not in templates:
            continue
        value = templates[key]
        arms = [value] if isinstance(value, str) else [
            a for a in (value or []) if isinstance(a, str) and a.strip()
        ]
        arms = arms[:WA_MAX_ARMS]
        if not arms:
            continue
        updates[_wa_owner_key(setting_key, owner_id)] = (
            arms[0] if len(arms) == 1 else json.dumps(arms)
        )
    if followup_days is not None:
        try:
            updates[_wa_owner_key(WA_FOLLOWUP_DAYS_KEY, owner_id)] = str(
                max(1, int(followup_days))
            )
        except (TypeError, ValueError):
            pass
    if updates:
        save_settings(updates)


def pick_wa_arm(arms: list, position: int) -> tuple:
    """
    Which arm this lead gets. Returns (label, text).

    Cycled by position rather than chosen at random: on the batch sizes this
    runs at -- often a handful of leads -- random assignment routinely deals
    every lead to one arm, and a test with nothing in the other side answers
    nothing.
    """
    if not arms:
        return "", ""
    idx = position % len(arms)
    label = WA_ARM_LABELS[idx] if len(arms) > 1 else ""
    return label, arms[idx]


def get_wa_variant_stats(owner_id=None, wa_campaign_id=None) -> list:
    """
    Reply rate per template arm -- the entire point of running a test.

    Counts leads, not messages: a lead that got an opener and three follow-ups
    is one prospect who did or didn't reply, and counting each send separately
    would make a long cadence look like a persuasive template. Paraphrased and
    plain are reported apart, because an AI rewrite is a different message and
    folding it in would confound the arm it was rewritten from.

    Leads carrying their own copy are reported apart for the same reason, and
    it matters more: every lead is dealt an arm label whether or not it is
    ever sent that arm's words, so counting an imported message's reply under
    version A would credit copy that was never sent. Those leads come back as
    own_copy rows, which is all that can honestly be said about them -- with
    a bespoke message per lead there is no repeated copy to compare.

    Scoped to one campaign when given one: arm A of one campaign's copy and arm
    A of another's are different messages, so pooling them measures nothing.
    """
    own_copy_sql = "CASE WHEN w.message_edited = 1 OR w.draft_followups != '' THEN 1 ELSE 0 END"
    with get_db() as conn:
        where, params = ["b.owner_id = ?", "w.sent_date IS NOT NULL"], \
            [_resolve_owner_id(conn, owner_id)]
        if wa_campaign_id:
            where.append("w.wa_campaign_id = ?")
            params.append(int(wa_campaign_id))
        rows = conn.execute(f"""
            SELECT COALESCE(NULLIF(w.template_variant,''),'-') AS arm,
                   w.paraphrased                               AS paraphrased,
                   {own_copy_sql}                              AS own_copy,
                   COUNT(*)                                    AS sent,
                   SUM(w.replied)                              AS replied
              FROM wa_leads w JOIN businesses b ON b.id = w.business_id
             WHERE {' AND '.join(where)}
             GROUP BY arm, w.paraphrased, own_copy
             ORDER BY own_copy, arm
        """, params).fetchall()
    out = []
    for r in rows:
        sent = r["sent"] or 0
        replied = r["replied"] or 0
        out.append({
            "arm": r["arm"],
            "paraphrased": bool(r["paraphrased"]),
            "own_copy": bool(r["own_copy"]),
            "sent": sent,
            "replied": replied,
            "reply_rate": round(replied / sent * 100, 1) if sent else 0.0,
        })
    return out


# ── WhatsApp: templates and variables ────────────────────────────────────────

# One opener and one follow-up per campaign. The opener used to come in two
# flavours chosen by an automatic booking check on the clinic's website; that
# check is gone -- it could only see one narrow thing, and a lead now goes out
# straight from its scrape, with any audit done later, by the operator.
WA_TEMPLATE_KINDS = ("opener", "followup")

# What a template can say about the lead, in the order the editor lists them.
# All of it comes from the Maps listing, so every lead has it the moment it
# arrives. Campaign variables add to these; a lead's own value wins over a
# campaign variable of the same name, as it does for email.
WA_TEMPLATE_FIELDS = [
    ("business_name", "Business name"),
    ("city",          "City"),
    ("category",      "Business category"),
    ("rating",        "Google rating"),
    ("review_count",  "Number of reviews"),
    ("website",       "Website"),
    ("address",       "Street address"),
]

# {{name}}, {{ name }} or {{name|fallback}} -- the same syntax as email copy,
# so one habit works everywhere.
_WA_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|([^}]*))?\}\}")


def render_wa_message(template: str, lead: dict, variables: dict = None) -> str:
    """
    Fill a template for one lead. `lead` is any row with the business joined
    in (`company` for the name, as every WhatsApp view returns it).

    A placeholder with no value becomes its fallback, or nothing -- never
    literal braces in a message someone reads on their phone. The business
    name alone falls back to "there", so "Hi {{business_name}}" can't come out
    as "Hi ," when a listing had no name.
    """
    fields = {str(k): v for k, v in (variables or {}).items()}
    for key, _label in WA_TEMPLATE_FIELDS:
        if key == "business_name":
            continue
        value = lead.get(key)
        if value not in (None, ""):
            fields[key] = value
    name = (lead.get("company") or lead.get("name") or "").strip()
    fields["business_name"] = name
    fields["company"] = name
    # Old templates may still say {{signal_detail}}; it fills from whatever a
    # lead had recorded before the automatic check was retired, else nothing.
    fields["signal_detail"] = lead.get("signal_detail") or ""

    def _resolve(match):
        key, fallback = match.group(1), match.group(2)
        value = fields.get(key)
        if value is None or not str(value).strip():
            if fallback is not None:
                return fallback.strip()
            return "there" if key in ("business_name", "company") else ""
        return str(value).strip()

    return _WA_PLACEHOLDER_RE.sub(_resolve, template or "")


def _render_wa_template(template: str, business: dict, signal_detail: str) -> str:
    """Older call shape, kept for callers that only have a name and a signal."""
    return render_wa_message(template, {"company": business.get("name") or "",
                                        "signal_detail": signal_detail})


_DEFAULT_WA_TEMPLATE_OPENER = (
    "Hi {{business_name}}! I came across your {{category|business}} in "
    "{{city|town}} and had a quick look at how you show up online. I help "
    "businesses like yours bring in more customers through their website, "
    "Google and social media. Would you be open to a quick chat?"
)


def _wa_factory_templates() -> dict:
    return {"opener": [_DEFAULT_WA_TEMPLATE_OPENER], "followup": [_DEFAULT_WA_TEMPLATE_FOLLOWUP]}


def _clean_wa_arms(value) -> list:
    arms = [value] if isinstance(value, str) else (value or [])
    return [a for a in arms if isinstance(a, str) and a.strip()][:WA_MAX_ARMS]


def parse_lead_followups(raw) -> list:
    """
    One lead's bespoke follow-ups, from the JSON in wa_leads.draft_followups.

    Always a list of exactly WA_MAX_LEAD_FOLLOWUPS strings, so callers can
    index it without bounds checks; an absent follow-up is ''. Unreadable
    JSON reads as "no bespoke follow-ups" rather than raising: a lead whose
    copy can't be parsed should quietly fall back to its campaign's template,
    not break the queue it appears in.
    """
    out = [""] * WA_MAX_LEAD_FOLLOWUPS
    if isinstance(raw, str):
        if not raw.strip():
            return out
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return out
    if not isinstance(raw, list):
        return out
    for i, value in enumerate(raw[:WA_MAX_LEAD_FOLLOWUPS]):
        if isinstance(value, str):
            out[i] = value.strip()
    return out


def serialize_lead_followups(followups) -> str:
    """
    Store the other way round: '' when there is nothing bespoke to keep, so
    the common case costs no JSON and `draft_followups != ''` is a usable
    test for "this lead brought its own follow-ups".
    """
    cleaned = parse_lead_followups(followups if isinstance(followups, (list, str)) else [])
    return json.dumps(cleaned) if any(cleaned) else ""


# The import keys a hyper-personalised row carries its copy in. Matched
# exactly, like every other import column -- nothing here lowercases or
# aliases a header, so the documented spelling is the only one that works.
WA_DRAFT_KEY = "message"
WA_FOLLOWUP_KEYS = tuple(f"followup_{i + 1}" for i in range(WA_MAX_LEAD_FOLLOWUPS))


def row_draft_copy(row: dict) -> tuple:
    """
    The bespoke copy one import row carries: (opener, followups, rejected).

    `rejected` counts messages thrown away for being implausibly long. They
    are dropped rather than truncated, and rather than failing the row: the
    lead is still worth importing, and half a message is worse than the
    campaign's template, which is what an empty slot falls back to.
    """
    rejected = 0

    def _one(value):
        nonlocal rejected
        if not isinstance(value, str):
            return ""
        text = value.strip()
        if not text:
            return ""
        if len(text) > WA_MAX_DRAFT_CHARS:
            rejected += 1
            return ""
        return text

    opener = _one(row.get(WA_DRAFT_KEY))
    followups = [_one(row.get(key)) for key in WA_FOLLOWUP_KEYS]
    return opener, followups, rejected


def new_draft_report() -> dict:
    """
    What an import will say about the copy it was given. Counted per slot,
    because a model writing four messages for each of 200 leads drops some,
    and a follow-up that silently fell back to the template is worth seeing
    while the file is still to hand rather than three weeks later.
    """
    return {
        "opener": 0,
        "followups": [0] * WA_MAX_LEAD_FOLLOWUPS,
        "too_long": 0,
        "kept_edits": 0,
        "no_phone": 0,
    }


def _clean_wa_variables(value) -> dict:
    """Keys become identifiers ({{my name}} can't be typed into a template)."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for k, v in value.items():
        key = re.sub(r"[^A-Za-z0-9_]", "_", str(k).strip()).strip("_")
        if key and not key[0].isdigit():
            out[key[:60]] = str(v if v is not None else "")[:500]
    return out


def _wa_label(index: int, arm_count: int) -> str:
    """The version label a lead carries. '' while a campaign has one version,
    which every rendering treats as the first."""
    return WA_ARM_LABELS[index] if arm_count > 1 else ""


def _wa_arm_index(label: str, arm_count: int) -> int:
    label = (label or "").strip()
    idx = WA_ARM_LABELS.index(label) if label in WA_ARM_LABELS else 0
    return idx if idx < max(arm_count, 1) else idx % max(arm_count, 1)


# ── WhatsApp: campaigns ───────────────────────────────────────────────────────

def _parse_wa_campaign(row) -> dict:
    out = dict(row)
    try:
        raw = json.loads(out.get("templates") or "{}")
    except (TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    factory = _wa_factory_templates()
    out["templates"] = {
        # "gap" was the opener's name while openers came in two flavours.
        "opener": _clean_wa_arms(raw.get("opener")) or _clean_wa_arms(raw.get("gap")) or factory["opener"],
        "followup": _clean_wa_arms(raw.get("followup")) or factory["followup"],
    }
    try:
        out["variables"] = _clean_wa_variables(json.loads(out.get("variables") or "{}"))
    except (TypeError, ValueError):
        out["variables"] = {}
    try:
        out["followup_days"] = max(1, int(out.get("followup_days")))
    except (TypeError, ValueError):
        out["followup_days"] = WA_DEFAULT_FOLLOWUP_DAYS
    return out


def create_wa_campaign(name: str, owner_id=None, country: str = "", notes: str = "",
                       copy_from=None) -> int:
    """
    A new campaign. Its copy starts from another of this operator's campaigns
    when `copy_from` names one -- the usual case, tweaking a pitch that works --
    and from the factory templates otherwise. Never from anyone else's.
    """
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        templates, followup_days, variables = _wa_factory_templates(), WA_DEFAULT_FOLLOWUP_DAYS, {}
        if copy_from:
            src = conn.execute("SELECT * FROM wa_campaigns WHERE id=? AND owner_id=?",
                               (int(copy_from), owner)).fetchone()
            if src:
                parsed = _parse_wa_campaign(src)
                templates = parsed["templates"]
                followup_days = parsed["followup_days"]
                variables = parsed["variables"]
        return conn.execute("""
            INSERT INTO wa_campaigns(owner_id, name, notes, country, templates,
                                     followup_days, variables)
            VALUES(?,?,?,?,?,?,?)
        """, (owner, (name or "").strip() or "Untitled campaign", notes or "",
              (country or "").strip().upper(), json.dumps(templates), followup_days,
              json.dumps(variables))).lastrowid


def get_wa_campaign(cid: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM wa_campaigns WHERE id=?", (int(cid),)).fetchone()
        return _parse_wa_campaign(row) if row else None


def _deal_wa_label(conn, campaign_id, arm_count: int, exclude_lead_id=None) -> str:
    """
    The version the next lead in this campaign gets: whichever has the fewest
    leads so far, first version on a tie. That alternates A, B, A, B on a fresh
    campaign and quietly rebalances after leads are moved, removed, or a
    version is deleted -- a strict turn counter would drift after any of those.
    """
    if not campaign_id or arm_count <= 1:
        return ""
    counts = [0] * arm_count
    params = [int(campaign_id)]
    extra = ""
    if exclude_lead_id:
        extra = "AND id != ?"
        params.append(int(exclude_lead_id))
    for r in conn.execute(f"""
        SELECT template_variant AS label, COUNT(*) AS n FROM wa_leads
         WHERE wa_campaign_id = ? AND moved_to = '' AND removed_at IS NULL {extra}
         GROUP BY template_variant
    """, params):
        counts[_wa_arm_index(r["label"], arm_count)] += r["n"]
    return WA_ARM_LABELS[counts.index(min(counts))]


def _remap_wa_versions(conn, cid: int, old_count: int, new_count: int, opener_from=None):
    """
    After the opener's versions change, point every UNSENT lead at the version
    it should now get. Sent leads keep their label: it records what they were
    actually sent, and their reply counts belong to it.

    `opener_from` lists, for each version as saved, the label it had before
    (None for a new one). Without it, versions are assumed to have kept their
    places. A lead whose version was deleted is dealt to whichever remaining
    version has fewest leads.
    """
    if opener_from is None:
        opener_from = [WA_ARM_LABELS[i] if i < old_count else None for i in range(new_count)]
    mapping = {}
    for new_idx, old_label in enumerate(opener_from[:new_count]):
        if old_label in WA_ARM_LABELS:
            mapping[old_label] = new_idx
    unsent = conn.execute("""
        SELECT id, template_variant FROM wa_leads
         WHERE wa_campaign_id = ? AND sent_date IS NULL AND wa_status = 'drafted'
         ORDER BY id
    """, (cid,)).fetchall()
    orphans = []
    for lead in unsent:
        old = lead["template_variant"] or "A"
        if old in mapping:
            conn.execute("UPDATE wa_leads SET template_variant=? WHERE id=?",
                         (_wa_label(mapping[old], new_count), lead["id"]))
        else:
            orphans.append(lead["id"])
    for lead_id in orphans:
        conn.execute("UPDATE wa_leads SET template_variant=? WHERE id=?",
                     (_deal_wa_label(conn, cid, new_count, exclude_lead_id=lead_id), lead_id))


def update_wa_campaign(cid: int, opener_from=None, **fields):
    """
    Save whichever of name, notes, country, status, templates, followup_days
    and variables were given. A template can't be saved empty -- a lead added
    later would have nothing to be written from.

    Unsent leads always read the current template (see wa_message_for), so a
    template edit reaches them with nothing to rewrite. Only a change in the
    NUMBER of opener versions needs work here: see _remap_wa_versions.
    """
    updates = {}
    if "name" in fields:
        name = (fields["name"] or "").strip()
        if not name:
            raise ValueError("A campaign needs a name")
        updates["name"] = name[:200]
    if "notes" in fields:
        updates["notes"] = str(fields["notes"] or "")
    if "country" in fields:
        updates["country"] = str(fields["country"] or "").strip().upper()
    if "status" in fields:
        if fields["status"] not in ("active", "archived"):
            raise ValueError("Unknown status")
        updates["status"] = fields["status"]
    if "followup_days" in fields:
        try:
            updates["followup_days"] = max(1, min(365, int(fields["followup_days"])))
        except (TypeError, ValueError):
            raise ValueError("The follow-up gap has to be a number of days")
    if "variables" in fields:
        updates["variables"] = json.dumps(_clean_wa_variables(fields["variables"]))

    current = get_wa_campaign(cid)
    old_count = len(current["templates"]["opener"]) if current else 1
    new_count = old_count
    if "templates" in fields:
        given = fields["templates"]
        if not isinstance(given, dict):
            raise ValueError("templates must be an object")
        merged = dict(current["templates"]) if current else _wa_factory_templates()
        for kind, value in given.items():
            if kind not in WA_TEMPLATE_KINDS:
                continue
            arms = _clean_wa_arms(value)
            if not arms:
                label = "opener" if kind == "opener" else "follow-up"
                raise ValueError(f"The {label} needs at least one version")
            merged[kind] = arms
        new_count = len(merged["opener"])
        updates["templates"] = json.dumps(merged)
    if not updates:
        return
    with get_db() as conn:
        conn.execute(f"UPDATE wa_campaigns SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?",
                     (*updates.values(), int(cid)))
        if "templates" in fields and (new_count != old_count or opener_from is not None):
            _remap_wa_versions(conn, int(cid), old_count, new_count, opener_from)


def delete_wa_campaign(cid: int) -> int:
    """
    Removes the campaign, never its leads. They stay on WhatsApp with no
    campaign, where they wait to be moved into one -- a lead without a
    campaign has no copy to be written from. Returns how many were left so.
    """
    with get_db() as conn:
        left = conn.execute("UPDATE wa_leads SET wa_campaign_id=NULL WHERE wa_campaign_id=?",
                            (int(cid),)).rowcount
        conn.execute("DELETE FROM wa_campaigns WHERE id=?", (int(cid),))
        return left


# A lead still on WhatsApp: not moved off it, not taken off by hand.
_WA_ACTIVE = "w.moved_to = '' AND w.removed_at IS NULL"
# ...and one there's still work on: not marked as not on WhatsApp. A marked
# lead stays on the channel, waiting to be moved off, but in no queue.
_WA_WORKABLE = f"{_WA_ACTIVE} AND w.no_whatsapp_at IS NULL"


def _wa_due_clause(days=None):
    """
    "Due a follow-up" for a sent lead. Each campaign has its own gap; a lead
    with no campaign falls back to the default. `days` overrides both, for a
    caller asking about one fixed window.
    """
    if days is not None:
        return "datetime(w.sent_date) <= datetime('now', ?)", [f"-{int(days)} days"]
    return (f"datetime(w.sent_date) <= datetime('now', '-' || "
            f"COALESCE(c.followup_days, {int(WA_DEFAULT_FOLLOWUP_DAYS)}) || ' days')"), []


def get_wa_campaigns(owner_id=None) -> list:
    """This operator's campaigns, newest first, each with the numbers it's judged by."""
    due, due_params = _wa_due_clause()
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT c.*,
              SUM(CASE WHEN w.id IS NOT NULL AND {_WA_ACTIVE} THEN 1 ELSE 0 END) AS leads,
              SUM(CASE WHEN w.id IS NOT NULL AND {_WA_WORKABLE} AND w.wa_status = 'drafted'
                        AND w.paused = 0 THEN 1 ELSE 0 END)                       AS ready,
              SUM(CASE WHEN w.wa_status IN ('sent','replied') THEN 1 ELSE 0 END)  AS messaged,
              SUM(CASE WHEN w.replied = 1 THEN 1 ELSE 0 END)                      AS replied,
              SUM(CASE WHEN w.id IS NOT NULL AND {_WA_WORKABLE} AND w.wa_status = 'sent'
                        AND w.replied = 0 AND w.paused = 0 AND w.sent_date IS NOT NULL
                        AND {due} THEN 1 ELSE 0 END)                              AS due,
              SUM(CASE WHEN w.id IS NOT NULL AND {_WA_ACTIVE}
                        AND w.no_whatsapp_at IS NOT NULL THEN 1 ELSE 0 END)       AS no_whatsapp
              FROM wa_campaigns c
              LEFT JOIN wa_leads w ON w.wa_campaign_id = c.id
             WHERE c.owner_id = ?
             GROUP BY c.id
             ORDER BY c.status = 'archived', c.id DESC
        """, (*due_params, _resolve_owner_id(conn, owner_id))).fetchall()
    out = []
    for r in rows:
        c = _parse_wa_campaign(r)
        for k in ("leads", "ready", "messaged", "replied", "due", "no_whatsapp"):
            c[k] = c.get(k) or 0
        c["reply_rate"] = round(c["replied"] / c["messaged"] * 100, 1) if c["messaged"] else 0.0
        out.append(c)
    return out


def get_wa_variable_coverage(cid: int) -> dict:
    """
    How many of a campaign's leads have a value for each template field, so a
    template leaning on {{city}} can be seen to be missing it for half the
    list before a message goes out saying "in ".
    """
    with get_db() as conn:
        base = f"FROM wa_leads w JOIN businesses b ON b.id = w.business_id " \
               f"WHERE w.wa_campaign_id = ? AND {_WA_ACTIVE}"
        total = conn.execute(f"SELECT COUNT(*) {base}", (int(cid),)).fetchone()[0]
        pieces = []
        for key, _label in WA_TEMPLATE_FIELDS:
            col = "b.name" if key == "business_name" else f"b.{key}"
            if key in ("rating", "review_count"):
                expr = f"{col} IS NOT NULL"
            else:
                expr = f"NULLIF(TRIM(COALESCE({col},'')),'') IS NOT NULL"
            pieces.append(f"SUM(CASE WHEN {expr} THEN 1 ELSE 0 END) AS {key}")
        row = conn.execute(f"SELECT {', '.join(pieces)} {base}", (int(cid),)).fetchone()
    return {
        "total": total,
        "fields": [{"key": k, "label": label, "filled": (row[k] or 0) if total else 0}
                   for k, label in WA_TEMPLATE_FIELDS],
    }


def set_wa_leads_campaign(wa_lead_ids, cid, owner_id=None) -> int:
    """
    Move leads into a campaign. Both have to be this operator's. An unsent
    lead is dealt a version in its new campaign; a sent one keeps the label it
    was sent under.
    """
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        campaign = None
        if cid is not None:
            row = conn.execute("SELECT * FROM wa_campaigns WHERE id=? AND owner_id=?",
                               (int(cid), owner)).fetchone()
            if not row:
                return 0
            campaign = _parse_wa_campaign(row)
        moved = 0
        for lead_id in _own_wa_lead_ids(conn, wa_lead_ids, owner):
            lead = conn.execute("SELECT sent_date, wa_campaign_id FROM wa_leads WHERE id=?",
                                (lead_id,)).fetchone()
            if campaign and lead["wa_campaign_id"] == campaign["id"]:
                continue
            conn.execute("UPDATE wa_leads SET wa_campaign_id=? WHERE id=?",
                         (campaign["id"] if campaign else None, lead_id))
            if campaign and lead["sent_date"] is None:
                conn.execute("UPDATE wa_leads SET template_variant=? WHERE id=?", (
                    _deal_wa_label(conn, campaign["id"], len(campaign["templates"]["opener"]),
                                   exclude_lead_id=lead_id), lead_id))
            moved += 1
        return moved


_WA_CAMPAIGNS_MARKER = "_migrated_wa_campaigns"


def _migrate_wa_campaigns(conn):
    """
    One-shot: give every operator's existing WhatsApp work a campaign.

    Copy moved from per-operator settings to per-campaign. Each operator with
    WhatsApp leads, or with templates of their own, gets one campaign holding
    their current copy, follow-up gap and every lead they already had -- so
    the switch changes where the copy is edited and nothing else. The old
    settings rows are left where they are; nothing reads them after this.

    Waits for a first user to exist, because campaigns belong to someone.
    """
    if conn.execute("SELECT 1 FROM settings WHERE key=?", (_WA_CAMPAIGNS_MARKER,)).fetchone():
        return
    users = [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]
    if not users:
        return
    settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}

    owners = {r["owner_id"] for r in conn.execute("""
        SELECT DISTINCT b.owner_id FROM wa_leads w JOIN businesses b ON b.id = w.business_id
         WHERE w.wa_campaign_id IS NULL
    """)}
    own_copy_keys = [key for key, _d in _WA_TEMPLATE_SETTINGS_KEYS.values()] + [WA_FOLLOWUP_DAYS_KEY]
    for uid in users:
        if any(settings.get(_wa_owner_key(k, uid)) not in (None, "") for k in own_copy_keys):
            owners.add(uid)
    owners.discard(OWNER_UNASSIGNED)

    for owner in sorted(owners):
        templates = {
            kind: _wa_arms(_wa_setting(settings, key, owner, default), default)
            for kind, (key, default) in _WA_TEMPLATE_SETTINGS_KEYS.items()
        }
        try:
            followup_days = max(1, int(_wa_setting(settings, WA_FOLLOWUP_DAYS_KEY, owner,
                                                   WA_DEFAULT_FOLLOWUP_DAYS)))
        except (TypeError, ValueError):
            followup_days = WA_DEFAULT_FOLLOWUP_DAYS
        country_row = conn.execute("""
            SELECT w.country, COUNT(*) AS n FROM wa_leads w JOIN businesses b ON b.id = w.business_id
             WHERE b.owner_id = ? AND w.country != '' GROUP BY w.country ORDER BY n DESC LIMIT 1
        """, (owner,)).fetchone()
        cid = conn.execute("""
            INSERT INTO wa_campaigns(owner_id, name, country, templates, followup_days)
            VALUES(?,?,?,?,?)
        """, (owner, "My first campaign", country_row["country"] if country_row else "",
              json.dumps(templates), followup_days)).lastrowid
        moved = conn.execute("""
            UPDATE wa_leads SET wa_campaign_id = ?
             WHERE wa_campaign_id IS NULL
               AND business_id IN (SELECT id FROM businesses WHERE owner_id = ?)
        """, (cid, owner)).rowcount
        logger.info("WhatsApp campaigns: put %d existing lead(s) and the current copy "
                    "into a first campaign for user %s", moved, owner)

    conn.execute("INSERT INTO settings(key, value) VALUES(?, '1')", (_WA_CAMPAIGNS_MARKER,))


_WA_NO_REVIEW_MARKER = "_migrated_wa_no_review"


def _migrate_wa_no_review(conn):
    """
    One-shot: leads stop waiting for a website check and a review.

    - Campaign templates: the "no booking" opener becomes the one opener. The
      "has booking" copy is kept in the JSON under `retired_no_gap`, unread,
      so it can still be recovered from the database.
    - Leads that were waiting to be checked, reviewed or written move to
      Ready to send, with no saved text -- their message reads the template.
    - Leads that already had a written message keep that text only if it
      differs from what the template gives now (a hand edit, an AI rewording,
      or copy from the retired opener), marked as edited so a template change
      won't overwrite it; otherwise they follow the template too.
    - Unsent leads in a multi-version campaign that never got a version are
      dealt one.
    """
    if conn.execute("SELECT 1 FROM settings WHERE key=?", (_WA_NO_REVIEW_MARKER,)).fetchone():
        return
    campaigns = {}
    for row in conn.execute("SELECT * FROM wa_campaigns").fetchall():
        try:
            raw = json.loads(row["templates"] or "{}")
        except (TypeError, ValueError):
            raw = {}
        if isinstance(raw, dict) and "opener" not in raw:
            migrated = {"opener": _clean_wa_arms(raw.get("gap")) or [_DEFAULT_WA_TEMPLATE_OPENER],
                        "followup": _clean_wa_arms(raw.get("followup")) or [_DEFAULT_WA_TEMPLATE_FOLLOWUP]}
            if _clean_wa_arms(raw.get("no_gap")):
                migrated["retired_no_gap"] = _clean_wa_arms(raw.get("no_gap"))
            conn.execute("UPDATE wa_campaigns SET templates=? WHERE id=?",
                         (json.dumps(migrated), row["id"]))
        refreshed = conn.execute("SELECT * FROM wa_campaigns WHERE id=?", (row["id"],)).fetchone()
        campaigns[row["id"]] = _parse_wa_campaign(refreshed)

    moved = conn.execute("""
        UPDATE wa_leads SET wa_status='drafted', draft_message='', message_edited=0
         WHERE wa_status IN ('', 'signal_ready', 'confirmed')
    """).rowcount

    kept = 0
    for lead in conn.execute(f"""
        SELECT w.id, w.template_variant, w.draft_message, w.wa_campaign_id, w.signal_detail,
               b.name AS company, b.city, b.category, b.rating, b.review_count, b.website, b.address
          FROM wa_leads w JOIN businesses b ON b.id = w.business_id
         WHERE w.wa_status = 'drafted' AND w.sent_date IS NULL AND w.draft_message != ''
    """).fetchall():
        campaign = campaigns.get(lead["wa_campaign_id"])
        current = ""
        if campaign:
            arms = campaign["templates"]["opener"]
            current = render_wa_message(arms[_wa_arm_index(lead["template_variant"], len(arms))],
                                        dict(lead), campaign["variables"])
        if lead["draft_message"].strip() == current.strip():
            conn.execute("UPDATE wa_leads SET draft_message='', message_edited=0 WHERE id=?",
                         (lead["id"],))
        else:
            conn.execute("UPDATE wa_leads SET message_edited=1 WHERE id=?", (lead["id"],))
            kept += 1

    for cid, campaign in campaigns.items():
        count = len(campaign["templates"]["opener"])
        if count <= 1:
            continue
        for lead in conn.execute("""
            SELECT id FROM wa_leads WHERE wa_campaign_id=? AND sent_date IS NULL
               AND wa_status='drafted' AND template_variant='' ORDER BY id
        """, (cid,)).fetchall():
            conn.execute("UPDATE wa_leads SET template_variant=? WHERE id=?",
                         (_deal_wa_label(conn, cid, count, exclude_lead_id=lead["id"]), lead["id"]))

    conn.execute("INSERT INTO settings(key, value) VALUES(?, '1')", (_WA_NO_REVIEW_MARKER,))
    if moved or kept:
        logger.info("WhatsApp: %d lead(s) moved to Ready to send; %d kept their written text",
                    moved, kept)


_WA_MESSAGE_SOURCE_MARKER = "_migrated_wa_message_source"


def _migrate_wa_message_source(conn):
    """
    One-shot: say where each lead's own copy came from.

    message_edited and paraphrased between them already carried this fact --
    edited-and-paraphrased meant the AI wrote it, edited alone meant the
    operator did. message_source states it directly, so an import can tell
    copy it may safely replace from copy that exists nowhere else.

    Every lead that predates imported drafts was written here by hand or by
    the reword pass, so nothing backfills to 'import'.
    """
    if conn.execute("SELECT 1 FROM settings WHERE key=?", (_WA_MESSAGE_SOURCE_MARKER,)).fetchone():
        return
    filled = conn.execute("""
        UPDATE wa_leads
           SET message_source = CASE WHEN paraphrased = 1 THEN 'ai' ELSE 'manual' END
         WHERE message_edited = 1 AND message_source = ''
    """).rowcount
    conn.execute("INSERT INTO settings(key, value) VALUES(?, '1')", (_WA_MESSAGE_SOURCE_MARKER,))
    if filled:
        logger.info("WhatsApp: recorded where %d lead(s)' own copy came from", filled)


# ── WhatsApp: leads ───────────────────────────────────────────────────────────

def _own_wa_lead_ids(conn, wa_lead_ids, owner_id=None) -> list:
    """The WhatsApp lead ids, out of these, that belong to this operator."""
    ids = []
    for i in wa_lead_ids or []:
        try:
            ids.append(int(i))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    keep = {r["id"] for r in conn.execute(
        f"SELECT w.id FROM wa_leads w JOIN businesses b ON b.id = w.business_id "
        f"WHERE w.id IN ({','.join('?' * len(ids))}) AND b.owner_id = ?",
        (*ids, _resolve_owner_id(conn, owner_id)),
    )}
    return [i for i in ids if i in keep]


def _own_wa_campaign(conn, wa_campaign_id, owner: int):
    if not wa_campaign_id:
        return None
    row = conn.execute("SELECT * FROM wa_campaigns WHERE id=? AND owner_id=?",
                       (int(wa_campaign_id), owner)).fetchone()
    return _parse_wa_campaign(row) if row else None


def _put_on_wa(conn, existing, business_id, phone, country, campaign):
    """
    Create or bring back one WhatsApp lead, ready to send. Shared by import and
    add-existing so both deal versions the same way. `existing` is the lead's
    current row, if it has one; the caller has already ruled out a number that
    was ruled out.
    """
    cid = campaign["id"] if campaign else None
    arm_count = len(campaign["templates"]["opener"]) if campaign else 1
    if existing:
        conn.execute("""
            UPDATE wa_leads SET removed_at=NULL, wa_campaign_id=?,
                   template_variant = CASE WHEN sent_date IS NULL THEN ? ELSE template_variant END,
                   wa_status = CASE WHEN sent_date IS NULL AND wa_status IN ('','signal_ready','confirmed')
                                    THEN 'drafted' ELSE wa_status END
             WHERE id=?
        """, (cid, _deal_wa_label(conn, cid, arm_count, exclude_lead_id=existing["id"]),
              existing["id"]))
        return existing["id"]
    return conn.execute("""
        INSERT INTO wa_leads(business_id, wa_number, country, number_type, wa_campaign_id,
                             wa_status, template_variant)
        VALUES(?,?,?,?,?,'drafted',?)
    """, (business_id, format_whatsapp_number(phone, country), country,
          classify_number_type(phone, country), cid,
          _deal_wa_label(conn, cid, arm_count))).lastrowid


def upsert_wa_leads(rows: list, default_country: str = "", owner_id=None,
                    wa_campaign_id=None) -> tuple:
    """
    Import WhatsApp leads: resolve/create the business the same way any
    channel's import does (find_or_create_business, so a clinic already
    known from email or calling is recognised rather than duplicated), then
    attach or update its wa_leads row -- ready to send straight away.

    `country` on a row overrides `default_country` -- a CSV can carry a
    country column of its own; the picker in the import UI is the fallback
    for one that doesn't.

    A row with no phone number still creates its business (it lands in
    Contacts) but can't go on WhatsApp. A lead already on WhatsApp keeps its
    campaign; one taken off by hand comes back, into this campaign; one ruled
    out as not on WhatsApp stays ruled out.

    A row may also carry its own copy -- `message` and `followup_1..3` -- for
    a lead written ahead of time rather than from the campaign's templates.
    Imported copy replaces copy that came from an earlier import, and never
    replaces what the operator wrote here by hand or had the AI reword: that
    text exists nowhere else, while an imported draft can be regenerated from
    the file. Nothing about a draft re-queues a lead that has already been
    messaged -- only _put_on_wa moves a lead's status, and only when it has
    never been sent.

    Returns (accepted, business_ids, drafts): accepted counts the rows now on
    WhatsApp; drafts is a new_draft_report() of the copy that came with them.
    """
    with get_db() as conn:
        accepted = 0
        touched = set()
        ordered_ids = []
        drafts_report = new_draft_report()
        owner_id = _resolve_owner_id(conn, owner_id)
        campaign = _own_wa_campaign(conn, wa_campaign_id, owner_id)

        for r in rows:
            name = (r.get("company") or r.get("name") or "").strip()
            phone = (r.get("phone") or "").strip()
            email = (r.get("email") or "").strip().lower()
            website = (r.get("website") or "").strip()
            if not any((email, name, phone, website)):
                continue

            opener_draft, followup_drafts, rejected = row_draft_copy(r)
            drafts_report["too_long"] += rejected
            if opener_draft:
                drafts_report["opener"] += 1
            for i, text in enumerate(followup_drafts):
                if text:
                    drafts_report["followups"][i] += 1
            has_draft = bool(opener_draft or any(followup_drafts))

            business_id = find_or_create_business(conn, r, owner_id=owner_id)
            touched.add(business_id)
            if business_id not in ordered_ids:
                ordered_ids.append(business_id)

            if email and "@" in email:
                status = r.get("status", "active")
                if status in ("no_website", "form_only", "no_email", ""):
                    status = "active"
                conn.execute("""
                    INSERT INTO email_leads(business_id, owner_id, email, first_name, last_name, status, mx_valid)
                    VALUES(:business_id,:owner_id,:email,:first_name,:last_name,:status,:mx_valid)
                    ON CONFLICT(owner_id, email) WHERE email IS NOT NULL AND email != '' DO UPDATE SET
                        first_name = COALESCE(NULLIF(excluded.first_name,''), email_leads.first_name),
                        last_name  = COALESCE(NULLIF(excluded.last_name,''),  email_leads.last_name),
                        mx_valid   = COALESCE(excluded.mx_valid,              email_leads.mx_valid)
                """, {
                    "business_id": business_id, "owner_id": owner_id, "email": email,
                    "first_name": r.get("first_name", ""), "last_name": r.get("last_name", ""),
                    "status": status, "mx_valid": r.get("mx_valid"),
                })
                conn.execute(
                    "UPDATE businesses SET web_status='has_email' WHERE id=? AND web_status=''",
                    (business_id,),
                )

            if not phone:
                # The business is filed, but there is no wa_lead for its copy
                # to live on. Counted rather than dropped in silence: a row
                # someone paid a model to write four messages for should not
                # vanish because the phone column was empty.
                if has_draft:
                    drafts_report["no_phone"] += 1
                continue
            country = (r.get("country") or default_country or "").strip().upper()
            existing = conn.execute(
                "SELECT id, wa_number, removed_at, moved_to, wa_campaign_id "
                "FROM wa_leads WHERE business_id=?", (business_id,)
            ).fetchone()
            if existing:
                wa_number = format_whatsapp_number(phone, country)
                conn.execute("""
                    UPDATE wa_leads SET
                        wa_number   = COALESCE(NULLIF(wa_number,''), ?),
                        country     = COALESCE(NULLIF(country,''), ?),
                        number_type = CASE WHEN COALESCE(NULLIF(wa_number,''), ?) != wa_number
                                           THEN ? ELSE number_type END
                    WHERE id=?
                """, (wa_number, country, wa_number, classify_number_type(phone, country),
                      existing["id"]))
                if existing["removed_at"] is not None and not existing["moved_to"]:
                    _put_on_wa(conn, existing, business_id, phone, country, campaign)
                elif existing["wa_campaign_id"] is None and campaign:
                    _put_on_wa(conn, existing, business_id, phone, country, campaign)
                wa_lead_id = existing["id"]
            else:
                wa_lead_id = _put_on_wa(conn, None, business_id, phone, country, campaign)
            if has_draft and not _apply_imported_drafts(
                conn, wa_lead_id, opener_draft, followup_drafts
            ):
                drafts_report["kept_edits"] += 1
            accepted += 1

        for business_id in touched:
            _pick_business_winner(conn, business_id)

        return accepted, ordered_ids, drafts_report


def _apply_imported_drafts(conn, wa_lead_id: int, opener: str, followups: list) -> bool:
    """
    Write one row's bespoke copy onto its lead. Returns False when the lead
    was left alone because its copy is the operator's own.

    The gate is message_source, which describes the lead's copy as a whole:
    once anything on it has been typed or reworded here, a re-import stops
    touching any of it rather than replacing an opener that a hand-edited
    follow-up was written to follow on from.

    An empty slot in the file leaves whatever that slot already held, so a
    model that returned three follow-ups instead of four doesn't blank the
    fourth from a previous import.
    """
    row = conn.execute(
        "SELECT draft_message, draft_followups, message_source FROM wa_leads WHERE id=?",
        (wa_lead_id,),
    ).fetchone()
    if not row:
        return True
    if (row["message_source"] or "") in ("manual", "ai"):
        return False

    merged = parse_lead_followups(row["draft_followups"])
    for i, text in enumerate(followups[:WA_MAX_LEAD_FOLLOWUPS]):
        if text:
            merged[i] = text
    message = opener or (row["draft_message"] or "")
    conn.execute("""
        UPDATE wa_leads SET draft_message=?, message_edited=?, message_source='import',
               paraphrased=0, draft_followups=?
         WHERE id=?
    """, (message, 1 if message.strip() else 0, serialize_lead_followups(merged), wa_lead_id))
    return True


def add_businesses_to_wa(business_ids, country: str, owner_id=None, wa_campaign_id=None) -> dict:
    """
    Put leads this operator already has onto WhatsApp, into a campaign, ready
    to send.

    Anything skipped is counted, so the operator is told why rather than left
    wondering where their selection went:
      no_phone   -- nothing to put in a wa.me link
      ruled_out  -- moved off WhatsApp earlier because the number wasn't on it;
                    adding it back would requeue a number known to be dead
      opted_out  -- do_not_contact is set; they asked to be left alone
      already    -- already on WhatsApp
    A lead only taken off WhatsApp by hand is added back, into this campaign.
    """
    country = (country or "").strip().upper()
    counts = {"added": 0, "already": 0, "no_phone": 0, "ruled_out": 0, "opted_out": 0}
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        campaign = _own_wa_campaign(conn, wa_campaign_id, owner)
        for business_id in _own_business_ids(conn, business_ids, owner):
            biz = conn.execute(
                "SELECT phone, do_not_contact FROM businesses WHERE id=?", (business_id,)
            ).fetchone()
            existing = conn.execute(
                "SELECT id, moved_to, removed_at FROM wa_leads WHERE business_id=?", (business_id,)
            ).fetchone()
            if existing and existing["moved_to"]:
                counts["ruled_out"] += 1
                continue
            if existing and existing["removed_at"] is None:
                counts["already"] += 1
                continue
            if biz["do_not_contact"]:
                counts["opted_out"] += 1
                continue
            phone = (biz["phone"] or "").strip()
            if not phone:
                counts["no_phone"] += 1
                continue
            _put_on_wa(conn, existing, business_id, phone, country, campaign)
            counts["added"] += 1
    return counts


# Every row a WhatsApp list view needs, business and campaign joined in the
# same shape the other channels use -- `company` for the name, so the UI needs
# no special-casing per channel.
_WA_LEAD_COLUMNS = """
    w.id, w.business_id, w.wa_number, w.country, w.number_type, w.wa_status,
    w.signal_type, w.signal_detail, w.draft_message, w.message_edited,
    w.draft_followups, w.message_source,
    w.template_variant, w.paraphrased, w.sent_date, w.replied, w.followup_count, w.paused,
    w.moved_to, w.removed_at, w.opened_at, w.no_whatsapp_at, w.notes, w.created_at, w.wa_campaign_id,
    c.name AS campaign_name, c.followup_days AS campaign_followup_days,
    b.name AS company, b.website, b.address, b.city, b.phone, b.category,
    b.rating, b.review_count, b.do_not_contact, b.source_job_id,
    b.pipeline_stage, b.pipeline_channel, b.pipeline_at, b.next_action_at
"""
_WA_LEAD_JOIN = ("FROM wa_leads w JOIN businesses b ON b.id = w.business_id "
                 "LEFT JOIN wa_campaigns c ON c.id = w.wa_campaign_id")


def wa_message_for(lead: dict, campaign: dict = None, kind: str = "opener") -> str:
    """
    The message this lead would be sent right now.

    The opener reads the campaign's CURRENT template for the lead's version,
    so editing a template reaches every unsent lead at once -- unless the
    operator edited this lead's message by hand, had AI reword it, or an
    import brought bespoke copy, in which case that text is kept.

    Follow-ups take the lead's own follow-up for the one that is next out
    (followup_count 0 means the first has yet to go), and fall back to the
    campaign's follow-up template for any slot that is empty -- including
    every follow-up past the last bespoke one, since the cadence runs
    indefinitely and only the first few are ever written by hand.

    Either way the text goes through render_wa_message, so a placeholder left
    in bespoke copy fills like it would in a template rather than reaching
    somebody's phone as literal braces.
    """
    variables = campaign["variables"] if campaign else {}
    if kind == "opener":
        if lead.get("message_edited") and (lead.get("draft_message") or "").strip():
            return render_wa_message(lead["draft_message"], lead, variables)
    else:
        drafts = parse_lead_followups(lead.get("draft_followups"))
        index = int(lead.get("followup_count") or 0)
        if 0 <= index < len(drafts) and drafts[index]:
            return render_wa_message(drafts[index], lead, variables)
    if not campaign:
        return (lead.get("draft_message") or "") if kind == "opener" else ""
    arms = campaign["templates"]["followup" if kind == "followup" else "opener"]
    return render_wa_message(arms[_wa_arm_index(lead.get("template_variant"), len(arms))],
                             lead, campaign["variables"])


def _attach_wa_messages(rows: list, kind: str = "opener") -> list:
    cache = {}
    for r in rows:
        cid = r.get("wa_campaign_id")
        if cid not in cache:
            cache[cid] = get_wa_campaign(cid) if cid else None
        r["message"] = wa_message_for(r, cache[cid], kind)
    return rows


def get_wa_lead(wa_lead_id: int, with_message: bool = False):
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {_WA_LEAD_COLUMNS} {_WA_LEAD_JOIN} WHERE w.id=?", (wa_lead_id,)
        ).fetchone()
    if not row:
        return None
    lead = dict(row)
    if with_message:
        _attach_wa_messages([lead])
    return lead


def get_wa_leads(status: str = None, limit: int = 200, owner_id=None, wa_campaign_id=None,
                 include_inactive: bool = False, with_message: bool = False) -> list:
    """
    The WhatsApp list, optionally scoped to one lifecycle stage:
    'drafted' (ready to send), 'sent' (in the follow-up cadence), 'replied'.

    Leads taken off WhatsApp are left out unless asked for.
    """
    clauses, params = ["b.owner_id = ?"], []
    with get_db() as conn:
        params.append(_resolve_owner_id(conn, owner_id))
        if not include_inactive:
            clauses.append(_WA_ACTIVE)
        if status is not None:
            clauses.append("w.wa_status = ?")
            params.append(status)
        if wa_campaign_id:
            clauses.append("w.wa_campaign_id = ?")
            params.append(int(wa_campaign_id))
        params.append(limit)
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_WA_LEAD_COLUMNS} {_WA_LEAD_JOIN} WHERE {' AND '.join(clauses)} "
            f"ORDER BY w.created_at DESC LIMIT ?", params,
        ).fetchall()]
    return _attach_wa_messages(rows) if with_message else rows


def get_wa_ready(limit: int = 500, owner_id=None, wa_campaign_id=None) -> list:
    """
    To do -> Ready to send: unsent leads, oldest first, messages filled in.
    Landlines go last -- they're the numbers least likely to be on WhatsApp.
    """
    with get_db() as conn:
        where = ["w.wa_status = 'drafted'", "w.paused = 0", _WA_WORKABLE, "b.owner_id = ?"]
        params = [_resolve_owner_id(conn, owner_id)]
        if wa_campaign_id:
            where.append("w.wa_campaign_id = ?")
            params.append(int(wa_campaign_id))
        rows = [dict(r) for r in conn.execute(f"""
            SELECT {_WA_LEAD_COLUMNS} {_WA_LEAD_JOIN}
             WHERE {' AND '.join(where)}
             ORDER BY w.number_type = 'landline', w.id ASC LIMIT ?
        """, (*params, int(limit))).fetchall()]
    return _attach_wa_messages(rows)


def _wa_stage_sql() -> tuple:
    """The one-word stage a lead is in, as the Leads table shows and filters it."""
    due, params = _wa_due_clause()
    return f"""
        CASE
          WHEN w.removed_at IS NOT NULL THEN 'removed'
          WHEN w.moved_to != ''         THEN 'moved'
          WHEN w.no_whatsapp_at IS NOT NULL THEN 'no_whatsapp'
          WHEN w.replied = 1            THEN 'replied'
          WHEN w.paused = 1             THEN 'paused'
          WHEN w.wa_status = 'sent' AND w.sent_date IS NOT NULL AND {due} THEN 'due'
          WHEN w.wa_status = 'sent'     THEN 'waiting'
          ELSE 'ready'
        END""", params


WA_STAGES = ("ready", "due", "waiting", "replied", "paused", "no_whatsapp", "moved", "removed")

_WA_LEAD_SORT = {
    "company": "company", "campaign_name": "campaign_name", "stage": "stage",
    "sent_date": "sent_date", "followup_count": "followup_count", "created_at": "created_at",
    "template_variant": "template_variant", "city": "city",
}


def get_wa_leads_page(page=1, per_page=50, q="", stage="", wa_campaign_id=None,
                      sort_col="", sort_dir="desc", owner_id=None) -> dict:
    """
    Every WhatsApp lead as one table -- the Leads tab.

    stage: '' everything still on WhatsApp, 'off' everything taken off it,
    'messaged' for everything already sent to and still waiting, or one stage
    from WA_STAGES. A value starting 'deal:' filters on the business's
    pipeline stage instead ('deal:booked'), which is how the Leads tab shows
    one chain -- the channel stage stops meaning anything once a lead has
    replied, and the deal stage means nothing before it.

    wa_campaign_id: a campaign id, or 'none' for leads with no campaign.
    """
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 50), 500))
    stage_sql, stage_params = _wa_stage_sql()
    inner_where, inner_params = ["b.owner_id = ?"], [_owner_or_default(owner_id)]
    if wa_campaign_id == "none":
        inner_where.append("w.wa_campaign_id IS NULL")
    elif wa_campaign_id:
        inner_where.append("w.wa_campaign_id = ?")
        inner_params.append(int(wa_campaign_id))
    q = (q or "").strip()
    if q:
        # Notes and the email address are in here because someone searching
        # for a lead reaches for whatever they remember about it, and an
        # address that silently matches nothing reads as a broken search.
        inner_where.append(
            "(b.name LIKE ? OR b.phone LIKE ? OR w.wa_number LIKE ? "
            " OR b.website LIKE ? OR b.city LIKE ? OR b.notes LIKE ? "
            " OR EXISTS (SELECT 1 FROM email_leads el WHERE el.business_id = b.id "
            "            AND el.email LIKE ?))")
        inner_params.extend([f"%{q}%"] * 7)

    stage = (stage or "").strip()
    outer_where, outer_params = [], []
    if stage.startswith("deal:"):
        outer_where.append("stage NOT IN ('moved','removed')")
        outer_where.append("pipeline_stage = ?")
        outer_params.append(stage[len("deal:"):])
    elif stage == "deal":
        outer_where.append("stage NOT IN ('moved','removed')")
        outer_where.append("pipeline_stage != ''")
    elif stage == "off":
        outer_where.append("stage IN ('moved','removed')")
    elif stage == "messaged":
        # "Who have I sent to and not heard back from" -- one question that
        # the channel stages split in two by whether the follow-up gap has
        # elapsed, which is a fact about the queue, not about the lead.
        outer_where.append("stage IN ('waiting','due')")
    elif stage in WA_STAGES:
        outer_where.append("stage = ?")
        outer_params.append(stage)
    else:
        outer_where.append("stage NOT IN ('moved','removed')")

    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    col = _WA_LEAD_SORT.get(sort_col)
    order = f"{col} IS NULL, {col} {direction}" if col else "created_at DESC, id DESC"

    inner = (f"SELECT {_WA_LEAD_COLUMNS}, {stage_sql} AS stage {_WA_LEAD_JOIN} "
             f"WHERE {' AND '.join(inner_where)}")
    params = [*stage_params, *inner_params]
    where = " AND ".join(outer_where)
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({inner}) WHERE {where}",
                             (*params, *outer_params)).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM ({inner}) WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, *outer_params, per_page, (page - 1) * per_page),
        ).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page": page,
            "per_page": per_page, "pages": max(1, (total + per_page - 1) // per_page)}


def get_wa_sent_log(owner_id=None, wa_campaign_id=None, since=None, limit=500) -> list:
    """
    What actually went out, newest first -- the list behind the "sent today"
    count. wa_log has held every send all along; nothing displayed it, so
    "which leads did I message?" had no answer but counting pills on the
    Leads tab.

    `since` is a UTC 'YYYY-MM-DD HH:MM:SS' -- the browser knows where the
    operator's day starts, the server doesn't. Omitted, it's everything.
    """
    with get_db() as conn:
        where = ["b.owner_id = ?"]
        params = [_resolve_owner_id(conn, owner_id)]
        if wa_campaign_id:
            where.append("w.wa_campaign_id = ?")
            params.append(int(wa_campaign_id))
        if since:
            where.append("l.sent_at >= ?")
            params.append(str(since).replace("T", " ")[:19])
        rows = conn.execute(f"""
            SELECT l.id, l.wa_lead_id, l.kind, l.message, l.sent_at,
                   w.business_id, w.wa_number, w.replied, w.followup_count,
                   b.name AS company, b.pipeline_stage,
                   c.name AS campaign_name
              FROM wa_log l
              JOIN wa_leads w   ON w.id = l.wa_lead_id
              JOIN businesses b ON b.id = w.business_id
              LEFT JOIN wa_campaigns c ON c.id = w.wa_campaign_id
             WHERE {' AND '.join(where)}
             ORDER BY l.sent_at DESC, l.id DESC
             LIMIT ?
        """, (*params, limit)).fetchall()
    return [dict(r) for r in rows]


def get_wa_summary(owner_id=None, wa_campaign_id=None, since=None) -> dict:
    """
    Counts for the WhatsApp page and the Dashboard. `since` (a UTC
    'YYYY-MM-DD HH:MM:SS') is the start of the operator's own day, for the
    sent-today counter -- the browser knows the local midnight, the server
    doesn't.
    """
    due, due_params = _wa_due_clause()
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        where, params = ["b.owner_id = ?"], [owner]
        if wa_campaign_id:
            where.append("w.wa_campaign_id = ?")
            params.append(int(wa_campaign_id))
        row = conn.execute(f"""
            SELECT
              SUM(CASE WHEN {_WA_ACTIVE} THEN 1 ELSE 0 END)                                AS total,
              SUM(CASE WHEN {_WA_WORKABLE} AND w.wa_status='drafted' AND w.paused=0 THEN 1 ELSE 0 END) AS ready_to_send,
              SUM(CASE WHEN {_WA_ACTIVE} AND w.wa_status='sent' THEN 1 ELSE 0 END)         AS in_cadence,
              SUM(CASE WHEN {_WA_WORKABLE} AND w.wa_status='sent' AND w.replied=0 AND w.paused=0
                        AND w.sent_date IS NOT NULL AND {due} THEN 1 ELSE 0 END)           AS due,
              SUM(CASE WHEN {_WA_ACTIVE} AND w.no_whatsapp_at IS NOT NULL THEN 1 ELSE 0 END) AS no_whatsapp,
              SUM(CASE WHEN w.wa_status IN ('sent','replied') THEN 1 ELSE 0 END)           AS messaged,
              SUM(w.replied)                                                               AS replied,
              SUM(CASE WHEN w.moved_to != '' OR w.removed_at IS NOT NULL THEN 1 ELSE 0 END) AS moved,
              SUM(CASE WHEN {_WA_ACTIVE} AND w.wa_campaign_id IS NULL THEN 1 ELSE 0 END)   AS no_campaign
              FROM wa_leads w JOIN businesses b ON b.id = w.business_id
              LEFT JOIN wa_campaigns c ON c.id = w.wa_campaign_id
             WHERE {' AND '.join(where)}
        """, (*due_params, *params)).fetchone()
        out = {k: (row[k] or 0) for k in row.keys()}

        log_where = ["b.owner_id = ?"]
        log_params = [owner]
        if wa_campaign_id:
            log_where.append("w.wa_campaign_id = ?")
            log_params.append(int(wa_campaign_id))
        if since:
            log_where.append("l.sent_at >= ?")
            log_params.append(str(since).replace("T", " ")[:19])
        else:
            log_where.append("DATE(l.sent_at) = DATE('now')")
        out["sent_today"] = conn.execute(f"""
            SELECT COUNT(*) FROM wa_log l JOIN wa_leads w ON w.id = l.wa_lead_id
              JOIN businesses b ON b.id = w.business_id
             WHERE {' AND '.join(log_where)}
        """, log_params).fetchone()[0]
    out["reply_rate"] = round(out["replied"] / out["messaged"] * 100, 1) if out["messaged"] else 0.0
    return out


def set_wa_message(wa_lead_id: int, message: str, paraphrased: bool = False):
    """
    The operator's own wording for this lead (or an AI rewording of it). Marked
    as edited, so a later template change doesn't overwrite it -- and sourced,
    so a later import doesn't either: this text exists only here, where an
    imported draft can always be regenerated from the file it came from.
    """
    with get_db() as conn:
        conn.execute("""
            UPDATE wa_leads SET draft_message=?, message_edited=1, paraphrased=?, message_source=?
             WHERE id=?
        """, (message, 1 if paraphrased else 0, "ai" if paraphrased else "manual", wa_lead_id))


def reset_wa_message(wa_lead_id: int):
    """
    Back to following the campaign's template.

    The opener only -- this lead's bespoke follow-ups are left alone, because
    they are separate messages the operator resets one at a time from their
    own boxes. Resetting the opener silently discarding three follow-ups
    written days earlier would be a surprising amount of collateral.
    """
    with get_db() as conn:
        conn.execute("""
            UPDATE wa_leads SET draft_message='', message_edited=0, paraphrased=0, message_source=''
             WHERE id=?
        """, (wa_lead_id,))


def set_wa_followup_draft(wa_lead_id: int, index: int, message: str) -> bool:
    """
    This lead's own wording for one follow-up. An empty message clears that
    slot, which puts the follow-up back on the campaign's template.

    Returns False for a slot that doesn't exist, so a caller with an id from
    a URL doesn't silently write nothing.
    """
    if not 0 <= index < WA_MAX_LEAD_FOLLOWUPS:
        return False
    with get_db() as conn:
        row = conn.execute(
            "SELECT draft_followups FROM wa_leads WHERE id=?", (wa_lead_id,)
        ).fetchone()
        if not row:
            return False
        drafts = parse_lead_followups(row["draft_followups"])
        drafts[index] = (message or "").strip()
        # Marks the lead's copy as the operator's own, the same as editing the
        # opener does. Coarse on purpose: a later import then leaves the whole
        # lead alone rather than replacing an opener that this follow-up was
        # written to follow on from. See _apply_imported_drafts.
        conn.execute(
            "UPDATE wa_leads SET draft_followups=?, message_source='manual' WHERE id=?",
            (serialize_lead_followups(drafts), wa_lead_id),
        )
    return True


def update_wa_message(wa_lead_id: int, message: str):
    """Older name for set_wa_message."""
    set_wa_message(wa_lead_id, message)


def mark_wa_opened(wa_lead_id: int, opened: bool = True):
    """
    The operator opened this lead's chat in WhatsApp, or said it didn't send
    (`opened=False`). Records nothing as sent: WhatsApp can't tell the app
    whether a message went -- or whether the number is even on WhatsApp -- so
    only the operator's own "Sent" does that (mark_wa_sent).
    """
    with get_db() as conn:
        if opened:
            conn.execute("UPDATE wa_leads SET opened_at=datetime('now') WHERE id=?", (wa_lead_id,))
        else:
            conn.execute("UPDATE wa_leads SET opened_at=NULL WHERE id=?", (wa_lead_id,))


def mark_wa_sent(wa_lead_id: int, message: str, kind: str = "opener",
                 template_variant: str = "", paraphrased: bool = False):
    """
    Records that the operator says the message went -- their word, not
    delivery confirmation; see wa_log's comment in init_db. Follow-ups call
    this too, incrementing followup_count so the cadence knows how many have
    gone out; the opener does not count as a follow-up.
    """
    with get_db() as conn:
        conn.execute("""
            INSERT INTO wa_log(wa_lead_id, kind, message, template_variant, paraphrased)
            VALUES(?,?,?,?,?)
        """, (wa_lead_id, kind, message, template_variant, 1 if paraphrased else 0))
        if kind == "followup":
            conn.execute("""
                UPDATE wa_leads SET
                    sent_date=datetime('now'), wa_status='sent', opened_at=NULL,
                    followup_count = followup_count + 1
                 WHERE id=?
            """, (wa_lead_id,))
        else:
            conn.execute(
                "UPDATE wa_leads SET sent_date=datetime('now'), wa_status='sent', opened_at=NULL WHERE id=?",
                (wa_lead_id,),
            )


def correct_wa_sent_date(wa_lead_id: int, sent_date: str = None):
    """
    Manual fix for "I opened the link but didn't actually send." sent_date is
    never verified against WhatsApp itself -- there's no way to -- so this is
    the one correction the operator has. Clearing it on a lead that has only
    had its opener opened puts it back in Ready to send.
    """
    with get_db() as conn:
        conn.execute("UPDATE wa_leads SET sent_date=? WHERE id=?", (sent_date, wa_lead_id))
        if sent_date is None:
            conn.execute("""
                UPDATE wa_leads SET wa_status='drafted'
                 WHERE id=? AND wa_status='sent' AND followup_count=0 AND replied=0
            """, (wa_lead_id,))


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


def set_wa_paused_bulk(wa_lead_ids, paused: bool, owner_id=None) -> int:
    with get_db() as conn:
        ids = _own_wa_lead_ids(conn, wa_lead_ids, owner_id)
        if not ids:
            return 0
        return conn.execute(
            f"UPDATE wa_leads SET paused=? WHERE id IN ({','.join('?' * len(ids))})",
            (1 if paused else 0, *ids),
        ).rowcount


def get_wa_followups_due(days: int = None, limit: int = 200, owner_id=None,
                         wa_campaign_id=None) -> list:
    """
    Leads due for a follow-up: sent at least their campaign's follow-up gap
    ago (or `days`, if given), not replied, not paused, still on WhatsApp. A
    live query, not a scheduled job -- nothing about surfacing "this is due"
    should touch the network, and computing it on read means there is no
    background code path here at all for anyone auditing the manual-send
    constraint to have to trust.
    """
    due, due_params = _wa_due_clause(days)
    with get_db() as conn:
        where = ["w.wa_status = 'sent'", "w.replied = 0", "w.paused = 0", _WA_WORKABLE,
                 "w.sent_date IS NOT NULL", due, "b.owner_id = ?"]
        params = [*due_params, _resolve_owner_id(conn, owner_id)]
        if wa_campaign_id:
            where.append("w.wa_campaign_id = ?")
            params.append(int(wa_campaign_id))
        rows = conn.execute(f"""
            SELECT {_WA_LEAD_COLUMNS} {_WA_LEAD_JOIN}
             WHERE {' AND '.join(where)}
             ORDER BY w.sent_date ASC LIMIT ?
        """, (*params, limit)).fetchall()
        return [dict(r) for r in rows]


WA_MOVE_DESTINATIONS = ("call", "email", "none")


class WaMoveRefused(ValueError):
    """A lead that can't go where it was sent. `reason` is what a bulk move
    counts it under: 'opted_out', 'no_phone' or 'no_email'."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def move_wa_lead(wa_lead_id: int, destination: str, owner_id=None, campaign_id=None) -> dict:
    """
    The number turned out not to be on WhatsApp (discovered by the operator,
    not this app -- see the module handover for why there's no automatic
    check). Marks moved_to so it drops out of WhatsApp for good and a later
    re-scrape can't quietly re-queue a number already ruled out here, and
    files the lead where the operator chose:

      call   -- onto Calling, and into `campaign_id` (a call campaign) if given
      email  -- enrolled in `campaign_id` (an email campaign) if given; needs an
                address on file, since there is nothing to email otherwise
      none   -- nowhere; it stays in Contacts, under Unassigned

    The other channel is set up first, so a refusal (no address, opted out)
    leaves the lead on WhatsApp rather than half-moved.
    """
    if destination not in WA_MOVE_DESTINATIONS:
        raise ValueError(f"Unknown destination: {destination}")
    lead = get_wa_lead(wa_lead_id)
    if not lead:
        raise ValueError("WhatsApp lead not found")
    owner = _owner_or_default(owner_id)
    business_id = lead["business_id"]
    result = {"destination": destination, "business_id": business_id}

    if destination == "call":
        counts = add_to_calling([business_id], owner_id=owner, call_campaign_id=campaign_id)
        if counts["opted_out"]:
            raise WaMoveRefused("opted_out", "They asked not to be contacted, so they can't go on Calling")
        if counts["no_phone"]:
            raise WaMoveRefused("no_phone", "There's no phone number on file to call")
        with get_db() as conn:
            result["call_lead_id"] = conn.execute(
                "SELECT id FROM call_leads WHERE business_id=?", (business_id,)
            ).fetchone()["id"]
        result["in_campaign"] = counts["in_campaign"]
    elif destination == "email":
        with get_db() as conn:
            row = conn.execute("""
                SELECT id FROM email_leads
                 WHERE business_id=? AND status='active' AND owner_id=?
                 ORDER BY duplicate_of IS NOT NULL, id LIMIT 1
            """, (business_id, owner)).fetchone()
        if not row:
            raise WaMoveRefused("no_email", "There's no email address on file for this business")
        result["email_lead_id"] = row["id"]
        if campaign_id:
            enrolled, skipped = enroll_contacts_bulk(int(campaign_id), [row["id"]], owner_id=owner)
            result["enrolled"] = enrolled
            result["skipped"] = skipped

    with get_db() as conn:
        conn.execute("UPDATE wa_leads SET moved_to=?, paused=1 WHERE id=?",
                     (destination, wa_lead_id))
    return result


def move_wa_leads(wa_lead_ids, destination: str, owner_id=None, campaign_id=None) -> dict:
    """
    Move many leads off WhatsApp at once -- usually everything marked as not
    on WhatsApp. Each goes through move_wa_lead, so a lead that can't go where
    it was sent (no email address, say) stays on WhatsApp, still marked, and
    is counted by why.
    """
    if destination not in WA_MOVE_DESTINATIONS:
        raise ValueError(f"Unknown destination: {destination}")
    owner = _owner_or_default(owner_id)
    with get_db() as conn:
        ids = [i for i in _own_wa_lead_ids(conn, wa_lead_ids, owner)
               if conn.execute("SELECT 1 FROM wa_leads w WHERE w.id=? AND " + _WA_ACTIVE, (i,)).fetchone()]
    out = {"moved": 0, "opted_out": 0, "no_phone": 0, "no_email": 0}
    for lead_id in ids:
        try:
            move_wa_lead(lead_id, destination, owner_id=owner, campaign_id=campaign_id)
            out["moved"] += 1
        except WaMoveRefused as exc:
            out[exc.reason] += 1
    return out


def set_wa_no_whatsapp(wa_lead_ids, marked: bool = True, owner_id=None) -> int:
    """
    Mark leads as not on WhatsApp, or take the mark off. A marked lead stays
    on WhatsApp but drops out of Ready to send and Follow-up due, so the
    operator can keep going and move them all off later.
    """
    with get_db() as conn:
        ids = _own_wa_lead_ids(conn, wa_lead_ids, owner_id)
        if not ids:
            return 0
        marks = ("no_whatsapp_at=datetime('now'), opened_at=NULL", "no_whatsapp_at IS NULL") if marked \
            else ("no_whatsapp_at=NULL", "no_whatsapp_at IS NOT NULL")
        return conn.execute(
            f"UPDATE wa_leads SET {marks[0]} WHERE {marks[1]} AND moved_to = '' AND removed_at IS NULL "
            f"AND id IN ({','.join('?' * len(ids))})", ids,
        ).rowcount


def remove_wa_leads(wa_lead_ids, owner_id=None) -> int:
    """
    Take leads off WhatsApp by hand -- a wrong import, a clinic you've decided
    against. Unlike "not on WhatsApp", this rules nothing out: adding the lead
    again brings it back, message history and all.
    """
    with get_db() as conn:
        ids = _own_wa_lead_ids(conn, wa_lead_ids, owner_id)
        if not ids:
            return 0
        return conn.execute(
            f"UPDATE wa_leads SET removed_at=datetime('now') "
            f"WHERE removed_at IS NULL AND id IN ({','.join('?' * len(ids))})", ids,
        ).rowcount


def get_wa_countries_used(owner_id=None) -> list:
    """Countries this operator already works in, for the top of the picker."""
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        rows = conn.execute("""
            SELECT country, COUNT(*) AS n FROM (
                SELECT w.country FROM wa_leads w JOIN businesses b ON b.id = w.business_id
                 WHERE b.owner_id = ? AND w.country != ''
                UNION ALL
                SELECT country FROM wa_campaigns WHERE owner_id = ? AND country != ''
            ) GROUP BY country ORDER BY n DESC
        """, (owner, owner)).fetchall()
    return [r["country"] for r in rows if r["country"] in WA_COUNTRY_CODES]


# ── Contacts: every business, on any channel or none ─────────────────────────
#
# Contacts is the one list that holds every business an operator has, with
# each channel's state alongside. The channel pages each show only their own
# leads; this is where a business on none of them can still be found, and
# sent somewhere.

# Why a business isn't on any channel, most specific first. Shown in the
# Unassigned view so a stray lead explains itself.
UNASSIGNED_REASONS = {
    "not_on_whatsapp":  "Not on WhatsApp",
    "removed_whatsapp": "Taken off WhatsApp",
    "removed_calling":  "Taken off Calling",
    "removed_email":    "Email address deleted",
    "no_email_found":   "Email scrape, no email found",
    "no_phone":         "Scraped with no phone number",
    "added_by_hand":    "Added by hand",
    "scraped":          "Scraped, never assigned",
}

_BUSINESS_VIEWS = ("all", "unassigned", "dnc")
_BUSINESS_CHANNELS = ("email", "calling", "whatsapp")


def _business_rows_sql() -> tuple:
    """
    One row per business with its channel state flattened in, as a subquery
    the list, the count and the id lookup all filter the same way.
    """
    stage_sql, stage_params = _wa_stage_sql()
    sql = f"""
        SELECT b.id, b.name AS company, b.phone, b.website, b.domain, b.address, b.city,
               b.country, b.category, b.rating, b.review_count, b.web_status,
               b.source_job_id, b.do_not_contact, b.notes, b.created_at,
               b.pipeline_stage, b.pipeline_channel, b.pipeline_at, b.next_action_at,
               (SELECT s.label FROM pipeline_stages s WHERE s.key = b.pipeline_stage) AS pipeline_label,
               (SELECT COUNT(*) FROM email_leads e
                 WHERE e.business_id = b.id AND e.status != 'deleted')          AS email_count,
               (SELECT e.email FROM email_leads e
                 WHERE e.business_id = b.id AND e.status != 'deleted'
                 ORDER BY e.duplicate_of IS NOT NULL, e.status != 'active', e.id LIMIT 1) AS email,
               (SELECT e.status FROM email_leads e
                 WHERE e.business_id = b.id AND e.status != 'deleted'
                 ORDER BY e.duplicate_of IS NOT NULL, e.status != 'active', e.id LIMIT 1) AS email_status,
               (SELECT en.status FROM enrollments en JOIN email_leads e ON e.id = en.email_lead_id
                 WHERE e.business_id = b.id ORDER BY en.enrolled_at DESC LIMIT 1)   AS email_enrollment,
               (SELECT c2.name FROM enrollments en JOIN email_leads e ON e.id = en.email_lead_id
                  JOIN campaigns c2 ON c2.id = en.campaign_id
                 WHERE e.business_id = b.id ORDER BY en.enrolled_at DESC LIMIT 1)   AS email_campaign,
               cl.id AS call_lead_id, cl.call_status, cl.next_call_at,
               w.id AS wa_lead_id, w.wa_campaign_id, c.name AS wa_campaign,
               CASE WHEN w.id IS NULL THEN NULL ELSE {stage_sql} END              AS wa_stage,
               j.destination AS source_destination,
               CASE
                 WHEN w.id IS NOT NULL AND w.moved_to != '' THEN 'not_on_whatsapp'
                 WHEN w.id IS NOT NULL AND w.removed_at IS NOT NULL THEN 'removed_whatsapp'
                 WHEN EXISTS (SELECT 1 FROM call_leads r WHERE r.business_id = b.id
                               AND r.removed_at IS NOT NULL) THEN 'removed_calling'
                 WHEN EXISTS (SELECT 1 FROM email_leads r WHERE r.business_id = b.id
                               AND r.status = 'deleted') THEN 'removed_email'
                 WHEN j.destination = 'email' THEN 'no_email_found'
                 WHEN j.destination IN ('calling','whatsapp') THEN 'no_phone'
                 WHEN b.source_job_id IS NULL THEN 'added_by_hand'
                 ELSE 'scraped'
               END AS unassigned_reason
          FROM businesses b
          LEFT JOIN call_leads cl   ON cl.business_id = b.id AND cl.removed_at IS NULL
          LEFT JOIN wa_leads w      ON w.business_id = b.id
          LEFT JOIN wa_campaigns c  ON c.id = w.wa_campaign_id
          LEFT JOIN scrape_jobs j   ON j.id = b.source_job_id AND j.owner_id = b.owner_id
         WHERE b.owner_id = ?
    """
    return sql, stage_params


def _business_filter(view="all", channel="", q="", source_job_id=None) -> tuple:
    on_email = "email_count > 0"
    on_calling = "call_lead_id IS NOT NULL"
    on_whatsapp = "wa_stage IS NOT NULL AND wa_stage NOT IN ('moved','removed')"
    where, params = [], []
    if view == "unassigned":
        where.append(f"do_not_contact = 0 AND NOT ({on_email}) AND NOT ({on_calling}) "
                     f"AND NOT ({on_whatsapp})")
    elif view == "dnc":
        where.append("do_not_contact = 1")
    if channel == "email":
        where.append(on_email)
    elif channel == "calling":
        where.append(on_calling)
    elif channel == "whatsapp":
        where.append(on_whatsapp)
    q = (q or "").strip()
    if q:
        where.append("(company LIKE ? OR phone LIKE ? OR website LIKE ? OR address LIKE ? "
                     "OR city LIKE ? OR category LIKE ? OR email LIKE ?)")
        params.extend([f"%{q}%"] * 7)
    if source_job_id not in (None, ""):
        if str(source_job_id) == SOURCE_MANUAL:
            where.append("source_job_id IS NULL")
        else:
            where.append("source_job_id = ?")
            params.append(int(source_job_id))
    return (" AND ".join(where) or "1=1"), params


_BUSINESS_SORT = {
    "company": "company", "phone": "phone", "email": "email", "city": "city",
    "category": "category", "rating": "rating", "created_at": "created_at",
}


def get_businesses_page(page=1, per_page=50, view="all", channel="", q="", source_job_id=None,
                        sort_col="", sort_dir="desc", owner_id=None) -> dict:
    """One page of Contacts, with counts for each view's tab."""
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 50), 500))
    view = view if view in _BUSINESS_VIEWS else "all"
    channel = channel if channel in _BUSINESS_CHANNELS else ""
    inner, inner_params = _business_rows_sql()
    owner = _owner_or_default(owner_id)
    where, params = _business_filter(view, channel, q, source_job_id)

    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    col = _BUSINESS_SORT.get(sort_col)
    order = (f"NULLIF({col}, '') IS NULL, {col} {direction}" if col
             else "created_at DESC, id DESC")

    base = [*inner_params, owner]
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({inner}) WHERE {where}",
                             (*base, *params)).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM ({inner}) WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*base, *params, per_page, (page - 1) * per_page),
        ).fetchall()]
        counts = {}
        for v in _BUSINESS_VIEWS:
            w, p = _business_filter(v)
            counts[v] = conn.execute(f"SELECT COUNT(*) FROM ({inner}) WHERE {w}",
                                     (*base, *p)).fetchone()[0]
    for r in rows:
        r["unassigned_label"] = UNASSIGNED_REASONS.get(r["unassigned_reason"], "")
    return {"rows": rows, "total": total, "page": page, "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page), "counts": counts}


def get_business_ids_matching(view="all", channel="", q="", source_job_id=None, owner_id=None) -> list:
    """Every id matching a Contacts filter, for "select all N matching"."""
    inner, inner_params = _business_rows_sql()
    where, params = _business_filter(view if view in _BUSINESS_VIEWS else "all",
                                     channel if channel in _BUSINESS_CHANNELS else "",
                                     q, source_job_id)
    with get_db() as conn:
        return [r["id"] for r in conn.execute(
            f"SELECT id FROM ({inner}) WHERE {where}",
            (*inner_params, _resolve_owner_id(conn, owner_id), *params),
        ).fetchall()]


def get_business_detail(business_id: int, owner_id=None):
    """
    One business with everything that's happened to it, on every channel, as
    one timeline. None if it isn't this operator's.
    """
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        b = conn.execute("SELECT * FROM businesses WHERE id=? AND owner_id=?",
                         (int(business_id), owner)).fetchone()
        if not b:
            return None
        out = dict(b)
        out["company"] = out["name"]
        out["emails"] = [dict(r) for r in conn.execute("""
            SELECT id, email, first_name, last_name, status, mx_valid, duplicate_of
              FROM email_leads WHERE business_id=? AND status != 'deleted'
             ORDER BY duplicate_of IS NOT NULL, id
        """, (business_id,))]
        out["enrollments"] = [dict(r) for r in conn.execute("""
            SELECT en.id, en.status, en.current_step, en.next_send_at, c.id AS campaign_id,
                   c.name AS campaign, e.email
              FROM enrollments en JOIN email_leads e ON e.id = en.email_lead_id
              JOIN campaigns c ON c.id = en.campaign_id
             WHERE e.business_id = ? ORDER BY en.enrolled_at DESC
        """, (business_id,))]
        cl = conn.execute("SELECT * FROM call_leads WHERE business_id=?", (business_id,)).fetchone()
        out["call"] = dict(cl) if cl else None
        if cl:
            out["call"]["campaigns"] = [dict(r) for r in conn.execute("""
                SELECT c.id, c.name FROM call_campaign_members m
                  JOIN call_campaigns c ON c.id = m.call_campaign_id WHERE m.call_lead_id=?
            """, (cl["id"],))]
        wa = conn.execute("SELECT id FROM wa_leads WHERE business_id=?", (business_id,)).fetchone()
        out["whatsapp"] = None

        timeline = []
        for r in conn.execute("""
            SELECT s.sent_at AS at, s.subject, s.step_num, c.name AS campaign, e.email
              FROM sends s JOIN email_leads e ON e.id = s.email_lead_id
              LEFT JOIN campaigns c ON c.id = s.campaign_id
             WHERE e.business_id = ?
        """, (business_id,)):
            timeline.append({"at": r["at"], "channel": "email",
                             "text": f"Email step {r['step_num']} sent to {r['email']}"
                                     + (f" ({r['campaign']})" if r["campaign"] else ""),
                             "detail": r["subject"] or ""})
        labels = {k: v["label"] for k, v in get_call_outcomes(include_archived=True,
                                                              owner_id=owner).items()}
        for r in conn.execute("""
            SELECT l.called_at AS at, l.outcome, l.notes FROM call_log l
              JOIN call_leads cl ON cl.id = l.call_lead_id WHERE cl.business_id = ?
        """, (business_id,)):
            timeline.append({"at": r["at"], "channel": "calling",
                             "text": f"Called: {labels.get(r['outcome'], r['outcome'])}",
                             "detail": r["notes"] or ""})
        for r in conn.execute("""
            SELECT l.sent_at AS at, l.kind, l.message FROM wa_log l
              JOIN wa_leads w ON w.id = l.wa_lead_id WHERE w.business_id = ?
        """, (business_id,)):
            timeline.append({"at": r["at"], "channel": "whatsapp",
                             "text": "WhatsApp follow-up opened" if r["kind"] == "followup"
                                     else "WhatsApp message opened",
                             "detail": r["message"] or ""})
    timeline.sort(key=lambda t: t["at"] or "", reverse=True)
    out["timeline"] = timeline
    if wa:
        lead = get_wa_lead(wa["id"], with_message=True)
        stage_sql, stage_params = _wa_stage_sql()
        with get_db() as conn:
            lead["stage"] = conn.execute(
                f"SELECT {stage_sql} FROM wa_leads w LEFT JOIN wa_campaigns c "
                f"ON c.id = w.wa_campaign_id WHERE w.id = ?", (*stage_params, wa["id"])
            ).fetchone()[0]
        out["whatsapp"] = lead
    try:
        out["audit"] = json.loads(out.get("audit") or "null")
    except (TypeError, ValueError):
        out["audit"] = None
    out["rating_context"] = get_rating_context(business_id, owner_id=owner)
    return out


# ── Audit ─────────────────────────────────────────────────────────────────────

def save_business_audit(business_id: int, results_json: str):
    with get_db() as conn:
        conn.execute("UPDATE businesses SET audit=?, audit_at=datetime('now') WHERE id=?",
                     (results_json, int(business_id)))


def get_rating_context(business_id: int, owner_id=None):
    """
    How a business's Google rating and review count compare with its peers:
    the other businesses from the same scrape, or failing that, the same
    category in the same city. Worked out from data already stored -- no
    lookups -- so it's there before anyone runs a check.
    """
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        b = conn.execute("SELECT * FROM businesses WHERE id=? AND owner_id=?",
                         (int(business_id), owner)).fetchone()
        if not b or b["rating"] is None:
            return None
        if b["source_job_id"]:
            where, params, scope = "source_job_id = ?", [b["source_job_id"]], "from the same scrape"
        elif b["category"] and b["city"]:
            where, params, scope = "category = ? AND city = ?", [b["category"], b["city"]], \
                f"{b['category']} in {b['city']}"
        else:
            return None
        peers = conn.execute(f"""
            SELECT rating, COALESCE(review_count, 0) AS reviews FROM businesses
             WHERE owner_id = ? AND id != ? AND rating IS NOT NULL AND {where}
        """, (owner, int(business_id), *params)).fetchall()
    if len(peers) < 3:
        return None
    reviews = b["review_count"] or 0
    return {
        "rating": b["rating"], "reviews": reviews, "scope": scope, "peers": len(peers),
        "avg_rating": round(sum(p["rating"] for p in peers) / len(peers), 1),
        "avg_reviews": round(sum(p["reviews"] for p in peers) / len(peers)),
        "rank_by_reviews": 1 + sum(1 for p in peers if p["reviews"] > reviews),
    }


def _audit_links_key(owner: int) -> str:
    return f"audit_links:{int(owner)}"


def get_audit_links(owner_id=None) -> list:
    """This operator's own audit links: [{label, url}], with {domain}-style
    fill-ins. Per operator -- they're part of how each person works."""
    owner = _owner_or_default(owner_id)
    raw = get_settings().get(_audit_links_key(owner)) or "[]"
    try:
        links = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [l for l in links if isinstance(l, dict) and l.get("url")]


def save_audit_links(links, owner_id=None) -> list:
    owner = _owner_or_default(owner_id)
    clean = []
    for link in links or []:
        if not isinstance(link, dict):
            continue
        url = str(link.get("url") or "").strip()
        label = str(link.get("label") or "").strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"Links have to start with https:// ({label or url})")
        clean.append({"label": (label or url)[:60], "url": url[:500]})
    save_settings({_audit_links_key(owner): json.dumps(clean[:40])})
    return clean


_BUSINESS_EDITABLE = ("name", "phone", "website", "address", "city", "category", "notes")


def update_business(business_id: int, fields: dict, owner_id=None):
    """
    Edit a business's own details. Returns (ok, error).

    A new phone number is re-formatted for WhatsApp against the lead's own
    country, so the wa.me link follows the edit instead of dialling the old
    number. Opting out can be set here; clearing it is refused once an address
    has unsubscribed, because that opt-out came from the prospect, not from a
    misclick.
    """
    updates = {k: str(fields[k] or "").strip() for k in _BUSINESS_EDITABLE if k in fields}
    if "name" in updates and not updates["name"]:
        return False, "A business needs a name"
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        if not conn.execute("SELECT 1 FROM businesses WHERE id=? AND owner_id=?",
                            (int(business_id), owner)).fetchone():
            return False, "Not found"
        if "phone" in updates:
            updates["phone_normalized"] = normalize_phone(updates["phone"])
        if "website" in updates:
            updates["domain"] = canonical_domain(updates["website"])
        if "do_not_contact" in fields:
            want = 1 if fields["do_not_contact"] else 0
            if want == 0 and conn.execute(
                "SELECT 1 FROM email_leads WHERE business_id=? AND status='unsubscribed'",
                (int(business_id),)
            ).fetchone():
                return False, ("They unsubscribed from your emails, so they stay marked "
                               "do-not-contact")
            updates["do_not_contact"] = want
        if updates:
            conn.execute(f"UPDATE businesses SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?",
                         (*updates.values(), int(business_id)))
        if "phone" in updates:
            wa = conn.execute("SELECT id, country FROM wa_leads WHERE business_id=?",
                              (int(business_id),)).fetchone()
            if wa:
                phone = updates["phone"]
                conn.execute("UPDATE wa_leads SET wa_number=?, number_type=? WHERE id=?", (
                    format_whatsapp_number(phone, wa["country"]) if phone else "",
                    classify_number_type(phone, wa["country"]) if phone else "unknown",
                    wa["id"]))
    return True, None


def delete_businesses(business_ids, owner_id=None) -> dict:
    """
    Delete businesses and everything on every channel with them. Returns
    {"deleted": n, "kept": n}.

    Anyone who opted out, unsubscribed or bounced is kept: their row is the
    only thing stopping a later scrape or import from putting them straight
    back on a list. Deleting them would quietly undo a "stop contacting me".
    """
    with get_db() as conn:
        ids = _own_business_ids(conn, business_ids, owner_id)
        if not ids:
            return {"deleted": 0, "kept": 0}
        ph = ",".join("?" * len(ids))
        protected = {r["id"] for r in conn.execute(f"""
            SELECT b.id FROM businesses b
             WHERE b.id IN ({ph}) AND (b.do_not_contact = 1 OR EXISTS (
                   SELECT 1 FROM email_leads e WHERE e.business_id = b.id
                      AND e.status IN ('unsubscribed','bounced')))
        """, ids)}
        doomed = [i for i in ids if i not in protected]
        if doomed:
            dph = ",".join("?" * len(doomed))
            # enrollments and call_campaign_members hang off the channel rows
            # and cascade with them; sends has no foreign key and keeps its
            # history for campaign totals.
            conn.execute(f"DELETE FROM businesses WHERE id IN ({dph})", doomed)
        return {"deleted": len(doomed), "kept": len(protected)}


def create_business(fields: dict, owner_id=None) -> tuple:
    """
    Add one business by hand, matched against existing ones the same way an
    import is. Returns (business_id, created). An email, if given, becomes an
    address on Email; nothing is put on Calling or WhatsApp until the operator
    says so.
    """
    name = (fields.get("name") or fields.get("company") or "").strip()
    if not name:
        raise ValueError("A business needs a name")
    row = {"company": name}
    for k in ("phone", "website", "address", "city", "category", "email"):
        if fields.get(k):
            row[k] = str(fields[k]).strip()
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        existing = find_existing_business(conn, email=row.get("email", ""), phone=row.get("phone", ""),
                                          website=row.get("website", ""), company=name,
                                          address=row.get("address", ""), owner_id=owner)
    _accepted, ids = upsert_businesses([row], owner_id=owner)
    if not ids:
        raise ValueError("Nothing to add")
    return ids[0], existing is None


def enroll_businesses(campaign_id: int, business_ids, owner_id=None) -> dict:
    """
    Enroll businesses into an email campaign by their best address. Businesses
    with no active address are counted, since there's nothing to send to.
    """
    with get_db() as conn:
        owner = _resolve_owner_id(conn, owner_id)
        ids = _own_business_ids(conn, business_ids, owner)
        lead_ids, no_email = [], 0
        for bid in ids:
            row = conn.execute("""
                SELECT id FROM email_leads WHERE business_id=? AND status='active' AND owner_id=?
                 ORDER BY duplicate_of IS NOT NULL, id LIMIT 1
            """, (bid, owner)).fetchone()
            if row:
                lead_ids.append(row["id"])
            else:
                no_email += 1
    enrolled, skipped = enroll_contacts_bulk(campaign_id, lead_ids, owner_id=owner) if lead_ids \
        else (0, {"other_campaign": 0, "duplicate_address": 0, "same_domain": 0, "do_not_contact": 0})
    return {"enrolled": enrolled, "skipped": skipped, "no_email": no_email}


# ── Dashboard ─────────────────────────────────────────────────────────────────

def get_dashboard(owner_id=None, since=None) -> dict:
    """
    Every channel's headline numbers, what's waiting on the operator today,
    and all their campaigns in one list. `since` is the start of the
    operator's own day, for "sent today".
    """
    owner = _owner_or_default(owner_id)
    email = get_stats(owner_id=owner)
    calling = get_call_summary(owner_id=owner)
    whatsapp = get_wa_summary(owner_id=owner, since=since)

    campaigns = []
    with get_db() as conn:
        for c in conn.execute("SELECT id, name, status FROM campaigns WHERE owner_id=? "
                              "ORDER BY created_at DESC", (owner,)).fetchall():
            s = get_stats(c["id"])
            done = s["completed"] + s["replied"] + s["bounced"]
            campaigns.append({
                "channel": "email", "id": c["id"], "name": c["name"], "status": c["status"],
                "leads": s["total"], "progress": round(done / s["total"] * 100) if s["total"] else 0,
                "result": f"{s['replied']} replied", "reply_rate": s["reply_rate"],
            })
    for c in get_call_campaigns(owner_id=owner):
        campaigns.append({
            "channel": "calling", "id": c["id"], "name": c["name"], "status": c["status"],
            "leads": c["total"],
            "progress": round(c["closed"] / c["total"] * 100) if c["total"] else 0,
            "result": f"{c['booked']} booked", "reply_rate": None,
        })
    for c in get_wa_campaigns(owner_id=owner):
        campaigns.append({
            "channel": "whatsapp", "id": c["id"], "name": c["name"], "status": c["status"],
            "leads": c["leads"],
            "progress": round(c["messaged"] / c["leads"] * 100) if c["leads"] else 0,
            "result": f"{c['replied']} replied", "reply_rate": c["reply_rate"],
        })

    active_email = sum(1 for c in campaigns if c["channel"] == "email" and c["status"] == "active")
    return {
        "email": {**email, "active_campaigns": active_email},
        "calling": calling,
        "whatsapp": whatsapp,
        "todo": {
            "wa_ready": whatsapp["ready_to_send"],
            "wa_due": whatsapp["due"],
            "wa_sent_today": whatsapp["sent_today"],
            "calls_due": calling["due"],
            "calls_new": calling["uncalled"],
        },
        "campaigns": campaigns,
    }


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats(campaign_id=None, owner_id=None):
    with get_db() as conn:
        # Enrollments and sends have no owner of their own -- they belong to a
        # campaign, and the campaign has one. Without this the dashboard would
        # add both operators' numbers together and report neither's.
        if campaign_id:
            scope, args = "WHERE campaign_id=?", (campaign_id,)
        else:
            scope = "WHERE campaign_id IN (SELECT id FROM campaigns WHERE owner_id=?)"
            args = (_resolve_owner_id(conn, owner_id),)

        def cnt(table, cond=""):
            return conn.execute(f"SELECT COUNT(*) FROM {table} {scope} {cond}", args).fetchone()[0]

        total     = cnt("enrollments")
        sent      = cnt("sends")
        replied   = cnt("enrollments", "AND status='replied'")
        bounced   = cnt("enrollments", "AND status='bounced'")
        completed = cnt("enrollments", "AND status='completed'")
        queued    = cnt("enrollments", "AND status='queued'")
        sent_to   = conn.execute(
            f"SELECT COUNT(DISTINCT email_lead_id) FROM sends {scope}", args
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
        # The founding account inherits anything already in the database. Rows
        # can predate it -- init_db builds the schema long before anyone signs
        # up -- and without this they would belong to owner 0 forever, visible
        # to nobody. Only when this is the only account: once a second exists,
        # unassigned rows are ambiguous and must not be handed to whoever
        # happened to register next.
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1:
            _backfill_owner_ids(conn)
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


def owned_row_counts(uid: int) -> dict:
    """What this user still owns, by table. Empty dict means nothing."""
    counts = {}
    with get_db() as conn:
        for table, label in (("businesses", "leads"), ("campaigns", "campaigns"),
                             ("call_campaigns", "call campaigns"),
                             ("wa_campaigns", "WhatsApp campaigns"),
                             ("scrape_jobs", "scrape jobs")):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE owner_id=?", (uid,)
            ).fetchone()[0]
            if n:
                counts[label] = n
    return counts


def delete_user(uid: int) -> tuple:
    """
    Remove a user, but never their work. Returns (ok, error).

    Deleting an operator who still owns leads would either orphan those rows
    where nobody can reach them, or silently hand someone else's outreach --
    replies, call outcomes, unsubscribes -- to whoever looks next. Both are
    worse than refusing, so the rows have to be dealt with first.
    """
    owned = owned_row_counts(uid)
    if owned:
        detail = ", ".join(f"{n} {label}" for label, n in owned.items())
        return False, (f"This user still owns {detail}. Reassign or delete "
                       f"that data first - deleting the account would leave "
                       f"it unreachable.")
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
    return True, None


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
