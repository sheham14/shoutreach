"""Contacts as every business, the Unassigned and Do-not-contact views, and the
three-channel Dashboard.

Run:  python tests/test_contacts_hub.py

Each channel page lists only its own leads. Contacts is the one place a
business on no channel at all can still be found -- which is the whole reason
the Unassigned view exists: a lead taken off WhatsApp, or scraped for email
with no address found, must not simply vanish.
"""
import importlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def main():
    work = tempfile.mkdtemp(prefix="contacts-hub-")
    try:
        os.environ["DB_PATH"] = os.path.join(work, "hub.db")
        os.environ["SECRET_KEY"] = "test-secret"
        import db
        importlib.reload(db)
        db.init_db()
        me = db.create_user("admin", "test-password-123", is_admin=True)
        other = db.create_user("other", "test-password-123", is_admin=False)
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True

        def client_for(uid, admin):
            c = app_mod.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = uid
                s["is_admin"] = admin
                s["csrf_token"] = "t"
            return c
        client, theirs = client_for(me, True), client_for(other, False)
        hdr = {"X-CSRF-Token": "t"}

        def biz(name):
            with db.get_db() as conn:
                return conn.execute("SELECT id FROM businesses WHERE name=? AND owner_id=?",
                                    (name, me)).fetchone()["id"]

        # One of everything.
        job = db.create_scrape_job("dentists", "Dubai", owner_id=me, destination="email")
        db.upsert_businesses([
            {"company": "Email Only", "email": "hi@emailonly.ae", "phone": "04 111 0001"},
            {"company": "Calling Only", "phone": "04 111 0002"},
            {"company": "By Hand", "phone": "04 111 0003"},
            {"company": "No Email Found", "phone": "04 111 0004", "website": "https://nef.ae",
             "status": "no_email", "source_job_id": job},
            {"company": "Opted Out", "phone": "04 111 0005"},
            {"company": "Off Calling", "phone": "04 111 0006"},
            {"company": "Unsubscribed Co", "email": "bye@unsub.ae", "phone": "04 111 0007"},
        ], owner_id=me)
        db.add_to_calling([biz("Calling Only"), biz("Off Calling")], owner_id=me)
        db.remove_from_calling([biz("Off Calling")], owner_id=me)
        with db.get_db() as conn:
            conn.execute("UPDATE businesses SET do_not_contact=1 WHERE id=?", (biz("Opted Out"),))
        db.unsubscribe_contact("bye@unsub.ae")
        wa_camp = db.create_wa_campaign("Dubai", owner_id=me, country="AE")
        db.upsert_wa_leads([{"company": "On WhatsApp", "phone": "050 111 0008"},
                            {"company": "Not On WhatsApp", "phone": "050 111 0009"}],
                           default_country="AE", owner_id=me, wa_campaign_id=wa_camp)
        not_on = next(l for l in db.get_wa_leads(owner_id=me) if l["company"] == "Not On WhatsApp")
        db.move_wa_lead(not_on["id"], "none", owner_id=me)
        db.upsert_businesses([{"company": "Somebody Else's", "phone": "04 999 0000"}], owner_id=other)

        print("\n1. CONTACTS LISTS EVERY BUSINESS, WITH ITS CHANNELS")
        page = client.get("/api/businesses?per_page=100").get_json()
        rows = {r["company"]: r for r in page["rows"]}
        check("every business of mine is listed", len(rows) == 9, str(sorted(rows)))
        check("and nobody else's", "Somebody Else's" not in rows)
        check("an email lead shows its address", rows["Email Only"]["email"] == "hi@emailonly.ae")
        check("a calling lead shows it's on Calling", rows["Calling Only"]["call_lead_id"] is not None)
        check("a WhatsApp lead shows its stage and campaign",
              rows["On WhatsApp"]["wa_stage"] == "checking" and rows["On WhatsApp"]["wa_campaign"] == "Dubai",
              str(rows["On WhatsApp"]))
        check("a lead taken off Calling no longer shows as on it",
              rows["Off Calling"]["call_lead_id"] is None)
        check("the tab counts come back", page["counts"]["all"] == 9, str(page["counts"]))

        by_channel = {c: {r["company"] for r in client.get(
            f"/api/businesses?channel={c}&per_page=100").get_json()["rows"]}
            for c in ("email", "calling", "whatsapp")}
        check("filtering to Email", by_channel["email"] == {"Email Only", "Unsubscribed Co"},
              str(by_channel["email"]))
        check("filtering to Calling", by_channel["calling"] == {"Calling Only"}, str(by_channel["calling"]))
        check("filtering to WhatsApp leaves out the one ruled out",
              by_channel["whatsapp"] == {"On WhatsApp"}, str(by_channel["whatsapp"]))

        print("\n2. UNASSIGNED: EVERY STRAY LEAD, AND WHY IT'S THERE")
        un = client.get("/api/businesses?view=unassigned&per_page=100").get_json()
        reasons = {r["company"]: r["unassigned_reason"] for r in un["rows"]}
        check("exactly the leads on no channel",
              set(reasons) == {"By Hand", "No Email Found", "Off Calling", "Not On WhatsApp"},
              str(reasons))
        check("taken off WhatsApp says so", reasons.get("Not On WhatsApp") == "not_on_whatsapp")
        check("taken off Calling says so", reasons.get("Off Calling") == "removed_calling")
        check("an email scrape with no address says so", reasons.get("No Email Found") == "no_email_found")
        check("a hand-added lead says so", reasons.get("By Hand") == "added_by_hand")
        check("with a readable label", all(r["unassigned_label"] for r in un["rows"]))
        check("an opted-out business is never offered as a stray", "Opted Out" not in reasons)
        dnc = {r["company"] for r in client.get("/api/businesses?view=dnc").get_json()["rows"]}
        check("it has its own view instead", dnc == {"Opted Out", "Unsubscribed Co"}, str(dnc))

        ids = client.get("/api/businesses/ids?view=unassigned").get_json()
        check("select-all-matching returns the same set", ids["total"] == 4, str(ids))

        print("\n3. ONE BUSINESS, EVERY CHANNEL, ONE TIMELINE")
        db.log_call(db.get_call_lead_view(biz("Calling Only"), owner_id=me)["call_lead_id"],
                    "no_answer", "rang out")
        detail = client.get(f"/api/businesses/{biz('Calling Only')}").get_json()
        check("the detail loads with its call lead", detail["call"] is not None, str(detail)[:120])
        check("and the call is on the timeline",
              any(t["channel"] == "calling" and "No answer" in t["text"] for t in detail["timeline"]),
              str(detail["timeline"]))
        wa_lead = next(l for l in db.get_wa_leads(owner_id=me) if l["company"] == "On WhatsApp")
        db.mark_wa_sent(wa_lead["id"], "Hello there")
        detail = client.get(f"/api/businesses/{wa_lead['business_id']}").get_json()
        check("so is a WhatsApp message",
              any(t["channel"] == "whatsapp" and t["detail"] == "Hello there" for t in detail["timeline"]))
        check("another operator can't open it",
              theirs.get(f"/api/businesses/{wa_lead['business_id']}").status_code == 404)

        print("\n4. EDITING A BUSINESS")
        r = client.put(f"/api/businesses/{wa_lead['business_id']}", headers=hdr,
                       json={"phone": "055 222 3333", "city": "Dubai", "notes": "ask for Rana"})
        check("the edit saves", r.status_code == 200, str(r.get_json()))
        check("a new phone number is re-formatted for WhatsApp",
              db.get_wa_lead(wa_lead["id"])["wa_number"] == "971552223333",
              db.get_wa_lead(wa_lead["id"])["wa_number"])
        r = client.put(f"/api/businesses/{biz('Email Only')}", headers=hdr, json={"name": ""})
        check("a business can't lose its name", r.status_code == 400)
        r = client.put(f"/api/businesses/{biz('Unsubscribed Co')}", headers=hdr,
                       json={"do_not_contact": False})
        check("an unsubscribe can't be undone from here", r.status_code == 400, str(r.get_json()))
        r = client.put(f"/api/businesses/{biz('Opted Out')}", headers=hdr,
                       json={"do_not_contact": False})
        check("a do-not-contact set by hand can be cleared",
              r.status_code == 200 and db.get_business(biz("Opted Out"))["do_not_contact"] == 0)
        r = theirs.put(f"/api/businesses/{biz('By Hand')}", headers=hdr, json={"notes": "mine"})
        check("another operator can't edit it", r.status_code == 404)

        print("\n5. ADDING BY HAND, AND ENROLLING FROM CONTACTS")
        r = client.post("/api/businesses", headers=hdr,
                        json={"name": "Typed In Clinic", "phone": "04 333 0001", "email": "t@typed.ae"})
        body = r.get_json()
        check("a business can be added by hand", r.status_code == 200 and body["created"], str(body))
        again = client.post("/api/businesses", headers=hdr,
                            json={"name": "Typed In Clinic", "phone": "04 333 0001"}).get_json()
        check("adding it twice finds the same business", again["id"] == body["id"] and not again["created"],
              str(again))
        camp = db.create_campaign("Intro", owner_id=me)
        db.upsert_step(camp, 1, "Hi", "Body", 0)
        r = client.post("/api/businesses/enroll", headers=hdr,
                        json={"campaign_id": camp, "business_ids": [body["id"], biz("By Hand")]})
        res = r.get_json()
        check("businesses with an address are enrolled, the rest counted",
              res["enrolled"] == 1 and res["no_email"] == 1, str(res))
        r = theirs.post("/api/businesses/enroll", headers=hdr,
                        json={"campaign_id": camp, "business_ids": [body["id"]]})
        check("nobody else can enroll into your campaign", r.status_code == 404)

        print("\n6. DELETING KEEPS ANYONE WHO ASKED TO BE LEFT ALONE")
        doomed = [biz("By Hand"), biz("Unsubscribed Co")]
        res = client.post("/api/businesses/delete", headers=hdr, json={"business_ids": doomed}).get_json()
        check("an ordinary lead is deleted, an unsubscribed one kept",
              res["deleted"] == 1 and res["kept"] == 1, str(res))
        check("the unsubscribed business is still there to suppress re-imports",
              db.get_business(biz("Unsubscribed Co")) is not None)
        calling_id = biz("Calling Only")
        res = theirs.post("/api/businesses/delete", headers=hdr,
                          json={"business_ids": [calling_id]}).get_json()
        check("another operator can't delete it", res["deleted"] == 0 and db.get_business(calling_id))

        print("\n7. THE DASHBOARD COVERS EVERY CHANNEL")
        dash = client.get("/api/dashboard").get_json()
        check("it has all three channels", all(k in dash for k in ("email", "calling", "whatsapp")),
              str(list(dash)))
        check("and today's to-do", {"wa_review", "wa_due", "calls_due"} <= set(dash["todo"]),
              str(dash["todo"]))
        channels = {c["channel"] for c in dash["campaigns"]}
        check("every campaign is in one list, tagged by channel",
              channels == {"email", "whatsapp"}, str(dash["campaigns"]))
        db.create_call_campaign("Batch", owner_id=me)
        dash = client.get("/api/dashboard").get_json()
        check("including calling campaigns", "calling" in {c["channel"] for c in dash["campaigns"]})
        check("WhatsApp numbers count what was sent", dash["whatsapp"]["messaged"] == 1,
              str(dash["whatsapp"]))
        other_dash = theirs.get("/api/dashboard").get_json()
        check("the other operator's dashboard shows none of it",
              other_dash["campaigns"] == [] and "Dubai" not in json.dumps(other_dash),
              json.dumps(other_dash)[:160])

        print("\n8. SETTINGS DON'T LEAK ANYONE'S OWN COPY")
        db.save_settings({f"wa_template_gap:{me}": "my private pitch", "daily_limit": "50"})
        settings = theirs.get("/api/settings").get_json()
        check("per-operator settings are not in the shared settings response",
              not any(":" in k for k in settings) and "my private pitch" not in json.dumps(settings),
              str(sorted(settings))[:160])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
