"""A Cal.com booking updates the lead (Plan 2 package 3).

Run:  python tests/test_cal_bookings.py     (exits 0 on pass, 1 on failure)

Built on real deliveries, captured from the live route and scrubbed
(tests/fixtures/cal_*.json): a ping, a booking made through hexiv.co.uk's
form, its reschedule, and its cancellation. Each is signed and posted to /api/inbound/cal the
way Cal.com does, and the lead, its stage, its meeting and its follow-ups are
checked afterwards. The cancellation was made from the link in Cal.com's
email, after the reschedule. Nothing here may write a wa_log row: WhatsApp is only ever
sent by a person.
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["SHOUTREACH_NO_SCHEDULER"] = "1"

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


def q(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


CREATED, MOVED, PING = (fixture("cal_booking_created.json"), fixture("cal_booking_rescheduled.json"),
                        fixture("cal_ping.json"))
CANCELLED = fixture("cal_booking_cancelled.json")
EMAIL = CREATED["payload"]["attendees"][0]["email"]          # sam.booker@example.com
OLD_UID, NEW_UID = CREATED["payload"]["uid"], MOVED["payload"]["uid"]


def boot(work, name):
    path = os.path.join(work, name)
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
    secret = db.rotate_cal_secret()
    client = app_mod.app.test_client()

    def deliver(body):
        raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return client.post("/api/inbound/cal", data=raw,
                           headers={"X-Cal-Signature-256": sig, "Content-Type": "application/json"})
    return db, path, a, b, deliver, app_mod


def biz_for(path, email):
    rows = q(path, "SELECT business_id FROM email_leads WHERE email=?", (email,))
    return rows[0][0] if rows else None


def test_ping_and_created(work):
    print("\n1. A PING IS IGNORED; A BOOKING MAKES A BOOKED LEAD")
    db, path, a, b, deliver, app_mod = boot(work, "created.db")
    db.save_website_config({"sites": {"hexiv.co.uk": {"owner_id": b, "country": "GB"}}})
    r = deliver(PING)
    check("a ping answers 200", r.status_code == 200, str(r.status_code))
    check("and creates nothing", q(path, "SELECT COUNT(*) FROM businesses")[0][0] == 0)
    check("it's marked read and ignored",
          q(path, "SELECT processed_at IS NOT NULL, error FROM cal_events") == [(1, "ignored (PING)")])

    r = deliver(CREATED)
    check("the booking answers 200", r.status_code == 200, str(r.status_code))
    bid = biz_for(path, EMAIL)
    check("a lead for the site's owner (no enquiry came first)",
          bid and q(path, "SELECT owner_id, name, phone FROM businesses WHERE id=?", (bid,))
          == [(b, "Booker Dental", "07700900123")], str(q(path, "SELECT owner_id, name, phone FROM businesses")))
    stage = q(path, "SELECT pipeline_stage, pipeline_channel, next_action_at FROM businesses WHERE id=?", (bid,))
    # 22:00 UTC on 22 Nov is 18:30 in St John's (UTC-3:30), the host's zone.
    check("booked, via the website, at the host's wall-clock time",
          stage == [("booked", "website", "2026-11-22 18:30:00")], str(stage))
    m = q(path, "SELECT source, cal_uid, state, starts_at, timezone, starts_utc, minutes, join_url "
                "FROM meetings WHERE business_id=?", (bid,))
    check("the meeting: from Cal.com, its uid, booked, both times, 30 min, the join link",
          m == [("cal", OLD_UID, "booked", "2026-11-22 18:30:00", "America/St_Johns",
                 "2026-11-22 22:00:00", 30, f"https://app.cal.com/video/{OLD_UID}")], str(m))
    e = q(path, "SELECT site, cal_booking_id, answers FROM website_enquiries WHERE email=?", (EMAIL,))
    answers = json.loads(e[0][2]) if e else {}
    check("an enquiry is recorded with the booking's uid", e and e[0][:2] == ("hexiv.co.uk", OLD_UID))
    check("its answers come from the site's summary, contact lines left out",
          answers.get("Business type") == "Something else" and answers.get("Enquiries per month") == "10–50"
          and "Business name" not in answers and "Phone / WhatsApp" not in answers, str(answers))
    check("the email is an email lead with the first name",
          q(path, "SELECT first_name, status FROM email_leads WHERE email=?", (EMAIL,)) == [("Sam", "active")])

    print("   the same booking again")
    before = q(path, "SELECT * FROM meetings") + q(path, "SELECT id, pipeline_stage, next_action_at FROM businesses")
    deliver(CREATED)
    again = copy.deepcopy(CREATED)
    again["createdAt"] = "2026-11-22T01:33:00.000Z"      # a retry whose body differs
    r = deliver(again)
    after = q(path, "SELECT * FROM meetings") + q(path, "SELECT id, pipeline_stage, next_action_at FROM businesses")
    check("a repeat delivery changes nothing", r.status_code == 200 and before == after)
    check("and adds no second lead or enquiry",
          q(path, "SELECT COUNT(*) FROM businesses")[0][0] == 1
          and q(path, "SELECT COUNT(*) FROM website_enquiries")[0][0] == 1)

    print("   rescheduled")
    r = deliver(MOVED)
    check("answers 200", r.status_code == 200)
    m = q(path, "SELECT cal_uid, state, starts_at, starts_utc, join_url FROM meetings WHERE business_id=?", (bid,))
    # 23:30 UTC is 20:00 in St John's.
    check("the meeting moves: new uid, 'moved', new time, new link",
          m == [(NEW_UID, "moved", "2026-11-22 20:00:00", "2026-11-22 23:30:00",
                 f"https://app.cal.com/video/{NEW_UID}")], str(m))
    check("the lead's time follows; the stage stays booked",
          q(path, "SELECT pipeline_stage, next_action_at FROM businesses WHERE id=?", (bid,))
          == [("booked", "2026-11-22 20:00:00")])
    check("the enquiry now names the new booking",
          q(path, "SELECT cal_booking_id FROM website_enquiries") == [(NEW_UID,)])
    deliver(MOVED)
    check("a repeat reschedule changes nothing",
          q(path, "SELECT cal_uid, state FROM meetings") == [(NEW_UID, "moved")])

    print("   cancelled")
    before_stage = q(path, "SELECT pipeline_stage, next_action_at FROM businesses WHERE id=?", (bid,))
    r = deliver(CANCELLED)
    check("answers 200", r.status_code == 200)
    m = q(path, "SELECT state, cancelled_at IS NOT NULL, cal_uid FROM meetings WHERE business_id=?", (bid,))
    check("the meeting (found by the rescheduled uid) is marked cancelled", m == [("cancelled", 1, NEW_UID)], str(m))
    check("the stage is NOT changed: the operator decides",
          q(path, "SELECT pipeline_stage, next_action_at FROM businesses WHERE id=?", (bid,)) == before_stage
          == [("booked", "2026-11-22 20:00:00")])
    when = q(path, "SELECT cancelled_at FROM meetings")
    deliver(CANCELLED)
    check("a repeat cancellation changes nothing", q(path, "SELECT cancelled_at FROM meetings") == when)
    check("the lead panel's detail carries the cancelled meeting",
          db.get_business_detail(bid, owner_id=b)["meeting"]["state"] == "cancelled")
    stray = copy.deepcopy(CANCELLED)
    stray["payload"]["uid"] = "never-booked"
    r = deliver(stray)
    check("cancelling a booking we never saw: 200, noted on the event, nothing else",
          r.status_code == 200 and q(path, "SELECT error FROM cal_events ORDER BY id DESC")[0][0]
          == "cancelled booking not on record")
    check("every delivery was read",
          q(path, "SELECT COUNT(*) FROM cal_events WHERE processed_at IS NULL")[0][0] == 0)
    check("no wa_log row was written", q(path, "SELECT COUNT(*) FROM wa_log")[0][0] == 0)


def test_enquiry_first_and_followups(work):
    print("\n2. THE ENQUIRY CAME FIRST; THE BOOKING STOPS ITS FOLLOW-UPS")
    db, path, a, b, deliver, app_mod = boot(work, "followups.db")
    db.save_website_config({"sites": {"hexiv.co.uk": {"owner_id": b, "country": "GB"}}})
    enquiry = db.clean_website_enquiry({"site": "hexiv.co.uk", "name": "Sam Booker", "email": EMAIL,
                                        "business": "Booker Dental", "phone": "07700 900123",
                                        "answers": {"Business type": "Something else"}})
    db.record_website_enquiry(enquiry)
    bid = biz_for(path, EMAIL)
    lead = q(path, "SELECT id FROM email_leads WHERE email=?", (EMAIL,))[0][0]
    camp = db.create_campaign("Nurture", owner_id=b)
    db.enroll_contacts_bulk(camp, [lead], owner_id=b)
    wa = db.create_wa_campaign("WA", owner_id=b)
    wa = wa[0] if isinstance(wa, tuple) else wa
    db.upsert_wa_leads([{"company": "Booker Dental", "phone": "07700 900123"}], default_country="GB",
                       owner_id=b, wa_campaign_id=wa)
    check("before: on WhatsApp To do and in an email sequence",
          q(path, "SELECT paused, sent_date FROM wa_leads WHERE business_id=?", (bid,)) == [(0, None)]
          and q(path, "SELECT status FROM enrollments") == [("queued",)])
    deliver(CREATED)
    check("the booking lands on the enquiry's lead, not a new one",
          q(path, "SELECT COUNT(*) FROM businesses")[0][0] == 1 and biz_for(path, EMAIL) == bid)
    check("the enquiry is marked booked", q(path, "SELECT cal_booking_id FROM website_enquiries") == [(OLD_UID,)])
    check("its email sequence has ended", q(path, "SELECT status FROM enrollments") == [("completed",)])
    check("its unsent WhatsApp lead is paused, so it leaves To do",
          q(path, "SELECT paused, replied FROM wa_leads WHERE business_id=?", (bid,)) == [(1, 0)])
    check("and nothing was sent: no wa_log row", q(path, "SELECT COUNT(*) FROM wa_log")[0][0] == 0)


def test_booked_from_any_channel(work):
    print("\n3. BOOKED FROM ANY CHANNEL STOPS EMAIL AND UNSENT WHATSAPP")
    db, path, a, b, deliver, app_mod = boot(work, "anychannel.db")
    db.upsert_businesses([{"company": "Call Clinic", "email": "c@callclinic.co.uk", "phone": "07700 900555"}],
                         owner_id=a)
    bid = biz_for(path, "c@callclinic.co.uk")
    camp = db.create_campaign("Seq", owner_id=a)
    db.enroll_contacts_bulk(camp, [q(path, "SELECT id FROM email_leads")[0][0]], owner_id=a)
    wa = db.create_wa_campaign("WA", owner_id=a)
    wa = wa[0] if isinstance(wa, tuple) else wa
    db.upsert_wa_leads([{"company": "Call Clinic", "phone": "07700 900555"}], default_country="GB",
                       owner_id=a, wa_campaign_id=wa)
    db.set_business_pipeline(bid, "booked", "whatsapp", next_action_at="2026-12-01 10:00", owner_id=a)
    check("set by hand: email ended, unsent WhatsApp paused",
          q(path, "SELECT status FROM enrollments") == [("completed",)]
          and q(path, "SELECT paused FROM wa_leads") == [(1,)])
    db.upsert_businesses([{"company": "Phone Clinic", "email": "p@phoneclinic.co.uk"}], owner_id=a)
    pid = biz_for(path, "p@phoneclinic.co.uk")
    db.enroll_contacts_bulk(camp, [q(path, "SELECT id FROM email_leads WHERE business_id=?", (pid,))[0][0]],
                            owner_id=a)
    with db.get_db() as conn:
        cl = db.get_or_create_call_lead(conn, pid)
    db.log_call(cl, "booked", next_call_at="2026-12-02 09:00")
    check("a call outcome 'booked' (the shared stop-email helper): email ended",
          q(path, "SELECT e.status FROM enrollments e JOIN email_leads el ON el.id=e.email_lead_id "
                  "WHERE el.business_id=?", (pid,)) == [("completed",)])
    db.upsert_businesses([{"company": "Proposal Clinic", "email": "q@proposalclinic.co.uk"}], owner_id=a)
    qid = biz_for(path, "q@proposalclinic.co.uk")
    db.enroll_contacts_bulk(camp, [q(path, "SELECT id FROM email_leads WHERE business_id=?", (qid,))[0][0]],
                            owner_id=a)
    db.set_business_pipeline(qid, "proposal_sent", "email", owner_id=a)
    check("other stages don't stop email (unchanged behaviour)",
          q(path, "SELECT e.status FROM enrollments e JOIN email_leads el ON el.id=e.email_lead_id "
                  "WHERE el.business_id=?", (qid,)) == [("queued",)])
    check("no wa_log row", q(path, "SELECT COUNT(*) FROM wa_log")[0][0] == 0)


def test_awkward_deliveries(work):
    print("\n4. AWKWARD DELIVERIES")
    db, path, a, b, deliver, app_mod = boot(work, "awkward.db")
    r = deliver(CREATED)
    check("a site with no owner yet: 200, kept for later", r.status_code == 200
          and q(path, "SELECT processed_at IS NULL, error != '' FROM cal_events") == [(1, 1)])
    check("nothing filed yet", q(path, "SELECT COUNT(*) FROM businesses")[0][0] == 0)
    db.save_website_config({"sites": {"hexiv.co.uk": {"owner_id": a, "country": "GB"}}})
    db.file_waiting_enquiries()
    db.process_cal_events()
    check("once an owner is set, it's filed and booked",
          q(path, "SELECT owner_id, pipeline_stage FROM businesses") == [(a, "booked")]
          and q(path, "SELECT processed_at IS NOT NULL, error FROM cal_events") == [(1, "")])
    check("still only one enquiry", q(path, "SELECT COUNT(*) FROM website_enquiries")[0][0] == 1)

    nosite = copy.deepcopy(CREATED)
    nosite["payload"]["uid"] = "no-site-uid"
    nosite["payload"]["metadata"].pop("site")
    r = deliver(nosite)
    check("a booking with no site: 200, recorded as not filed",
          r.status_code == 200 and "no known site" in q(path, "SELECT error FROM cal_events ORDER BY id DESC")[0][0])

    broken = copy.deepcopy(CREATED)
    broken["payload"]["uid"] = "broken-uid"
    broken["payload"].pop("startTime")
    r = deliver(broken)
    check("a malformed booking: still 200, the error written on it",
          r.status_code == 200 and q(path, "SELECT error FROM cal_events ORDER BY id DESC")[0][0].startswith("error"))

    orphan = copy.deepcopy(MOVED)
    orphan["payload"]["uid"] = "orphan-new"
    orphan["payload"]["rescheduleUid"] = "never-seen"
    orphan["payload"]["attendees"][0]["email"] = "late@example.com"
    deliver(orphan)
    check("a reschedule whose original never arrived is filed as a booking, marked moved",
          q(path, "SELECT m.state, m.cal_uid FROM meetings m JOIN email_leads el ON el.business_id=m.business_id "
                  "WHERE el.email='late@example.com'") == [("moved", "orphan-new")])
    check("no wa_log row", q(path, "SELECT COUNT(*) FROM wa_log")[0][0] == 0)


def test_rebooked_by_hand(work):
    print("\n5. CANCELLED ON CAL.COM, THEN BOOKED BY HAND")
    db, path, a, b, deliver, app_mod = boot(work, "rebooked.db")
    db.save_website_config({"sites": {"hexiv.co.uk": {"owner_id": b, "country": "GB"}}})
    deliver(CREATED)
    deliver(MOVED)
    deliver(CANCELLED)
    bid = biz_for(path, EMAIL)
    c = app_mod.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = b
        sess["csrf_token"] = "t"
    r = c.put(f"/api/businesses/{bid}/pipeline", headers={"X-CSRF-Token": "t"},
              json={"stage": "booked", "channel": "whatsapp", "next_action_at": "2026-12-03T14:00"})
    check("the hand booking saves", r.status_code == 200, str(r.status_code))
    check("the cancelled Cal.com meeting is replaced: no row left",
          q(path, "SELECT COUNT(*) FROM meetings WHERE business_id=?", (bid,))[0][0] == 0)
    m = [x for x in db.get_meetings(owner_id=b) if x["business_id"] == bid]
    check("Meetings shows the new time, set from WhatsApp, not 'cancelled'",
          len(m) == 1 and m[0]["starts_at"] == "2026-12-03 14:00:00" and m[0]["pipeline_channel"] == "whatsapp"
          and m[0]["meeting_state"] is None, str(m))
    check("and the lead's detail has no meeting row to show", db.get_business_detail(bid, owner_id=b)["meeting"] is None)

    db.save_website_config({"sites": {"hexiv.co.uk": {"owner_id": a, "country": "GB"}}})
    live = json.loads(json.dumps(CREATED))
    live["payload"]["uid"] = "live-booking"
    live["payload"]["attendees"][0]["email"] = "live@example.com"
    live["payload"]["metadata"]["businessName"] = "Live Clinic"
    live["payload"]["metadata"]["phone"] = "07700 900765"
    deliver(live)
    lid = biz_for(path, "live@example.com")
    with c.session_transaction() as sess:
        sess["user_id"] = a
    c.put(f"/api/businesses/{lid}/pipeline", headers={"X-CSRF-Token": "t"},
          json={"stage": "booked", "channel": "call", "next_action_at": "2026-12-04T09:00"})
    check("a Cal.com meeting that's still on is never touched by a hand booking",
          q(path, "SELECT cal_uid, state FROM meetings WHERE business_id=?", (lid,)) == [("live-booking", "booked")])
    with db.get_db() as conn:
        conn.execute("UPDATE meetings SET state='cancelled' WHERE business_id=?", (lid,))
        cl = db.get_or_create_call_lead(conn, lid)
    db.log_call(cl, "booked", next_call_at="2026-12-05 11:00")
    check("a 'Meeting booked' call outcome replaces a cancelled one too",
          q(path, "SELECT COUNT(*) FROM meetings WHERE business_id=?", (lid,))[0][0] == 0)
    check("no wa_log row", q(path, "SELECT COUNT(*) FROM wa_log")[0][0] == 0)


def main():
    work = tempfile.mkdtemp(prefix="cal_bookings_")
    try:
        test_ping_and_created(work)
        test_enquiry_first_and_followups(work)
        test_booked_from_any_channel(work)
        test_awkward_deliveries(work)
        test_rebooked_by_hand(work)
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
