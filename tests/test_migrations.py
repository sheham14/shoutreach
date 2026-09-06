"""Schema migration regression tests.

Run:  python tests/test_migrations.py     (exits 0 on pass, 1 on failure)

Covers the paths through init_db() after the contacts -> businesses split:
a brand-new database, a pre-split production database being migrated for the
first time, a re-run on an already-migrated database (must be a no-op), and
the refuse-to-double-migrate guard.

The original version of this file guarded a narrower bug (Fable Audit 2.1: a
legacy-rebuild block that omitted mx_valid and silently dropped the column).
That specific rebuild path is gone -- the split below now does that job for
every pre-split schema shape, old or new, since it reads rows with `.get()`
rather than assuming which columns exist. If you add a column to businesses,
email_leads, call_leads or wa_leads, add it to the CREATE TABLE in db.py and
extend the matching EXPECTED_* set below.
"""
import importlib
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXPECTED_BUSINESS_COLUMNS = {
    "id", "name", "phone", "phone_normalized", "website", "domain", "address",
    "city", "country", "category", "rating", "review_count", "web_status",
    "source_job_id", "extra", "do_not_contact", "notes", "created_at",
}
EXPECTED_EMAIL_LEAD_COLUMNS = {
    "id", "business_id", "email", "first_name", "last_name", "status",
    "mx_valid", "soft_bounce_count", "duplicate_of", "created_at",
}

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def load_db(path):
    """Point db.py at `path` and reload it so DB_PATH is re-read."""
    os.environ["DB_PATH"] = path
    import db
    importlib.reload(db)
    return db


def columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def table_exists(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None
    finally:
        conn.close()


def scalar(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def rows(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_fresh_database(work):
    """A brand-new DB must be fully formed after a single init_db()."""
    print("\n1. FRESH DATABASE")
    path = os.path.join(work, "fresh.db")
    db = load_db(path)
    db.init_db()

    check("contacts table does not exist on a fresh install",
          not table_exists(path, "contacts"))
    check("businesses has every expected column",
          EXPECTED_BUSINESS_COLUMNS.issubset(set(columns(path, "businesses"))),
          f"missing={EXPECTED_BUSINESS_COLUMNS - set(columns(path, 'businesses'))}")
    check("email_leads has every expected column",
          EXPECTED_EMAIL_LEAD_COLUMNS.issubset(set(columns(path, "email_leads"))),
          f"missing={EXPECTED_EMAIL_LEAD_COLUMNS - set(columns(path, 'email_leads'))}")
    for t in ("call_leads", "wa_leads", "wa_log"):
        check(f"{t} table exists", table_exists(path, t))

    # The original bug this file guarded: a fresh DB whose first import crashes
    # because a column referenced by the insert doesn't actually exist yet.
    try:
        db.upsert_businesses([{"email": "x@gmail.com", "mx_valid": 1, "company": "Acme"}])
        check("upsert_businesses works on first run", True)
    except Exception as exc:
        check("upsert_businesses works on first run", False, f"{type(exc).__name__}: {exc}")

    # Uniqueness comes from the partial index on email_leads, not a table
    # constraint -- re-importing the same address must update, not duplicate.
    db.upsert_businesses([{"email": "x@gmail.com", "company": "Acme Renamed"}])
    check("partial unique index dedupes by email",
          scalar(path, "SELECT COUNT(*) FROM email_leads WHERE email='x@gmail.com'") == 1)
    check("company on an existing email is not overwritten by a later import",
          scalar(path, """SELECT b.name FROM businesses b
                           JOIN email_leads el ON el.business_id=b.id
                           WHERE el.email='x@gmail.com'""") == "Acme")

    # No-email prospects must still land as businesses, one per distinct
    # identity, and must never collide with each other just for lacking email.
    db.upsert_businesses([{"website": "http://a.ca", "company": "A Co", "status": "no_email"},
                          {"website": "http://b.ca", "company": "B Co", "status": "no_email"}])
    check("distinct no-email prospects both stored",
          scalar(path, "SELECT COUNT(*) FROM businesses WHERE domain IN ('a.ca','b.ca')") == 2)
    check("no-email prospects have no email_leads row",
          scalar(path, """SELECT COUNT(*) FROM email_leads el
                           JOIN businesses b ON b.id=el.business_id
                           WHERE b.domain IN ('a.ca','b.ca')""") == 0)


def _create_legacy_contacts(path):
    """
    The single-table shape every production database has right now: contacts
    holds identity, address and calling state all on one row, with no
    businesses table in sight. This is what init_db() must split correctly.
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT DEFAULT NULL,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            extra TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            website TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            soft_bounce_count INTEGER NOT NULL DEFAULT 0,
            mx_valid INTEGER DEFAULT NULL,
            domain TEXT NOT NULL DEFAULT '',
            duplicate_of INTEGER DEFAULT NULL,
            phone TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            rating REAL DEFAULT NULL,
            review_count INTEGER DEFAULT NULL,
            source_job_id INTEGER DEFAULT NULL,
            phone_normalized TEXT NOT NULL DEFAULT '',
            call_status TEXT NOT NULL DEFAULT '',
            next_call_at TEXT DEFAULT NULL,
            call_attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', daily_limit INTEGER NOT NULL DEFAULT 30,
            send_start_hour INTEGER NOT NULL DEFAULT 9, send_end_hour INTEGER NOT NULL DEFAULT 17,
            min_delay_secs INTEGER NOT NULL DEFAULT 45, max_delay_secs INTEGER NOT NULL DEFAULT 120,
            bounce_pause_pct REAL NOT NULL DEFAULT 5.0, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE enrollments (id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER NOT NULL,
            contact_id INTEGER NOT NULL, current_step INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'queued', next_send_at TEXT,
            enrolled_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(campaign_id, contact_id));
        CREATE TABLE sends (id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER,
            contact_id INTEGER, step_num INTEGER, subject TEXT, msg_id TEXT,
            status TEXT NOT NULL DEFAULT 'sent', sent_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE call_log (id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL,
            outcome TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', next_call_at TEXT,
            called_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE call_campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE call_campaign_members (call_campaign_id INTEGER NOT NULL, contact_id INTEGER NOT NULL,
            added_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY (call_campaign_id, contact_id));

        -- One clinic, two addresses (a billing inbox and the owner's), sharing
        -- a website: the migration must collapse these to ONE business.
        INSERT INTO contacts(email,company,website,domain,phone,phone_normalized,status,rating)
            VALUES('info@paradisedental.ca','Paradise Dental','https://paradisedental.ca',
                   'paradisedental.ca','709-555-0123','7095550123','active',4.8);
        INSERT INTO contacts(email,company,website,domain,status)
            VALUES('owner@paradisedental.ca','Paradise Dental','http://www.paradisedental.ca/',
                   'paradisedental.ca','active');

        -- A no-website, no-email prospect that has already been called --
        -- the case that must produce a business AND a call_leads row.
        INSERT INTO contacts(company,phone,phone_normalized,status,call_status,call_attempts)
            VALUES('Kenmount Physio','709-555-0456','7095550456','no_website','no_answer',2);

        INSERT INTO campaigns(name) VALUES('Q1');
        INSERT INTO enrollments(campaign_id,contact_id,current_step) VALUES(1,1,2);
        INSERT INTO sends(campaign_id,contact_id,step_num,subject) VALUES(1,1,1,'Hi');
        INSERT INTO call_log(contact_id,outcome,notes) VALUES(3,'no_answer','rang out');
        INSERT INTO call_campaigns(name) VALUES('StJohns');
        INSERT INTO call_campaign_members(call_campaign_id,contact_id) VALUES(1,3);
    """)
    conn.commit()
    conn.close()


def test_split_migration(work):
    """A pre-split production DB must become businesses + per-channel leads."""
    print("\n2. PRE-SPLIT DATABASE (contacts -> businesses + email_leads/call_leads)")
    path = os.path.join(work, "legacy.db")
    _create_legacy_contacts(path)

    db = load_db(path)
    db.init_db()

    check("contacts table is gone after the split", not table_exists(path, "contacts"))

    biz = rows(path, "SELECT id, name, domain, rating FROM businesses ORDER BY id")
    check("two businesses created (one merged, one prospect)", len(biz) == 2, f"got {biz}")

    paradise = next((b for b in biz if b[1] == "Paradise Dental"), None)
    check("Paradise Dental survived with its domain and rating",
          paradise is not None and paradise[2] == "paradisedental.ca" and paradise[3] == 4.8,
          f"row={paradise}")

    if paradise:
        addrs = rows(path, "SELECT email FROM email_leads WHERE business_id=? ORDER BY email",
                     (paradise[0],))
        check("both addresses attached to the one merged business",
              addrs == [("info@paradisedental.ca",), ("owner@paradisedental.ca",)],
              f"addrs={addrs}")

    kenmount = next((b for b in biz if b[1] == "Kenmount Physio"), None)
    check("Kenmount Physio (no email, already called) preserved", kenmount is not None)
    if kenmount:
        cl = rows(path, "SELECT call_status, call_attempts FROM call_leads WHERE business_id=?",
                  (kenmount[0],))
        check("its call history became a call_leads row",
              cl == [("no_answer", 2)], f"call_leads={cl}")
        hist = rows(path, """SELECT outcome, notes FROM call_log l
                              JOIN call_leads cl ON cl.id=l.call_lead_id
                              WHERE cl.business_id=?""", (kenmount[0],))
        check("call_log repointed to the new call_lead", hist == [("no_answer", "rang out")],
              f"history={hist}")
        member = scalar(path, """SELECT COUNT(*) FROM call_campaign_members m
                                  JOIN call_leads cl ON cl.id=m.call_lead_id
                                  WHERE cl.business_id=?""", (kenmount[0],))
        check("call campaign membership repointed", member == 1)

    enr = rows(path, """SELECT e.current_step, el.email FROM enrollments e
                         JOIN email_leads el ON el.id=e.email_lead_id""")
    check("enrollment repointed to the winning address", enr == [(2, "info@paradisedental.ca")],
          f"enr={enr}")
    sent = rows(path, """SELECT s.step_num, el.email FROM sends s
                          JOIN email_leads el ON el.id=s.email_lead_id""")
    check("send history repointed to the winning address",
          sent == [(1, "info@paradisedental.ca")], f"sent={sent}")

    try:
        db.upsert_businesses([{"email": "new@x.ca", "mx_valid": 0, "company": "New Co"}])
        check("upsert_businesses works right after migration", True)
    except Exception as exc:
        check("upsert_businesses works right after migration", False, f"{type(exc).__name__}: {exc}")

    return path


def test_idempotent(path):
    """init_db() runs on every process start -- reruns must change nothing."""
    print("\n3. RERUN ON AN ALREADY-MIGRATED DATABASE (idempotency)")
    db = load_db(path)
    before_biz  = scalar(path, "SELECT COUNT(*) FROM businesses")
    before_lead = scalar(path, "SELECT COUNT(*) FROM email_leads")
    before_cols = columns(path, "businesses")
    db.init_db()
    db.init_db()
    check("no businesses lost or duplicated across reruns",
          scalar(path, "SELECT COUNT(*) FROM businesses") == before_biz,
          f"{before_biz} -> {scalar(path, 'SELECT COUNT(*) FROM businesses')}")
    check("no email leads lost or duplicated across reruns",
          scalar(path, "SELECT COUNT(*) FROM email_leads") == before_lead,
          f"{before_lead} -> {scalar(path, 'SELECT COUNT(*) FROM email_leads')}")
    check("no columns lost across reruns", columns(path, "businesses") == before_cols)


def test_refuses_double_migration(work):
    """
    A contacts table left behind (e.g. an interrupted deploy) alongside an
    already-populated businesses table must not be migrated again -- that
    would double every business that survived the first split.
    """
    print("\n4. CONTACTS TABLE LEFT OVER ALONGSIDE A POPULATED businesses TABLE")
    path = os.path.join(work, "double.db")
    _create_legacy_contacts(path)
    db = load_db(path)
    db.init_db()
    before = scalar(path, "SELECT COUNT(*) FROM businesses")

    # Simulate an interrupted deploy: contacts reappears (e.g. restored from a
    # stale backup) while businesses already holds real, migrated data.
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY, email TEXT, company TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("INSERT INTO contacts(email, company) VALUES('stray@x.ca', 'Stray Co')")
    conn.commit()
    conn.close()

    db2 = load_db(path)
    db2.init_db()  # must not raise, and must not touch businesses
    check("businesses count unchanged when contacts reappears non-empty",
          scalar(path, "SELECT COUNT(*) FROM businesses") == before,
          f"{before} -> {scalar(path, 'SELECT COUNT(*) FROM businesses')}")
    check("the stray contacts table is left alone for the operator to inspect",
          table_exists(path, "contacts"))


def main():
    work = tempfile.mkdtemp(prefix="shoutreach_mig_")
    try:
        test_fresh_database(work)
        legacy_path = test_split_migration(work)
        test_idempotent(legacy_path)
        test_refuses_double_migration(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
