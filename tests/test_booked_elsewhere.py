"""The other operator's copy (Plan 2 package 6 -- crosses the wall on purpose).

Run:  python tests/test_booked_elsewhere.py     (exits 0 on pass, 1 on failure)

When a website enquiry or booking is filed for one operator and the other
already holds the same business, the other's copy says what really happened
-- "Filled in the website form — not booked" for an enquiry alone, "Booked
via the website" once there's a booking -- and its follow-ups stop. Decided 2026-11-21. It must reveal nothing
else to them -- no time, no answers, no contact details, not who holds it --
and the operator the lead was filed for learns nothing about the other copy.
"""
import hashlib
import hmac
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
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


def main():
    work = tempfile.mkdtemp(prefix="elsewhere_")
    try:
        path = os.path.join(work, "e.db")
        os.environ["DB_PATH"] = path
        os.environ["SECRET_KEY"] = "test-secret"
        import db
        importlib.reload(db)
        db.init_db()
        sheham = db.create_user("sheham", "test-password-123", is_admin=True)
        anas = db.create_user("anas", "test-password-123", is_admin=False)
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True

        def client_for(uid):
            c = app_mod.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = uid
                s["csrf_token"] = "t"
            return c
        cs, ca = client_for(sheham), client_for(anas)

        # Sheham already works this clinic: email sequence, WhatsApp (messaged
        # once) -- and a second clinic on WhatsApp not yet messaged.
        db.upsert_businesses([{"company": "Harbour Dental", "phone": "+44 7700 900321",
                               "email": "info@harbourdental.co.uk", "website": "harbourdental.co.uk",
                               "country": "GB"}], owner_id=sheham)
        mine = q(path, "SELECT id FROM businesses WHERE owner_id=?", (sheham,))[0][0]
        camp = db.create_campaign("Cold", owner_id=sheham)
        db.enroll_contacts_bulk(camp, [q(path, "SELECT id FROM email_leads WHERE business_id=?", (mine,))[0][0]],
                                owner_id=sheham)
        wa = db.create_wa_campaign("WA", owner_id=sheham)
        wa = wa[0] if isinstance(wa, tuple) else wa
        db.upsert_wa_leads([{"company": "Harbour Dental", "phone": "+44 7700 900321"}], default_country="GB",
                           owner_id=sheham, wa_campaign_id=wa)
        with db.get_db() as conn:
            conn.execute("UPDATE wa_leads SET wa_status='sent', sent_date=datetime('now','-3 days') "
                         "WHERE business_id=?", (mine,))
        db.upsert_businesses([{"company": "Quay Physio", "phone": "+44 7700 900654", "country": "GB"}],
                             owner_id=sheham)
        quay = q(path, "SELECT id FROM businesses WHERE name='Quay Physio'")[0][0]
        db.upsert_wa_leads([{"company": "Quay Physio", "phone": "+44 7700 900654"}], default_country="GB",
                           owner_id=sheham, wa_campaign_id=wa)
        detail_before = cs.get(f"/api/businesses/{mine}").get_json()

        print("\n1. AN ENQUIRY FILED FOR THE OTHER OPERATOR")
        db.save_website_config({"sites": {"hexiv.co.uk": {"owner_id": anas, "country": "GB"}}})
        key = db.rotate_website_key()
        r = app_mod.app.test_client().post("/api/inbound/website-lead", headers={"X-API-Key": key}, json={
            "site": "hexiv.co.uk", "name": "Hana Harbour", "email": "hana@harbourdental.co.uk",
            "business": "Harbour Dental Ltd", "phone": "07700 900321", "website": "harbourdental.co.uk",
            "answers": {"Biggest challenge": "Too many no-shows"}, "summary": "secret summary"})
        check("filed for Anas", r.status_code == 200 and q(path, "SELECT owner_id FROM website_enquiries") == [(anas,)])
        stage = q(path, "SELECT pipeline_stage, pipeline_channel, pipeline_at, next_action_at FROM businesses WHERE id=?",
                  (mine,))
        check("an enquiry alone: Sheham's copy reads 'Filled in the website form — not booked', no time at all",
              stage == [("enquired_elsewhere", "website", None, None)], str(stage))
        check("both stages are terminal built-ins with those labels",
              q(path, "SELECT key, label, is_terminal, is_builtin FROM pipeline_stages "
                      "WHERE key IN ('enquired_elsewhere','booked_elsewhere') ORDER BY key")
              == [("booked_elsewhere", "Booked via the website", 1, 1),
                  ("enquired_elsewhere", "Filled in the website form — not booked", 1, 1)])
        check("its email sequence ended", q(path, "SELECT status FROM enrollments") == [("completed",)])
        check("its messaged WhatsApp lead stops (marked replied)",
              q(path, "SELECT replied FROM wa_leads WHERE business_id=?", (mine,)) == [(1,)])
        check("nothing was merged: two separate businesses",
              q(path, "SELECT COUNT(*) FROM businesses WHERE name LIKE 'Harbour Dental%'")[0][0] == 2)

        print("   what Sheham can see")
        d = cs.get(f"/api/businesses/{mine}").get_json()
        blob = json.dumps(d)
        check("the stage, and nothing more", d["pipeline_stage"] == "enquired_elsewhere" and not d.get("enquiries")
              and d.get("meeting") is None and d.get("next_action_at") is None and d.get("pipeline_at") is None)
        check("no answers, no summary, no name, email or who holds it",
              not any(x in blob for x in ("no-shows", "secret summary", "Hana", "hana@", "anas")), blob[:200])
        check("no new line on the lead's history",
              len(d.get("timeline") or []) == len(detail_before.get("timeline") or []))
        everything = json.dumps([cs.get(u).get_json() for u in (
            "/api/meetings", "/api/dashboard", "/api/contacts/sources", "/api/businesses?per_page=500",
            "/api/wa/leads?per_page=500", "/api/pipeline")])
        check("no route of Sheham's carries Anas's lead, answers, or name",
              not any(x in everything for x in ("Hana", "hana@", "no-shows", "Harbour Dental Ltd", "anas")))
        check("it isn't on Sheham's Meetings", cs.get("/api/meetings").get_json() == [])

        print("   what Anas can see")
        anas_biz = q(path, "SELECT business_id FROM website_enquiries")[0][0]
        da = json.dumps(ca.get(f"/api/businesses/{anas_biz}").get_json())
        all_anas = da + json.dumps([ca.get(u).get_json() for u in (
            "/api/meetings", "/api/dashboard", "/api/businesses?per_page=500", "/api/pipeline")])
        check("nothing about Sheham's copy: not its id, campaigns or who holds it",
              f'"business_id": {mine},' not in all_anas and anas_biz != mine
              and "sheham" not in all_anas and "Cold" not in all_anas
              and '"WA"' not in all_anas)
        check("Sheham's copy is 404 to Anas", ca.get(f"/api/businesses/{mine}").status_code == 404)

        print("\n2. A BOOKING FILED FOR THE OTHER OPERATOR")
        secret = db.rotate_cal_secret()

        def deliver(body):
            raw = json.dumps(body).encode()
            return app_mod.app.test_client().post("/api/inbound/cal", data=raw, headers={
                "X-Cal-Signature-256": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()})
        with open(os.path.join(HERE, "fixtures", "cal_booking_created.json"), encoding="utf-8") as f:
            harbour = json.load(f)
        harbour["payload"]["uid"] = "harbour-booking"
        harbour["payload"]["attendees"][0]["email"] = "hana@harbourdental.co.uk"
        harbour["payload"]["metadata"].update({"businessName": "Harbour Dental Ltd", "phone": "07700 900321",
                                               "website": "harbourdental.co.uk"})
        deliver(harbour)
        check("the enquiry then books: Sheham's copy moves up to 'Booked via the website', still no time",
              q(path, "SELECT pipeline_stage, pipeline_at, next_action_at FROM businesses WHERE id=?", (mine,))
              == [("booked_elsewhere", None, None)])
        app_mod.app.test_client().post("/api/inbound/website-lead", headers={"X-API-Key": key}, json={
            "site": "hexiv.co.uk", "name": "Hana Harbour", "email": "hana2@harbourdental.co.uk",
            "business": "Harbour Dental Ltd", "phone": "07700 900321"})
        check("a later enquiry never moves it back down",
              q(path, "SELECT pipeline_stage FROM businesses WHERE id=?", (mine,)) == [("booked_elsewhere",)])
        with open(os.path.join(HERE, "fixtures", "cal_booking_created.json"), encoding="utf-8") as f:
            booking = json.load(f)
        booking["payload"]["metadata"]["businessName"] = "Quay Physio"
        booking["payload"]["metadata"]["phone"] = "07700 900654"
        booking["payload"]["attendees"][0]["email"] = "q@quayphysio.co.uk"
        raw = json.dumps(booking).encode()
        r = app_mod.app.test_client().post("/api/inbound/cal", data=raw, headers={
            "X-Cal-Signature-256": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()})
        check("booked for Anas", r.status_code == 200 and q(path, "SELECT pipeline_stage FROM businesses b "
              "JOIN website_enquiries e ON e.business_id=b.id WHERE e.email='q@quayphysio.co.uk'") == [("booked",)])
        check("Sheham's Quay Physio: Booked via the website, no time",
              q(path, "SELECT pipeline_stage, next_action_at, pipeline_at FROM businesses WHERE id=?", (quay,))
              == [("booked_elsewhere", None, None)])
        check("its unsent WhatsApp lead is paused, so it leaves To do",
              q(path, "SELECT paused FROM wa_leads WHERE business_id=?", (quay,)) == [(1,)]
              and all(l["business_id"] != quay for l in db.get_wa_ready(owner_id=sheham)))
        check("and no meeting on Sheham's side", q(path, "SELECT COUNT(*) FROM meetings WHERE business_id=?", (quay,))[0][0] == 0)

        print("\n3. WHAT IT LEAVES ALONE")
        db.upsert_businesses([{"company": "Own Booking", "phone": "+44 7700 900987", "country": "GB"}],
                             owner_id=sheham)
        own = q(path, "SELECT id FROM businesses WHERE name='Own Booking'")[0][0]
        db.set_business_pipeline(own, "booked", "call", next_action_at="2026-12-05 10:00", owner_id=sheham)
        db.upsert_businesses([{"company": "Won Already", "phone": "+44 7700 900876", "country": "GB"}],
                             owner_id=sheham)
        won = q(path, "SELECT id FROM businesses WHERE name='Won Already'")[0][0]
        db.set_business_pipeline(won, "won", "call", owner_id=sheham)
        for name, phone, email in (("Own Booking", "07700 900987", "o@own.co.uk"),
                                   ("Won Already", "07700 900876", "w@won.co.uk")):
            db.record_website_enquiry(db.clean_website_enquiry({"site": "hexiv.co.uk", "name": "X", "email": email,
                                                                "business": name, "phone": phone}))
        check("a copy the other operator has booked themselves keeps its meeting",
              q(path, "SELECT pipeline_stage, next_action_at FROM businesses WHERE id=?", (own,))
              == [("booked", "2026-12-05 10:00:00")])
        check("a copy already on a terminal stage keeps it",
              q(path, "SELECT pipeline_stage FROM businesses WHERE id=?", (won,)) == [("won",)])

        print("\n4. ONLY A COPY WITH NO STAGE IS MARKED (decided 2026-11-22)")
        db.upsert_businesses([{"company": "Proposal Clinic", "phone": "+44 7700 900111", "country": "GB"}],
                             owner_id=sheham)
        prop = q(path, "SELECT id FROM businesses WHERE name='Proposal Clinic'")[0][0]
        db.set_business_pipeline(prop, "proposal_due", "email", next_action_at="2026-12-01", owner_id=sheham)
        custom = db.create_pipeline_stage("Sent a loom", owner_id=sheham)
        db.upsert_businesses([{"company": "Custom Clinic", "phone": "+44 7700 900112", "country": "GB"}],
                             owner_id=sheham)
        cust = q(path, "SELECT id FROM businesses WHERE name='Custom Clinic'")[0][0]
        db.set_business_pipeline(cust, custom, "call", owner_id=sheham)
        for name, phone, email in (("Proposal Clinic", "07700 900111", "p@prop.co.uk"),
                                   ("Custom Clinic", "07700 900112", "c@cust.co.uk")):
            db.record_website_enquiry(db.clean_website_enquiry({"site": "hexiv.co.uk", "name": "X", "email": email,
                                                                "business": name, "phone": phone}))
        check("a copy on 'Proposal due, 1 Dec' keeps its stage AND its date",
              q(path, "SELECT pipeline_stage, next_action_at FROM businesses WHERE id=?", (prop,))
              == [("proposal_due", "2026-12-01")])
        check("a copy on the owner's own custom stage keeps it",
              q(path, "SELECT pipeline_stage FROM businesses WHERE id=?", (cust,)) == [(custom,)])
        db.upsert_businesses([{"company": "Moved Back", "phone": "+44 7700 900113", "country": "GB"}],
                             owner_id=sheham)
        back = q(path, "SELECT id FROM businesses WHERE name='Moved Back'")[0][0]
        enquiry = {"site": "hexiv.co.uk", "name": "X", "email": "m@back.co.uk", "business": "Moved Back",
                   "phone": "07700 900113"}
        db.record_website_enquiry(db.clean_website_enquiry(enquiry))
        check("a copy with no stage is marked",
              q(path, "SELECT pipeline_stage FROM businesses WHERE id=?", (back,)) == [("enquired_elsewhere",)])
        db.set_business_pipeline(back, "replied", "call", owner_id=sheham)
        db.record_website_enquiry(db.clean_website_enquiry({**enquiry, "summary": "again"}))
        check("once its owner moves it on, a repeat post leaves it alone",
              q(path, "SELECT pipeline_stage FROM businesses WHERE id=?", (back,)) == [("replied",)])
        check("no wa_log row", q(path, "SELECT COUNT(*) FROM wa_log")[0][0] == 0)
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
