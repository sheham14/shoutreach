"""Following up people who reached the calendar and didn't book (Plan 2 package 4).

Run:  python tests/test_website_followups.py     (exits 0 on pass, 1 on failure)

After the WhatsApp wait (30 minutes by default) an enquiry that hasn't booked
is followed up once: added to its owner's chosen WhatsApp campaign, ready to
send by hand, and enrolled in their chosen email campaign with the first email
held until arrival plus the email wait (24 hours). Every check runs the job
with an injected clock. Nothing here may send a WhatsApp: no wa_log row.
"""
import datetime
import importlib
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SHOUTREACH_NO_SCHEDULER"] = "1"

_failures = []
# Two days ahead of the real clock, on the hour. The job takes an injected
# "now", but enroll_contacts_bulk compares a first send's "not before" with the
# real clock -- so a fixed date passes only until the real day catches up
# with it. (It did: this test went red on 22 Nov after 12:00 UTC.)
ARRIVED = (datetime.datetime.utcnow() + datetime.timedelta(days=2)).replace(minute=0, second=0, microsecond=0)


def ts(minutes):
    """ARRIVED plus this many minutes, as the database writes it."""
    return (ARRIVED + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


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


def setup(work, name):
    path = os.path.join(work, name)
    os.environ["DB_PATH"] = path
    os.environ["SECRET_KEY"] = "test-secret"
    import db
    importlib.reload(db)
    db.init_db()
    a = db.create_user("sheham", "test-password-123", is_admin=True)
    b = db.create_user("anas", "test-password-123", is_admin=False)
    db.save_website_config({"sites": {"hexiv.co.uk": {"owner_id": b, "country": "GB"}}})
    email_camp = db.create_campaign("Website nurture", owner_id=b)
    wa = db.create_wa_campaign("Website WA", owner_id=b)
    wa = wa[0] if isinstance(wa, tuple) else wa
    db.save_website_followup(email_camp, wa, owner_id=b)
    return db, path, a, b, email_camp, wa


def enquire(db, path, email, phone="07700 900123", business="Clinic"):
    db.record_website_enquiry(db.clean_website_enquiry({
        "site": "hexiv.co.uk", "name": "Pat Person", "email": email, "business": business, "phone": phone}))
    with db.get_db() as conn:
        conn.execute("UPDATE website_enquiries SET created_at=? WHERE email=?",
                     (ARRIVED.strftime("%Y-%m-%d %H:%M:%S"), email))
    return q(path, "SELECT business_id FROM website_enquiries WHERE email=?", (email,))[0][0]


def at(minutes):
    return ARRIVED + datetime.timedelta(minutes=minutes)


def main():
    work = tempfile.mkdtemp(prefix="web_followups_")
    try:
        print("\n1. THE WAIT, THEN BOTH FOLLOW-UPS, ONCE")
        db, path, a, b, email_camp, wa = setup(work, "both.db")
        bid = enquire(db, path, "pat@clinic.co.uk")
        db.run_website_followups(now=at(29))
        check("nothing before the WhatsApp wait (29 of 30 minutes)",
              q(path, "SELECT COUNT(*) FROM wa_leads")[0][0] == 0 and q(path, "SELECT COUNT(*) FROM enrollments")[0][0] == 0
              and q(path, "SELECT followup_queued_at FROM website_enquiries") == [(None,)])
        counts = db.run_website_followups(now=at(31))
        check("after it: on the chosen WhatsApp campaign", q(path, "SELECT wa_campaign_id, wa_status, sent_date, paused "
              "FROM wa_leads WHERE business_id=?", (bid,)) == [(wa, "drafted", None, 0)], str(counts))
        check("ready to send by hand -- in To do, nothing sent",
              any(l["business_id"] == bid for l in db.get_wa_ready(owner_id=b))
              and q(path, "SELECT COUNT(*) FROM wa_log")[0][0] == 0)
        enrol = q(path, "SELECT campaign_id, status, next_send_at FROM enrollments")
        check("and enrolled in the chosen email campaign, first email at arrival + 24 hours",
              enrol == [(email_camp, "queued", ts(24 * 60))], str(enrol))
        check("marked queued", q(path, "SELECT followup_queued_at FROM website_enquiries") == [(ts(31),)])
        db.run_website_followups(now=at(90))
        db.record_website_enquiry(db.clean_website_enquiry({
            "site": "hexiv.co.uk", "name": "Pat Person", "email": "pat@clinic.co.uk", "summary": "posted again"}))
        check("a repeat post of the form doesn't end the follow-up email it started",
              q(path, "SELECT status FROM enrollments") == [("queued",)])
        check("run twice, queued once",
              q(path, "SELECT COUNT(*) FROM wa_leads")[0][0] == 1 and q(path, "SELECT COUNT(*) FROM enrollments")[0][0] == 1
              and q(path, "SELECT followup_queued_at FROM website_enquiries") == [(ts(31),)])
        with db.get_db() as conn:
            conn.execute("UPDATE campaigns SET status='active' WHERE id=?", (email_camp,))
        check("the email isn't due before its time", not db.get_due_enrollments(email_camp, 10))

        print("\n2. ONLY EMAIL WHEN THERE'S NO NUMBER")
        db, path, a, b, email_camp, wa = setup(work, "nophone.db")
        enquire(db, path, "nonum@clinic.co.uk", phone="")
        db.run_website_followups(now=at(31))
        check("no WhatsApp lead", q(path, "SELECT COUNT(*) FROM wa_leads")[0][0] == 0)
        check("but enrolled for email", q(path, "SELECT COUNT(*) FROM enrollments")[0][0] == 1)

        print("\n3. NEITHER WHEN THEY BOOKED IN BETWEEN")
        db, path, a, b, email_camp, wa = setup(work, "booked.db")
        bid = enquire(db, path, "booked@clinic.co.uk")
        with db.get_db() as conn:
            conn.execute("UPDATE website_enquiries SET cal_booking_id='abc'")
        db.run_website_followups(now=at(31))
        check("booked on Cal.com: no WhatsApp, no email",
              q(path, "SELECT COUNT(*) FROM wa_leads")[0][0] == 0 and q(path, "SELECT COUNT(*) FROM enrollments")[0][0] == 0)
        check("and it isn't looked at again", q(path, "SELECT followup_queued_at IS NOT NULL FROM website_enquiries") == [(1,)])
        bid2 = enquire(db, path, "hello@otherclinic.co.uk", business="Other Clinic", phone="07700 900999")
        check("(a separate lead from the first)", bid2 != bid)
        db.set_business_pipeline(bid2, "booked", "call", next_action_at=ts(3 * 24 * 60)[:16], owner_id=b)
        db.run_website_followups(now=at(31))
        check("booked by hand on another channel: neither either",
              q(path, "SELECT COUNT(*) FROM wa_leads")[0][0] == 0 and q(path, "SELECT COUNT(*) FROM enrollments")[0][0] == 0)

        print("   or moved on during the wait (review item 5)")
        for i, stage in enumerate(("not_interested", "won", "replied", "proposal_due")):
            # Each its own domain: the matcher treats a shared company domain
            # as the same business.
            mid = enquire(db, path, f"hello@moved{i}.co.uk", business=f"Moved Clinic {i}", phone=f"07700 90080{i}")
            db.set_business_pipeline(mid, stage, "email", owner_id=b)
        db.run_website_followups(now=at(31))
        check("Not interested, Won, Replied, Proposal due within the wait: no WhatsApp, no email",
              q(path, "SELECT COUNT(*) FROM wa_leads")[0][0] == 0 and q(path, "SELECT COUNT(*) FROM enrollments")[0][0] == 0)
        check("those were four separate leads",
              len({q(path, "SELECT business_id FROM website_enquiries WHERE email=?", (f"hello@moved{i}.co.uk",))[0][0]
                   for i in range(4)}) == 4)
        still = enquire(db, path, "hello@still.co.uk", business="Still Enquired", phone="07700 900900")
        db.run_website_followups(now=at(31))
        check("one still on 'Enquired on the website' is followed up",
              q(path, "SELECT COUNT(*) FROM wa_leads WHERE business_id=?", (still,))[0][0] == 1)

        print("\n4. A SUPPRESSED ADDRESS GETS NO EMAIL")
        db, path, a, b, email_camp, wa = setup(work, "suppressed.db")
        db.mark_bounced("dead@clinic.co.uk")
        bid = enquire(db, path, "dead@clinic.co.uk")
        db.run_website_followups(now=at(31))
        check("no enrolment for an address that bounced", q(path, "SELECT COUNT(*) FROM enrollments")[0][0] == 0)
        check("its WhatsApp follow-up still happens (it gave a number)",
              q(path, "SELECT COUNT(*) FROM wa_leads WHERE business_id=?", (bid,))[0][0] == 1)
        db.unsubscribe_contact("hello@stopclinic.co.uk")
        stop = enquire(db, path, "hello@stopclinic.co.uk", business="Stop Clinic", phone="07700 900444")
        check("(a separate lead from the bounced one)", stop != bid)
        db.run_website_followups(now=at(31))
        check("an unsubscribed address: no email, and no WhatsApp either (do not contact)",
              q(path, "SELECT COUNT(*) FROM enrollments")[0][0] == 0
              and q(path, "SELECT COUNT(*) FROM wa_leads w JOIN businesses b ON b.id=w.business_id "
                          "WHERE b.name='Stop Clinic'")[0][0] == 0)

        print("\n5. NO CAMPAIGNS CHOSEN, AND THE WAITS AS SETTINGS")
        db, path, a, b, email_camp, wa = setup(work, "unset.db")
        db.save_website_followup(None, None, owner_id=b)
        enquire(db, path, "free@clinic.co.uk")
        db.run_website_followups(now=at(31))
        check("an owner who chose nothing: recorded, not followed up",
              q(path, "SELECT COUNT(*) FROM wa_leads")[0][0] == 0 and q(path, "SELECT COUNT(*) FROM enrollments")[0][0] == 0
              and q(path, "SELECT COUNT(*) FROM website_enquiries")[0][0] == 1)
        db, path, a, b, email_camp, wa = setup(work, "waits.db")
        db.save_website_config({"wa_wait_minutes": 120, "email_wait_hours": 2})
        enquire(db, path, "slow@clinic.co.uk")
        db.run_website_followups(now=at(90))
        check("a 2-hour WhatsApp wait: nothing at 90 minutes", q(path, "SELECT COUNT(*) FROM wa_leads")[0][0] == 0)
        db.run_website_followups(now=at(121))
        check("both after it; the email held to arrival + 2 hours",
              q(path, "SELECT COUNT(*) FROM wa_leads")[0][0] == 1
              and q(path, "SELECT next_send_at FROM enrollments") == [(ts(120),)],
              str(q(path, "SELECT next_send_at FROM enrollments")))

        print("\n6. THE SCHEDULER RUNS IT, AND IT CAN'T STOP THE LOOP")
        import scheduler
        importlib.reload(scheduler)
        src = open(scheduler.__file__, encoding="utf-8").read()
        check("the pass calls the job", "run_website_followups()" in src.split("def _one_pass")[1])
        real = db.run_website_followups
        db.run_website_followups = lambda now=None: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            scheduler.run_website_followups()
            check("a failing job is caught", True)
        except Exception as exc:
            check("a failing job is caught", False, repr(exc))
        finally:
            db.run_website_followups = real
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
