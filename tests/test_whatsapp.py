"""WhatsApp module: phone formatting for any country, leads that arrive ready
to send, messages that follow the current template, A/B versions, the
follow-up cadence, and the manual-send boundary.

Run:  python tests/test_whatsapp.py

The one property every section here ultimately protects: nothing in this
module can cause a WhatsApp message to be sent. Every "send" path is asserted
to stop at recording that a link would have been opened.
"""
import importlib
import inspect
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def boot(work):
    os.environ["DB_PATH"] = os.path.join(work, "wa.db")
    os.environ["SECRET_KEY"] = "test-secret"
    import db
    importlib.reload(db)
    db.init_db()
    db.create_user("admin", "testpassword123", is_admin=True)
    import app as app_mod
    importlib.reload(app_mod)
    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()
    r = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.get_data(as_text=True)).group(1)
    client.post("/login", data={"username": "admin", "password": "testpassword123",
                                "csrf_token": csrf})
    token = client.get("/api/csrf").get_json()["csrf_token"]
    return db, app_mod, client, token


def api(client, token, method, url, **kw):
    fn = getattr(client, method)
    headers = kw.pop("headers", {})
    headers["X-CSRF-Token"] = token
    r = fn(url, headers=headers, **kw)
    return r.status_code, r.get_json()


def lead_named(db, name):
    return next(l for l in db.get_wa_leads(include_inactive=True, with_message=True)
                if l["company"] == name)


def test_phone_formatting(db, client, token):
    print("\n1. PHONE NUMBERS: ANY COUNTRY, BY THAT COUNTRY'S OWN RULES")
    cases = [
        ("050 123 4567", "AE", "971501234567"),       # UAE mobile, trunk 0 dropped
        ("+971 50 123 4567", "AE", "971501234567"),   # already international
        ("00971501234567", "AE", "971501234567"),     # 00-prefixed international
        ("04 339 2045", "AE", "97143392045"),         # UAE landline
        ("5512 3456", "QA", "97455123456"),           # Qatar, no trunk prefix at all
        ("+974 5512 3456", "QA", "97455123456"),
        ("055 123 4567", "SA", "966551234567"),       # Saudi Arabia
        ("07700 900123", "GB", "447700900123"),       # UK drops its 0
        ("06 6982 4321", "IT", "390669824321"),       # Italy keeps its 0
        ("98765 43210", "IN", "919876543210"),        # India
    ]
    for raw, country, want in cases:
        got = db.format_whatsapp_number(raw, country)
        check(f"'{raw}' ({country}) -> {want}", got == want, f"got {got}")
    check("no digits at all yields nothing", db.format_whatsapp_number("call us", "AE") == "")
    check("an unknown country returns the digits as-is, not a guess",
          db.format_whatsapp_number("050 123 4567", "XX") == "0501234567")
    check("an international number is recognised with no country given",
          db.format_whatsapp_number("971501234567", "") == "971501234567")

    print("\n2. MOBILE OR LANDLINE, FOR ANY COUNTRY (A SOFT SIGNAL ONLY)")
    check("UAE mobile", db.classify_number_type("050 123 4567", "AE") == "mobile")
    check("UAE landline", db.classify_number_type("04 339 2045", "AE") == "landline")
    check("Qatar mobile", db.classify_number_type("5512 3456", "QA") == "mobile")
    check("Qatar landline", db.classify_number_type("4412 3456", "QA") == "landline")
    check("Saudi mobile", db.classify_number_type("055 123 4567", "SA") == "mobile")
    check("no country is 'unknown', not a guess",
          db.classify_number_type("050 123 4567", "") == "unknown")

    s, body = api(client, token, "get", "/api/countries")
    codes = {c["code"]: c["dial"] for c in body["countries"]}
    check("the country list covers the world", len(codes) > 200 and codes.get("SA") == "966"
          and codes.get("GB") == "44", str(len(codes)))


def test_ready_on_arrival(db, client, token, camp):
    print("\n3. A LEAD ARRIVES READY TO SEND, ITS MESSAGE ALREADY WRITTEN")
    api(client, token, "patch", f"/api/wa/campaigns/{camp}", json={
        "templates": {"opener": ["Hi {{business_name}} in {{city|your area}}, version A",
                                 "Hello {{business_name}}, version B"]},
        "variables": {"my_name": "Sam"},
    })
    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": f"Clinic {i}", "phone": f"050 100 00{i:02d}", "city": "Dubai"}
                 for i in range(4)] + [{"company": "No Phone Clinic", "website": "https://np.ae"}],
        "country": "AE", "wa_campaign_id": camp,
    })
    check("the import succeeds", s == 200 and body["inserted"] == 4, str(body))
    ready = db.get_wa_ready()
    names = [l["company"] for l in ready]
    check("every lead with a phone is in Ready to send, no review step",
          names == [f"Clinic {i}" for i in range(4)], str(names))
    check("a row with no phone isn't put on WhatsApp",
          all(l["company"] != "No Phone Clinic" for l in db.get_wa_leads(include_inactive=True)))
    labels = [l["template_variant"] for l in ready]
    check("versions alternate A, B, A, B", labels == ["A", "B", "A", "B"], str(labels))
    check("each message is written from its own version",
          ready[0]["message"] == "Hi Clinic 0 in Dubai, version A"
          and ready[1]["message"] == "Hello Clinic 1, version B", str([l["message"] for l in ready[:2]]))
    check("nothing is saved as text -- the message reads the template",
          all(not l["draft_message"] and not l["message_edited"] for l in ready))

    print("\n4. A TEMPLATE CHANGE REACHES EVERY UNSENT LEAD; HAND EDITS ARE KEPT")
    c0, c1 = lead_named(db, "Clinic 0"), lead_named(db, "Clinic 2")
    s, body = api(client, token, "put", f"/api/wa/leads/{c1['id']}/message",
                  json={"message": "My own words for Clinic 2"})
    check("editing one lead's message saves", s == 200, str(body))
    api(client, token, "patch", f"/api/wa/campaigns/{camp}", json={
        "templates": {"opener": ["Rewritten A for {{business_name}}", "Hello {{business_name}}, version B"]},
    })
    check("the unsent lead now reads the new wording",
          lead_named(db, "Clinic 0")["message"] == "Rewritten A for Clinic 0",
          lead_named(db, "Clinic 0")["message"])
    check("the hand-edited lead keeps its own words",
          lead_named(db, "Clinic 2")["message"] == "My own words for Clinic 2")
    s, body = api(client, token, "delete", f"/api/wa/leads/{c1['id']}/message")
    check("reset brings the template back", s == 200 and body["message"] == "Rewritten A for Clinic 2",
          str(body))
    s, body = api(client, token, "put", f"/api/wa/leads/{c0['id']}/message", json={"message": "  "})
    check("an empty message is refused", s == 400, str(body))

    print("\n5. AI REWORDING NEEDS AI SWITCHED ON, AND SAYS SO")
    s, body = api(client, token, "post", f"/api/wa/leads/{c0['id']}/reword", json={})
    check("without AI it explains rather than failing silently",
          s == 400 and "not enabled" in (body.get("error") or ""), f"{s} {body}")


def test_send_and_versions(db, client, token, camp):
    print("\n6. 'SENT' MEANS THE LINK WAS OPENED -- AND IT'S CORRECTABLE")
    c0 = lead_named(db, "Clinic 0")
    s, body = api(client, token, "post", f"/api/wa/leads/{c0['id']}/sent",
                  json={"kind": "opener", "message": "What was actually in the box"})
    check("marking sent succeeds", s == 200, str(body))
    lead = lead_named(db, "Clinic 0")
    check("the lead leaves Ready to send", lead["wa_status"] == "sent" and lead["sent_date"])
    with db.get_db() as conn:
        logged = conn.execute("SELECT kind, message, template_variant FROM wa_log WHERE wa_lead_id=?",
                              (c0["id"],)).fetchall()
    check("the log keeps exactly what was sent, and under which version",
          len(logged) == 1 and logged[0]["message"] == "What was actually in the box"
          and logged[0]["template_variant"] == "A", str([dict(r) for r in logged]))
    s, summary = api(client, token, "get", "/api/wa/summary?since=2000-01-01 00:00:00")
    check("the sent-today counter counts it", summary["sent_today"] == 1, str(summary))
    s, later = api(client, token, "get", "/api/wa/summary?since=2999-01-01 00:00:00")
    check("and only counts from the start of the operator's day", later["sent_today"] == 0)

    s, body = api(client, token, "put", f"/api/wa/leads/{c0['id']}/sent-date", json={"sent_date": None})
    check("'didn't actually send' is accepted", s == 200)
    check("and puts the lead back in Ready to send",
          any(l["id"] == c0["id"] for l in db.get_wa_ready()))
    api(client, token, "post", f"/api/wa/leads/{c0['id']}/sent", json={"kind": "opener"})
    check("opening it again sends it", db.get_wa_lead(c0["id"])["wa_status"] == "sent")

    print("\n7. DELETING A VERSION RE-DEALS ITS UNSENT LEADS; SENT ONES KEEP THEIRS")
    sent_b = lead_named(db, "Clinic 1")
    api(client, token, "post", f"/api/wa/leads/{sent_b['id']}/sent", json={"kind": "opener"})
    api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": "Clinic 4", "phone": "050 100 0004"}, {"company": "Clinic 5", "phone": "050 100 0005"}],
        "country": "AE", "wa_campaign_id": camp})
    unsent_b = [l for l in db.get_wa_ready(wa_campaign_id=camp) if l["template_variant"] == "B"]
    check("(fixture) there are unsent leads on version B", bool(unsent_b),
          str([l["template_variant"] for l in db.get_wa_ready()]))
    s, body = api(client, token, "patch", f"/api/wa/campaigns/{camp}", json={
        "templates": {"opener": ["Only version left, for {{business_name}}"]}, "opener_from": ["A"]})
    check("the version is removed", s == 200, str(body))
    ready = db.get_wa_ready(wa_campaign_id=camp)
    check("every unsent lead now reads the version that's left",
          all(l["message"] == f"Only version left, for {l['company']}"
              for l in ready if not l["message_edited"]),
          str([l["message"] for l in ready]))
    check("a lead already sent on B keeps B", db.get_wa_lead(sent_b["id"])["template_variant"] == "B")

    api(client, token, "patch", f"/api/wa/campaigns/{camp}", json={
        "templates": {"opener": ["One {{business_name}}", "Two {{business_name}}", "Three {{business_name}}"]},
        "opener_from": ["A", None, None]})
    api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": f"Batch {i}", "phone": f"050 200 00{i:02d}"} for i in range(6)],
        "country": "AE", "wa_campaign_id": camp})
    with db.get_db() as conn:
        counts = dict(conn.execute("""SELECT template_variant, COUNT(*) FROM wa_leads
                                       WHERE wa_campaign_id=? GROUP BY template_variant""", (camp,)).fetchall())
    check("new leads go to the versions with the fewest leads, evening them out",
          counts.get("B", 0) >= 2 and counts.get("C", 0) >= 2, str(counts))

    print("\n8. THE MANUAL-SEND BOUNDARY")
    import app as app_mod
    wa_routes = [fn for name, fn in vars(app_mod).items() if name.startswith("api_wa_") and callable(fn)]
    sources = "".join(inspect.getsource(fn) for fn in wa_routes)
    check("no WhatsApp route contacts WhatsApp", "wa.me" not in sources
          and "whatsapp.com" not in sources.lower() and "requests." not in sources)
    import scheduler
    check("nothing WhatsApp-related runs in the background loop",
          "wa_" not in inspect.getsource(scheduler._run_loop))


def test_followup_cadence(db, client, token, camp):
    print("\n9. FOLLOW-UPS ARE INFINITE, FROM THE LEAD'S OWN VERSION")
    wid = lead_named(db, "Clinic 1")["id"]
    api(client, token, "patch", f"/api/wa/campaigns/{camp}", json={
        "templates": {"followup": ["Follow A {{business_name}}", "Follow B {{business_name}}"]}})
    with db.get_db() as conn:
        conn.execute("UPDATE wa_leads SET sent_date=datetime('now','-10 days') WHERE id=?", (wid,))
    s, body = api(client, token, "get", "/api/wa/followups-due")
    lead_due = next((l for l in body if l["id"] == wid), None)
    check("an old-enough send is due, with its follow-up written",
          lead_due and lead_due["followup_draft"] == "Follow B Clinic 1",
          str(lead_due and lead_due["followup_draft"]))
    for i in range(4):
        s, _ = api(client, token, "post", f"/api/wa/leads/{wid}/sent",
                   json={"kind": "followup", "message": f"Follow-up #{i+1}"})
        check(f"follow-up #{i+1} is accepted with no cap", s == 200)
        with db.get_db() as conn:
            conn.execute("UPDATE wa_leads SET sent_date=datetime('now','-10 days') WHERE id=?", (wid,))
    lead = db.get_wa_lead(wid)
    check("followup_count accumulates", lead["followup_count"] == 4, str(lead["followup_count"]))
    check("and it's still due for another", any(l["id"] == wid for l in db.get_wa_followups_due()))

    print("\n10. PAUSING STOPS THE CADENCE; A REPLY STOPS IT AT ANY COUNT")
    api(client, token, "post", f"/api/wa/leads/{wid}/pause", json={"paused": True})
    check("a paused lead is not due", all(l["id"] != wid for l in db.get_wa_followups_due(days=0)))
    api(client, token, "post", f"/api/wa/leads/{wid}/pause", json={"paused": False})
    check("resuming brings it back", any(l["id"] == wid for l in db.get_wa_followups_due(days=0)))
    api(client, token, "post", f"/api/wa/leads/{wid}/replied", json={"replied": True})
    check("a replied lead drops out of the cadence",
          all(l["id"] != wid for l in db.get_wa_followups_due(days=0))
          and db.get_wa_lead(wid)["wa_status"] == "replied")


def test_not_on_whatsapp(db, client, token, camp):
    print("\n11. 'NOT ON WHATSAPP' FILES THE LEAD WHERE YOU CHOOSE")
    db.upsert_wa_leads([{"company": "Wrong Number Co", "phone": "050 555 0099"}],
                       default_country="AE", wa_campaign_id=camp)
    lead = lead_named(db, "Wrong Number Co")
    s, body = api(client, token, "post", f"/api/wa/leads/{lead['id']}/move", json={"destination": "bogus"})
    check("an unknown destination is refused", s == 400, str(body))
    call_camp = db.create_call_campaign("Moved from WhatsApp")
    s, body = api(client, token, "post", f"/api/wa/leads/{lead['id']}/move",
                  json={"destination": "call", "campaign_id": call_camp})
    check("moving to Calling succeeds", s == 200 and "call_lead_id" in body, str(body))
    check("it leaves Ready to send", all(l["id"] != lead["id"] for l in db.get_wa_ready()))
    check("and is on Calling, in the campaign picked",
          any(l["id"] == lead["business_id"] for l in db.get_call_queue("all", call_campaign_id=call_camp)))

    db.upsert_wa_leads([{"company": "No Email Clinic", "phone": "050 555 0100"}],
                       default_country="AE", wa_campaign_id=camp)
    no_email = lead_named(db, "No Email Clinic")
    s, body = api(client, token, "post", f"/api/wa/leads/{no_email['id']}/move", json={"destination": "email"})
    check("moving to Email with no address on file is refused", s == 400, str(body))
    check("and the lead stays on WhatsApp", db.get_wa_lead(no_email["id"])["moved_to"] == "")
    s, body = api(client, token, "post", f"/api/wa/leads/{no_email['id']}/move", json={"destination": "none"})
    check("'just take it off WhatsApp' works and rules the number out",
          s == 200 and db.get_wa_lead(no_email["id"])["moved_to"] == "none")
    offered = {r["company"] for r in db.search_businesses(q="No Email Clinic", not_on_channel="whatsapp")["rows"]}
    check("so the picker never offers it back", "No Email Clinic" not in offered, str(offered))


def test_campaigns_and_leads_table(db, client, token, camp):
    print("\n12. CAMPAIGNS: OWN COPY, FALLBACKS, SEPARATE FOLLOW-UP GAPS")
    s, c = api(client, token, "get", f"/api/wa/campaigns/{camp}")
    check("a campaign has one opener and one follow-up",
          set(c["templates"]) == {"opener", "followup"}, str(list(c["templates"])))
    check("the fields a template can use come from the listing",
          [f["key"] for f in c["fields"]][:3] == ["business_name", "city", "category"], str(c["fields"]))
    s, body = api(client, token, "patch", f"/api/wa/campaigns/{camp}", json={"templates": {"opener": []}})
    check("the opener can't be emptied", s == 400, str(body))
    out = db.render_wa_message("Hi {{business_name}} in {{city}}, a {{category|clinic}}. {{my_name}} here{{missing}}",
                               {"company": "Pearl Dental", "city": "Doha", "category": ""}, {"my_name": "Sam", "city": "x"})
    check("business fields, fallbacks and variables fill in", out == "Hi Pearl Dental in Doha, a clinic. Sam here", out)
    check("an unnamed business is 'there'", db.render_wa_message("Hi {{business_name}}!", {"company": ""}) == "Hi there!")

    s, body = api(client, token, "post", "/api/wa/campaigns", json={"name": "Doha", "country": "QA", "copy_from": camp})
    doha = body["id"]
    check("a campaign can start as a copy", db.get_wa_campaign(doha)["templates"] == db.get_wa_campaign(camp)["templates"])
    s, body = api(client, token, "post", "/api/wa/campaigns", json={"name": "Riyadh", "country": "SA"})
    check("any country can be a campaign's country", s == 200, str(body))
    s, body = api(client, token, "post", "/api/wa/campaigns", json={"name": "Nowhere", "country": "XX"})
    check("an unknown one is refused", s == 400, str(body))

    api(client, token, "patch", f"/api/wa/campaigns/{doha}", json={"followup_days": 10})
    db.upsert_wa_leads([{"company": "Gap Clinic", "phone": "5512 0001"}], default_country="QA", wa_campaign_id=doha)
    gap = lead_named(db, "Gap Clinic")
    db.mark_wa_sent(gap["id"], "hello")
    with db.get_db() as conn:
        conn.execute("UPDATE wa_leads SET sent_date=datetime('now','-7 days') WHERE id=?", (gap["id"],))
    check("7 days after sending isn't due in a 10-day campaign",
          all(l["id"] != gap["id"] for l in db.get_wa_followups_due()))
    s, body = api(client, token, "post", "/api/wa/leads/bulk", json={
        "action": "campaign", "wa_lead_ids": [gap["id"]], "wa_campaign_id": camp})
    check("moving it to a 3-day campaign makes it due",
          body["updated"] == 1 and any(l["id"] == gap["id"] for l in db.get_wa_followups_due()))

    print("\n13. THE LEADS TABLE: STAGES, AND TAKING A LEAD OFF")
    s, page = api(client, token, "get", "/api/wa/leads/page?per_page=500")
    stages = {r["company"]: r["stage"] for r in page["rows"]}
    check("unsent leads are 'ready'", stages.get("Batch 0") == "ready", str(stages.get("Batch 0")))
    check("a lead owed a follow-up is 'due'", stages.get("Gap Clinic") == "due", str(stages.get("Gap Clinic")))
    check("a replied lead is 'replied'", stages.get("Clinic 1") == "replied")
    check("taken-off leads aren't in the default view", "Wrong Number Co" not in stages)
    s, off = api(client, token, "get", "/api/wa/leads/page?stage=off")
    check("they have their own view", {"Wrong Number Co", "No Email Clinic"} <= {r["company"] for r in off["rows"]})

    batch0 = lead_named(db, "Batch 0")
    s, body = api(client, token, "post", "/api/wa/leads/bulk", json={"action": "remove", "wa_lead_ids": [batch0["id"]]})
    check("taking a lead off works", body["updated"] == 1 and all(l["id"] != batch0["id"] for l in db.get_wa_ready()))
    s, body = api(client, token, "post", "/api/wa/add-existing", json={
        "business_ids": [batch0["business_id"]], "country": "AE", "wa_campaign_id": doha, "confirm_conflicts": True})
    back = db.get_wa_lead(batch0["id"])
    check("adding it again brings it back, ready to send, in the new campaign",
          body.get("added") == 1 and back["removed_at"] is None and back["wa_campaign_id"] == doha
          and any(l["id"] == batch0["id"] for l in db.get_wa_ready()), f"{body}")

    s, body = api(client, token, "delete", f"/api/wa/campaigns/{doha}")
    check("deleting a campaign keeps its leads", s == 200 and body["leads_without_campaign"] >= 1, str(body))


def test_import_rules(db, client, token, camp):
    print("\n14. IMPORT: ONE BUSINESS IDENTITY ACROSS CHANNELS, A COUNTRY PER ROW")
    db.upsert_businesses([{"email": "front@sharedclinic.ae", "company": "Shared Clinic", "phone": "050 777 7777"}])
    shared = db.get_email_lead_by_email("front@sharedclinic.ae")["business_id"]
    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": "Shared Clinic", "phone": "050 777 7777"}], "country": "AE"})
    check("an import has to name its campaign", s == 400, str(body))
    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": "Shared Clinic", "phone": "050 777 7777"}], "country": "AE", "wa_campaign_id": camp})
    check("a business already on Email is held for a yes",
          body["inserted"] == 0 and body["conflicts"][0]["business_id"] == shared, str(body))
    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [body["conflicts"][0]["row"]], "country": "AE", "wa_campaign_id": camp, "confirm_conflicts": True})
    check("confirming lets it through", body["inserted"] == 1, str(body))
    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": "Riyadh Co", "phone": "055 123 4567", "country": "SA"}],
        "country": "AE", "wa_campaign_id": camp})
    riyadh = lead_named(db, "Riyadh Co")
    check("a row's own country wins", riyadh["country"] == "SA" and riyadh["wa_number"] == "966551234567",
          f"{riyadh['country']} {riyadh['wa_number']}")

    print("\n15. ADDING LEADS YOU ALREADY HAVE")
    db.upsert_businesses([{"company": "Existing With Phone", "phone": "050 111 2233"},
                          {"company": "Existing No Phone", "website": "https://enp.ae"}])
    ids = {r["company"]: r["id"] for r in db.search_businesses(q="Existing", not_on_channel="whatsapp")["rows"]}
    s, body = api(client, token, "post", "/api/wa/add-existing", json={"business_ids": list(ids.values()), "wa_campaign_id": camp})
    check("a country is required", s == 400)
    s, body = api(client, token, "post", "/api/wa/add-existing", json={"business_ids": list(ids.values()), "country": "AE"})
    check("so is a campaign", s == 400)
    s, body = api(client, token, "post", "/api/wa/add-existing", json={
        "business_ids": list(ids.values()), "country": "AE", "wa_campaign_id": camp})
    check("the one with a phone is added, the other counted",
          body.get("added") == 1 and body.get("no_phone") == 1, str(body))
    check("and it's ready to send straight away",
          any(l["company"] == "Existing With Phone" for l in db.get_wa_ready()))


def test_migration(db):
    print("\n16. LEADS WAITING FOR THE OLD REVIEW MOVE TO READY TO SEND")
    cid = db.create_wa_campaign("Legacy")
    with db.get_db() as conn:
        conn.execute("UPDATE wa_campaigns SET templates=? WHERE id=?", (
            '{"gap": ["Old gap opener for {{business_name}}"], "no_gap": ["Has booking copy"], '
            '"followup": ["Old follow-up"]}', cid))
        for name, status, draft in (("Pending Co", "", ""), ("Review Co", "signal_ready", ""),
                                    ("Confirmed Co", "confirmed", ""),
                                    ("Drafted Plain Co", "drafted", "Old gap opener for Drafted Plain Co"),
                                    ("Drafted Edited Co", "drafted", "Something I typed")):
            bid = conn.execute("INSERT INTO businesses(owner_id, name, phone) VALUES(1, ?, '050 999 0000')",
                               (name,)).lastrowid
            conn.execute("""INSERT INTO wa_leads(business_id, wa_number, wa_status, draft_message, wa_campaign_id)
                            VALUES(?, '971509990000', ?, ?, ?)""", (bid, status, draft, cid))
        conn.execute("DELETE FROM settings WHERE key='_migrated_wa_no_review'")
        db._migrate_wa_no_review(conn)
    campaign = db.get_wa_campaign(cid)
    check("the gap opener becomes the one opener",
          campaign["templates"]["opener"] == ["Old gap opener for {{business_name}}"], str(campaign["templates"]))
    with db.get_db() as conn:
        raw = conn.execute("SELECT templates FROM wa_campaigns WHERE id=?", (cid,)).fetchone()[0]
    check("the has-booking copy is kept, unused, in the database", "Has booking copy" in raw)
    ready = {l["company"]: l for l in db.get_wa_ready(wa_campaign_id=cid)}
    check("pending, reviewed and confirmed leads are all ready to send",
          {"Pending Co", "Review Co", "Confirmed Co"} <= set(ready), str(sorted(ready)))
    check("a draft that matched the template follows it now",
          not ready["Drafted Plain Co"]["message_edited"], str(ready["Drafted Plain Co"]["message_edited"]))
    check("a draft that differed is kept as the operator's own",
          ready["Drafted Edited Co"]["message_edited"] and ready["Drafted Edited Co"]["message"] == "Something I typed")


def test_http_auth(app_mod):
    print("\n17. THE ROUTES NEED A LOGIN")
    anon = app_mod.app.test_client()
    for path in ("/api/wa/leads", "/api/wa/ready", "/api/wa/summary", "/api/wa/followups-due",
                 "/api/wa/campaigns", "/api/wa/leads/page", "/api/countries"):
        r = anon.get(path)
        check(f"{path} requires a session", r.status_code in (401, 403), f"got {r.status_code}")


def main():
    work = tempfile.mkdtemp(prefix="whatsapp-")
    try:
        db, app_mod, client, token = boot(work)
        camp = db.create_wa_campaign("Dubai dental", country="AE")

        test_phone_formatting(db, client, token)
        test_ready_on_arrival(db, client, token, camp)
        test_send_and_versions(db, client, token, camp)
        test_followup_cadence(db, client, token, camp)
        test_not_on_whatsapp(db, client, token, camp)
        test_campaigns_and_leads_table(db, client, token, camp)
        test_import_rules(db, client, token, camp)
        test_migration(db)
        test_http_auth(app_mod)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
