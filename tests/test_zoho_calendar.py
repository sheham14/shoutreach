"""Booked meetings onto Zoho Calendar, with invites -- against a fake Zoho.

Run:  python tests/test_zoho_calendar.py

The properties worth protecting:
  - the client is only ever emailed when a box said so: the first invite,
    an update to a moved meeting, a cancellation;
  - changing or clearing the pipeline stage never cancels the meeting or
    touches Zoho -- a mis-click must not email a client;
  - what the client sees is the title, time and link -- never the phone
    number or which channel we found them on;
  - a booking is saved even when Zoho fails, with the error and a retry;
  - tokens never reach the browser, and one operator can't touch another's
    meeting or connection;
  - an address saved from an invite is put in no campaign, and one any
    operator saw unsubscribe arrives unsubscribed.

Never reaches the internet: zoho_calendar._http is replaced by FakeZoho.
"""
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeZoho:
    """Just enough of Zoho Accounts and Calendar, in the Saudi data centre."""

    def __init__(self):
        self.calls = []
        self.events = {}
        self.n = 0
        self.fail_next = None
        self.token_n = 0
        self.calendars = [
            {"uid": "own==", "name": "Personal", "privilege": "owner", "isdefault": True},
            {"uid": "team==", "name": "Hexiv Meetings", "privilege": "modify", "isdefault": False},
        ]

    def calendar_calls(self):
        return [c for c in self.calls if "calendar.zoho" in c["url"]]

    def __call__(self, method, url, **kw):
        params = kw.get("params") or {}
        data = kw.get("data") or {}
        headers = kw.get("headers") or {}
        eventdata = json.loads(params["eventdata"]) if "eventdata" in params else None
        self.calls.append({"method": method, "url": url, "params": params, "data": data,
                           "headers": headers, "eventdata": eventdata})
        if self.fail_next:
            status, msg = self.fail_next
            self.fail_next = None
            return FakeResp(status, {"error": [{"description": msg}]})
        u = urlparse(url)
        if u.netloc == "accounts.zoho.sa":
            if u.path == "/oauth/v2/token":
                if data.get("grant_type") == "authorization_code":
                    if data.get("code") != "good-code":
                        return FakeResp(200, {"error": "invalid_code"})
                    self.token_n += 1
                    return FakeResp(200, {"access_token": f"at{self.token_n}",
                                          "refresh_token": "rt-secret", "expires_in": 3600})
                if data.get("grant_type") == "refresh_token":
                    self.token_n += 1
                    return FakeResp(200, {"access_token": f"at{self.token_n}", "expires_in": 3600})
            if u.path == "/oauth/user/info":
                return FakeResp(200, {"Email": "alice@hexiv.co"})
            if u.path == "/oauth/v2/token/revoke":
                return FakeResp(200, {"status": "success"})
            return FakeResp(404, {"error": "unknown accounts path"})
        if u.netloc != "calendar.zoho.sa":
            return FakeResp(404, {"error": f"wrong data centre {u.netloc}"})
        if not headers.get("Authorization", "").startswith("Zoho-oauthtoken at"):
            return FakeResp(401, {"error": "invalid oauth token"})
        path = unquote(u.path)
        if method == "GET" and path == "/api/v1/calendars":
            return FakeResp(200, {"calendars": self.calendars})
        m = re.match(r"^/api/v1/calendars/([^/]+)/events(?:/(.+))?$", path)
        if not m:
            return FakeResp(404, {"error": "no such path"})
        cal, uid = m.group(1), m.group(2)
        if method == "POST":
            self.n += 1
            uid = f"ev{self.n}@zoho.com"
            ev = {**eventdata, "uid": uid, "etag": 1000 + self.n, "caluid": cal}
            if eventdata.get("conference") == "zmeeting":
                ev["conference_data"] = {"zmeeting": {"meetingdata": {
                    "join_url": f"https://meeting.zoho.sa/meeting/join/{self.n}"}}}
            self.events[uid] = ev
            return FakeResp(200, {"events": [ev]})
        ev = self.events.get(uid)
        if not ev:
            return FakeResp(404, {"error": [{"description": "Event not found"}]})
        if str(headers.get("etag")) != str(ev["etag"]):
            return FakeResp(400, {"error": [{"description": "etag mismatch"}]})
        if method == "PUT":
            self.n += 1
            ev = {**eventdata, "uid": uid, "etag": ev["etag"] + 100, "caluid": cal,
                  "conference_data": ev.get("conference_data")}
            self.events[uid] = ev
            return FakeResp(200, {"events": [ev]})
        if method == "DELETE":
            del self.events[uid]
            return FakeResp(200, {"events": [{"uid": uid, "estatus": "deleted", "caluid": cal}]})
        return FakeResp(405, {"error": "method"})


def boot(work):
    os.environ["DB_PATH"] = os.path.join(work, "zoho.db")
    os.environ["SECRET_KEY"] = "test-secret"
    import db
    importlib.reload(db)
    db.init_db()
    db.create_user("admin", "testpassword123", is_admin=True)
    import zoho_calendar
    importlib.reload(zoho_calendar)
    import app as app_mod
    importlib.reload(app_mod)
    app_mod.zoho_calendar = zoho_calendar
    fake = FakeZoho()
    zoho_calendar._http = fake
    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()
    token = login(client, "admin")
    return db, zoho_calendar, app_mod, client, token, fake


def login(client, username):
    r = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.get_data(as_text=True)).group(1)
    client.post("/login", data={"username": username, "password": "testpassword123",
                                "csrf_token": csrf})
    return client.get("/api/csrf").get_json()["csrf_token"]


def api(client, token, method, url, **kw):
    headers = kw.pop("headers", {})
    headers["X-CSRF-Token"] = token
    r = getattr(client, method)(url, headers=headers, **kw)
    return r.status_code, r.get_json()


def new_lead(db, name, phone, camp, owner_id=None):
    db.upsert_wa_leads([{"company": name, "phone": phone}], default_country="SA",
                       wa_campaign_id=camp, owner_id=owner_id)
    return next(l for l in db.get_wa_leads(include_inactive=True, owner_id=owner_id)
                if l["company"] == name)["business_id"]


def book(client, token, bid, when, **meeting):
    return api(client, token, "put", f"/api/businesses/{bid}/pipeline",
               json={"stage": "booked", "channel": "whatsapp", "next_action_at": when,
                     "meeting": meeting})


# ── 1. Setting Zoho up ───────────────────────────────────────────────────────

def test_setup_and_connect(db, z, client, token, fake):
    print("\n1. AN ADMIN SETS ZOHO UP; EACH PERSON CONNECTS THEIR OWN LOGIN")
    s, body = api(client, token, "get", "/api/zoho/status")
    check("before setup, it says it isn't configured", s == 200 and body["configured"] is False)
    check("the region defaults to Saudi Arabia", body["region"] == "sa", body["region"])
    check("the redirect address to paste into Zoho is offered",
          body["redirect_uri"].endswith("/zoho/callback"), body["redirect_uri"])
    r = client.get("/zoho/connect")
    check("Connect before setup goes nowhere near Zoho",
          r.status_code == 302 and "zoho=not_configured" in r.headers["Location"])

    api(client, token, "post", "/api/settings",
        json={"zoho_region": "sa", "zoho_client_id": "1000.CLIENT", "zoho_client_secret": "shh-secret"})
    s, settings = api(client, token, "get", "/api/settings")
    check("the client secret is masked when settings are read back",
          settings.get("zoho_client_secret") != "shh-secret" and "shh-secret" not in json.dumps(settings))

    r = client.get("/zoho/connect")
    loc = r.headers.get("Location", "")
    q = parse_qs(urlparse(loc).query)
    check("Connect sends you to Zoho's Saudi sign-in page",
          loc.startswith("https://accounts.zoho.sa/oauth/v2/auth"), loc[:80])
    check("asking for a lasting (offline) connection",
          q.get("access_type") == ["offline"] and q.get("prompt") == ["consent"])
    check("with calendar, meeting and profile scopes",
          all(sc in q.get("scope", [""])[0] for sc in
              ("ZohoCalendar.event.ALL", "ZohoCalendar.calendar.READ", "ZohoMeeting.meeting.ALL")))
    state = q.get("state", [""])[0]
    check("and a state value tied to this session", len(state) > 20)

    r = client.get("/zoho/callback?code=good-code&state=not-the-state")
    check("a callback with the wrong state is refused",
          "zoho=failed" in r.headers.get("Location", "") and not db.get_zoho_connection(1))

    client.get("/zoho/connect")
    with client.session_transaction() as sess:
        state = sess["zoho_state"]
    r = client.get(f"/zoho/callback?code=good-code&state={state}&accounts-server=https://accounts.zoho.eu")
    check("an account in another data centre is told so rather than half-connected",
          "zoho=wrong_region" in r.headers.get("Location", "") and not db.get_zoho_connection(1))

    client.get("/zoho/connect")
    with client.session_transaction() as sess:
        state = sess["zoho_state"]
    r = client.get(f"/zoho/callback?code=good-code&state={state}&accounts-server=https://accounts.zoho.sa")
    check("the right state and code connect", "zoho=connected" in r.headers.get("Location", ""),
          r.headers.get("Location", ""))
    s, body = api(client, token, "get", "/api/zoho/status")
    check("status says connected, and as whom",
          body["connected"] and body["zoho_email"] == "alice@hexiv.co", str(body)[:160])
    check("it starts on the Zoho default calendar", body["calendar_name"] == "Personal")
    check("no token ever reaches the browser",
          "rt-secret" not in json.dumps(body) and "at1" not in json.dumps(body))
    with client.session_transaction() as sess:
        check("the state is used once", "zoho_state" not in sess)

    s, body = api(client, token, "get", "/api/zoho/calendars")
    check("calendars are listed, shared ones too",
          s == 200 and [c["name"] for c in body["calendars"]] == ["Personal", "Hexiv Meetings"], str(body))


def test_prefs(db, z, client, token):
    print("\n2. YOUR DEFAULTS: THE SHARED CALENDAR, TIMEZONE, LENGTH, TITLE, LINK")
    s, body = api(client, token, "put", "/api/zoho/prefs",
                  json={"calendar_uid": "team==", "calendar_name": "Hexiv Meetings",
                        "timezone": "Asia/Riyadh", "minutes": 60, "link_kind": "zmeeting"})
    check("saved", s == 200 and body["calendar_name"] == "Hexiv Meetings", str(body)[:120])
    prefs = db.get_meeting_prefs(1)
    check("an hour is the usual length", prefs["minutes"] == 60)
    check("the invite title defaults to Hexiv × {{business}}: intro call",
          prefs["invite_title"] == "Hexiv × {{business}}: intro call", prefs["invite_title"])
    s, _ = api(client, token, "put", "/api/zoho/prefs", json={"timezone": "Mars/Olympus"})
    check("an unknown timezone is refused", s == 400)
    s, _ = api(client, token, "put", "/api/zoho/prefs", json={"own_link": "javascript:alert(1)"})
    check("a link that isn't https is refused", s == 400)
    s, _ = api(client, token, "put", "/api/zoho/prefs", json={"minutes": 2})
    check("a two-minute meeting is refused", s == 400)


# ── 3. Booking ───────────────────────────────────────────────────────────────

def test_book_with_invite(db, z, client, token, fake, camp):
    print("\n3. BOOKING PUTS IT ON THE SHARED CALENDAR AND INVITES THE CLIENT")
    bid = new_lead(db, "Smile Dental", "050 123 0001", camp)
    fake.calls.clear()
    s, body = book(client, token, bid, "2026-11-05T10:00", timezone="Asia/Riyadh", minutes=60,
                   link_kind="zmeeting", invite_email="Owner@SmileDental.sa",
                   send_invite=True, save_email=True)
    check("the booking saves", s == 200 and body["pipeline_stage"] == "booked", str(body)[:160])
    calls = fake.calendar_calls()
    check("one event is created, on the calendar you chose",
          len(calls) == 1 and calls[0]["method"] == "POST" and "/calendars/team%3D%3D/events" in calls[0]["url"],
          str([(c["method"], c["url"]) for c in calls]))
    ev = calls[0]["eventdata"]
    check("10:00 in Riyadh is 07:00 GMT, an hour long",
          ev["dateandtime"] == {"timezone": "Asia/Riyadh", "start": "20261105T070000Z",
                                "end": "20261105T080000Z"}, str(ev["dateandtime"]))
    check("titled for the client", ev["title"] == "Hexiv × Smile Dental: intro call", ev["title"])
    check("with a Zoho Meeting room", ev["conference"] == "zmeeting")
    check("the client is on it, and Zoho is told to email them",
          ev.get("attendees") == [{"email": "owner@smiledental.sa", "attendance": 1, "permission": 0}]
          and ev["notify_attendee"] == 1, str(ev.get("attendees")))
    blob = json.dumps(ev).lower()
    check("nothing internal is on it: no phone, no channel",
          "050" not in blob and "1230001" not in blob and "whatsapp" not in blob, blob[:200])

    m = body["meeting"]
    check("the page hears it's on the calendar", m["status"] == "on_calendar", str(m)[:160])
    check("and that the invite went", bool(m["invited_at"]) and m["invite_email"] == "owner@smiledental.sa")
    check("the Zoho Meeting link is kept for a Join button",
          m["join_url"].startswith("https://meeting.zoho.sa/"), m["join_url"])
    check("no etag is sent to the page", "event_etag" not in m)

    detail = db.get_business_detail(bid, owner_id=1)
    check("the address is saved to the lead",
          [e["email"] for e in detail["emails"]] == ["owner@smiledental.sa"], str(detail["emails"]))
    check("but put in no email campaign", detail["enrollments"] == [])
    check("the panel gets the meeting", detail["meeting"] and detail["meeting"]["event_uid"])

    board = {r["business_id"]: r for r in db.get_pipeline_board(stage="booked", owner_id=1)}
    row = board.get(bid) or {}
    check("the Booked tab knows it's on the calendar and who was invited",
          row.get("has_meeting") and row.get("meeting_event_uid")
          and row.get("meeting_invite_email") == "owner@smiledental.sa", str(row)[:200])

    r = client.get(f"/api/businesses/{bid}/ics")
    text = r.get_data(as_text=True)
    check("the .ics download is an hour long too",
          "DTSTART:20261105T100000" in text and "DTEND:20261105T110000" in text, text[:300])
    return bid


def test_moving_and_stage_changes(db, z, client, token, fake, bid):
    print("\n4. MOVING IT EMAILS THE CLIENT ONLY IF ASKED; THE STAGE NEVER CANCELS IT")
    fake.calls.clear()
    s, body = book(client, token, bid, "2026-11-05T11:00", notify_change=False)
    calls = fake.calendar_calls()
    check("moving the time updates the same event",
          len(calls) == 1 and calls[0]["method"] == "PUT", str([c["method"] for c in calls]))
    ev = calls[0]["eventdata"]
    check("with the etag Zoho asked for", calls[0]["headers"].get("etag") == "1001", str(calls[0]["headers"]))
    check("still an hour, still in Riyadh",
          ev["dateandtime"]["start"] == "20261105T080000Z" and ev["dateandtime"]["timezone"] == "Asia/Riyadh")
    check("the client stays on it but isn't emailed, since the box was unticked",
          ev.get("attendees") and ev["notify_attendee"] == 0)

    fake.calls.clear()
    book(client, token, bid, "2026-11-06T11:00", notify_change=True)
    ev = fake.calendar_calls()[0]["eventdata"]
    check("ticked, the client is emailed the new time", ev["notify_attendee"] == 1)

    fake.calls.clear()
    book(client, token, bid, "2026-11-06T11:00", notify_change=True)
    ev = fake.calendar_calls()[0]["eventdata"]
    check("saving again without moving it emails nobody", ev["notify_attendee"] == 0)

    fake.calls.clear()
    s, _ = api(client, token, "put", f"/api/businesses/{bid}/pipeline",
               json={"stage": "won", "channel": "whatsapp"})
    check("changing the stage doesn't touch Zoho", s == 200 and fake.calendar_calls() == [])
    s, _ = api(client, token, "put", f"/api/businesses/{bid}/pipeline", json={"stage": ""})
    check("nor does clearing it", s == 200 and fake.calendar_calls() == [])
    m = db.get_meeting(bid)
    check("the meeting is still on", m and not m["cancelled_at"] and m["event_uid"])
    check("and still on the calendar", m["event_uid"] in fake.events)

    book(client, token, bid, "2026-11-06T11:00")
    check("booking it again picks the same meeting back up",
          db.get_meeting(bid)["event_uid"] == m["event_uid"])


def test_invite_later(db, z, client, token, fake, camp):
    print("\n5. BOOKED WITHOUT AN INVITE, THEN INVITED FROM THE PANEL")
    bid = new_lead(db, "Bright Clinic", "050 123 0002", camp)
    fake.calls.clear()
    s, body = book(client, token, bid, "2026-11-07T15:30", timezone="Asia/Riyadh",
                   invite_email="info@bright.sa", send_invite=False)
    ev = fake.calendar_calls()[0]["eventdata"]
    check("unticked: the client is not on the event", "attendees" not in ev and ev["notify_attendee"] == 0)
    check("and hasn't been invited", body["meeting"]["invited_at"] is None)
    check("the address typed is still remembered", body["meeting"]["invite_email"] == "info@bright.sa")

    fake.calls.clear()
    s, body = api(client, token, "post", f"/api/businesses/{bid}/meeting/invite",
                  json={"email": "dr@bright.sa", "save_email": True})
    ev = fake.calendar_calls()[0]["eventdata"] if fake.calendar_calls() else {}
    check("Send invite adds them and tells Zoho to email them",
          s == 200 and ev.get("attendees") == [{"email": "dr@bright.sa", "attendance": 1, "permission": 0}]
          and ev.get("notify_attendee") == 1, str(body)[:160])
    check("and records that it went", bool(body["meeting"]["invited_at"]))
    check("the address is saved to the lead",
          "dr@bright.sa" in [e["email"] for e in db.get_business_detail(bid, owner_id=1)["emails"]])
    s, _ = api(client, token, "post", f"/api/businesses/{bid}/meeting/invite", json={"email": "nope"})
    check("an address that isn't one is refused", s == 400)


def test_own_link_and_bad_input(db, z, client, token, fake, camp):
    print("\n6. YOUR OWN LINK (GOOGLE MEET), AND INPUT THAT'S REFUSED WHOLE")
    bid = new_lead(db, "Meet Clinic", "050 123 0003", camp)
    fake.calls.clear()
    s, body = book(client, token, bid, "2026-11-08T09:00", link_kind="own",
                   own_link="https://meet.google.com/abc-defg-hij")
    ev = fake.calendar_calls()[0]["eventdata"]
    check("no Zoho room is made", ev["conference"] == "none")
    check("the link is where the client will see it",
          ev.get("location") == "https://meet.google.com/abc-defg-hij"
          and "meet.google.com/abc-defg-hij" in ev.get("description", ""))
    check("and it's the Join link", body["meeting"]["join_url"] == "https://meet.google.com/abc-defg-hij")

    other = new_lead(db, "Refused Clinic", "050 123 0004", camp)
    fake.calls.clear()
    s, body = book(client, token, other, "2026-11-08T09:00", link_kind="own", own_link="")
    check("own link with no link is refused", s == 400, str(body))
    check("and nothing was saved: not the stage, not a meeting, not Zoho",
          db.get_business(other)["pipeline_stage"] == "" and db.get_meeting(other) is None
          and fake.calendar_calls() == [])
    s, _ = book(client, token, other, "2026-11-08T09:00", send_invite=True, invite_email="")
    check("an invite with no address is refused", s == 400)
    s, _ = book(client, token, other, "2026-11-08T09:00", timezone="Nowhere/Land")
    check("an unknown timezone is refused", s == 400)


def test_zoho_failure_and_retry(db, z, client, token, fake, camp):
    print("\n7. ZOHO FAILING NEVER LOSES THE BOOKING")
    bid = new_lead(db, "Flaky Clinic", "050 123 0005", camp)
    fake.fail_next = (500, "Calendar service is unavailable")
    s, body = book(client, token, bid, "2026-11-09T12:00")
    check("the booking is saved anyway", s == 200 and body["pipeline_stage"] == "booked")
    check("with Zoho's words and a failed status",
          body["meeting"]["status"] == "failed"
          and "unavailable" in body["meeting"]["sync_error"], str(body["meeting"])[:160])
    s, body = api(client, token, "post", f"/api/businesses/{bid}/meeting/sync")
    check("Try again puts it on the calendar", s == 200 and body["ok"]
          and body["meeting"]["status"] == "on_calendar", str(body)[:160])
    check("and clears the error", body["meeting"]["sync_error"] == "")


def test_token_refresh(db, z, client, token, fake, bid):
    print("\n8. AN EXPIRED ACCESS TOKEN IS RENEWED QUIETLY")
    db.save_zoho_connection(1, access_expires=0)
    fake.calls.clear()
    s, body = api(client, token, "post", f"/api/businesses/{bid}/meeting/sync")
    refresh = [c for c in fake.calls if c["data"].get("grant_type") == "refresh_token"]
    check("the refresh token is used", len(refresh) == 1 and refresh[0]["data"]["refresh_token"] == "rt-secret")
    check("and the sync carries on with the new token", s == 200 and body["ok"], str(body)[:120])
    check("which is kept", db.get_zoho_connection(1)["access_expires"] > 0)


def test_cancel(db, z, client, token, fake, bid):
    print("\n9. CANCEL MEETING: THE ONE THING THAT CANCELS")
    uid = db.get_meeting(bid)["event_uid"]
    fake.calls.clear()
    s, body = api(client, token, "post", f"/api/businesses/{bid}/meeting/cancel",
                  json={"notify": True, "clear_stage": True})
    calls = fake.calendar_calls()
    check("it comes off the calendar",
          s == 200 and len(calls) == 1 and calls[0]["method"] == "DELETE" and uid not in fake.events,
          str(body)[:120])
    check("with the client told, since they were invited and the box was ticked",
          calls[0]["eventdata"]["notify_attendee"] == 1)
    check("it reads as cancelled", body["meeting"]["status"] == "cancelled")
    check("and the stage was cleared as asked", db.get_business(bid)["pipeline_stage"] == "")
    check("so it's off the Booked tab",
          bid not in [r["business_id"] for r in db.get_pipeline_board(stage="booked", owner_id=1)])
    s, _ = api(client, token, "post", f"/api/businesses/{bid}/meeting/cancel", json={})
    check("cancelling twice is refused", s == 400)

    fake.calls.clear()
    s, body = book(client, token, bid, "2026-11-20T10:00", send_invite=False)
    calls = fake.calendar_calls()
    check("booking again after a cancel makes a new event",
          calls and calls[0]["method"] == "POST" and body["meeting"]["event_uid"] != uid)
    check("and forgets the old invite", body["meeting"]["invited_at"] is None)

    fake.calls.clear()
    other_uid = body["meeting"]["event_uid"]
    del fake.events[other_uid]
    s, body = api(client, token, "post", f"/api/businesses/{bid}/meeting/cancel", json={"notify": True})
    check("an event already gone from Zoho still cancels here", s == 200 and body["ok"], str(body))
    check("and an uninvited client is never emailed", fake.calendar_calls()[0]["eventdata"]["notify_attendee"] == 0)


def test_calling_books(db, z, client, token, fake):
    print("\n10. A MEETING BOOKED ON A CALL GOES ON THE CALENDAR, WITHOUT AN INVITE")
    db.upsert_businesses([{"company": "Phoned Clinic", "phone": "050 123 0006"}])
    bid = next(b["id"] for b in db.get_businesses_page(q="Phoned Clinic")["rows"])
    fake.calls.clear()
    s, body = api(client, token, "post", "/api/calls/log",
                  json={"contact_id": bid, "outcome": "booked",
                        "next_call_at": "2026-11-10T16:00", "timezone": "Europe/London"})
    calls = fake.calendar_calls()
    check("logged, and on the calendar", s == 200 and body["meeting"]["status"] == "on_calendar",
          str(body)[:200])
    ev = calls[0]["eventdata"]
    check("in your own timezone from My settings (Riyadh), not the browser's",
          ev["dateandtime"]["timezone"] == "Asia/Riyadh" and ev["dateandtime"]["start"] == "20261110T130000Z",
          str(ev["dateandtime"]))
    check("nobody is invited", "attendees" not in ev and ev["notify_attendee"] == 0)


def test_suppression_warning(db, z, client, token, camp):
    print("\n11. AN ADDRESS THAT UNSUBSCRIBED: A WARNING, AND IT STAYS UNSUBSCRIBED")
    bob = db.get_user_by_username("bob")["id"]
    db.create_email_lead("gone@away.sa", company="Away Co", owner_id=bob)
    db.unsubscribe_contact("gone@away.sa")
    s, body = api(client, token, "get", "/api/meetings/address-check?email=Gone@Away.sa")
    check("you're warned, whoever's list it unsubscribed from",
          s == 200 and "unsubscribed" in body["warning"], str(body))
    check("without saying whose list it was", "bob" not in body["warning"].lower())
    s, body = api(client, token, "get", "/api/meetings/address-check?email=fresh@new.sa")
    check("a fresh address gets no warning", body["warning"] == "")

    bid = new_lead(db, "Away Lead", "050 123 0007", camp, owner_id=1)
    ok, err = db.add_business_email(bid, "gone@away.sa", owner_id=1)
    status = next(e["status"] for e in db.get_business_detail(bid, owner_id=1)["emails"])
    check("saving it to a lead keeps it unsubscribed, so no sequence can mail it",
          ok and status == "unsubscribed", f"{ok} {err} {status}")


def test_walls(db, z, app_mod, client, token, fake, alice_bid, camp):
    print("\n12. WALLED PER OPERATOR")
    bob = db.get_user_by_username("bob")["id"]
    bob_client = app_mod.app.test_client()
    bob_token = login(bob_client, "bob")

    for path in ("sync", "invite", "cancel"):
        s, _ = api(bob_client, bob_token, "post", f"/api/businesses/{alice_bid}/meeting/{path}",
                   json={"email": "x@y.sa"})
        check(f"Bob can't {path} Alice's meeting (404, not 403)", s == 404, str(s))
    s, body = api(bob_client, bob_token, "get", "/api/zoho/status")
    check("Bob isn't connected because Alice is", s == 200 and body["connected"] is False)
    check("and sees none of her connection", "alice@hexiv.co" not in json.dumps(body))
    check("his defaults are his own", body["prefs"]["timezone"] == "")

    bob_camp = db.create_wa_campaign("Bob's", owner_id=bob, country="SA")
    bob_bid = new_lead(db, "Bob Clinic", "050 123 0008", bob_camp, owner_id=bob)
    fake.calls.clear()
    s, body = book(bob_client, bob_token, bob_bid, "2026-11-11T10:00")
    check("unconnected, a booking is kept here only",
          s == 200 and body["meeting"]["status"] == "not_on_calendar" and fake.calls == [], str(body)[:160])
    s, body = api(bob_client, bob_token, "post", f"/api/businesses/{bob_bid}/meeting/sync")
    check("and Add to calendar asks him to connect", s == 400 and "Connect" in body["error"])
    s, _ = api(client, token, "post", f"/api/businesses/{bob_bid}/meeting/cancel", json={})
    check("Alice can't cancel Bob's", s == 404)


def test_region_change_and_disconnect(db, z, client, token, fake, bid):
    print("\n13. CHANGING THE REGION, AND DISCONNECTING")
    api(client, token, "post", "/api/settings", json={"zoho_region": "eu"})
    s, body = api(client, token, "get", "/api/zoho/status")
    check("a region change asks for a reconnect", not body["connected"] and body["needs_reconnect"])
    s, body = api(client, token, "post", f"/api/businesses/{bid}/meeting/sync")
    check("and nothing is sent to the wrong data centre", s == 400)
    api(client, token, "post", "/api/settings", json={"zoho_region": "sa"})

    fake.calls.clear()
    s, _ = api(client, token, "post", "/api/zoho/disconnect")
    revoked = [c for c in fake.calls if c["url"].endswith("/oauth/v2/token/revoke")]
    check("disconnecting asks Zoho to revoke the token", s == 200 and len(revoked) == 1)
    check("and forgets it here", db.get_zoho_connection(1) is None)
    check("meetings already booked stay", db.get_meeting(bid) is not None)


def test_delete_and_restore(db, bid):
    print("\n14. DELETING A LEAD TAKES ITS MEETING TO RECENTLY DELETED, AND BACK")
    db.delete_businesses([bid], owner_id=1)
    check("the meeting goes with the lead", db.get_meeting(bid) is None)
    trashed = [i["id"] for g in db.get_deleted_leads(owner_id=1) for i in g["items"]
               if i["business_id"] == bid]
    res = db.restore_deleted_leads(ids=trashed, owner_id=1)
    check("and comes back with it", db.get_meeting(bid) is not None, str(res))


def test_reinit(work):
    print("\n15. RE-RUNNING init_db IS HARMLESS")
    import db as db_mod
    db_mod.init_db()
    db_mod.init_db()
    with db_mod.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    check("meetings survive a restart", n > 0, str(n))


def main():
    work = tempfile.mkdtemp(prefix="zoho-")
    try:
        db, z, app_mod, client, token, fake = boot(work)
        camp = db.create_wa_campaign("Zoho campaign", country="SA")

        test_setup_and_connect(db, z, client, token, fake)
        test_prefs(db, z, client, token)
        bid = test_book_with_invite(db, z, client, token, fake, camp)
        test_moving_and_stage_changes(db, z, client, token, fake, bid)
        test_invite_later(db, z, client, token, fake, camp)
        test_own_link_and_bad_input(db, z, client, token, fake, camp)
        test_zoho_failure_and_retry(db, z, client, token, fake, camp)
        test_token_refresh(db, z, client, token, fake, bid)
        test_cancel(db, z, client, token, fake, bid)
        # Single-operator helpers refuse to guess an owner once a second user
        # exists, so everything above runs before Bob does.
        test_calling_books(db, z, client, token, fake)
        db.create_user("bob", "testpassword123", is_admin=False)
        test_suppression_warning(db, z, client, token, camp)
        alice_live = new_lead(db, "Alice Live", "050 123 0009", camp, owner_id=1)
        book(client, token, alice_live, "2026-11-12T10:00")
        test_walls(db, z, app_mod, client, token, fake, alice_live, camp)
        test_region_change_and_disconnect(db, z, client, token, fake, alice_live)
        test_delete_and_restore(db, alice_live)
        test_reinit(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
