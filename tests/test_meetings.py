"""One Meetings page, the website block, booking links (Plan 2 package 5).

Run:  python tests/test_meetings.py     (exits 0 on pass, 1 on failure)

The Meetings page lists every booked meeting of the operator's, from any
channel or the website; its count is asked for when the app loads. "Copy
booking link" gives the Cal.com page prefilled for a lead with a signed
reference in metadata, and a booking that comes back carrying it lands on
exactly that lead. The WhatsApp Booked tab is gone.

§5 is package 8: ShoutReach's own Zoho booking is removed. Setting a lead to
Meeting booked by hand, with a time, still works and touches nothing but the
stage; an old Zoho-era meeting row still reaches the panel; the Zoho tokens
and settings are cleared once by a guarded migration.
"""
import copy
import hashlib
import hmac
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
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


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


def main():
    work = tempfile.mkdtemp(prefix="meetings_")
    try:
        path = os.path.join(work, "m.db")
        os.environ["DB_PATH"] = path
        os.environ["SECRET_KEY"] = "test-secret"
        import db
        importlib.reload(db)
        db.init_db()
        a = db.create_user("sheham", "test-password-123", is_admin=True)
        b = db.create_user("anas", "test-password-123", is_admin=False)
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True

        def client_for(uid):
            c = app_mod.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = uid
                s["csrf_token"] = "t"
            return c
        ca, cb = client_for(a), client_for(b)
        secret = db.rotate_cal_secret()
        anon = app_mod.app.test_client()

        def deliver(body):
            raw = json.dumps(body).encode()
            return anon.post("/api/inbound/cal", data=raw, headers={
                "X-Cal-Signature-256": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()})

        print("\n1. EVERY BOOKED MEETING, FROM EVERY CHANNEL, IN ONE LIST")
        db.save_website_config({"sites": {"hexiv.co.uk": {"owner_id": a, "country": "GB"}},
                                "cal_booking_link": "https://cal.com/hexiv/free-strategy-call"})
        deliver(fixture("cal_booking_created.json"))
        db.upsert_businesses([{"company": "Call Clinic", "phone": "07700 900111", "email": "c@call.co.uk"},
                              {"company": "WA Clinic", "phone": "07700 900222"},
                              {"company": "Not Booked", "phone": "07700 900333"}], owner_id=a)
        ids = {n: i for i, n in q(path, "SELECT id, name FROM businesses")}
        db.set_business_pipeline(ids["Call Clinic"], "booked", "call", next_action_at="2026-12-01 10:00", owner_id=a)
        db.set_business_pipeline(ids["WA Clinic"], "booked", "whatsapp", next_action_at="2026-12-02 15:30", owner_id=a)
        db.set_business_pipeline(ids["Not Booked"], "proposal_sent", "call", owner_id=a)
        db.upsert_businesses([{"company": "Theirs", "phone": "07700 900444"}], owner_id=b)
        theirs = q(path, "SELECT id FROM businesses WHERE name='Theirs'")[0][0]
        db.set_business_pipeline(theirs, "booked", "call", next_action_at="2026-12-03 09:00", owner_id=b)

        rows = ca.get("/api/meetings").get_json()
        names = [r["company"] for r in rows]
        check("the website booking, the call and the WhatsApp booking are listed",
              names == ["Booker Dental", "Call Clinic", "WA Clinic"], str(names))
        web = rows[0]
        check("the website one: wall-clock time, from the website, its site, Cal.com state and Join",
              web["starts_at"] == "2026-11-22 18:30:00" and web["pipeline_channel"] == "website"
              and web["site"] == "hexiv.co.uk" and web["meeting_state"] == "booked"
              and web["join_url"].startswith("https://app.cal.com/video/"), str(web))
        check("a time set by hand is listed exactly as typed",
              rows[1]["starts_at"] == "2026-12-01 10:00:00" and rows[1]["pipeline_channel"] == "call")
        check("a business on another stage isn't", "Not Booked" not in names)
        check("the other operator's booking isn't, and theirs shows only theirs",
              "Theirs" not in names and [r["company"] for r in cb.get("/api/meetings").get_json()] == ["Theirs"])
        check("the sidebar count", ca.get("/api/meetings/count").get_json() == {"count": 3})
        deliver(fixture("cal_booking_rescheduled.json"))
        deliver(fixture("cal_booking_cancelled.json"))
        rows = ca.get("/api/meetings").get_json()
        check("a cancelled meeting stays listed, marked cancelled (the stage is the operator's call)",
              rows[0]["company"] == "Booker Dental" and rows[0]["meeting_state"] == "cancelled")
        check("and leaves the count", ca.get("/api/meetings/count").get_json() == {"count": 2})
        check("signed out: no meetings", anon.get("/api/meetings").status_code == 401)

        print("\n2. COPY BOOKING LINK")
        r = ca.get(f"/api/businesses/{ids['Call Clinic']}/booking-link?channel=call")
        url = (r.get_json() or {}).get("url", "")
        parts = urlsplit(url)
        params = parse_qs(parts.query)
        check("the Cal.com page, prefilled with the lead's email and a reference",
              url.startswith("https://cal.com/hexiv/free-strategy-call?") and params.get("email") == ["c@call.co.uk"]
              and params.get("name") == ["Call Clinic"] and f"metadata[{db.CAL_REF_KEY}]" in params, url)
        ref = params[f"metadata[{db.CAL_REF_KEY}]"][0]
        check("the reference is signed and reads back as this lead and channel",
              db.read_booking_ref(ref) == (ids["Call Clinic"], "call"))
        check("a forged one doesn't", db.read_booking_ref(f"{theirs}.call.0000") is None
              and db.read_booking_ref(ref.replace(str(ids["Call Clinic"]), str(theirs), 1)) is None)
        check("another operator's lead: 404", ca.get(f"/api/businesses/{theirs}/booking-link").status_code == 404)

        print("   the booking that comes back")
        db.set_business_pipeline(ids["Not Booked"], "proposal_sent", "call", owner_id=a)
        ref2 = db.make_booking_ref(ids["Not Booked"], "whatsapp")
        booking = copy.deepcopy(fixture("cal_booking_created.json"))
        booking["payload"]["uid"] = "ref-booking-1"
        booking["payload"]["attendees"][0]["email"] = "someone.else@example.com"
        booking["payload"]["metadata"] = {db.CAL_REF_KEY: ref2,
                                          "videoCallUrl": "https://app.cal.com/video/ref-booking-1"}
        r = deliver(booking)
        check("lands on exactly that lead (no site, a different email)",
              r.status_code == 200 and q(path, "SELECT pipeline_stage, pipeline_channel, next_action_at FROM "
                                                "businesses WHERE id=?", (ids["Not Booked"],))
              == [("booked", "whatsapp", "2026-11-22 18:30:00")],
              str(q(path, "SELECT pipeline_stage, pipeline_channel FROM businesses WHERE id=?", (ids["Not Booked"],))))
        check("with its Cal.com meeting", q(path, "SELECT cal_uid, source FROM meetings WHERE business_id=?",
                                            (ids["Not Booked"],)) == [("ref-booking-1", "cal")])
        check("and no new lead or enquiry", q(path, "SELECT COUNT(*) FROM businesses WHERE owner_id=?", (a,))[0][0] == 4
              and q(path, "SELECT COUNT(*) FROM website_enquiries")[0][0] == 1)
        forged = copy.deepcopy(booking)
        forged["payload"]["uid"] = "ref-booking-2"
        forged["payload"]["metadata"] = {db.CAL_REF_KEY: f"{theirs}.call.deadbeefdeadbeefdeadbeef"}
        deliver(forged)
        check("a forged reference books nobody's lead",
              q(path, "SELECT pipeline_stage FROM businesses WHERE id=?", (theirs,)) == [("booked",)]
              and q(path, "SELECT COUNT(*) FROM meetings WHERE cal_uid='ref-booking-2'")[0][0] == 0)
        db.save_website_config({"cal_booking_link": ""})
        r = ca.get(f"/api/businesses/{ids['Call Clinic']}/booking-link")
        check("no booking link set: a plain sentence", r.status_code == 400 and "booking link" in r.get_json()["error"])

        print("\n3. THE DASHBOARD'S 'FROM THE WEBSITE'")
        d = ca.get("/api/dashboard").get_json()
        check("recent enquiries and where each stands",
              [(w["company"], w["site"], w["status"]) for w in d.get("website", [])]
              == [("Booker Dental", "hexiv.co.uk", "Booking cancelled")], str(d.get("website")))
        check("the other operator's dashboard has none", cb.get("/api/dashboard").get_json().get("website") == [])

        print("\n4. THE WHATSAPP BOOKED TAB IS GONE")
        html = open(os.path.join(ROOT, "templates", "sections", "whatsapp.html"), encoding="utf-8").read()
        js = open(os.path.join(ROOT, "static", "js", "whatsapp.js"), encoding="utf-8").read()
        check("no Booked tab or pane", 'data-tab="booked"' not in html and 'data-pane="booked"' not in html)
        check("no Booked code left", "renderWaBooked" not in js and "wa-booked" not in js)
        side = open(os.path.join(ROOT, "templates", "sections", "sidebar.html"), encoding="utf-8").read()
        app_js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
        check("Meetings is in the sidebar, its count loaded with the app",
              'data-section="meetings"' in side and "refreshMeetingCount()" in app_js)

        print("\n5. SHOUTREACH'S ZOHO BOOKING IS GONE (package 8)")
        csrf = {"X-CSRF-Token": "t"}
        check("no zoho_calendar module, and app doesn't import one",
              not os.path.exists(os.path.join(ROOT, "zoho_calendar.py")) and "zoho_calendar" not in sys.modules)
        bid = ids["Call Clinic"]
        gone = [("get", "/api/zoho/status"), ("get", "/api/zoho/calendars"), ("put", "/api/zoho/prefs"),
                ("post", "/api/zoho/disconnect"), ("get", "/zoho/connect"), ("get", "/zoho/callback?state=x"),
                ("post", f"/api/businesses/{bid}/meeting/sync"), ("post", f"/api/businesses/{bid}/meeting/invite"),
                ("post", f"/api/businesses/{bid}/meeting/cancel"),
                ("get", "/api/meetings/address-check?email=c%40call.co.uk")]
        codes = {u: getattr(ca, m)(u, json={}, headers=csrf).status_code for m, u in gone}
        check("every Zoho, meeting sync/invite/cancel and address-check route is a 404",
              set(codes.values()) == {404}, str(codes))

        print("   booking by hand still works, and touches nothing else")
        db.upsert_businesses([{"company": "AC\\DC Dental", "phone": "07700 900555"},
                              {"company": "Phone Booked", "phone": "07700 900666"},
                              {"company": "Old Zoho", "phone": "07700 900777"}], owner_id=a)
        ids = {n: i for i, n in q(path, "SELECT id, name FROM businesses")}
        acdc = ids["AC\\DC Dental"]
        r = ca.put(f"/api/businesses/{acdc}/pipeline", headers=csrf, json={
            "stage": "booked", "channel": "whatsapp", "next_action_at": "2026-12-05T11:15",
            # What the old dialog sent. Ignored now: no calendar, no invite, no saved address.
            "meeting": {"send_invite": True, "invite_email": "boss@acdc.example", "save_email": True,
                        "link_kind": "zmeeting", "timezone": "Asia/Dubai", "minutes": 30}})
        check("a backslash in the name (B-8): booked by hand with a time, 200",
              r.status_code == 200, f"{r.status_code} {r.get_data(as_text=True)[:200]}")
        check("the stage, channel and typed time are saved",
              q(path, "SELECT pipeline_stage, pipeline_channel, next_action_at FROM businesses WHERE id=?", (acdc,))
              == [("booked", "whatsapp", "2026-12-05 11:15:00")])
        check("no meeting row, and the answer says nothing about a meeting",
              q(path, "SELECT COUNT(*) FROM meetings WHERE business_id=?", (acdc,))[0][0] == 0
              and "meeting" not in (r.get_json() or {}))
        check("the address typed for an invite isn't saved anywhere",
              q(path, "SELECT COUNT(*) FROM email_leads WHERE business_id=?", (acdc,))[0][0] == 0)
        listed = {m["company"]: m for m in ca.get("/api/meetings").get_json()}
        check("it's on Meetings at the typed time", (listed.get("AC\\DC Dental") or {}).get("starts_at")
              == "2026-12-05 11:15:00", str(listed.get("AC\\DC Dental")))

        r = ca.post("/api/calls/log", headers=csrf, json={
            "contact_id": ids["Phone Booked"], "outcome": "booked", "next_call_at": "2026-12-06T09:00",
            "timezone": "Asia/Dubai"})
        check("a 'Meeting booked' call outcome with a time: 200, stage booked at that time, no meeting row",
              r.status_code == 200 and "meeting" not in (r.get_json() or {})
              and q(path, "SELECT pipeline_stage, next_action_at FROM businesses WHERE id=?", (ids["Phone Booked"],))
              == [("booked", "2026-12-06 09:00:00")]
              and q(path, "SELECT COUNT(*) FROM meetings WHERE business_id=?", (ids["Phone Booked"],))[0][0] == 0,
              f"{r.status_code} {r.get_data(as_text=True)[:200]}")

        print("   an old Zoho-era meeting still shows")
        old = ids["Old Zoho"]
        db.set_business_pipeline(old, "booked", "call", next_action_at="2026-12-07 10:00", owner_id=a)
        conn = sqlite3.connect(path)
        conn.execute("""INSERT INTO meetings(business_id, starts_at, timezone, minutes, title, invite_email,
                            link_kind, own_link, join_url, calendar_uid, event_uid, event_etag, invited_at)
                        VALUES(?, '2026-12-07 10:00:00', 'Asia/Riyadh', 60, 'Hexiv x Old Zoho: intro call',
                               'c@old.example', 'own', 'https://meet.google.com/abc-defg-hij',
                               'https://meet.google.com/abc-defg-hij', 'cal1', 'ev1', '7', '2026-10-30 10:00:00')""",
                     (old,))
        conn.commit()
        conn.close()
        d = ca.get(f"/api/businesses/{old}").get_json()
        m = d.get("meeting") or {}
        check("the panel still gets it (source '', its Join link, no etag) for the reduced box",
              m.get("source") == "" and m.get("join_url") == "https://meet.google.com/abc-defg-hij"
              and "event_etag" not in m and d.get("next_action_at") == "2026-12-07 10:00:00", str(m))
        row = {x["company"]: x for x in ca.get("/api/meetings").get_json()}.get("Old Zoho") or {}
        check("and it's on Meetings, with Join, not cancelled",
              row.get("join_url") == "https://meet.google.com/abc-defg-hij" and row.get("meeting_state") != "cancelled"
              and row.get("cancelled_at") is None, str(row))
        r = ca.put(f"/api/businesses/{old}/pipeline", headers=csrf,
                   json={"stage": "booked", "channel": "call", "next_action_at": "2026-12-08T16:00"})
        check("moving it by hand changes the stage's time and leaves the old row alone",
              r.status_code == 200
              and q(path, "SELECT next_action_at FROM businesses WHERE id=?", (old,)) == [("2026-12-08 16:00:00",)]
              and q(path, "SELECT starts_at, event_uid, cancelled_at FROM meetings WHERE business_id=?", (old,))
              == [("2026-12-07 10:00:00", "ev1", None)])

        print("   nothing in the page still calls the Zoho code")
        page = ""
        for folder in ("static/js", "templates"):
            for dirpath, _, files in os.walk(os.path.join(ROOT, folder)):
                for name in files:
                    if name.endswith((".js", ".html")):
                        with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                            page += f.read()
        stale = [s for s in ("/api/zoho", "/zoho/connect", "meeting/sync", "meeting/invite", "meeting/cancel",
                             "address-check", "meetingFieldsHtml", "loadZohoStatus", "retryMeetingSync",
                             "sendMeetingInvite", "cancelMeeting(", "saveZohoSetup", "cfg-zoho", "Zoho Calendar",
                             "Zoho Meeting")
                 if s in page]
        check("no Zoho Calendar code, cards or wording left in static/js or templates", not stale, str(stale))
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
            app_src = f.read()
        check("app.py has no Zoho code left", "zoho_calendar" not in app_src and "_book_meeting" not in app_src)

        print("   the one-time clean-up of Zoho's tokens and settings")
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO zoho_connections(owner_id, region, refresh_token, access_token) "
                     "VALUES(?, 'sa', 'refresh-token', 'access-token')", (a,))
        conn.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                         [("zoho_client_secret", "s3cret"), ("zoho_client_id", "1000.X"), ("zoho_region", "sa"),
                          (f"meeting_prefs:{a}", '{"own_link": "https://meet.example/x"}'),
                          (f"audit_links:{a}", "[]")])
        # A database from before package 8: the marker isn't there yet.
        conn.execute("DELETE FROM settings WHERE key='_retired_zoho_calendar'")
        conn.commit()
        conn.close()
        db.init_db()
        check("init_db empties zoho_connections", q(path, "SELECT COUNT(*) FROM zoho_connections")[0][0] == 0)
        check("and deletes the Zoho settings and meeting defaults (B-7, B-13)",
              q(path, "SELECT key FROM settings WHERE key LIKE 'zoho_%' OR key LIKE 'meeting_prefs:%'") == [])
        check("but nothing else (another per-person key stays)",
              q(path, "SELECT value FROM settings WHERE key=?", (f"audit_links:{a}",)) == [("[]",)])
        check("the tables are kept",
              q(path, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('zoho_connections','meetings')")
              [0][0] == 2 and q(path, "SELECT COUNT(*) FROM meetings WHERE business_id=?", (old,))[0][0] == 1)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO zoho_connections(owner_id, region) VALUES(?, 'sa')", (a,))
        conn.commit()
        conn.close()
        db.init_db()
        check("it runs once: a second init_db leaves the table alone",
              q(path, "SELECT COUNT(*) FROM zoho_connections")[0][0] == 1
              and q(path, "SELECT COUNT(*) FROM settings WHERE key='_retired_zoho_calendar'")[0][0] == 1)
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
