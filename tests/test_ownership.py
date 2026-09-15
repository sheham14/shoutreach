"""Per-operator data ownership.

Run:  python tests/test_ownership.py     (exits 0 on pass, 1 on failure)

Two people share this install. They share the sending accounts, the domain and
the daily cap -- but not their leads. Each operator's businesses are their own
rows, so both can work the same clinic without seeing the other's notes, call
outcomes or replies. Overlap is reported at import time and never merged.

The three things most worth guarding here, all of which were live bugs in the
first draft of this feature rather than hypotheticals:

  * The email uniqueness index used to be global. While it was, a second
    operator importing an address the first already held did not get their own
    row -- the ON CONFLICT clause in upsert_businesses silently rewrote the
    FIRST operator's row with the second one's data.

  * Unsubscribes and bounces resolved an address with `=(SELECT ...)`, a scalar
    subquery that quietly returns one row. Correct while addresses were
    globally unique; once each operator holds their own copy it would suppress
    one person's sending and leave the other mailing someone who had just
    opted out, over the same accounts and the same domain.

  * Ownership has no safe default once two accounts exist. A write that forgets
    to say who it belongs to must fail loudly rather than file one person's
    leads under the other.
"""
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def load_db(path):
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


def indexes(path):
    return {r[0] for r in rows(path, "SELECT name FROM sqlite_master WHERE type='index'")}


# ─────────────────────────────────────────────────────────────────────────────

def _create_pre_ownership_db(path):
    """
    The shape every database has before this feature: businesses and per-channel
    leads already split out, but nothing saying who any of it belongs to. Only
    the tables that gain owner_id are pre-created; init_db builds the rest, which
    also exercises the fresh-install half of the same change.
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '', phone_normalized TEXT NOT NULL DEFAULT '',
            website TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
            rating REAL DEFAULT NULL, review_count INTEGER DEFAULT NULL,
            web_status TEXT NOT NULL DEFAULT '', source_job_id INTEGER DEFAULT NULL,
            extra TEXT NOT NULL DEFAULT '{}', do_not_contact INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE email_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            email TEXT DEFAULT NULL, first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
            mx_valid INTEGER DEFAULT NULL, soft_bounce_count INTEGER NOT NULL DEFAULT 0,
            duplicate_of INTEGER DEFAULT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', daily_limit INTEGER NOT NULL DEFAULT 30,
            send_start_hour INTEGER NOT NULL DEFAULT 9, send_end_hour INTEGER NOT NULL DEFAULT 17,
            min_delay_secs INTEGER NOT NULL DEFAULT 45, max_delay_secs INTEGER NOT NULL DEFAULT 120,
            bounce_pause_pct REAL NOT NULL DEFAULT 5.0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE UNIQUE INDEX email_leads_email_unique
            ON email_leads(email) WHERE email IS NOT NULL AND email != '';

        -- A non-admin created first, so "backfill to user 1" and "backfill to
        -- the founding admin" give different answers and the test can tell
        -- which one actually happened.
        INSERT INTO users(username,password_hash,is_admin) VALUES('helper','x',0);
        INSERT INTO users(username,password_hash,is_admin) VALUES('hexiv','x',1);

        INSERT INTO businesses(name,domain,website)
            VALUES('Paradise Dental','paradisedental.ca','https://paradisedental.ca');
        INSERT INTO email_leads(business_id,email) VALUES(1,'info@paradisedental.ca');
        INSERT INTO campaigns(name) VALUES('Q1');
    """)
    conn.commit()
    conn.close()


def test_migration(work):
    print("\n1. MIGRATING A DATABASE THAT PREDATES OWNERSHIP")
    path = os.path.join(work, "pre_ownership.db")
    _create_pre_ownership_db(path)

    db = load_db(path)
    db.init_db()

    for table in ("businesses", "email_leads", "campaigns", "call_campaigns", "scrape_jobs"):
        check(f"{table} gained owner_id", "owner_id" in columns(path, table))

    admin_id = scalar(path, "SELECT id FROM users WHERE username='hexiv'")
    check("existing business handed to the founding admin, not merely to user 1",
          scalar(path, "SELECT owner_id FROM businesses WHERE id=1") == admin_id,
          f"owner={scalar(path, 'SELECT owner_id FROM businesses WHERE id=1')} admin={admin_id}")
    check("existing email lead handed to the same owner",
          scalar(path, "SELECT owner_id FROM email_leads WHERE id=1") == admin_id)
    check("existing campaign handed to the same owner",
          scalar(path, "SELECT owner_id FROM campaigns WHERE id=1") == admin_id)
    check("nothing is left unassigned",
          scalar(path, "SELECT COUNT(*) FROM businesses WHERE owner_id=0") == 0)

    check("the global email index is gone",
          "email_leads_email_unique" not in indexes(path), f"indexes={sorted(indexes(path))}")
    check("a per-owner email index replaced it",
          "email_leads_owner_email_unique" in indexes(path))

    before = rows(path, "SELECT id, owner_id FROM businesses ORDER BY id")
    db.init_db()
    db.init_db()
    check("reruns change no ownership", rows(path, "SELECT id, owner_id FROM businesses ORDER BY id") == before)


def test_default_owner_precedence(work):
    print("\n2. WHO INHERITS PRE-EXISTING DATA")
    path = os.path.join(work, "precedence.db")
    db = load_db(path)
    db.init_db()
    with db.get_db() as conn:
        check("no users yet means nothing to assign to",
              db._default_owner_id(conn) == db.OWNER_UNASSIGNED)
    helper = db.create_user("helper", "pw", is_admin=False)
    admin = db.create_user("boss", "pw", is_admin=True)
    with db.get_db() as conn:
        check("an admin outranks a lower-numbered non-admin",
              db._default_owner_id(conn) == admin, f"helper={helper} admin={admin}")


def _two_operators(path):
    db = load_db(path)
    db.init_db()
    a = db.create_user("owner_a", "pw", is_admin=True)
    b = db.create_user("owner_b", "pw", is_admin=False)
    return db, a, b


def test_the_wall(work):
    print("\n3. THE SAME ADDRESS IMPORTED BY BOTH OPERATORS")
    path = os.path.join(work, "wall.db")
    db, a, b = _two_operators(path)

    db.upsert_businesses([{"email": "info@sharedclinic.ae", "company": "Shared Clinic",
                           "first_name": "Aisha", "website": "https://sharedclinic.ae"}],
                         owner_id=a)
    db.upsert_businesses([{"email": "info@sharedclinic.ae", "company": "Shared Clinic",
                           "first_name": "Bilal", "website": "https://sharedclinic.ae"}],
                         owner_id=b)

    check("each operator got their own business row",
          scalar(path, "SELECT COUNT(*) FROM businesses WHERE domain='sharedclinic.ae'") == 2,
          f"got {scalar(path, 'SELECT COUNT(*) FROM businesses')}")
    check("each operator got their own email lead",
          scalar(path, "SELECT COUNT(*) FROM email_leads WHERE email='info@sharedclinic.ae'") == 2)

    names = dict(rows(path, """SELECT el.owner_id, el.first_name FROM email_leads el
                                WHERE el.email='info@sharedclinic.ae'"""))
    check("the first operator's record was not rewritten by the second's import",
          names.get(a) == "Aisha", f"owner {a} has first_name={names.get(a)!r}")
    check("the second operator's own record holds their own data",
          names.get(b) == "Bilal", f"owner {b} has first_name={names.get(b)!r}")

    owners = {r[0] for r in rows(path, """SELECT b.owner_id FROM businesses b
                                           WHERE b.domain='sharedclinic.ae'""")}
    check("the two businesses belong to the two different operators", owners == {a, b})

    db.upsert_businesses([{"email": "info@sharedclinic.ae", "company": "Shared Clinic"}],
                         owner_id=a)
    check("re-importing into the same account still dedupes rather than piling up",
          scalar(path, "SELECT COUNT(*) FROM email_leads WHERE email='info@sharedclinic.ae'") == 2)


def test_resolver_isolation(work):
    print("\n4. IDENTITY LOOKUP STOPS AT THE WALL")
    path = os.path.join(work, "isolation.db")
    db, a, b = _two_operators(path)
    db.upsert_businesses([{"company": "Kenmount Physio", "phone": "+971 50 555 0101",
                           "website": "https://kenmount.ae"}], owner_id=a)

    with db.get_db() as conn:
        mine = db.find_existing_business(conn, phone="+971 50 555 0101", owner_id=a)
        theirs = db.find_existing_business(conn, phone="+971 50 555 0101", owner_id=b)
        across = db.find_existing_business(conn, phone="+971 50 555 0101", exclude_owner_id=b)
    check("an operator finds their own business", mine is not None)
    check("the other operator does not see it at all", theirs is None)
    check("an explicit cross-owner lookup does see it", across is not None)

    db.upsert_businesses([{"company": "Kenmount Physio", "phone": "+971 50 555 0101",
                           "website": "https://kenmount.ae"}], owner_id=b)
    check("so the second operator creates their own row rather than joining the first's",
          scalar(path, "SELECT COUNT(*) FROM businesses WHERE domain='kenmount.ae'") == 2)


def test_cross_owner_notice(work):
    print("\n5. OVERLAP IS REPORTED, NOT MERGED")
    path = os.path.join(work, "notice.db")
    db, a, b = _two_operators(path)
    db.upsert_businesses([{"company": "Jumeirah Dental", "phone": "+971 50 555 0202",
                           "website": "https://jumeirahdental.ae",
                           "email": "hello@jumeirahdental.ae"}], owner_id=b)

    matches = db.find_cross_owner_matches(
        [{"company": "Jumeirah Dental", "phone": "+971 50 555 0202"}], owner_id=a)
    check("the other operator's clinic is flagged", len(matches) == 1, f"got {matches}")
    if matches:
        m = matches[0]
        check("the notice names who is working it", m["owner_name"] == "owner_b", f"got {m['owner_name']}")
        check("the notice says which channel", "Email" in m["channel_labels"], f"got {m['channel_labels']}")
        check("the notice carries a date", len(m["since"]) == 10, f"got {m['since']!r}")
        leaked = {"phone", "email", "address", "notes", "website", "business_id"} & set(m)
        check("the notice leaks no contact details or row identity", not leaked, f"leaked={leaked}")

    check("nothing was merged or created by looking",
          scalar(path, "SELECT COUNT(*) FROM businesses") == 1)

    none_for_self = db.find_cross_owner_matches(
        [{"company": "Jumeirah Dental", "phone": "+971 50 555 0202"}], owner_id=b)
    check("an operator is not warned about their own leads", none_for_self == [])


def test_suppression_crosses_the_wall(work):
    print("\n6. AN UNSUBSCRIBE BINDS EVERY OPERATOR")
    path = os.path.join(work, "suppress.db")
    db, a, b = _two_operators(path)
    for owner in (a, b):
        db.upsert_businesses([{"email": "stop@optout.ae", "company": "Opt Out Clinic",
                               "website": "https://optout.ae"}], owner_id=owner)

    lead_ids = [r[0] for r in rows(path, "SELECT id FROM email_leads WHERE email='stop@optout.ae'")]
    check("both operators hold the address before the opt-out", len(lead_ids) == 2)
    for owner, lead_id in zip((a, b), lead_ids):
        cid = db.create_campaign(f"C{owner}", owner_id=owner)
        db.enroll_contacts_bulk(cid, [lead_id], owner_id=owner)

    db.unsubscribe_contact("stop@optout.ae")

    suppressed = scalar(path, """SELECT COUNT(*) FROM email_leads
                                  WHERE email='stop@optout.ae' AND status='unsubscribed'""")
    check("every copy of the address is unsubscribed", suppressed == 2,
          f"{suppressed} of 2")
    blocked = scalar(path, """SELECT COUNT(*) FROM businesses
                               WHERE domain='optout.ae' AND do_not_contact=1""")
    check("both operators' businesses are suppressed on every channel", blocked == 2,
          f"{blocked} of 2")
    stopped = scalar(path, "SELECT COUNT(*) FROM enrollments WHERE status='unsubscribed'")
    check("both operators' queued sends are stopped", stopped == 2, f"{stopped} of 2")


def test_bounce_crosses_the_wall(work):
    print("\n7. A DEAD ADDRESS IS DEAD FOR EVERY OPERATOR")
    path = os.path.join(work, "bounce.db")
    db, a, b = _two_operators(path)
    for owner in (a, b):
        db.upsert_businesses([{"email": "nobody@deadmail.ae", "company": "Dead Mail",
                               "website": "https://deadmail.ae"}], owner_id=owner)
    db.mark_bounced("nobody@deadmail.ae")
    check("every copy of the address is marked bounced",
          scalar(path, """SELECT COUNT(*) FROM email_leads
                           WHERE email='nobody@deadmail.ae' AND status='bounced'""") == 2)


def test_writes_fail_closed(work):
    print("\n8. A WRITE THAT DOES NOT SAY WHO IT BELONGS TO")
    path = os.path.join(work, "failclosed.db")
    db, a, b = _two_operators(path)

    try:
        db.upsert_businesses([{"email": "orphan@nowhere.ae", "company": "Orphan"}])
        check("an unattributed import is refused once two accounts exist", False,
              "it was accepted")
    except ValueError:
        check("an unattributed import is refused once two accounts exist", True)

    check("and nothing was written", scalar(path, "SELECT COUNT(*) FROM businesses") == 0)

    db.upsert_businesses([{"email": "orphan@nowhere.ae", "company": "Orphan"}], owner_id=a)
    check("the same import succeeds when it names an owner",
          scalar(path, "SELECT COUNT(*) FROM businesses WHERE owner_id=?", (a,)) == 1)


def test_single_operator_needs_no_ceremony(work):
    print("\n9. A ONE-PERSON INSTALL IS UNAFFECTED")
    path = os.path.join(work, "solo.db")
    db = load_db(path)
    db.init_db()
    solo = db.create_user("solo", "pw", is_admin=True)
    db.upsert_businesses([{"email": "hi@solo.ae", "company": "Solo Clinic"}])
    check("an import with no stated owner still works and lands on the only account",
          scalar(path, "SELECT owner_id FROM businesses") == solo)


def test_delete_user_guard(work):
    print("\n10. DELETING AN OPERATOR WHO STILL OWNS WORK")
    path = os.path.join(work, "delete.db")
    db, a, b = _two_operators(path)
    db.upsert_businesses([{"email": "x@theirs.ae", "company": "Theirs"}], owner_id=b)

    ok, err = db.delete_user(b)
    check("deletion is refused while they own leads", ok is False)
    check("the refusal says what is in the way", bool(err) and "leads" in err, f"err={err!r}")
    check("the account still exists",
          scalar(path, "SELECT COUNT(*) FROM users WHERE id=?", (b,)) == 1)

    db.delete_email_leads([r[0] for r in rows(path, "SELECT id FROM email_leads")],
                          owner_id=b)
    with db.get_db() as conn:
        conn.execute("DELETE FROM businesses WHERE owner_id=?", (b,))
    ok, err = db.delete_user(b)
    check("deletion goes through once their data is gone", ok is True, f"err={err!r}")
    check("the account is removed",
          scalar(path, "SELECT COUNT(*) FROM users WHERE id=?", (b,)) == 0)


def test_routes_enforce_the_wall(work):
    """
    The wall has to hold at the HTTP layer, not just in db.py. Everything below
    is one operator pointing a request at another operator's row id -- which is
    all it takes, since ids are sequential and guessable.
    """
    print("\n11. ONE OPERATOR REACHING FOR ANOTHER'S ROWS OVER HTTP")
    path = os.path.join(work, "routes.db")
    os.environ["DB_PATH"] = path
    os.environ["SECRET_KEY"] = "test-secret"

    import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    a = db_mod.create_user("alice", "test-password-123", is_admin=True)
    b = db_mod.create_user("bob", "test-password-123", is_admin=True)

    import app as app_mod
    importlib.reload(app_mod)
    app_mod.app.config["TESTING"] = True

    def client_for(uid):
        c = app_mod.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = uid
            s["is_admin"] = True
            s["csrf_token"] = "t"
        return c

    ca, cb = client_for(a), client_for(b)

    db_mod.upsert_businesses([{"email": "lead@alice.ae", "company": "Alice Clinic",
                               "phone": "+971 50 555 1111"}], owner_id=a)
    db_mod.upsert_businesses([{"email": "lead@bob.ae", "company": "Bob Clinic",
                               "phone": "+971 50 555 2222"}], owner_id=b)
    a_campaign = db_mod.create_campaign("Alice Q1", owner_id=a)
    b_campaign = db_mod.create_campaign("Bob Q1", owner_id=b)
    db_mod.upsert_wa_leads([{"company": "Bob WA", "phone": "+971 50 555 3333"}],
                           default_country="AE", owner_id=b)
    b_wa = db_mod.get_wa_leads(owner_id=b)[0]["id"]

    listed = ca.get("/api/contacts").get_json()
    emails = {r["email"] for r in listed["rows"]}
    check("the contact list shows only your own leads", emails == {"lead@alice.ae"},
          f"got {emails}")

    campaigns = {c["name"] for c in ca.get("/api/campaigns").get_json()}
    check("the campaign list shows only your own", campaigns == {"Alice Q1"}, f"got {campaigns}")

    wa = ca.get("/api/wa/leads").get_json()
    check("the WhatsApp list shows only your own", wa == [], f"got {wa}")

    calls = ca.get("/api/calls/queue?bucket=new").get_json()
    names = {l.get("company") or l.get("name") for l in calls["leads"]}
    check("the call queue shows only your own", names == {"Alice Clinic"}, f"got {names}")

    hdr = {"X-CSRF-Token": "t"}
    probes = [
        ("read another's campaign",        ca.get(f"/api/campaigns/{b_campaign}")),
        ("read another's campaign stats",  ca.get(f"/api/stats/{b_campaign}")),
        ("read another's campaign steps",  ca.get(f"/api/campaigns/{b_campaign}/steps")),
        ("export another's campaign",      ca.get(f"/api/campaigns/{b_campaign}/export")),
        ("pause another's campaign",       ca.post(f"/api/campaigns/{b_campaign}/pause", headers=hdr)),
        ("delete another's campaign",      ca.delete(f"/api/campaigns/{b_campaign}", headers=hdr)),
        ("edit another's WhatsApp draft",  ca.put(f"/api/wa/leads/{b_wa}/message",
                                                 json={"message": "hi"}, headers=hdr)),
        ("mark another's WhatsApp sent",   ca.post(f"/api/wa/leads/{b_wa}/sent",
                                                  json={}, headers=hdr)),
        ("pause another's WhatsApp lead",  ca.post(f"/api/wa/leads/{b_wa}/pause",
                                                  json={"paused": True}, headers=hdr)),
    ]
    for label, resp in probes:
        check(f"cannot {label}", resp.status_code == 404, f"got {resp.status_code}")

    check("and the other operator's campaign is untouched",
          db_mod.get_campaign(b_campaign)["status"] != "paused")
    check("the owner themselves is not locked out",
          cb.get(f"/api/campaigns/{b_campaign}").status_code == 200)

    # ── ids in the request body ──────────────────────────────────────────────
    # The URL-id sweep never saw these. Each one was reachable when this
    # section was written: an operator naming another's row in a POST body
    # could read it, mail it, delete it, or kill it.
    print("\n12. IDS SMUGGLED IN A REQUEST BODY, NOT THE URL")

    b_lead = db_mod.get_email_leads(owner_id=b)[0]["id"]
    check("(fixture) the other operator's lead exists to aim at", bool(b_lead))

    enrolled = ca.post(f"/api/campaigns/{a_campaign}/contacts",
                       json={"contact_ids": [b_lead]}, headers=hdr).get_json()
    check("cannot enrol another operator's lead into your campaign",
          (enrolled or {}).get("enrolled") == 0, f"got {enrolled}")
    report = ca.get(f"/api/campaigns/{a_campaign}/contacts").get_json()
    leaked = [r for r in (report or []) if "bob" in str(r.get("email", ""))]
    check("so their address never appears in your campaign report", not leaked,
          f"leaked={leaked}")

    ca.post("/api/contacts/bulk-delete", json={"ids": [b_lead]}, headers=hdr)
    still_there = db_mod.get_email_leads(owner_id=b)
    check("cannot hard-delete another operator's lead", len(still_there) == 1,
          f"they have {len(still_there)} left")

    logged = ca.post("/api/calls/log",
                     json={"contact_id": b_business_id(db_mod, b),
                           "outcome": "not_interested"}, headers=hdr)
    check("cannot log a call against another operator's business",
          logged.status_code == 404, f"got {logged.status_code}")

    found = ca.get("/api/businesses/search?q=").get_json() or {}
    blob = json.dumps(found)
    check("the business search returns only your own",
          found.get("total") == 1 and "Bob" not in blob and "555 2222" not in blob,
          f"total={found.get('total')} body={blob[:200]}")

    cover = ca.get("/api/variable-coverage").get_json()
    check("variable coverage counts only your own contacts",
          (cover or {}).get("total") == 1, f"got total={(cover or {}).get('total')}")

    # ── WhatsApp templates are personal ──────────────────────────────────────
    print("\n13. EACH OPERATOR WRITES THEIR OWN MESSAGES")
    saved = ca.put("/api/wa/templates", headers=hdr, json={
        "templates": {"gap": ["Alice's own opener for {{business_name}}"]},
        "followup_days": 9,
    })
    check("an operator can save their own templates without being an admin",
          saved.status_code == 200, f"got {saved.status_code} {saved.get_json()}")

    mine = ca.get("/api/wa/templates").get_json()
    theirs = cb.get("/api/wa/templates").get_json()
    check("their own copy is what they get back",
          mine["gap"] == ["Alice's own opener for {{business_name}}"], f"got {mine['gap']}")
    check("the other operator does not see it", "Alice" not in json.dumps(theirs["gap"]),
          f"got {theirs['gap']}")
    check("nor inherits their follow-up interval",
          mine["followup_days"] == 9 and theirs["followup_days"] != 9,
          f"mine={mine['followup_days']} theirs={theirs['followup_days']}")
    check("and the other operator still has working copy of their own",
          bool(theirs["gap"] and theirs["gap"][0].strip()), f"got {theirs['gap']}")

    # ── Calling ──────────────────────────────────────────────────────────────
    print("\n14. THE CALLING SECTION SHOWS NOTHING OF THE OTHER OPERATOR")

    saved = ca.put("/api/call-script", headers=hdr, json={
        "name": "Alice's pitch",
        "sections": [{"title": "Opening", "body": "Alice's secret opener"}],
    })
    check("an operator can write their own call script without being an admin",
          saved.status_code == 200, f"got {saved.status_code} {saved.get_json()}")

    my_script = ca.get("/api/call-script").get_json()
    their_script = cb.get("/api/call-script").get_json()
    check("they get their own script back",
          json.dumps(my_script).count("Alice's secret opener") == 1, str(my_script)[:160])
    check("the other operator's script does not contain a word of it",
          "Alice" not in json.dumps(their_script), str(their_script)[:160])
    check("and starts blank rather than inheriting a pitch in someone else's voice",
          all(not s["body"] for s in their_script["sections"]), str(their_script)[:160])

    made = ca.post("/api/call-outcomes", headers=hdr,
                   json={"label": "Sent Alice's proposal"}).get_json()
    check("an operator can invent their own outcome", bool((made or {}).get("key")), str(made))

    mine_out = {o["label"] for o in ca.get("/api/call-outcomes").get_json()}
    theirs_out = {o["label"] for o in cb.get("/api/call-outcomes").get_json()}
    check("it appears in their own dialler", "Sent Alice's proposal" in mine_out)
    check("but not in the other operator's", "Sent Alice's proposal" not in theirs_out,
          f"got {sorted(theirs_out)}")
    check("while the shared built-ins still reach both",
          "Not interested" in mine_out and "Not interested" in theirs_out,
          f"mine={sorted(mine_out)} theirs={sorted(theirs_out)}")

    stolen = cb.delete(f"/api/call-outcomes/{made['key']}", headers=hdr)
    check("and the other operator cannot delete it out from under them",
          stolen.status_code == 400, f"got {stolen.status_code}")
    check("so it survives", "Sent Alice's proposal" in
          {o["label"] for o in ca.get("/api/call-outcomes").get_json()})

    b_business = b_business_id(db_mod, b)
    probes = [
        ("read another's call detail", ca.get(f"/api/calls/contact/{b_business}")),
        ("download their calendar invite", ca.get(f"/api/calls/{b_business}/ics")),
        ("reopen their closed lead", ca.post(f"/api/calls/{b_business}/reopen", headers=hdr)),
    ]
    for label, resp in probes:
        check(f"cannot {label}", resp.status_code == 404, f"got {resp.status_code}")

    queue = ca.get("/api/calls/queue?bucket=all").get_json()
    check("and the call queue never mentions their business",
          "Bob" not in json.dumps(queue.get("leads", [])), str(queue)[:160])


def b_business_id(db_mod, owner):
    """The business id behind that operator's only lead."""
    with db_mod.get_db() as conn:
        return conn.execute(
            "SELECT id FROM businesses WHERE owner_id=? ORDER BY id LIMIT 1", (owner,)
        ).fetchone()["id"]


def test_wa_copy_is_not_inherited(work):
    """
    Message copy must not cross the wall, not even as a starting point.

    An earlier version fell back to the shared pre-multi-user value when an
    operator had none of their own, so a second operator opened the editor
    onto the first one's messages. Two people here lead with different
    services: that copy is wrong for them and not theirs to read.
    """
    print("\n15. WHATSAPP COPY IS NOT INHERITED FROM THE OTHER OPERATOR")
    path = os.path.join(work, "wacopy.db")
    db = load_db(path)
    db.init_db()
    a = db.create_user("first", "pw", is_admin=True)

    # The shape an install had before templates were per-person.
    db.save_settings({"wa_template_gap": "First operator's own pitch",
                      "wa_followup_days": "7"})
    db.init_db()          # startup runs the migration

    mine = db.get_wa_templates(owner_id=a)
    check("the operator who wrote it keeps it",
          mine["gap"] == ["First operator's own pitch"], f"got {mine['gap']}")
    check("along with their follow-up interval", mine["followup_days"] == 7,
          f"got {mine['followup_days']}")
    check("and the shared copy is gone rather than left readable",
          db.get_settings().get("wa_template_gap") in (None, ""),
          f"got {db.get_settings().get('wa_template_gap')!r}")

    b = db.create_user("second", "pw", is_admin=False)
    theirs = db.get_wa_templates(owner_id=b)
    check("a second operator does not inherit a word of it",
          "First operator" not in json.dumps(theirs["gap"]), f"got {theirs['gap']}")
    check("they get the factory default to write over",
          theirs["gap"] == [db._DEFAULT_WA_TEMPLATE_GAP], f"got {theirs['gap']}")
    check("and the factory follow-up interval, not the other operator's",
          theirs["followup_days"] == db.WA_DEFAULT_FOLLOWUP_DAYS,
          f"got {theirs['followup_days']}")

    db.save_wa_templates({"gap": ["Second operator's own pitch"]}, owner_id=b)
    check("saving their own leaves the first operator's untouched",
          db.get_wa_templates(owner_id=a)["gap"] == ["First operator's own pitch"],
          f"got {db.get_wa_templates(owner_id=a)['gap']}")


def test_worker_key_migration(work):
    """
    The install-wide worker key has to survive the switch to per-operator keys.

    It's saved on the founding operator's laptop, and their worker is usually
    running straight through the deploy. If the upgrade dropped or re-keyed it,
    scraping would just stop, with nothing on screen saying why.
    """
    print("\n16. THE OLD SHARED WORKER KEY SURVIVES THE UPGRADE")
    path = os.path.join(work, "workerkey.db")
    db = load_db(path)
    db.init_db()
    founder = db.create_user("founder", "pw", is_admin=True)

    # The shape an install had before worker keys were per operator.
    with db.get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) "
                     "VALUES('_worker_api_key','old-shared-key')")
        conn.execute("INSERT OR REPLACE INTO settings(key,value) "
                     "VALUES('_worker_last_seen','2026-09-15 09:00:00')")
    db.init_db()          # startup runs the migration

    check("the key saved on the laptop still authenticates",
          db.worker_owner_for_key("old-shared-key") == founder,
          f"got {db.worker_owner_for_key('old-shared-key')}")
    check("and it now belongs to the founding operator",
          db.get_or_create_worker_key(founder) == "old-shared-key")
    leftover = {k: v for k, v in db.get_settings().items()
                if k in ("_worker_api_key", "_worker_last_seen")}
    check("no ownerless shared copy is left behind", not leftover, f"got {leftover}")

    second = db.create_user("second", "pw", is_admin=False)
    check("a second operator gets their own key, not the old shared one",
          db.get_or_create_worker_key(second) != "old-shared-key")
    check("and the old key doesn't let anyone act as them",
          db.worker_owner_for_key("old-shared-key") != second)

    db.init_db()
    check("re-running startup changes nothing",
          db.worker_owner_for_key("old-shared-key") == founder)


def main():
    work = tempfile.mkdtemp(prefix="shoutreach_owner_")
    try:
        test_migration(work)
        test_default_owner_precedence(work)
        test_the_wall(work)
        test_resolver_isolation(work)
        test_cross_owner_notice(work)
        test_suppression_crosses_the_wall(work)
        test_bounce_crosses_the_wall(work)
        test_writes_fail_closed(work)
        test_single_operator_needs_no_ceremony(work)
        test_delete_user_guard(work)
        test_routes_enforce_the_wall(work)
        test_wa_copy_is_not_inherited(work)
        test_worker_key_migration(work)
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
