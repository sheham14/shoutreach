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


def test_scheduler_scan(db):
    print("\n4. THE BACKGROUND SCAN PICKS UP PENDING LEADS")
    import scheduler
    port = free_port()
    srv = serve(_make_handler(b"<html><body>" + b"content " * 60 + b"</body></html>"), port)
    try:
        db.upsert_wa_leads([
            {"company": "Scan Me Clinic", "phone": "050 555 0001",
             "website": f"http://127.0.0.1:{port}"},
        ], default_country="AE")
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


def test_confirm_and_draft(db, client, token):
    print("\n6. CONFIRMING A SIGNAL GATES DRAFTING")
    db.upsert_wa_leads([{"company": "Confirm Test Clinic", "phone": "050 555 0002"}],
                       default_country="AE")
    lead = db.get_wa_leads()[-1]
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
    check("the send is logged for history", len(db.get_wa_lead(wid)) > 0)  # log checked below
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

    due = db.get_wa_followups_due(days=3)
    check("an old-enough send surfaces as due", any(l["id"] == wid for l in due), str([l["id"] for l in due]))

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


def test_move_lead(db, client, token):
    print("\n13. A NUMBER NOT ON WHATSAPP MOVES TO ANOTHER CHANNEL, RATHER THAN BEING LOST")
    db.upsert_wa_leads([{"company": "Wrong Number Co", "phone": "050 555 0099"}], default_country="AE")
    lead = db.get_wa_leads()[-1]

    s, body = api(client, token, "post", f"/api/wa/leads/{lead['id']}/move",
                 json={"destination": "bogus"})
    check("an unknown destination is refused", s == 400, str(body))

    s, body = api(client, token, "post", f"/api/wa/leads/{lead['id']}/move",
                 json={"destination": "call"})
    check("moving to calling succeeds", s == 200, str(body))
    check("a call_lead_id comes back", "call_lead_id" in body, str(body))

    lead2 = db.get_wa_lead(lead["id"])
    check("moved_to is recorded", lead2["moved_to"] == "call")
    check("it is paused so it can't quietly re-surface", lead2["paused"] == 1)
    check("it drops out of the follow-up-due list",
          all(l["id"] != lead["id"] for l in db.get_wa_followups_due(days=0)))
    check("the business is now findable in the call queue",
          any(l["id"] == lead2["business_id"] for l in db.get_call_queue("all")))


def test_templates(db, client, token):
    print("\n14. TEMPLATES ARE OPERATOR-EDITABLE, WITH REAL STARTING COPY")
    templates = db.get_wa_templates()
    check("all three templates exist out of the box",
          all(k in templates for k in ("gap", "no_gap", "followup")))
    check("they are not empty placeholders", all(templates[k].strip() for k in templates))

    s, body = api(client, token, "put", "/api/wa/templates",
                 json={"templates": {"gap": "New gap template {{business_name}}"}})
    check("saving succeeds", s == 200, str(body))
    s, body = api(client, token, "get", "/api/wa/templates")
    check("the change round-trips", body["gap"] == "New gap template {{business_name}}", str(body))
    check("the other templates are untouched by a partial save",
          body["no_gap"] == templates["no_gap"], str(body))


def test_import_and_cross_channel(db, client, token):
    print("\n15. IMPORT: DEDUPES THROUGH THE SAME BUSINESS IDENTITY AS EVERY OTHER CHANNEL")
    db.upsert_businesses([{"email": "front@sharedclinic.ae", "company": "Shared Clinic",
                           "phone": "050 777 7777"}])
    shared_biz = db.get_email_lead_by_email("front@sharedclinic.ae")["business_id"]

    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": "Shared Clinic", "phone": "050 777 7777"}], "country": "AE",
    })
    check("the request succeeds", s == 200, str(body))
    check("the row is held back — this business is already an email contact",
          len(body["conflicts"]) == 1 and body["conflicts"][0]["business_id"] == shared_biz,
          str(body))
    check("nothing was inserted yet", body["inserted"] == 0, str(body))

    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [body["conflicts"][0]["row"]], "country": "AE", "confirm_conflicts": True,
    })
    check("confirming lets it through", s == 200 and body["inserted"] == 1, str(body))
    with db.get_db() as conn:
        wa_row = conn.execute(
            "SELECT wa_number FROM wa_leads WHERE business_id=?", (shared_biz,)
        ).fetchone()
    check("the same business now has a WhatsApp number attached",
          wa_row and wa_row["wa_number"] == "971507777777", str(dict(wa_row) if wa_row else None))

    print("\n16. A COUNTRY COLUMN ON THE ROW OVERRIDES THE IMPORT'S DEFAULT")
    s, body = api(client, token, "post", "/api/wa/import", json={
        "rows": [{"company": "Qatar Override Co", "phone": "5512 3456", "country": "QA"}],
        "country": "AE",
    })
    biz_id = body["business_ids"][0]
    with db.get_db() as conn:
        wa_row = conn.execute(
            "SELECT wa_number, country FROM wa_leads WHERE business_id=?", (biz_id,)
        ).fetchone()
    check("the row's own country wins over the import default",
          wa_row["country"] == "QA" and wa_row["wa_number"] == "97455123456",
          str(dict(wa_row)))


def test_http_auth(app_mod):
    print("\n17. THE ROUTES ARE ADMIN-ONLY")
    anon = app_mod.app.test_client()
    for path in ("/api/wa/leads", "/api/wa/summary", "/api/wa/followups-due",
                 "/api/wa/templates"):
        r = anon.get(path)
        check(f"{path} requires a session", r.status_code in (401, 403), f"got {r.status_code}")


def main():
    work = tempfile.mkdtemp(prefix="whatsapp-")
    try:
        db, app_mod, client, token = boot(work)

        test_phone_formatting(db)
        test_signal_detection()
        test_scheduler_scan(db)
        wid = test_confirm_and_draft(db, client, token)
        wid = test_message_and_sent(db, client, token, wid)
        test_followup_cadence(db, client, token, wid)
        test_move_lead(db, client, token)
        test_templates(db, client, token)
        test_import_and_cross_channel(db, client, token)
        test_http_auth(app_mod)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
