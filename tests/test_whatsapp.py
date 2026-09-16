"""WhatsApp module: phone formatting, signal detection, the confirm/draft/send
lifecycle, the follow-up cadence, and the manual-send boundary.

Run:  python tests/test_whatsapp.py

Signal detection is tested against a local HTTP server (same pattern as
test_resilience.py), never the live internet -- a flaky test suite that
depends on some clinic's real website staying up is worse than no test.

The one property every section here ultimately protects: nothing in this
file, or in the module it tests, can cause a WhatsApp message to be sent.
Every "send" path here is asserted to stop at recording that a link would
have been opened -- see section 8.
"""
import http.server
import importlib
import os
import re
import shutil
import socket
import sys
import tempfile
import threading

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


# ── Local HTTP server for signal detection ───────────────────────────────────

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_handler(body: bytes, status: int = 200):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return Handler


def serve(handler, port):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_phone_formatting(db):
    print("\n1. PHONE FORMATTING: UAE AND QATAR")
    cases = [
        ("050 123 4567", "AE", "971501234567"),   # local UAE mobile, trunk 0 stripped
        ("+971 50 123 4567", "AE", "971501234567"),  # already international
        ("00971501234567", "AE", "971501234567"),    # 00-prefixed international
        ("04 123 4567", "AE", "97141234567"),         # UAE landline
        ("5512 3456", "QA", "97455123456"),           # Qatar, no trunk prefix to strip
        ("+974 5512 3456", "QA", "97455123456"),
    ]
    for raw, country, want in cases:
        got = db.format_whatsapp_number(raw, country)
        check(f"'{raw}' ({country}) -> {want}", got == want, f"got {got}")

    check("no digits at all yields nothing", db.format_whatsapp_number("call us", "AE") == "")
    check("unrecognized country returns digits as-is, not a guess",
          db.format_whatsapp_number("050 123 4567", "US") == "0501234567")
    check("already-international number recognized even with no country given",
          db.format_whatsapp_number("971501234567", "") == "971501234567")

    print("\n2. NUMBER TYPE: MOBILE VS LANDLINE (SOFT SIGNAL ONLY)")
    check("UAE mobile prefix", db.classify_number_type("050 123 4567", "AE") == "mobile")
    check("UAE landline prefix", db.classify_number_type("04 123 4567", "AE") == "landline")
    check("Qatar mobile prefix", db.classify_number_type("5512 3456", "QA") == "mobile")
    check("Qatar landline prefix", db.classify_number_type("4412 3456", "QA") == "landline")
    check("unrecognized country is 'unknown', not a guess",
          db.classify_number_type("050 123 4567", "") == "unknown")


def test_signal_detection():
    import wa_signal

    print("\n3. SIGNAL DETECTION: A LOCAL SERVER, NEVER THE LIVE INTERNET")

    port = free_port()
    keyword_body = b"<html><body>" + b"Welcome to our clinic. " * 40 + b"You can book online here.</body></html>"
    srv = serve(_make_handler(keyword_body), port)
    try:
        result = wa_signal.detect_signal(f"http://127.0.0.1:{port}")
        check("a booking keyword is detected as no_gap", result["signal_type"] == "no_gap", str(result))
        check("the detail quotes the keyword found", "book online" in result["signal_detail"], str(result))
    finally:
        srv.shutdown()

    port = free_port()
    widget_body = b'<html><body><script src="https://calendly.com/widget.js"></script></body></html>'
    srv = serve(_make_handler(widget_body), port)
    try:
        result = wa_signal.detect_signal(f"http://127.0.0.1:{port}")
        check("a known booking widget is detected as no_gap", result["signal_type"] == "no_gap", str(result))
        check("the widget host is named", "calendly.com" in result["signal_detail"], str(result))
    finally:
        srv.shutdown()

    port = free_port()
    plain_body = ("<html><body>" + "Welcome to our clinic, serving the community for over twenty years. " * 20 + "</body></html>").encode()
    srv = serve(_make_handler(plain_body), port)
    try:
        result = wa_signal.detect_signal(f"http://127.0.0.1:{port}")
        check("a normal page with no booking signal is gap_found",
              result["signal_type"] == "gap_found", str(result))
    finally:
        srv.shutdown()

    port = free_port()
    thin_body = b"<html><body>Hi</body></html>"
    srv = serve(_make_handler(thin_body), port)
    try:
        result = wa_signal.detect_signal(f"http://127.0.0.1:{port}")
        check("a page too thin to judge is 'unclear', not guessed either way",
              result["signal_type"] == "unclear", str(result))
        check("the detail explains why", "little content" in result["signal_detail"], str(result))
    finally:
        srv.shutdown()

    # Nothing listening -- connection refused, deterministic and fast, unlike
    # depending on a real domain not existing.
    dead_port = free_port()
    result = wa_signal.detect_signal(f"http://127.0.0.1:{dead_port}")
    check("a fetch that fails outright is 'unclear', not 'no_gap' by default",
          result["signal_type"] == "unclear", str(result))

    check("no website on file is 'unclear' without attempting a fetch",
          wa_signal.detect_signal("")["signal_type"] == "unclear")


def test_scheduler_scan(db, camp):
    print("\n4. THE BACKGROUND SCAN PICKS UP PENDING LEADS")
    import scheduler
    port = free_port()
    srv = serve(_make_handler(b"<html><body>" + b"content " * 60 + b"</body></html>"), port)
    try:
        db.upsert_wa_leads([
            {"company": "Scan Me Clinic", "phone": "050 555 0001",
             "website": f"http://127.0.0.1:{port}"},
        ], default_country="AE", wa_campaign_id=camp)
        lead = db.get_wa_leads()[0]
        check("freshly imported lead awaits a signal", lead["wa_status"] == "")

        scheduler.run_wa_signal_scan()

        lead = db.get_wa_lead(lead["id"])
        check("the scan found and recorded a signal",
              lead["wa_status"] == "signal_ready" and lead["signal_type"] == "gap_found",
              str(lead))
    finally:
        srv.shutdown()

    print("\n5. THE SCAN BATCH IS SMALL, ON PURPOSE")
    src = __import__("inspect").getsource(scheduler.run_wa_signal_scan)
    check("the scan reads its batch size from a module constant, not a magic number",
          "WA_SIGNAL_BATCH_SIZE" in src, src[:200])
    check("the batch size stays small enough not to stall the shared scheduler thread",
          scheduler.WA_SIGNAL_BATCH_SIZE <= 5, str(scheduler.WA_SIGNAL_BATCH_SIZE))


def test_confirm_and_draft(db, client, token, camp):
    print("\n6. CONFIRMING A SIGNAL GATES DRAFTING")
    db.upsert_wa_leads([{"company": "Confirm Test Clinic", "phone": "050 555 0002"}],
                       default_country="AE", wa_campaign_id=camp)
    lead = next(l for l in db.get_wa_leads() if l["company"] == "Confirm Test Clinic")
    check("an imported lead lands in its campaign", lead["wa_campaign_id"] == camp,
          str(lead["wa_campaign_id"]))
    db.set_wa_signal(lead["id"], "gap_found", "No booking link seen")

    ready = db.get_wa_leads_ready_to_draft()
    check("an unconfirmed signal is not ready to draft",
          all(r["id"] != lead["id"] for r in ready))

    s, body = api(client, token, "post", f"/api/wa/leads/{lead['id']}/confirm",
                 json={"signal_type": "gap_found", "signal_detail": "Confirmed: no booking link"})
    check("confirming succeeds", s == 200, str(body))
    lead = db.get_wa_lead(lead["id"])
    check("signal_confirmed is set", lead["signal_confirmed"] == 1)
    check("status moves to confirmed", lead["wa_status"] == "confirmed")

    ready = db.get_wa_leads_ready_to_draft()
    check("now it is ready to draft", any(r["id"] == lead["id"] for r in ready))

    s, body = api(client, token, "post", f"/api/wa/leads/{lead['id']}/confirm",
                 json={"signal_type": "bogus", "signal_detail": "x"})
    check("an unrecognized signal type is refused", s == 400, str(body))

    print("\n7. DRAFTING WITHOUT AI CONFIGURED FALLS BACK TO THE PLAIN TEMPLATE")
    s, body = api(client, token, "post", "/api/wa/draft-batch", json={})
    check("the batch call succeeds even with no AI key set", s == 200, str(body))
    check("it drafts the confirmed lead", body["drafted"] >= 1, str(body))
    check("it reports falling back, not silently pretending to have paraphrased",
          body.get("variant") == "template" and "note" in body, str(body))

    lead = db.get_wa_lead(lead["id"])
    check("the draft used the confirmed signal detail",
          "Confirmed: no booking link" in lead["draft_message"], lead["draft_message"])
    check("status moves to drafted", lead["wa_status"] == "drafted")

    print("\n8. THE DRAFT BATCH NEVER TALKS TO WHATSAPP -- ONLY TO THE CONFIGURED AI PROVIDER")
    import inspect
    import app as app_mod
    src = inspect.getsource(app_mod.api_wa_draft_batch)
    check("no wa.me reference anywhere in the drafting path",
          "wa.me" not in src and "whatsapp.com" not in src.lower())
    return lead["id"]


def test_message_and_sent(db, client, token, wid):
    print("\n9. THE DRAFT IS EDITABLE BEFORE SENDING")
    s, body = api(client, token, "put", f"/api/wa/leads/{wid}/message",
                 json={"message": "Edited by hand before sending"})
    check("editing succeeds", s == 200, str(body))
    lead = db.get_wa_lead(wid)
    check("the edit is what's stored now",
          lead["draft_message"] == "Edited by hand before sending", lead["draft_message"])
    check("editing the message does not change wa_status",
          lead["wa_status"] == "drafted", lead["wa_status"])

    s, body = api(client, token, "put", f"/api/wa/leads/{wid}/message", json={"message": "  "})
    check("an empty edit is refused", s == 400, str(body))

    print("\n10. 'SENT' MEANS THE LINK WAS OPENED -- NOTHING MORE, AND IT'S CORRECTABLE")
    s, body = api(client, token, "post", f"/api/wa/leads/{wid}/sent",
                 json={"kind": "opener", "message": "Edited by hand before sending"})
    check("marking sent succeeds", s == 200, str(body))
    lead = db.get_wa_lead(wid)
    check("sent_date is set", lead["sent_date"] is not None)
    check("status moves to sent", lead["wa_status"] == "sent")
    with db.get_db() as conn:
        logged = conn.execute(
            "SELECT kind, message FROM wa_log WHERE wa_lead_id=?", (wid,)
        ).fetchall()
    check("wa_log recorded the send, append-only",
          len(logged) == 1 and logged[0]["kind"] == "opener", str([dict(r) for r in logged]))

    s, body = api(client, token, "put", f"/api/wa/leads/{wid}/sent-date",
                 json={"sent_date": None})
    check("the operator can clear a mistaken sent_date", s == 200, str(body))
    check("cleared sent_date drops it out of the due list",
          all(l["id"] != wid for l in db.get_wa_followups_due(days=0)),
          "sent_date should be null now")
    check("it really is null", db.get_wa_lead(wid)["sent_date"] is None)

    # Re-send for the cadence tests that follow.
    api(client, token, "post", f"/api/wa/leads/{wid}/sent",
        json={"kind": "opener", "message": "Edited by hand before sending"})
    return wid


def test_followup_cadence(db, client, token, wid):
    print("\n11. FOLLOW-UPS ARE INFINITE -- NO AUTO-DORMANT CAP")
    with db.get_db() as conn:
        conn.execute("UPDATE wa_leads SET sent_date=datetime('now','-10 days') WHERE id=?", (wid,))

    due = db.get_wa_followups_due()
    check("an old-enough send surfaces as due, by its campaign's gap",
          any(l["id"] == wid for l in due), str([l["id"] for l in due]))

    s, body = api(client, token, "get", "/api/wa/followups-due")
    lead_due = next((l for l in body if l["id"] == wid), None)
    check("the due list rides over HTTP with a rendered follow-up draft",
          lead_due is not None and lead_due.get("followup_draft"), str(lead_due))

    for i in range(5):
        s, body = api(client, token, "post", f"/api/wa/leads/{wid}/sent",
                     json={"kind": "followup", "message": f"Follow-up #{i+1}"})
        check(f"follow-up #{i+1} is accepted with no cap", s == 200, str(body))
        with db.get_db() as conn:
            conn.execute("UPDATE wa_leads SET sent_date=datetime('now','-10 days') WHERE id=?", (wid,))

    lead = db.get_wa_lead(wid)
    check("followup_count accumulates without limit", lead["followup_count"] == 5, str(lead["followup_count"]))
    check("the lead is still active, not auto-dormant after any threshold",
          lead["wa_status"] == "sent" and not lead["paused"], str(lead))
    check("and it is still due for another one",
          any(l["id"] == wid for l in db.get_wa_followups_due(days=3)))

    print("\n12. PAUSING STOPS THE CADENCE; REPLYING STOPS IT IMMEDIATELY, REGARDLESS OF COUNT")
    s, body = api(client, token, "post", f"/api/wa/leads/{wid}/pause", json={"paused": True})
    check("pausing succeeds", s == 200)
    check("a paused lead is not due", all(l["id"] != wid for l in db.get_wa_followups_due(days=0)))
    api(client, token, "post", f"/api/wa/leads/{wid}/pause", json={"paused": False})
    check("unpausing restores it to the due list", any(l["id"] == wid for l in db.get_wa_followups_due(days=0)))

    s, body = api(client, token, "post", f"/api/wa/leads/{wid}/replied", json={"replied": True})
    check("marking replied succeeds", s == 200)
    lead = db.get_wa_lead(wid)
    check("wa_status reflects it", lead["wa_status"] == "replied")
    check("a replied lead drops out of the cadence at ANY follow-up count (5 here)",
          all(l["id"] != wid for l in db.get_wa_followups_due(days=0)))


def test_not_on_whatsapp(db, client, token, camp):
    print("\n13. 'NOT ON WHATSAPP' FILES THE LEAD WHERE YOU CHOOSE")
    db.upsert_wa_leads([{"company": "Wrong Number Co", "phone": "050 555 0099"}],
                       default_country="AE", wa_campaign_id=camp)
    lead = next(l for l in db.get_wa_leads() if l["company"] == "Wrong Number Co")

    s, body = api(client, token, "post", f"/api/wa/leads/{lead['id']}/move",
                 json={"destination": "bogus"})
    check("an unknown destination is refused", s == 400, str(body))

    call_camp = db.create_call_campaign("Moved from WhatsApp")
    s, body = api(client, token, "post", f"/api/wa/leads/{lead['id']}/move",
                 json={"destination": "call", "campaign_id": call_camp})
    check("moving to Calling succeeds", s == 200, str(body))
    check("a call_lead_id comes back", "call_lead_id" in body, str(body))
    lead2 = db.get_wa_lead(lead["id"])
    check("moved_to is recorded", lead2["moved_to"] == "call")
    check("it drops out of every WhatsApp list",
          all(l["id"] != lead["id"] for l in db.get_wa_leads()))
    check("and out of the follow-up-due list",
          all(l["id"] != lead["id"] for l in db.get_wa_followups_due(days=0)))
    check("the business is now on Calling",
          any(l["id"] == lead2["business_id"] for l in db.get_call_queue("all")))
    check("inside the call campaign that was picked",
          any(l["id"] == lead2["business_id"]
              for l in db.get_call_queue("all", call_campaign_id=call_camp)))

    db.upsert_wa_leads([{"company": "No Email Clinic", "phone": "050 555 0100"}],
                       default_country="AE", wa_campaign_id=camp)
    no_email = next(l for l in db.get_wa_leads() if l["company"] == "No Email Clinic")
    s, body = api(client, token, "post", f"/api/wa/leads/{no_email['id']}/move",
                 json={"destination": "email"})
    check("moving to Email with no address on file is refused", s == 400, str(body))
    check("and the lead stays on WhatsApp rather than half-moved",
          db.get_wa_lead(no_email["id"])["moved_to"] == "", db.get_wa_lead(no_email["id"])["moved_to"])

    db.upsert_businesses([{"company": "Has Email Clinic", "email": "hi@hasemail.ae",
                           "phone": "050 555 0101"}])
    db.upsert_wa_leads([{"company": "Has Email Clinic", "phone": "050 555 0101"}],
                       default_country="AE", wa_campaign_id=camp)
    has_email = next(l for l in db.get_wa_leads() if l["company"] == "Has Email Clinic")
    email_camp = db.create_campaign("Emails for WhatsApp misses")
    db.upsert_step(email_camp, 1, "Hi", "Body", 0)
    s, body = api(client, token, "post", f"/api/wa/leads/{has_email['id']}/move",
                 json={"destination": "email", "campaign_id": email_camp})
    check("moving to Email with an address works", s == 200 and body.get("enrolled") == 1, str(body))

    s, body = api(client, token, "post", f"/api/wa/leads/{no_email['id']}/move",
                 json={"destination": "none"})
    check("'just take it off WhatsApp' works", s == 200, str(body))
    check("and rules the number out", db.get_wa_lead(no_email["id"])["moved_to"] == "none")
    offered = {r["company"] for r in db.search_businesses(q="No Email Clinic",
                                                          not_on_channel="whatsapp")["rows"]}
    check("so the picker never offers it back to WhatsApp", "No Email Clinic" not in offered,
          str(offered))


def test_campaign_copy(db, client, token, camp):
    print("\n14. EACH CAMPAIGN HAS ITS OWN COPY, WITH REAL STARTING TEMPLATES")
    s, c = api(client, token, "get", f"/api/wa/campaigns/{camp}")
    check("the campaign loads", s == 200, str(c)[:120])
    templates = c["templates"]
    check("all three templates exist out of the box",
          all(k in templates for k in ("gap", "no_gap", "followup")))
    check("each is a list of A/B arms", all(
        isinstance(templates[k], list) and templates[k] for k in ("gap", "no_gap", "followup")))
    check("they are not empty placeholders", all(
        arm.strip() for k in ("gap", "no_gap", "followup") for arm in templates[k]))
    check("one arm each, so nothing is being tested yet",
          all(len(templates[k]) == 1 for k in ("gap", "no_gap", "followup")))
    check("the fields a template can use are listed",
          {f["key"] for f in c["fields"]} >= {"business_name", "signal_detail", "city"}, str(c["fields"]))

    s, body = api(client, token, "patch", f"/api/wa/campaigns/{camp}",
                  json={"templates": {"gap": "New gap template {{business_name}}"}})
    check("saving succeeds", s == 200, str(body))
    s, c2 = api(client, token, "get", f"/api/wa/campaigns/{camp}")
    check("the change round-trips", c2["templates"]["gap"] == ["New gap template {{business_name}}"],
          str(c2["templates"]))
    check("the other templates are untouched by a partial save",
          c2["templates"]["no_gap"] == templates["no_gap"], str(c2["templates"]))

    s, body = api(client, token, "patch", f"/api/wa/campaigns/{camp}", json={
        "templates": {"gap": ["Arm A {{business_name}}", "Arm B {{business_name}}"]},
        "followup_days": 5, "variables": {"my name": "Sam", "offer": "online booking"},
    })
    check("a second arm, a follow-up gap and variables save together", s == 200, str(body))
    s, c3 = api(client, token, "get", f"/api/wa/campaigns/{camp}")
    check("both arms come back",
          c3["templates"]["gap"] == ["Arm A {{business_name}}", "Arm B {{business_name}}"],
          str(c3["templates"]))
    check("the follow-up gap saves", c3["followup_days"] == 5, str(c3["followup_days"]))
    check("variable names become something a template can use",
          c3["variables"] == {"my_name": "Sam", "offer": "online booking"}, str(c3["variables"]))

    s, body = api(client, token, "patch", f"/api/wa/campaigns/{camp}",
                  json={"templates": {"gap": []}})
    check("a template cannot be emptied", s == 400, f"{s} {body}")

    print("\n15. VARIABLES FILL IN, WITH FALLBACKS")
    lead = {"company": "Pearl Dental", "city": "Doha", "category": "", "signal_detail": "no booking"}
    out = db.render_wa_message(
        "Hi {{business_name}} in {{city}}, a {{category|clinic}}. {{my_name}} here about "
        "{{offer}} ({{signal_detail}}){{missing}}", lead, {"my_name": "Sam", "offer": "booking",
                                                            "city": "Nowhere"})
    check("business fields, fallbacks and campaign variables all fill in",
          out == "Hi Pearl Dental in Doha, a clinic. Sam here about booking (no booking)", out)
    check("an unnamed business is 'there', never an empty greeting",
          db.render_wa_message("Hi {{business_name}}!", {"company": ""}) == "Hi there!")


def test_import_and_cross_channel(db, client, token, camp):
    print("\n16. IMPORT: DEDUPES THROUGH THE SAME BUSINESS IDENTITY AS EVERY OTHER CHANNEL")
    db.upsert_businesses([{"email": "front@sharedclinic.ae", "company": "Shared Clinic",
                           "phone": "050 777 7777"}])
    shared_biz = db.get_email_lead_by_email("front@sharedclinic.ae")["business_id"]

    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": "Shared Clinic", "phone": "050 777 7777"}], "country": "AE",
    })
    check("an import has to name its campaign", s == 400, f"{s} {body}")

    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": "Shared Clinic", "phone": "050 777 7777"}], "country": "AE",
        "wa_campaign_id": camp,
    })
    check("the request succeeds", s == 200, str(body))
    check("the row is held back — this business is already an email contact",
          len(body["conflicts"]) == 1 and body["conflicts"][0]["business_id"] == shared_biz,
          str(body))
    check("nothing was inserted yet", body["inserted"] == 0, str(body))

    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [body["conflicts"][0]["row"]], "country": "AE", "confirm_conflicts": True,
        "wa_campaign_id": camp,
    })
    check("confirming lets it through", s == 200 and body["inserted"] == 1, str(body))
    with db.get_db() as conn:
        wa_row = conn.execute(
            "SELECT wa_number, wa_campaign_id FROM wa_leads WHERE business_id=?", (shared_biz,)
        ).fetchone()
    check("the same business now has a WhatsApp number attached, in the campaign",
          wa_row and wa_row["wa_number"] == "971507777777" and wa_row["wa_campaign_id"] == camp,
          str(dict(wa_row) if wa_row else None))

    print("\n17. A COUNTRY COLUMN ON THE ROW OVERRIDES THE IMPORT'S DEFAULT")
    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": "Qatar Override Co", "phone": "5512 3456", "country": "QA"}],
        "country": "AE", "wa_campaign_id": camp,
    })
    biz_id = body["business_ids"][0]
    with db.get_db() as conn:
        wa_row = conn.execute(
            "SELECT wa_number, country FROM wa_leads WHERE business_id=?", (biz_id,)
        ).fetchone()
    check("the row's own country wins over the import default",
          wa_row["country"] == "QA" and wa_row["wa_number"] == "97455123456",
          str(dict(wa_row)))


def test_ab_arms(db, client, token):
    print("\n18. A/B ARMS ARE DEALT OUT AND MEASURED, PER CAMPAIGN")
    s, body = api(client, token, "post", "/api/wa/campaigns", json={"name": "AB test", "country": "AE"})
    ab = body["id"]
    api(client, token, "patch", f"/api/wa/campaigns/{ab}",
        json={"templates": {"gap": ["Arm A for {{business_name}}", "Arm B for {{business_name}}"]}})

    db.upsert_wa_leads([{"company": f"AB Clinic {i}", "phone": f"05011122{i:02d}"}
                        for i in range(4)], default_country="AE", wa_campaign_id=ab)
    for lead in db.get_wa_leads(status="", wa_campaign_id=ab):
        db.confirm_wa_signal(lead["id"], "gap_found", "no booking link")

    s, body = api(client, token, "post", "/api/wa/draft-batch",
                  json={"limit": 2, "wa_campaign_id": ab})
    s, body2 = api(client, token, "post", "/api/wa/draft-batch",
                   json={"limit": 2, "wa_campaign_id": ab})
    check("two small batches draft", body["drafted"] == 2 and body2["drafted"] == 2,
          f"{body} {body2}")

    drafted = [l for l in db.get_wa_leads(status="drafted", wa_campaign_id=ab)]
    labels = [l["template_variant"] for l in drafted]
    check("every lead carries an arm label", all(l in ("A", "B") for l in labels), f"{labels}")
    check("and the arms stay even across batches instead of restarting at A",
          labels.count("A") == labels.count("B") == 2, f"{labels}")
    check("the message a lead got matches the arm it was given", all(
        ("Arm A" in l["draft_message"]) == (l["template_variant"] == "A") for l in drafted))

    # A follow-up must not switch a lead to the other arm mid-conversation.
    api(client, token, "patch", f"/api/wa/campaigns/{ab}",
        json={"templates": {"followup": ["Follow A {{business_name}}", "Follow B {{business_name}}"]}})
    lead_b = next(l for l in drafted if l["template_variant"] == "B")
    api(client, token, "post", f"/api/wa/leads/{lead_b['id']}/sent", json={"kind": "opener"})
    db.correct_wa_sent_date(lead_b["id"], "2020-01-01 09:00:00")
    s, body = api(client, token, "get", f"/api/wa/followups-due?wa_campaign_id={ab}")
    due = [d for d in body if d["id"] == lead_b["id"]]
    check("the lead is due a follow-up", len(due) == 1, str(body)[:160])
    check("and its follow-up comes from its own arm, filled in",
          due and due[0]["followup_draft"] == f"Follow B {lead_b['company']}", str(due)[:160])

    db.mark_wa_replied(lead_b["id"], True)
    stats = db.get_wa_variant_stats(wa_campaign_id=ab)
    arm_b = [s for s in stats if s["arm"] == "B"]
    check("the stats attribute the reply to the arm that earned it",
          len(arm_b) == 1 and arm_b[0]["replied"] == 1, f"{stats}")
    s, c = api(client, token, "get", f"/api/wa/campaigns/{ab}")
    check("and the campaign reports its own numbers", any(x["arm"] == "B" for x in c["stats"]),
          str(c["stats"]))


def test_campaigns(db, client, token, camp):
    print("\n19. CAMPAIGNS: COPY, SEPARATE FOLLOW-UP GAPS, MOVE LEADS, DELETE")
    s, body = api(client, token, "post", "/api/wa/campaigns",
                  json={"name": "Doha physio", "country": "QA", "copy_from": camp})
    check("a campaign can start from another's copy", s == 200, str(body))
    copy = db.get_wa_campaign(body["id"])
    original = db.get_wa_campaign(camp)
    check("with the same templates and gap",
          copy["templates"] == original["templates"] and copy["followup_days"] == original["followup_days"],
          str(copy["templates"]))
    api(client, token, "patch", f"/api/wa/campaigns/{copy['id']}",
        json={"templates": {"gap": "Doha pitch"}, "followup_days": 10})
    check("editing the copy leaves the original alone",
          db.get_wa_campaign(camp)["templates"]["gap"] == original["templates"]["gap"])

    db.upsert_wa_leads([{"company": "Gap Test Clinic", "phone": "5512 0001"}],
                       default_country="QA", wa_campaign_id=copy["id"])
    gap_lead = next(l for l in db.get_wa_leads() if l["company"] == "Gap Test Clinic")
    db.mark_wa_sent(gap_lead["id"], "hello")
    db.correct_wa_sent_date(gap_lead["id"], None)
    with db.get_db() as conn:
        conn.execute("UPDATE wa_leads SET sent_date=datetime('now','-7 days') WHERE id=?",
                     (gap_lead["id"],))
    check("a lead sent 7 days ago isn't due in a 10-day campaign",
          all(l["id"] != gap_lead["id"] for l in db.get_wa_followups_due()))
    s, body = api(client, token, "post", "/api/wa/leads/bulk", json={
        "action": "campaign", "wa_lead_ids": [gap_lead["id"]], "wa_campaign_id": camp})
    check("moving it to another campaign works", s == 200 and body["updated"] == 1, str(body))
    check("and in a 5-day campaign it is due",
          any(l["id"] == gap_lead["id"] for l in db.get_wa_followups_due()))

    campaigns = {c["id"]: c for c in db.get_wa_campaigns()}
    check("the campaign list counts each campaign's own leads",
          campaigns[camp]["leads"] >= 1 and campaigns[copy["id"]]["leads"] == 0,
          f"{campaigns[camp]['leads']} / {campaigns[copy['id']]['leads']}")
    check("and its follow-ups due", campaigns[camp]["due"] >= 1, str(campaigns[camp]["due"]))

    db.upsert_wa_leads([{"company": "Orphan Clinic", "phone": "5512 0002"}],
                       default_country="QA", wa_campaign_id=copy["id"])
    orphan = next(l for l in db.get_wa_leads() if l["company"] == "Orphan Clinic")
    db.confirm_wa_signal(orphan["id"], "gap_found", "nothing to book with")
    s, body = api(client, token, "delete", f"/api/wa/campaigns/{copy['id']}")
    check("deleting a campaign keeps its leads", s == 200 and body["leads_without_campaign"] == 1,
          str(body))
    check("which stay on WhatsApp, with no campaign",
          db.get_wa_lead(orphan["id"])["wa_campaign_id"] is None)
    s, body = api(client, token, "post", "/api/wa/draft-batch", json={})
    check("and aren't written from anyone's copy until they're moved",
          body.get("no_campaign", 0) >= 1 and db.get_wa_lead(orphan["id"])["wa_status"] == "confirmed",
          str(body))


def test_leads_table(db, client, token, camp):
    print("\n20. THE LEADS TABLE: EVERY LEAD, ITS STAGE, AND TAKING ONE OFF")
    s, page = api(client, token, "get", "/api/wa/leads/page?per_page=500")
    stages = {r["company"]: r["stage"] for r in page["rows"]}
    check("the table answers", s == 200 and page["total"] == len(page["rows"]), str(page["total"]))
    check("leads taken off WhatsApp aren't in the default view",
          "Wrong Number Co" not in stages, str(sorted(stages)))
    check("a replied lead shows as replied", stages.get("Confirm Test Clinic") == "replied",
          str(stages.get("Confirm Test Clinic")))
    check("a lead owed a follow-up shows as due", stages.get("Gap Test Clinic") == "due",
          str(stages.get("Gap Test Clinic")))
    s, off = api(client, token, "get", "/api/wa/leads/page?stage=off&per_page=500")
    check("and the taken-off ones have their own view",
          {"Wrong Number Co", "No Email Clinic"} <= {r["company"] for r in off["rows"]},
          str([r["company"] for r in off["rows"]]))
    s, due_only = api(client, token, "get", "/api/wa/leads/page?stage=due")
    check("filtering to one stage works", due_only["rows"] and
          all(r["stage"] == "due" for r in due_only["rows"]), str(len(due_only["rows"])))
    s, none = api(client, token, "get", "/api/wa/leads/page?wa_campaign_id=none")
    check("so does 'no campaign'", any(r["company"] == "Orphan Clinic" for r in none["rows"]),
          str([r["company"] for r in none["rows"]]))

    orphan = next(r for r in none["rows"] if r["company"] == "Orphan Clinic")
    s, body = api(client, token, "post", "/api/wa/leads/bulk",
                  json={"action": "remove", "wa_lead_ids": [orphan["id"]]})
    check("removing a lead works", s == 200 and body["updated"] == 1, str(body))
    check("it leaves every WhatsApp list", all(l["id"] != orphan["id"] for l in db.get_wa_leads()))
    offered = {r["company"] for r in db.search_businesses(q="Orphan", not_on_channel="whatsapp")["rows"]}
    check("but, unlike a number ruled out, it can be picked again", "Orphan Clinic" in offered,
          str(offered))
    s, body = api(client, token, "post", "/api/wa/add-existing", json={
        "business_ids": [orphan["business_id"]], "country": "QA", "wa_campaign_id": camp,
        "confirm_conflicts": True})
    back = db.get_wa_lead(orphan["id"])
    check("adding it again brings the same lead back, into the new campaign",
          body.get("added") == 1 and back["removed_at"] is None and back["wa_campaign_id"] == camp,
          f"{body} {back['removed_at']} {back['wa_campaign_id']}")

    s, body = api(client, token, "post", "/api/wa/leads/bulk",
                  json={"action": "pause", "wa_lead_ids": [orphan["id"]]})
    check("bulk pause works", s == 200 and db.get_wa_lead(orphan["id"])["paused"] == 1, str(body))


def test_add_existing(db, client, token, camp):
    print("\n21. PUTTING LEADS YOU ALREADY HAVE ONTO WHATSAPP")
    db.upsert_businesses([
        {"company": "Existing With Phone", "phone": "050 111 2233", "website": "https://ewp.ae"},
        {"company": "Existing No Phone", "website": "https://enp.ae"},
    ])
    found = db.search_businesses(q="Existing", not_on_channel="whatsapp")
    ids = {r["company"]: r["id"] for r in found["rows"]}
    check("the picker offers leads not yet on WhatsApp",
          {"Existing With Phone", "Existing No Phone"} <= set(ids), str(sorted(ids)))

    s, body = api(client, token, "post", "/api/wa/add-existing",
                  json={"business_ids": list(ids.values()), "wa_campaign_id": camp})
    check("a country is required", s == 400, f"{s} {body}")
    s, body = api(client, token, "post", "/api/wa/add-existing",
                  json={"business_ids": list(ids.values()), "country": "AE"})
    check("so is a campaign", s == 400, f"{s} {body}")

    s, body = api(client, token, "post", "/api/wa/add-existing",
                  json={"business_ids": list(ids.values()), "country": "AE", "wa_campaign_id": camp})
    check("the request succeeds", s == 200, str(body))
    check("the one with a phone is added", body.get("added") == 1, str(body))
    check("the one without is counted, not silently dropped", body.get("no_phone") == 1, str(body))
    again = {r["company"] for r in db.search_businesses(q="Existing", not_on_channel="whatsapp")["rows"]}
    check("and the picker stops offering it once it's on WhatsApp",
          "Existing With Phone" not in again, str(sorted(again)))

    s, body = api(client, token, "post", "/api/wa/add-existing",
                  json={"business_ids": [ids["Existing With Phone"]], "country": "AE",
                        "wa_campaign_id": camp})
    check("adding it twice doesn't duplicate it",
          body.get("added") == 0 and body.get("already") == 1, str(body))

    db.upsert_businesses([{"email": "hi@emailed.ae", "company": "Already Emailed",
                           "phone": "050 999 8877"}])
    emailed = db.search_businesses(q="Already Emailed")["rows"][0]["id"]
    s, body = api(client, token, "post", "/api/wa/add-existing",
                  json={"business_ids": [emailed], "country": "AE", "wa_campaign_id": camp})
    check("a lead already being emailed is held for confirmation",
          body.get("added") == 0 and len(body.get("conflicts") or []) == 1, str(body))
    s, body = api(client, token, "post", "/api/wa/add-existing",
                  json={"business_ids": [emailed], "country": "AE", "confirm_conflicts": True,
                        "wa_campaign_id": camp})
    check("and goes through once confirmed", body.get("added") == 1, str(body))

    wa_id = next(l["id"] for l in db.get_wa_leads() if l["company"] == "Existing With Phone")
    db.move_wa_lead(wa_id, "call")
    s, body = api(client, token, "post", "/api/wa/add-existing", json={
        "business_ids": [ids["Existing With Phone"]], "country": "AE", "confirm_conflicts": True,
        "wa_campaign_id": camp,
    })
    check("a number already ruled out as not on WhatsApp is not requeued",
          body.get("ruled_out") == 1 and body.get("added") == 0, str(body))

    db.upsert_businesses([{"company": "Asked To Stop", "phone": "050 444 5566"}])
    stop_id = db.search_businesses(q="Asked To Stop")["rows"][0]["id"]
    with db.get_db() as conn:
        conn.execute("UPDATE businesses SET do_not_contact=1 WHERE id=?", (stop_id,))
    s, body = api(client, token, "post", "/api/wa/add-existing",
                  json={"business_ids": [stop_id], "country": "AE", "wa_campaign_id": camp})
    check("someone who asked not to be contacted is not added",
          body.get("opted_out") == 1 and body.get("added") == 0, str(body))


def test_http_auth(app_mod):
    print("\n22. THE ROUTES NEED A LOGIN")
    anon = app_mod.app.test_client()
    for path in ("/api/wa/leads", "/api/wa/summary", "/api/wa/followups-due",
                 "/api/wa/campaigns", "/api/wa/leads/page"):
        r = anon.get(path)
        check(f"{path} requires a session", r.status_code in (401, 403), f"got {r.status_code}")


def main():
    work = tempfile.mkdtemp(prefix="whatsapp-")
    try:
        db, app_mod, client, token = boot(work)
        camp = db.create_wa_campaign("Dubai dental", country="AE")

        test_phone_formatting(db)
        test_signal_detection()
        test_scheduler_scan(db, camp)
        wid = test_confirm_and_draft(db, client, token, camp)
        wid = test_message_and_sent(db, client, token, wid)
        test_followup_cadence(db, client, token, wid)
        test_not_on_whatsapp(db, client, token, camp)
        test_campaign_copy(db, client, token, camp)
        test_import_and_cross_channel(db, client, token, camp)
        test_ab_arms(db, client, token)
        test_campaigns(db, client, token, camp)
        test_leads_table(db, client, token, camp)
        test_add_existing(db, client, token, camp)
        test_http_auth(app_mod)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
