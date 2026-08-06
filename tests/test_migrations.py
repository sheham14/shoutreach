"""Schema migration regression tests.

Run:  python tests/test_migrations.py     (exits 0 on pass, 1 on failure)

Covers the three paths through init_db(). The legacy-rebuild case is the one
that regressed as Fable Audit finding 2.1 -- contacts_new omitted mx_valid, so
a fresh database silently lost the column and the first contact import crashed
with "table contacts has no column named mx_valid".

If you add a column to contacts, add it in BOTH the ALTER loop and the
contacts_new rebuild in db.py, then extend EXPECTED_COLUMNS below.
"""
import importlib
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXPECTED_COLUMNS = {
    "id", "email", "first_name", "last_name", "company", "extra", "status",
    "created_at", "website", "address", "soft_bounce_count", "mx_valid",
    "domain", "duplicate_of",
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


def columns(path, table="contacts"):
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def email_is_nullable(path):
    conn = sqlite3.connect(path)
    try:
        row = next(r for r in conn.execute("PRAGMA table_info(contacts)") if r[1] == "email")
        return not row[3]  # row[3] is the notnull flag
    finally:
        conn.close()


def scalar(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_fresh_database(work):
    """A brand-new DB must be fully formed after a single init_db()."""
    print("\n1. FRESH DATABASE")
    path = os.path.join(work, "fresh.db")
    db = load_db(path)
    db.init_db()

    check("all expected columns present",
          EXPECTED_COLUMNS.issubset(set(columns(path))),
          f"missing={EXPECTED_COLUMNS - set(columns(path))}")
    check("email is nullable", email_is_nullable(path))

    # The original bug: this raised OperationalError on the first call.
    try:
        db.upsert_contacts([{"email": "x@gmail.com", "mx_valid": 1, "company": "Acme"}])
        check("upsert_contacts works on first run", True)
    except Exception as exc:
        check("upsert_contacts works on first run", False, f"{type(exc).__name__}: {exc}")

    # Uniqueness now comes from the partial index, not a table constraint.
    db.upsert_contacts([{"email": "x@gmail.com", "company": "Acme Renamed"}])
    check("partial unique index dedupes by email",
          scalar(path, "SELECT COUNT(*) FROM contacts WHERE email='x@gmail.com'") == 1)

    # ...which must still allow many NULL-email prospect rows.
    db.upsert_contacts([{"website": "http://a.ca", "status": "no_email"},
                        {"website": "http://b.ca", "status": "no_email"}])
    check("NULL emails may repeat",
          scalar(path, "SELECT COUNT(*) FROM contacts WHERE email IS NULL") == 2)


def test_legacy_rebuild(work):
    """A pre-nullable DB must be rebuilt without dropping columns or rows."""
    print("\n2. LEGACY DATABASE (email NOT NULL -- rebuild fires)")
    path = os.path.join(work, "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            extra TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO contacts(email, company) VALUES('legacy@x.ca', 'Legacy Co');
    """)
    conn.commit()
    conn.close()

    db = load_db(path)
    db.init_db()

    check("mx_valid survived the rebuild", "mx_valid" in columns(path), f"cols={columns(path)}")
    check("email became nullable", email_is_nullable(path))

    conn = sqlite3.connect(path)
    row = conn.execute("SELECT email, company FROM contacts").fetchone()
    conn.close()
    check("existing row preserved", row == ("legacy@x.ca", "Legacy Co"), f"row={row}")

    try:
        db.upsert_contacts([{"email": "new@x.ca", "mx_valid": 0}])
        check("upsert works after migration", True)
    except Exception as exc:
        check("upsert works after migration", False, f"{type(exc).__name__}: {exc}")

    return path


def test_idempotent(path):
    """init_db() runs on every process start -- reruns must change nothing."""
    print("\n3. RERUN ON A MIGRATED DATABASE (idempotency)")
    db = load_db(path)
    before_rows = scalar(path, "SELECT COUNT(*) FROM contacts")
    before_cols = columns(path)
    db.init_db()
    db.init_db()
    check("no rows lost across reruns",
          scalar(path, "SELECT COUNT(*) FROM contacts") == before_rows,
          f"{before_rows} -> {scalar(path, 'SELECT COUNT(*) FROM contacts')}")
    check("no columns lost across reruns", columns(path) == before_cols)


def main():
    work = tempfile.mkdtemp(prefix="shoutreach_mig_")
    try:
        test_fresh_database(work)
        legacy_path = test_legacy_rebuild(work)
        test_idempotent(legacy_path)
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
