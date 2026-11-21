"""Opt-outs and bounces are permanent, for every operator (audit B-1).

Run:  python tests/test_suppression.py     (exits 0 on pass, 1 on failure)

An unsubscribe or a bounce used to be only a status on whichever email_leads
rows existed at that moment. Three ordinary actions brought the address back
as 'active' and onto the send loop's due list: the other operator importing
it later, restoring a deleted lead, and editing the lead's status. Now every
opt-out and bounce is remembered in suppressed_addresses -- by address alone,
with no owner -- and checked when an address is created, restored or edited,
and by get_due_enrollments itself.
"""
import importlib
import inspect
import os
import re
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SHOUTREACH_NO_SCHEDULER"] = "1"

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def q(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def fresh(work, name):
    path = os.path.join(work, name)
    os.environ["DB_PATH"] = path
    os.environ["SECRET_KEY"] = "test-secret"
    import db
    importlib.reload(db)
    db.init_db()
    a = db.create_user("owner_a", "test-password-123", is_admin=True)
    b = db.create_user("owner_b", "test-password-123", is_admin=False)
    return db, path, a, b


def due_for(db, path, owner, email):
    """Enrol this owner's copy of `email` in a new active campaign and return
    whether the send loop would send it."""
    lead = q(path, "SELECT id FROM email_leads WHERE owner_id=? AND email=?", (owner, email))
    if not lead:
        return False
    cid = db.create_campaign(f"C{owner}-{email}", owner_id=owner)
    with db.get_db() as conn:
        conn.execute("INSERT INTO enrollments(campaign_id, email_lead_id, status) VALUES(?,?,'queued')",
                     (cid, lead[0][0]))
    return any(r["email"] == email for r in db.get_due_enrollments(cid, limit=100))


def statuses(path, email):
    return sorted(r[0] for r in q(path, "SELECT status FROM email_leads WHERE email=?", (email,)))


def test_later_import(work):
    print("\n1. OPT-OUT, THEN THE OTHER OPERATOR IMPORTS THE ADDRESS")
    db, path, a, b = fresh(work, "later.db")
    db.upsert_businesses([{"email": "stop@one.ae", "company": "One"}], owner_id=a)
    db.unsubscribe_contact("stop@one.ae")
    db.upsert_businesses([{"email": "Stop@One.ae ", "company": "One"}], owner_id=b)
    check("B's new copy is created unsubscribed", statuses(path, "stop@one.ae") == ["unsubscribed"] * 2,
          str(statuses(path, "stop@one.ae")))
    check("B's business is do-not-contact, as an existing copy's would be",
          q(path, "SELECT do_not_contact FROM businesses WHERE owner_id=?", (b,)) == [(1,)])
    check("and the send loop won't send it", not due_for(db, path, b, "stop@one.ae"))

    db.upsert_businesses([{"email": "dead@two.ae", "company": "Two"}], owner_id=a)
    db.mark_bounced("dead@two.ae")
    db.upsert_businesses([{"email": "dead@two.ae", "company": "Two"}], owner_id=b)
    check("a bounce: B's later copy arrives bounced", statuses(path, "dead@two.ae") == ["bounced"] * 2,
          str(statuses(path, "dead@two.ae")))
    check("a bounce: not on the due list", not due_for(db, path, b, "dead@two.ae"))

    db.create_email_lead("stop2@three.ae", company="Three", owner_id=a)
    db.unsubscribe_contact("stop2@three.ae")
    lead_id, err = db.create_email_lead("stop2@three.ae", company="Three", owner_id=b)
    check("an address typed in by hand arrives unsubscribed",
          statuses(path, "stop2@three.ae") == ["unsubscribed"] * 2, str(statuses(path, "stop2@three.ae")))

    camp = db.create_wa_campaign("WA", owner_id=b)
    camp = camp[0] if isinstance(camp, tuple) else camp
    db.upsert_businesses([{"email": "wa@four.ae", "company": "Four"}], owner_id=a)
    db.unsubscribe_contact("wa@four.ae")
    db.upsert_wa_leads([{"company": "Four", "phone": "+971 50 123 4567", "email": "wa@four.ae"}],
                       default_country="AE", owner_id=b, wa_campaign_id=camp)
    check("a WhatsApp import carrying the address files it unsubscribed",
          statuses(path, "wa@four.ae") == ["unsubscribed"] * 2, str(statuses(path, "wa@four.ae")))

    db.unsubscribe_contact("invite@fifteen.ae")  # nobody holds it
    four = q(path, "SELECT id FROM businesses WHERE owner_id=? AND name='Four'", (b,))[0][0]
    ok, err = db.add_business_email(four, "invite@fifteen.ae", owner_id=b)
    check("an address saved for a meeting invite arrives unsubscribed",
          ok and statuses(path, "invite@fifteen.ae") == ["unsubscribed"], f"{ok} {err}")


def test_routes(work):
    print("\n2. THE SAME THROUGH THE BROWSER AND WORKER ROUTES, AND THE LINK")
    db, path, a, b = fresh(work, "routes.db")
    import app as app_mod
    importlib.reload(app_mod)
    import sender
    app_mod.app.config["TESTING"] = True

    def client_for(uid, admin):
        c = app_mod.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = uid
            s["is_admin"] = admin
            s["csrf_token"] = "t"
        return c
    ca, cb = client_for(a, True), client_for(b, False)
    hdr = {"X-CSRF-Token": "t"}

    r = ca.post("/api/contacts/import", headers=hdr,
                json={"rows": [{"email": "web@five.ae", "company": "Five", "mx_valid": 1}]})
    check("A imports", r.status_code == 200, str(r.status_code))
    token = sender._make_unsub_token("web@five.ae")
    r = app_mod.app.test_client().get(f"/unsubscribe/{token}")
    check("the unsubscribe link works", r.status_code == 200)
    r = cb.post("/api/contacts/import", headers=hdr,
                json={"rows": [{"email": "web@five.ae", "company": "Five", "mx_valid": 1}]})
    check("B's browser import is created unsubscribed",
          statuses(path, "web@five.ae") == ["unsubscribed"] * 2, str(statuses(path, "web@five.ae")))

    wk = {"X-API-Key": db.get_or_create_worker_key(b)}
    db.mark_bounced("worker@six.ae")  # no row anywhere yet
    r = app_mod.app.test_client().post("/api/contacts/import", headers=wk, json={"rows": [
        {"email": "worker@six.ae", "company": "Six", "mx_valid": 1}]})
    check("the worker's import is created bounced", statuses(path, "worker@six.ae") == ["bounced"],
          f"{r.status_code} {statuses(path, 'worker@six.ae')}")

    lead = q(path, "SELECT id FROM email_leads WHERE owner_id=? AND email='web@five.ae'", (b,))[0][0]
    r = cb.put(f"/api/contacts/{lead}", headers=hdr, json={"status": "active"})
    check("editing the status back to active is refused, with a plain message",
          r.status_code == 400 and "unsubscribed" in (r.get_json() or {}).get("error", ""),
          f"{r.status_code} {r.get_json()}")
    check("and nothing changed", statuses(path, "web@five.ae") == ["unsubscribed"] * 2)
    r = cb.put(f"/api/contacts/{lead}", headers=hdr, json={"first_name": "Sam"})
    check("other fields of a suppressed address can still be edited", r.status_code == 200)

    r = ca.post("/api/contacts/import", headers=hdr,
                json={"rows": [{"email": "hand@seven.ae", "company": "Seven", "mx_valid": 1}]})
    lead = q(path, "SELECT id FROM email_leads WHERE email='hand@seven.ae'")[0][0]
    r = ca.put(f"/api/contacts/{lead}", headers=hdr, json={"status": "bounced"})
    check("setting bounced by hand is allowed", r.status_code == 200)
    check("and remembered", q(path, "SELECT reason FROM suppressed_addresses "
                                    "WHERE email='hand@seven.ae'") == [("bounced",)])
    r = ca.put(f"/api/contacts/{lead}", headers=hdr, json={"status": "active"})
    check("so it can't be set back either", r.status_code == 400)
    r = ca.put(f"/api/contacts/{lead}", headers=hdr, json={"status": "unsubscribed"})
    check("a bounced address can still be marked unsubscribed", r.status_code == 200)
    check("and unsubscribed wins", q(path, "SELECT reason FROM suppressed_addresses "
                                           "WHERE email='hand@seven.ae'") == [("unsubscribed",)])

    r = ca.post("/api/contacts/import", headers=hdr,
                json={"rows": [{"email": "rename@eight.ae", "company": "Eight", "mx_valid": 1}]})
    lead = q(path, "SELECT id FROM email_leads WHERE email='rename@eight.ae'")[0][0]
    r = ca.put(f"/api/contacts/{lead}", headers=hdr, json={"email": "worker@six.ae"})
    check("changing an address to one that bounced makes the row bounced",
          q(path, "SELECT status FROM email_leads WHERE id=?", (lead,)) == [("bounced",)],
          str(q(path, "SELECT status FROM email_leads WHERE id=?", (lead,))))


def test_restore(work):
    print("\n3. OPT-OUT WHILE THE LEAD IS IN RECENTLY DELETED, THEN RESTORE")
    db, path, a, b = fresh(work, "restore.db")
    db.upsert_businesses([{"email": "trash@nine.ae", "company": "Nine"}], owner_id=a)
    bid = q(path, "SELECT business_id FROM email_leads WHERE email='trash@nine.ae'")[0][0]
    lead = q(path, "SELECT id FROM email_leads WHERE email='trash@nine.ae'")[0][0]
    cid = db.create_campaign("C", owner_id=a)
    db.enroll_contacts_bulk(cid, [lead], owner_id=a)
    db.delete_businesses([bid], owner_id=a)
    check("the lead is gone", statuses(path, "trash@nine.ae") == [])
    db.unsubscribe_contact("trash@nine.ae")
    check("the opt-out is remembered with no row to mark",
          q(path, "SELECT reason FROM suppressed_addresses WHERE email='trash@nine.ae'") == [("unsubscribed",)])
    out = db.restore_deleted_leads(group=q(path, "SELECT delete_group FROM deleted_leads")[0][0], owner_id=a)
    check("it restores", out.get("restored") == 1, str(out))
    check("and comes back unsubscribed", statuses(path, "trash@nine.ae") == ["unsubscribed"],
          str(statuses(path, "trash@nine.ae")))
    check("its enrollment is stopped",
          q(path, "SELECT status FROM enrollments WHERE email_lead_id=?", (lead,)) == [("unsubscribed",)])
    check("and it's not on the due list",
          not any(r["email"] == "trash@nine.ae" for r in db.get_due_enrollments(cid, 100)))


def test_backstop(work):
    print("\n4. A ROW FORCED ACTIVE IN SQL IS STILL NOT SENT")
    db, path, a, b = fresh(work, "backstop.db")
    db.upsert_businesses([{"email": "forced@ten.ae", "company": "Ten"}], owner_id=a)
    db.unsubscribe_contact("forced@ten.ae")
    with db.get_db() as conn:
        conn.execute("UPDATE email_leads SET status='active' WHERE email='forced@ten.ae'")
        conn.execute("UPDATE businesses SET do_not_contact=0")
    check("the row is active and the business is contactable",
          statuses(path, "forced@ten.ae") == ["active"])
    check("get_due_enrollments still leaves it out", not due_for(db, path, a, "forced@ten.ae"))
    check("address_suppression still says unsubscribed",
          db.address_suppression("forced@ten.ae") == "unsubscribed")

    db.upsert_businesses([{"email": "soft@eleven.ae", "company": "Eleven"}], owner_id=a)
    for _ in range(3):
        db.increment_soft_bounce("soft@eleven.ae", 3)
    check("three soft bounces are remembered as a bounce",
          q(path, "SELECT reason FROM suppressed_addresses WHERE email='soft@eleven.ae'") == [("bounced",)])
    db.increment_soft_bounce("never@seen.ae", 3)
    check("a soft bounce for an address nobody holds isn't a bounce",
          db.address_suppression("never@seen.ae") == "")


def test_migration(work):
    print("\n5. THE MIGRATION BACKFILLS BOTH OPERATORS, ONCE")
    db, path, a, b = fresh(work, "migrate.db")
    db.upsert_businesses([{"email": "old-a@twelve.ae", "company": "Twelve"}], owner_id=a)
    db.upsert_businesses([{"email": "old-b@thirteen.ae", "company": "Thirteen"},
                          {"email": "both@fourteen.ae", "company": "Fourteen"}], owner_id=b)
    db.upsert_businesses([{"email": "both@fourteen.ae", "company": "Fourteen"}], owner_id=a)
    with db.get_db() as conn:
        # As a database from before the table existed would look.
        conn.execute("DELETE FROM suppressed_addresses")
        conn.execute("DELETE FROM settings WHERE key='_migrated_suppressed_addresses'")
        conn.execute("UPDATE email_leads SET status='unsubscribed' WHERE email='old-a@twelve.ae'")
        conn.execute("UPDATE email_leads SET status='bounced' WHERE email='old-b@thirteen.ae'")
        conn.execute("UPDATE email_leads SET status='bounced' WHERE email='both@fourteen.ae' AND owner_id=?", (a,))
        conn.execute("UPDATE email_leads SET status='unsubscribed' WHERE email='both@fourteen.ae' AND owner_id=?", (b,))
    db.init_db()
    got = dict(q(path, "SELECT email, reason FROM suppressed_addresses"))
    check("A's opt-out is copied", got.get("old-a@twelve.ae") == "unsubscribed", str(got))
    check("B's bounce is copied", got.get("old-b@thirteen.ae") == "bounced", str(got))
    check("unsubscribed wins over bounced", got.get("both@fourteen.ae") == "unsubscribed", str(got))
    with db.get_db() as conn:
        conn.execute("DELETE FROM suppressed_addresses WHERE email='old-a@twelve.ae'")
    db.init_db()
    check("re-running startup doesn't run it again (marker)",
          "old-a@twelve.ae" not in dict(q(path, "SELECT email, reason FROM suppressed_addresses")))


def test_no_owner_anywhere(work):
    print("\n6. NOTHING ABOUT SUPPRESSION CARRIES AN OWNER")
    db, path, a, b = fresh(work, "owner.db")
    cols = [r[1] for r in q(path, "PRAGMA table_info(suppressed_addresses)")]
    check("the table has no owner column", not any("owner" in c for c in cols), str(cols))
    for fn in (db._record_suppression, db._suppressed_reason, db._apply_suppression,
               db.unsubscribe_contact, db.mark_bounced, db.increment_soft_bounce,
               db.address_suppression, db._migrate_suppressed_addresses):
        src = inspect.getsource(fn)
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        code = re.sub(r'"""[\s\S]*?"""', "", code)
        check(f"{fn.__name__} never mentions an owner", "owner" not in code)
    src = inspect.getsource(db.get_due_enrollments)
    check("the due list's check has no owner in it",
          re.search(r"NOT EXISTS \(SELECT 1 FROM suppressed_addresses s WHERE s.email = el.email\)", src)
          is not None)


def test_send_preview(work):
    print("\n7. THE 'START SENDING?' COUNTS (B-9) RESPECT OWNER, SUPPRESSION, DO NOT CONTACT")
    db, path, a, b = fresh(work, "preview.db")
    db.upsert_businesses([{"email": f"p{i}@clinic{i}.ae", "company": f"Clinic {i}"} for i in range(5)],
                         owner_id=a)
    db.upsert_businesses([{"email": "theirs@other.ae", "company": "Other"}], owner_id=b)
    cid = db.create_campaign("C", owner_id=a)
    pre = db.get_campaign_send_preview(cid, owner_id=a)
    check("nothing enrolled: 0 sendable, all 5 enrollable",
          pre == {"sendable": 0, "enrollable": 5}, str(pre))
    db.unsubscribe_contact("p0@clinic0.ae")
    with db.get_db() as conn:
        conn.execute("UPDATE businesses SET do_not_contact=1 WHERE name='Clinic 1'")
    pre = db.get_campaign_send_preview(cid, owner_id=a)
    check("an opt-out and a do-not-contact business aren't counted", pre["enrollable"] == 3, str(pre))
    ids = [r[0] for r in q(path, "SELECT id FROM email_leads WHERE owner_id=?", (a,))]
    with db.get_db() as conn:
        for lid in ids:
            conn.execute("INSERT INTO enrollments(campaign_id, email_lead_id, status) VALUES(?,?,'queued')",
                         (cid, lid))
        # Forced active in SQL, as in section 4: still not counted.
        conn.execute("UPDATE email_leads SET status='active' WHERE email='p0@clinic0.ae'")
    pre = db.get_campaign_send_preview(cid, owner_id=a)
    check("enrolled: only the 3 the send loop would really email are sendable",
          pre["sendable"] == 3, str(pre))
    check("and none are left to enroll", pre["enrollable"] == 0, str(pre))
    cid_b = db.create_campaign("B's", owner_id=b)
    check("the other operator's figures are their own",
          db.get_campaign_send_preview(cid_b, owner_id=b) == {"sendable": 0, "enrollable": 1})

    import app as app_mod
    importlib.reload(app_mod)
    app_mod.app.config["TESTING"] = True
    c = app_mod.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = a
        sess["csrf_token"] = "t"
    r = c.get(f"/api/campaigns/{cid}/send-preview")
    d = r.get_json() or {}
    check("the route returns the counts, daily limit and start",
          r.status_code == 200 and d.get("sendable") == 3 and d.get("daily_limit") == 30
          and "starts_label" in d and "window_open" in d, str(d))
    check("another operator's campaign is 404", c.get(f"/api/campaigns/{cid_b}/send-preview").status_code == 404)


def main():
    work = tempfile.mkdtemp(prefix="shoutreach_suppress_")
    try:
        test_later_import(work)
        test_routes(work)
        test_restore(work)
        test_backstop(work)
        test_migration(work)
        test_no_owner_anywhere(work)
        test_send_preview(work)
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
