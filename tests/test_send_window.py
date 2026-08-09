"""Sending days, the send window, and saying why nothing is going out.

Run:  python tests/test_send_window.py

The bug this pins: the send gate hardcoded `weekday() >= 5`, so the weekend was
always Saturday and Sunday. Across much of the Middle East the working week
runs Sunday to Thursday, which means a campaign aimed there sat idle on its two
busiest days and would have sent on the two nobody works. Sending days are now
per campaign, evaluated in the campaign's own timezone.

It was also invisible. A follow-up that came due on a non-sending day simply
waits, and the only evidence is a "next send" timestamp in the past -- which
reads like the scheduler has died rather than like the weekend.
"""
import datetime
import importlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []
_REAL_DT = datetime.datetime


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


class _FrozenNow(_REAL_DT):
    """datetime.datetime with now() pinned, so weekday logic is deterministic."""
    target = None

    @classmethod
    def now(cls, tz=None):
        return cls.target.replace(tzinfo=tz) if tz else cls.target


def at(sender, moment, fn):
    """Run fn() as if it were `moment`."""
    _FrozenNow.target = moment
    sender.datetime.datetime = _FrozenNow
    try:
        return fn()
    finally:
        sender.datetime.datetime = _REAL_DT


def main():
    work = tempfile.mkdtemp(prefix="sendwindow-")
    os.environ["DB_PATH"] = os.path.join(work, "t.db")
    os.environ["SECRET_KEY"] = "test-secret"

    import db
    importlib.reload(db)
    db.init_db()
    import sender
    importlib.reload(sender)
    import app as app_mod
    importlib.reload(app_mod)

    try:
        MON_FRI = "0,1,2,3,4"
        SUN_THU = "6,0,1,2,3"
        western = {"id": 0, "send_start_hour": 9, "send_end_hour": 17,
                   "timezone": None, "send_days": MON_FRI}
        gulf    = {"id": 0, "send_start_hour": 9, "send_end_hour": 17,
                   "timezone": None, "send_days": SUN_THU}

        print("\n1. PARSING IS FORGIVING BUT NEVER SILENTLY STOPS SENDING")
        check("a normal list parses", sorted(sender.parse_send_days(MON_FRI)) == [0, 1, 2, 3, 4])
        check("Sun-Thu parses", sorted(sender.parse_send_days(SUN_THU)) == [0, 1, 2, 3, 6])
        check("junk falls back to Mon-Fri, not to nothing",
              sorted(sender.parse_send_days("banana")) == [0, 1, 2, 3, 4])
        check("empty falls back to Mon-Fri", sorted(sender.parse_send_days("")) == [0, 1, 2, 3, 4])
        check("out-of-range numbers are dropped",
              sorted(sender.parse_send_days("0,9,3,-1")) == [0, 3])

        print("\n2. THE WEEKEND IS WHATEVER THE CAMPAIGN SAYS IT IS")
        # 2026-08-07 is a Friday, 08 Saturday, 09 Sunday, 10 Monday.
        cases = [
            ("Friday",    _REAL_DT(2026, 8, 7, 10),  True,  False),
            ("Saturday",  _REAL_DT(2026, 8, 8, 10),  False, False),
            ("Sunday",    _REAL_DT(2026, 8, 9, 10),  False, True),
            ("Monday",    _REAL_DT(2026, 8, 10, 10), True,  True),
        ]
        for name, moment, want_w, want_g in cases:
            got_w = at(sender, moment, lambda: sender.is_business_hours(western))
            got_g = at(sender, moment, lambda: sender.is_business_hours(gulf))
            check(f"{name}: Mon-Fri sends = {want_w}", got_w == want_w, str(got_w))
            check(f"{name}: Sun-Thu sends = {want_g}", got_g == want_g, str(got_g))

        print("\n3. HOURS STILL GATE A SENDING DAY")
        monday = _REAL_DT(2026, 8, 10, 20)
        check("Monday 20:00 is outside a 09-17 window",
              at(sender, monday, lambda: sender.is_business_hours(western)) is False)
        early = _REAL_DT(2026, 8, 10, 7)
        check("Monday 07:00 is too early",
              at(sender, early, lambda: sender.is_business_hours(western)) is False)

        print("\n4. THE NEXT WINDOW IS THE ONE A USER WOULD EXPECT")
        sat = _REAL_DT(2026, 8, 8, 10)
        nxt = at(sender, sat, lambda: sender.next_send_window(western))
        check("from Saturday, Mon-Fri opens Monday 09:00",
              nxt and nxt.weekday() == 0 and nxt.hour == 9 and nxt.day == 10,
              nxt.strftime("%a %d %H:%M") if nxt else "None")
        nxt = at(sender, sat, lambda: sender.next_send_window(gulf))
        check("from Saturday, Sun-Thu opens Sunday 09:00",
              nxt and nxt.weekday() == 6 and nxt.day == 9,
              nxt.strftime("%a %d %H:%M") if nxt else "None")
        check("when the window is open it reports None",
              at(sender, _REAL_DT(2026, 8, 10, 10),
                 lambda: sender.next_send_window(western)) is None)
        nxt = at(sender, early, lambda: sender.next_send_window(western))
        check("before opening on a sending day it is later the same day",
              nxt and nxt.day == 10 and nxt.hour == 9,
              nxt.strftime("%a %d %H:%M") if nxt else "None")
        check("a start hour past the end hour never opens",
              at(sender, sat, lambda: sender.next_send_window(
                  {**western, "send_start_hour": 18, "send_end_hour": 9})) is None)

        print("\n5. SENDING DAYS PERSIST PER CAMPAIGN")
        cid = db.create_campaign("Gulf campaign", send_days=SUN_THU)
        check("the column stores what was passed",
              db.get_campaign(cid)["send_days"] == SUN_THU,
              db.get_campaign(cid)["send_days"])
        default_cid = db.create_campaign("Default campaign")
        check("a campaign created without them defaults to Mon-Fri",
              db.get_campaign(default_cid)["send_days"] == MON_FRI,
              db.get_campaign(default_cid)["send_days"])
        db.update_campaign(cid, send_days=MON_FRI)
        check("they can be updated", db.get_campaign(cid)["send_days"] == MON_FRI)

        print("\n6. THE CAMPAIGN SAYS WHY IT IS NOT SENDING")
        db.update_campaign(cid, send_days=SUN_THU, status="active")
        camp = db.get_campaign(cid)

        st = at(sender, sat, lambda: app_mod._campaign_send_status(camp))
        check("on a non-sending day it is not sending", st["sending"] is False)
        check("and it names the days it does send",
              "Sun" in st["days_label"] and "Sat" not in st["days_label"],
              st["days_label"])
        check("reading the week the way it is worked, Sunday first",
              st["days_label"] == "Sun, Mon, Tue, Wed, Thu", st["days_label"])
        check("a Mon-Fri week still reads Monday first",
              app_mod._campaign_send_status(
                  {**camp, "send_days": MON_FRI})["days_label"] == "Mon, Tue, Wed, Thu, Fri")
        check("and says when it next opens", st["next_open"] is not None, str(st["next_open"]))
        check("the reason is in plain language",
              "next opens" in st["reason"], st["reason"])

        st = at(sender, _REAL_DT(2026, 8, 9, 10),
                lambda: app_mod._campaign_send_status(camp))
        check("on a Sunday a Sun-Thu campaign is sending", st["sending"] is True, st["reason"])

        db.update_campaign(cid, status="paused")
        st = at(sender, _REAL_DT(2026, 8, 9, 10),
                lambda: app_mod._campaign_send_status(db.get_campaign(cid)))
        check("a paused campaign says so rather than blaming the clock",
              st["sending"] is False and "paused" in st["reason"], st["reason"])

        print("\n7. THE DAILY CAP IS REPORTED, AND FROM THE SAME COUNT THE SCHEDULER USES")
        db.update_campaign(cid, status="active", daily_limit=2)
        db.upsert_contacts([{"email": "cap1@x1.ca", "company": "X1", "website": "https://x1.ca"},
                            {"email": "cap2@x2.ca", "company": "X2", "website": "https://x2.ca"}])
        with db.get_db() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM contacts WHERE email LIKE 'cap%'").fetchall()]
        for n, contact_id in enumerate(ids):
            db.log_send(cid, contact_id, 1, "s", f"m{n}")
        check("db and scheduler agree on today's count",
              db.get_campaign_today_count(cid) == 2, str(db.get_campaign_today_count(cid)))
        import scheduler as sched
        importlib.reload(sched)
        check("the scheduler's helper is the same number",
              sched._get_campaign_today_count(cid) == db.get_campaign_today_count(cid))

        st = at(sender, _REAL_DT(2026, 8, 9, 10),
                lambda: app_mod._campaign_send_status(db.get_campaign(cid)))
        check("a capped-out campaign reports the cap, not the window",
              st["sending"] is False and "Daily limit" in st["reason"], st["reason"])

        print("\n8. THE OLD HARDCODED WEEKEND IS GONE")
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "sender.py"), encoding="utf-8").read()
        check("no bare weekday() >= 5 remains in the gate",
              "now.weekday() >= 5" not in src)
        check("the gate consults the campaign's days",
              "parse_send_days(campaign.get(\"send_days\"))" in src)

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
