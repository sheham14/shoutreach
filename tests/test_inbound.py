"""The two doors in from outside: website enquiries and Cal.com (Plan 2).

Run:  python tests/test_inbound.py     (exits 0 on pass, 1 on failure)

Contract A: a website posts a lead with its own key (X-API-Key). Contract B:
Cal.com posts a booking with the body signed (X-Cal-Signature-256, HMAC-SHA256
with the webhook secret). Neither has a session or a CSRF token; each checks
its own credential, and neither credential works anywhere else.
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


LEAD = {"site": "hexiv.co.uk", "name": "Sam Smith", "email": "Sam@Example.co.uk",
        "business": "Smith Dental", "phone": "07700 900123", "website": "smithdental.co.uk",
        "country": "GB", "answers": {"What do you do?": "Dental clinic"},
        "summary": "Dental clinic, wants more bookings"}


def test_enquiry_becomes_lead(db, path, anon, ca, co, hdr, web, admin, other):
    print("\n9. AN ENQUIRY BECOMES A LEAD (PACKAGE 2)")
    with db.get_db() as conn:   # start clean: sections above stored enquiries for nobody
        conn.execute("DELETE FROM website_enquiries")
    ca.put("/api/website-settings", headers=hdr, json={
        "sites": {"hexiv.co.uk": {"owner_id": other, "country": "GB"},
                  "hexiv.co": {"owner_id": 0, "country": ""}}})
    lead = {**LEAD, "email": "jo@brightsmiles.co.uk", "name": "Jo Bright", "business": "Bright Smiles",
            "phone": "07700 900456", "website": "brightsmiles.co.uk",
            "answers": {"What kind of business?": "Dental clinic", "Biggest challenge": "Not enough bookings"}}
    # Sent as raw JSON: the test client's json= sorts keys, and the order the
    # form asked its questions in is part of what's being checked.
    r = anon.post("/api/inbound/website-lead", data=json.dumps(lead),
                  headers={**web, "Content-Type": "application/json"})
    check("accepted", r.status_code == 200, str(r.status_code))
    rows = q(path, "SELECT owner_id, business_id FROM website_enquiries WHERE email='jo@brightsmiles.co.uk'")
    owner, bid = rows[0] if rows else (None, None)
    check("filed for the site's owner, from the setting", owner == other and bid, str(rows))
    check("the business is theirs", q(path, "SELECT owner_id, name FROM businesses WHERE id=?", (bid,))
          == [(other, "Bright Smiles")])
    check("the email is an email lead with the person's first name",
          q(path, "SELECT first_name, last_name, status FROM email_leads WHERE business_id=?", (bid,))
          == [("Jo", "Bright", "active")])
    check("stage: Enquired on the website, via the website",
          q(path, "SELECT pipeline_stage, pipeline_channel FROM businesses WHERE id=?", (bid,))
          == [("enquired", "website")])
    check("the stage is a built-in with that label",
          q(path, "SELECT label, is_builtin FROM pipeline_stages WHERE key='enquired'")
          == [("Enquired on the website", 1)])
    src = co.get("/api/contacts/sources").get_json()
    site_src = [x for x in src if x.get("name") == "Website — hexiv.co.uk"]
    check("the site has one named import the From filter offers", len(site_src) == 1, str(src)[:200])
    page = co.get(f"/api/businesses?source_job_id={site_src[0]['id'] if site_src else 0}").get_json()
    check("and filtering by it finds the lead",
          any(b.get("id") == bid for b in (page.get("rows") if isinstance(page, dict) else page) or []),
          str(page)[:200])
    d = co.get(f"/api/businesses/{bid}").get_json()
    check("the lead panel gets the enquiry with its answers",
          d.get("enquiries") and d["enquiries"][0]["answers"] ==
          [["What kind of business?", "Dental clinic"], ["Biggest challenge", "Not enough bookings"]],
          str(d.get("enquiries"))[:200])

    print("   the wall")
    check("the other operator gets 404 for the lead", ca.get(f"/api/businesses/{bid}").status_code == 404)
    listing = ca.get("/api/businesses?per_page=500").get_json()
    rows_ = listing.get("rows") if isinstance(listing, dict) else listing
    check("and it's absent from their Contacts", all(b.get("id") != bid for b in rows_ or []))
    check("and from their From list", all(x.get("name") != "Website — hexiv.co.uk"
                                          for x in ca.get("/api/contacts/sources").get_json()))
    check("and their own leads carry no enquiries",
          all(not db.get_business_enquiries(b, owner_id=admin) for (b,) in
              q(path, "SELECT id FROM businesses WHERE owner_id=?", (admin,))))

    print("   a repeat post, a second person, matching, suppression")
    anon.post("/api/inbound/website-lead", json={**lead, "answers": {}}, headers=web)
    check("a repeat post keeps one business and one enquiry, and its answers",
          q(path, "SELECT COUNT(*) FROM businesses WHERE name='Bright Smiles'")[0][0] == 1
          and len(co.get(f"/api/businesses/{bid}").get_json()["enquiries"]) == 1
          and co.get(f"/api/businesses/{bid}").get_json()["enquiries"][0]["answers"])

    db.upsert_businesses([{"company": "Harbour Physio", "phone": "+44 7700 900777",
                           "country": "GB"}], owner_id=other)
    held = q(path, "SELECT id FROM businesses WHERE name='Harbour Physio'")[0][0]
    anon.post("/api/inbound/website-lead", json={**LEAD, "email": "kim@harbourphysio.co.uk",
              "name": "Kim", "business": "Harbour Physio Ltd", "phone": "07700 900777", "country": ""},
              headers=web)
    got = q(path, "SELECT business_id FROM website_enquiries WHERE email='kim@harbourphysio.co.uk'")
    check("a local UK number matches the same number held in international form (site country GB)",
          got == [(held,)], f"{got} vs {held}")

    db.unsubscribe_contact("stop@nomail.co.uk")
    anon.post("/api/inbound/website-lead", json={**LEAD, "email": "stop@nomail.co.uk", "name": "Stop",
              "business": "No Mail Ltd"}, headers=web)
    check("a suppressed address is still recorded as an enquiry",
          q(path, "SELECT COUNT(*) FROM website_enquiries WHERE email='stop@nomail.co.uk'")[0][0] == 1)
    check("but its email lead is suppressed",
          q(path, "SELECT status FROM email_leads WHERE email='stop@nomail.co.uk'") == [("unsubscribed",)])

    with db.get_db() as conn:
        conn.execute("UPDATE businesses SET pipeline_stage='proposal_sent' WHERE id=?", (bid,))
    anon.post("/api/inbound/website-lead", json={**lead, "summary": "again"}, headers=web)
    check("an enquiry never moves a deal backwards",
          q(path, "SELECT pipeline_stage FROM businesses WHERE id=?", (bid,)) == [("proposal_sent",)])

    print("   an enquiry on a business its owner already holds (review item 6)")
    db.upsert_businesses([{"company": "Seq Clinic", "email": "info@seqclinic.co.uk", "phone": "+44 7700 900500",
                           "country": "GB"}], owner_id=other)
    seq_lead = q(path, "SELECT id FROM email_leads WHERE email='info@seqclinic.co.uk'")[0][0]
    seq_camp = db.create_campaign("Cold seq", owner_id=other)
    db.enroll_contacts_bulk(seq_camp, [seq_lead], owner_id=other)
    anon.post("/api/inbound/website-lead", headers=web, json={**LEAD, "email": "owner@seqclinic.co.uk",
              "name": "Seq Owner", "business": "Seq Clinic", "phone": "07700 900500"})
    check("it lands on that business", q(path, "SELECT b.name FROM website_enquiries e JOIN businesses b "
          "ON b.id=e.business_id WHERE e.email='owner@seqclinic.co.uk'") == [("Seq Clinic",)])
    check("and its running email sequence ends",
          q(path, "SELECT status FROM enrollments WHERE email_lead_id=?", (seq_lead,)) == [("completed",)])

    print("   a site set to nobody, then set")
    anon.post("/api/inbound/website-lead", json={**LEAD, "site": "hexiv.co", "email": "ali@clinic.ae",
              "name": "Ali", "business": "Ali Clinic", "locale": "ar"}, headers=web)
    check("kept, filed for no one, while hexiv.co has no owner",
          q(path, "SELECT owner_id, business_id FROM website_enquiries WHERE email='ali@clinic.ae'") == [(0, None)])
    r = ca.put("/api/website-settings", headers=hdr, json={"sites": {"hexiv.co": {"owner_id": admin}}})
    check("setting the owner files it", r.get_json().get("filed") == 1 and
          q(path, "SELECT owner_id FROM website_enquiries WHERE email='ali@clinic.ae'") == [(admin,)])

    print("   deleting the lead")
    r = co.post("/api/businesses/delete", headers=hdr, json={"business_ids": [bid]})
    check("the owner deletes the lead", r.status_code == 200 and r.get_json().get("deleted") == 1, str(r.get_json()))
    check("takes its enquiry with it", q(path, "SELECT COUNT(*) FROM website_enquiries WHERE business_id=?",
                                         (bid,))[0][0] == 0)
    group = q(path, "SELECT delete_group FROM deleted_leads WHERE business_id=?", (bid,))
    co.post("/api/trash/restore", headers=hdr, json={"group": group[0][0] if group else ""})
    check("and Recently deleted brings it back",
          q(path, "SELECT COUNT(*) FROM website_enquiries WHERE business_id=?", (bid,))[0][0] == 1)

    print("   the stage's own migration")
    with db.get_db() as conn:
        conn.execute("UPDATE pipeline_stages SET label='Web lead' WHERE key='enquired'")
    db.init_db()
    check("re-running startup doesn't re-seed a renamed built-in",
          q(path, "SELECT label FROM pipeline_stages WHERE key='enquired'") == [("Web lead",)])


def main():
    work = tempfile.mkdtemp(prefix="inbound_")
    try:
        path = os.path.join(work, "inbound.db")
        os.environ["DB_PATH"] = path
        os.environ["SECRET_KEY"] = "test-secret"
        os.environ["SHOUTREACH_INSECURE_COOKIES"] = "1"
        import db
        importlib.reload(db)
        db.init_db()
        admin = db.create_user("admin", "test-password-123", is_admin=True)
        other = db.create_user("other", "test-password-123", is_admin=False)
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True
        anon = app_mod.app.test_client()

        def client_for(uid):
            c = app_mod.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = uid
                s["csrf_token"] = "t"
            return c
        ca, co = client_for(admin), client_for(other)
        hdr = {"X-CSRF-Token": "t"}

        print("\n1. THE WEBSITE KEY")
        r = anon.post("/api/inbound/website-lead", json=LEAD)
        check("before any key exists, nothing gets in", r.status_code == 401, str(r.status_code))
        r = co.post("/api/website-settings/key", headers=hdr)
        check("a non-admin can't make the key", r.status_code == 403, str(r.status_code))
        r = ca.post("/api/website-settings/key", headers=hdr)
        key = (r.get_json() or {}).get("key", "")
        check("an admin makes one, shown once", r.status_code == 200 and key.startswith("web_"))
        stored = q(path, "SELECT value FROM settings WHERE key='_website_key_sha256'")
        check("only its digest is stored", stored and stored[0][0] == hashlib.sha256(key.encode()).hexdigest()
              and key not in json.dumps(q(path, "SELECT key, value FROM settings")))
        check("GET /api/settings shows neither the key nor the secret's setting",
              not any(k.startswith("_website") or k.startswith("_cal") for k in ca.get("/api/settings").get_json()))
        check("the settings card says it is set, without showing it",
              ca.get("/api/website-settings").get_json().get("has_website_key") is True
              and key not in ca.get("/api/website-settings").get_data(as_text=True))

        print("\n2. WHO MAY POST A LEAD")
        r = anon.post("/api/inbound/website-lead", json=LEAD)
        check("no key -> 401", r.status_code == 401, str(r.status_code))
        r = anon.post("/api/inbound/website-lead", json=LEAD, headers={"X-API-Key": "web_wrong"})
        check("wrong key -> 401", r.status_code == 401, str(r.status_code))
        worker_key = db.get_or_create_worker_key(admin)
        r = anon.post("/api/inbound/website-lead", json=LEAD, headers={"X-API-Key": worker_key})
        check("a worker key -> 401", r.status_code == 401, str(r.status_code))
        r = anon.post("/api/contacts/import", json={"rows": [{"email": "x@y.co"}]}, headers={"X-API-Key": key})
        check("and the website key doesn't work on the worker's import", r.status_code in (401, 403), str(r.status_code))
        r = anon.post("/api/scraper/claim", json={}, headers={"X-API-Key": key})
        check("nor on the worker's claim", r.status_code == 401, str(r.status_code))
        r = co.post("/api/inbound/website-lead", json=LEAD, headers=hdr)
        check("a signed-in session is not a website key", r.status_code == 401, str(r.status_code))

        web = {"X-API-Key": key}
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "site": "evil.example"}, headers=web)
        check("an unknown site -> 400", r.status_code == 400, str(r.status_code))
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "email": ""}, headers=web)
        check("no email -> 400", r.status_code == 400, str(r.status_code))
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "name": ""}, headers=web)
        check("no name -> 400", r.status_code == 400, str(r.status_code))
        r = anon.post("/api/inbound/website-lead", data="not json", headers={**web, "Content-Type": "application/json"})
        check("not JSON -> 400", r.status_code == 400, str(r.status_code))
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "summary": "x" * 40000}, headers=web)
        check("over 32 KB -> 413", r.status_code == 413, str(r.status_code))

        print("\n3. A GOOD POST, AND THE SAME ONE AGAIN")
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "unknown_field": "ignored",
                                                         "business": "B" * 999}, headers=web)
        check("accepted with {'ok': true} and nothing echoed", r.status_code == 200 and r.get_json() == {"ok": True},
              f"{r.status_code} {r.get_data(as_text=True)[:80]}")
        rows = q(path, "SELECT site, email, name, business_name, country, answers FROM website_enquiries")
        check("one enquiry, email lower-cased, strings capped",
              len(rows) == 1 and rows[0][1] == "sam@example.co.uk" and len(rows[0][3]) == 200, str(rows)[:160])
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "phone": "", "summary": "Second look"}, headers=web)
        rows = q(path, "SELECT phone, summary FROM website_enquiries")
        check("a repeat post doesn't duplicate", r.status_code == 200 and len(rows) == 1, str(rows))
        check("it updates, keeping what the second post left empty",
              rows and rows[0] == ("07700 900123", "Second look"), str(rows))
        anon.post("/api/inbound/website-lead", json={**LEAD, "site": "hexiv.co"}, headers=web)
        check("the same email from the other site is its own enquiry",
              q(path, "SELECT COUNT(*) FROM website_enquiries")[0][0] == 2)

        print("\n4. CAL.COM'S SIGNATURE")
        body = json.dumps({"hello": "cal"}).encode()
        r = anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": "00"})
        check("no secret set yet -> 401", r.status_code == 401, str(r.status_code))
        r = co.post("/api/website-settings/cal-secret", headers=hdr)
        check("a non-admin can't make the secret", r.status_code == 403)
        secret = ca.post("/api/website-settings/cal-secret", headers=hdr).get_json().get("secret", "")
        check("an admin makes one, shown once", len(secret) > 20)
        good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": "0" * 64})
        check("a bad signature -> 401", r.status_code == 401, str(r.status_code))
        r = anon.post("/api/inbound/cal", data=body + b" ", headers={"X-Cal-Signature-256": good})
        check("a changed body -> 401", r.status_code == 401, str(r.status_code))
        r = anon.post("/api/inbound/cal", data=body)
        check("no signature -> 401", r.status_code == 401, str(r.status_code))
        r = anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": "é" * 64})
        check("a signature with non-ASCII characters -> 401, not 500 (review item 7)",
              r.status_code == 401, str(r.status_code))
        r = anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": "sha256=" + good.upper()})
        check("the right signature, prefixed and upper-case, still -> 200", r.status_code == 200, str(r.status_code))
        r = anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": good})
        check("the right signature -> 200, no session or CSRF needed", r.status_code == 200, str(r.status_code))
        check("stored as received", q(path, "SELECT body FROM cal_events") == [(body.decode(),)])
        anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": good})
        check("Cal.com's retry of the same body is stored once",
              q(path, "SELECT COUNT(*) FROM cal_events")[0][0] == 1)
        big = b"{" + b" " * (300 * 1024) + b"}"
        r = anon.post("/api/inbound/cal", data=big,
                      headers={"X-Cal-Signature-256": hmac.new(secret.encode(), big, hashlib.sha256).hexdigest()})
        check("an oversize delivery -> 413", r.status_code == 413, str(r.status_code))
        r = anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": good, "X-API-Key": key})
        check("the website key isn't needed or used here", r.status_code == 200)
        r = anon.post("/api/inbound/website-lead", json=LEAD,
                      headers={"X-Cal-Signature-256": good})
        check("and a Cal.com signature doesn't open the website door", r.status_code == 401)

        r = ca.get("/api/website-settings/cal-events")
        check("an admin can download what Cal.com sent, as received",
              r.status_code == 200 and json.loads(r.data)[0]["body"] == {"hello": "cal"}
              and "attachment" in r.headers.get("Content-Disposition", ""))
        check("a non-admin can't", co.get("/api/website-settings/cal-events").status_code == 403)

        print("\n5. NOTHING ELSE OPENED UP")
        for path_ in ("/api/inbound/website-leadx", "/api/inbound/", "/api/inbound/cal/extra"):
            r = anon.post(path_, json=LEAD, headers=web)
            check(f"{path_} still needs sign-in", r.status_code == 401, str(r.status_code))
        check("the website settings are admin-only", co.get("/api/website-settings").status_code == 403)
        check("signed out, they need sign-in", anon.get("/api/website-settings").status_code == 401)

        print("\n6. THE SETTINGS")
        r = ca.put("/api/website-settings", headers=hdr, json={
            "sites": {"hexiv.co.uk": {"owner_id": other, "country": "gb"}},
            "wa_wait_minutes": 45, "email_wait_hours": 12,
            "cal_booking_link": "https://cal.com/hexiv/free-strategy-call"})
        cfg = ca.get("/api/website-settings").get_json()
        check("an admin sets who receives a site's leads, its country, the waits and the link",
              r.status_code == 200 and cfg["sites"]["hexiv.co.uk"] == {"owner_id": other, "country": "GB"}
              and cfg["wa_wait_minutes"] == 45 and cfg["email_wait_hours"] == 12
              and cfg["cal_booking_link"].startswith("https://cal.com/"), str(cfg)[:200])
        check("the other site keeps its defaults", cfg["sites"]["hexiv.co"] == {"owner_id": 0, "country": ""})
        check("the defaults are 30 minutes and 24 hours",
              db.WEBSITE_DEFAULTS["wa_wait_minutes"] == 30 and db.WEBSITE_DEFAULTS["email_wait_hours"] == 24)
        for bad in ({"sites": {"hexiv.co": {"owner_id": 999}}}, {"sites": {"hexiv.co": {"country": "GBR"}}},
                    {"wa_wait_minutes": "soon"}, {"email_wait_hours": -1},
                    {"cal_booking_link": "http://insecure.example"}):
            r = ca.put("/api/website-settings", headers=hdr, json=bad)
            check(f"refused: {bad}", r.status_code == 400, str(r.get_json()))
        check("the addresses to paste are offered",
              cfg["cal_webhook_url"].endswith("/api/inbound/cal")
              and cfg["website_lead_url"].endswith("/api/inbound/website-lead"))

        print("\n7. EACH OPERATOR'S FOLLOW-UP CAMPAIGNS")
        mine = db.create_campaign("Mine", owner_id=other)
        theirs = db.create_campaign("Theirs", owner_id=admin)
        wa = db.create_wa_campaign("WA", owner_id=other)
        wa = wa[0] if isinstance(wa, tuple) else wa
        check("unset by default", co.get("/api/website-followup").get_json() ==
              {"email_campaign_id": None, "wa_campaign_id": None})
        r = co.put("/api/website-followup", headers=hdr, json={"email_campaign_id": mine, "wa_campaign_id": wa})
        check("an operator picks their own campaigns", r.status_code == 200 and
              co.get("/api/website-followup").get_json() == {"email_campaign_id": mine, "wa_campaign_id": wa})
        r = co.put("/api/website-followup", headers=hdr, json={"email_campaign_id": theirs})
        check("another operator's campaign is refused", r.status_code == 400, str(r.status_code))
        check("and each sees only their own choice",
              ca.get("/api/website-followup").get_json() == {"email_campaign_id": None, "wa_campaign_id": None})
        r = co.put("/api/website-followup", headers=hdr, json={"email_campaign_id": None, "wa_campaign_id": ""})
        check("either may be left unset", r.status_code == 200 and
              co.get("/api/website-followup").get_json() == {"email_campaign_id": None, "wa_campaign_id": None})

        print("\n8. LIMITS")
        app_mod._inbound_hits.clear()
        codes = [anon.post("/api/inbound/website-lead", json=LEAD, headers=web).status_code
                 for _ in range(app_mod._INBOUND_PER_MINUTE + 1)]
        check("more than the per-minute limit -> 429", codes[-1] == 429 and codes[0] == 200, str(codes[-3:]))
        app_mod._inbound_hits.clear()
        r = ca.post("/api/website-settings/key", headers=hdr)
        r2 = anon.post("/api/inbound/website-lead", json=LEAD, headers=web)
        check("rotating the key stops the old one", r2.status_code == 401, str(r2.status_code))

        app_mod._inbound_hits.clear()
        key = ca.post("/api/website-settings/key", headers=hdr).get_json()["key"]
        web = {"X-API-Key": key}
        test_enquiry_becomes_lead(db, path, anon, ca, co, hdr, web, admin, other)
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
