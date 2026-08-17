"""Duplicate-send guard, the volume-gated bounce breaker, and business identity.

Run:  python tests/test_dedupe_and_guards.py

Three things are pinned here.

A send is three writes -- deliver over SMTP, log the send, advance the
enrollment -- and a restart between the first and the last leaves the
enrollment queued on a step the contact already received, so the scheduler
sends it again. The window is milliseconds, but a deploy lands in it
eventually and the cost is a duplicate cold email.

The bounce circuit-breaker compared a rate with no regard for volume. One bad
address out of twelve reads as 8%, over the default 5% threshold, so a
campaign auto-paused on its first typo'd scrape -- which the operator
experiences as it stopping for no reason.

And identity: no single column answers "have I already worked this business".
Email is absent on the best call leads, a domain is absent on no-website
leads, and a company name alone merges every "Main Street Dental" in the
country.
"""
import importlib
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
    work = tempfile.mkdtemp(prefix="guards-")
    os.environ["DB_PATH"] = os.path.join(work, "t.db")
    os.environ["SECRET_KEY"] = "test-secret"

    import db
    importlib.reload(db)
    db.init_db()

    try:
        print("\n1. A STEP ALREADY DELIVERED IS NEVER SENT TWICE")
        cid = db.create_campaign("Guard test")
        db.upsert_contacts([{"email": "a@guard1.ca", "company": "Guard 1",
                             "website": "https://guard1.ca"}])
        with db.get_db() as conn:
            contact_id = conn.execute(
                "SELECT id FROM contacts WHERE email='a@guard1.ca'").fetchone()["id"]

        check("nothing sent yet", db.has_sent_step(cid, contact_id, 1) is False)
        db.log_send(cid, contact_id, 1, "Subject", "msg-1")
        check("the delivered step is now recognised",
              db.has_sent_step(cid, contact_id, 1) is True)
        check("a later step is not", db.has_sent_step(cid, contact_id, 2) is False)
        check("nor the same step in another campaign",
              db.has_sent_step(cid + 999, contact_id, 1) is False)

        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scheduler.py"), encoding="utf-8").read()
        check("the scheduler checks before sending", "db.has_sent_step(" in src)
        guard_block = src.split("db.has_sent_step(")[1].split("# ── Send the email")[0]
        check("and recovers by advancing rather than re-sending",
              "_advance_after_step(" in guard_block and "send_email" not in guard_block)

        print("\n2. THE BOUNCE BREAKER WAITS FOR ENOUGH VOLUME")
        small = db.create_campaign("Small campaign")
        db.upsert_contacts([
            {"email": f"b{i}@small{i}.ca", "company": f"Small {i}",
             "website": f"https://small{i}.ca"} for i in range(12)
        ])
        with db.get_db() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM contacts WHERE email LIKE 'b%@small%'").fetchall()]
        db.upsert_step(small, 1, "s", "b", 0)
        db.enroll_contacts_bulk(small, ids)
        for n, contact in enumerate(ids):
            db.log_send(small, contact, 1, "s", f"sm{n}")
        with db.get_db() as conn:
            conn.execute("UPDATE enrollments SET status='bounced' WHERE campaign_id=? "
                         "AND contact_id=? ", (small, ids[0]))

        rate = db.get_bounce_rate(small)
        check("one bounce in twelve is over a 5% threshold", rate > 5.0, f"{rate:.1f}%")
        should, rate, sends = db.bounce_breaker_should_pause(small, 5.0)
        check("but the breaker holds, because the volume is too small",
              should is False, f"rate={rate:.1f}% sends={sends}")

        # Push past the volume floor with clean sends; the rate falls but the
        # point is that the breaker is now allowed to have an opinion.
        db.upsert_contacts([
            {"email": f"c{i}@big{i}.ca", "company": f"Big {i}",
             "website": f"https://big{i}.ca"} for i in range(30)
        ])
        with db.get_db() as conn:
            more = [r["id"] for r in conn.execute(
                "SELECT id FROM contacts WHERE email LIKE 'c%@big%'").fetchall()]
        db.enroll_contacts_bulk(small, more)
        for n, contact in enumerate(more):
            db.log_send(small, contact, 1, "s", f"bg{n}")
        with db.get_db() as conn:
            conn.execute("UPDATE enrollments SET status='bounced' WHERE campaign_id=? "
                         "AND contact_id IN ({})".format(",".join("?" * 10)),
                         (small, *more[:10]))
        should, rate, sends = db.bounce_breaker_should_pause(small, 5.0)
        check("past the floor a real bounce rate does pause it",
              should is True, f"rate={rate:.1f}% sends={sends}")
        should, rate, sends = db.bounce_breaker_should_pause(small, 99.0)
        check("and a rate under the threshold still does not", should is False,
              f"rate={rate:.1f}%")

        print("\n3. PHONE NUMBERS COMPARE REGARDLESS OF FORMATTING")
        same = ["+1 709-555-0123", "(709) 555-0123", "709.555.0123",
                "7095550123", "1-709-555-0123", " +1 (709) 555 0123 "]
        keys = {db.normalize_phone(p) for p in same}
        check("every format of one number collapses to one key", len(keys) == 1, str(keys))
        check("and it is the ten significant digits", keys.pop() == "7095550123")
        check("a different number stays different",
              db.normalize_phone("709-555-9999") != db.normalize_phone("709-555-0123"))
        check("junk yields no key", db.normalize_phone("call us!") == "")
        check("too short to dial yields no key", db.normalize_phone("12345") == "")

        print("\n4. COMPANY NAMES NORMALIZE, BUT ARE NEVER USED ALONE")
        check("legal suffixes and punctuation are noise",
              db.normalize_company("Paradise Dental Care Inc.")
              == db.normalize_company("paradise dental care"))
        check("distinct businesses stay distinct",
              db.normalize_company("Paradise Dental") != db.normalize_company("Paradise Physio"))

        print("\n5. IDENTITY RESOLVES PHONE FIRST, THEN DOMAIN, THEN NAME + PLACE")
        db.upsert_contacts([{
            "email": "hi@clinicx.ca", "company": "Clinic X Inc.",
            "website": "https://clinicx.ca", "phone": "(709) 555-7777",
            "address": "12 Water St, St John's, NL",
        }])
        with db.get_db() as conn:
            hit = db.find_existing_business(conn, phone="+1 709 555 7777")
            check("a differently-formatted phone finds the same business",
                  hit is not None and hit["email"] == "hi@clinicx.ca")

            hit = db.find_existing_business(conn, website="http://www.clinicx.ca/contact?x=1")
            check("a URL variant resolves by domain",
                  hit is not None and hit["email"] == "hi@clinicx.ca")

            hit = db.find_existing_business(
                conn, company="Clinic X", address="99 Duckworth St, St John's, NL")
            check("name plus locality resolves when there is nothing else",
                  hit is not None and hit["email"] == "hi@clinicx.ca", str(dict(hit) if hit else None))

            hit = db.find_existing_business(
                conn, company="Clinic X", address="500 Bay St, Toronto, ON")
            check("the same name in another city is NOT the same business",
                  hit is None, str(dict(hit) if hit else None))

            check("an unrelated business finds nothing",
                  db.find_existing_business(conn, phone="416-555-0000") is None)

        print("\n6. NORMALIZED PHONE IS STORED, AND BACKFILLED FOR OLD ROWS")
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT phone, phone_normalized FROM contacts WHERE email='hi@clinicx.ca'"
            ).fetchone()
        check("it is written on insert", row["phone_normalized"] == "7095557777",
              row["phone_normalized"])
        check("the display value is untouched", row["phone"] == "(709) 555-7777", row["phone"])

        with db.get_db() as conn:
            conn.execute("UPDATE contacts SET phone_normalized='' WHERE email='hi@clinicx.ca'")
        db.init_db()          # what runs at startup
        with db.get_db() as conn:
            back = conn.execute(
                "SELECT phone_normalized FROM contacts WHERE email='hi@clinicx.ca'"
            ).fetchone()["phone_normalized"]
        check("startup backfills rows scraped before the column existed",
              back == "7095557777", back)

        print("\n7. TOUCH HISTORY REPORTS RATHER THAN HIDES")
        hist = db.get_touch_history(ids[0])
        check("it counts emails already sent", hist["emails_sent"] >= 1, str(hist["emails_sent"]))
        check("it names the campaigns involved", len(hist["campaigns"]) >= 1, str(hist["campaigns"]))
        check("a bounced lead is flagged as already closed out", hist["closed"] is True)

        with db.get_db() as conn:
            fresh_id = db.find_existing_business(conn, phone="709-555-7777")["id"]
        untouched = db.get_touch_history(fresh_id)
        check("a never-contacted lead reports no history",
              untouched["emails_sent"] == 0 and untouched["closed"] is False, str(untouched))

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
