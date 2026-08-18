"""Cold calling: the queue, outcomes, and the crossover into email.

Run:  python tests/test_calling.py

Calling is deliberately not modelled as enrollment steps. An email sequence is
a schedule the machine runs; a call list is a pile worked through in a sitting,
and its states ("no answer, try again", "booked") do not map onto the
enrollment lifecycle. What the two channels must share is the answer: telling
someone "not interested" on the phone has to stop the emails, or the scheduler
sends a cheerful follow-up two days later.
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


def boot(work):
    os.environ["DB_PATH"] = os.path.join(work, "calls.db")
    os.environ["SECRET_KEY"] = "test-secret"
    import db
    importlib.reload(db)
    db.init_db()
    import app as app_mod
    importlib.reload(app_mod)
    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["is_admin"] = True
        s["csrf_token"] = "t"
    return db, app_mod, client


def main():
    work = tempfile.mkdtemp(prefix="calling-")
    try:
        db, app_mod, client = boot(work)
        hdr = {"X-CSRF-Token": "t"}

        db.upsert_contacts([
            {"email": f"c{i}@biz{i}.ca", "company": f"Biz {i}",
             "website": f"https://biz{i}.ca", "phone": f"709-555-01{i:02d}"}
            for i in range(5)
        ])
        db.upsert_contacts([{"company": "No Site Clinic", "website": "",
                             "phone": "709-555-0900", "status": "no_website",
                             "address": "1 Water St, St John's, NL"}])
        with db.get_db() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM contacts ORDER BY id").fetchall()]

        print("\n1. A NEVER-CALLED LEAD STARTS IN THE NEW PILE")
        new = db.get_call_queue("new")
        check("every phone-bearing lead is callable", len(new) == 6, str(len(new)))
        check("and none is due yet", db.get_call_queue("today") == [])

        print("\n2. LEADS WITHOUT A PHONE ARE NOT CALLABLE")
        db.upsert_contacts([{"email": "nophone@x.ca", "company": "No Phone",
                             "website": "https://nophone.ca"}])
        check("a lead with no number never enters the queue",
              all(l["company"] != "No Phone" for l in db.get_call_queue("all")))

        print("\n3. A CALLBACK COMES BACK WHEN IT IS DUE, NOT BEFORE")
        db.log_call(ids[0], "callback", "Asked me to try Thursday",
                    next_call_at="2099-01-01 10:00:00")
        check("a future callback is not in today's queue",
              all(l["id"] != ids[0] for l in db.get_call_queue("today")))
        check("it is in the scheduled bucket",
              any(l["id"] == ids[0] for l in db.get_call_queue("upcoming")))
        check("and it has left the new pile",
              all(l["id"] != ids[0] for l in db.get_call_queue("new")))

        db.log_call(ids[1], "callback", "call back this morning",
                    next_call_at="2020-01-01 10:00:00")
        check("an overdue callback IS due now",
              any(l["id"] == ids[1] for l in db.get_call_queue("today")))

        print("\n4. ATTEMPTS ACCUMULATE")
        db.log_call(ids[2], "no_answer")
        db.log_call(ids[2], "no_answer")
        db.log_call(ids[2], "voicemail", "left a message")
        contact = db.get_contact(ids[2])
        check("each attempt counts", contact["call_attempts"] == 3,
              str(contact["call_attempts"]))
        check("the latest outcome is the current state",
              contact["call_status"] == "voicemail", contact["call_status"])
        check("but the history keeps all three",
              len(db.get_call_history(ids[2])) == 3)
        check("an un-reached lead stays callable",
              any(l["id"] == ids[2] for l in db.get_call_queue("all")))

        print("\n5. A TERMINAL OUTCOME RETIRES THE LEAD FROM EVERY BUCKET")
        db.log_call(ids[3], "not_interested", "Happy with their current guy")
        for bucket in ("today", "new", "upcoming", "all"):
            check(f"gone from '{bucket}'",
                  all(l["id"] != ids[3] for l in db.get_call_queue(bucket)))

        print("\n6. A TERMINAL CALL STOPS THE EMAIL SEQUENCE")
        cid = db.create_campaign("Live campaign")
        db.upsert_step(cid, 1, "s", "b", 0)
        db.enroll_contacts_bulk(cid, [ids[4]])
        with db.get_db() as conn:
            before = conn.execute(
                "SELECT status FROM enrollments WHERE contact_id=?", (ids[4],)
            ).fetchone()["status"]
        check("the contact starts queued for email", before == "queued", before)

        res = db.log_call(ids[4], "not_interested", "said no on the phone")
        check("the outcome reports that it stopped email", res["stopped_email"] is True)
        with db.get_db() as conn:
            after = conn.execute(
                "SELECT status FROM enrollments WHERE contact_id=?", (ids[4],)
            ).fetchone()["status"]
        check("and the enrollment is no longer sendable",
              after not in ("queued", "paused"), after)
        check("so the scheduler sees nothing due",
              all(e["contact_id"] != ids[4] for e in db.get_due_enrollments(cid)))

        print("\n7. DO NOT CALL SUPPRESSES EVERY CHANNEL")
        db.log_call(ids[5], "do_not_call", "asked not to be contacted again")
        contact = db.get_contact(ids[5])
        check("the contact is unsubscribed, not merely un-callable",
              contact["status"] == "unsubscribed", contact["status"])
        check("and cannot resurface in a call queue",
              all(l["id"] != ids[5] for l in db.get_call_queue("all")))

        print("\n8. A NON-TERMINAL OUTCOME LEAVES EMAIL ALONE")
        db.upsert_contacts([{"email": "keep@keepme.ca", "company": "Keep Me",
                             "website": "https://keepme.ca", "phone": "709-555-0777"}])
        keep = db.get_contact_by_email("keep@keepme.ca")["id"]
        db.enroll_contacts_bulk(cid, [keep])
        db.log_call(keep, "voicemail", "left a message")
        with db.get_db() as conn:
            still = conn.execute(
                "SELECT status FROM enrollments WHERE contact_id=?", (keep,)
            ).fetchone()["status"]
        check("a voicemail does not cancel the email sequence", still == "queued", still)

        print("\n9. THE ROUTES BEHAVE")
        r = client.get("/api/calls/queue?bucket=new")
        body = r.get_json()
        check("the queue route answers", r.status_code == 200 and "leads" in body)
        check("it reports bucket counts", "counts" in body and "today" in body["counts"])
        check("and the outcome vocabulary", any(o["key"] == "booked" for o in body["outcomes"]))
        check("prior contact rides along with each lead",
              all("touch" in l for l in body["leads"]))

        r = client.post("/api/calls/log", json={"contact_id": ids[2], "outcome": "banana"},
                        headers=hdr)
        check("an unknown outcome is refused", r.status_code == 400, str(r.status_code))

        r = client.post("/api/calls/log", json={"contact_id": ids[2], "outcome": "callback"},
                        headers=hdr)
        check("a callback with no date is refused", r.status_code == 400,
              str(r.get_json()))

        r = client.post("/api/calls/log", json={
            "contact_id": ids[2], "outcome": "booked",
            "next_call_at": "2099-03-04T14:30",
        }, headers=hdr)
        check("a booked meeting is accepted", r.status_code == 200, str(r.get_json()))
        stored = db.get_contact(ids[2])["next_call_at"]
        check("the datetime-local value is stored in the DB's own shape",
              stored == "2099-03-04 14:30:00", str(stored))

        print("\n10. THE CALENDAR FILE IS REAL ICS")
        r = client.get(f"/api/calls/{ids[2]}/ics")
        check("it downloads", r.status_code == 200, str(r.status_code))
        text = r.get_data(as_text=True)
        check("with a calendar content type", "text/calendar" in r.headers.get("Content-Type", ""))
        check("and a well-formed event",
              text.startswith("BEGIN:VCALENDAR") and "BEGIN:VEVENT" in text
              and "DTSTART:20990304T143000" in text and text.strip().endswith("END:VCALENDAR"),
              text[:80])
        # ids[3] went "not interested": terminal with no use for a date, so its
        # next_call_at was dropped and there is nothing to put in a calendar.
        r = client.get(f"/api/calls/{ids[3]}/ics")
        check("a lead with nothing scheduled has nothing to download",
              r.status_code == 404, str(r.status_code))
        check("and a terminal outcome with no date left none behind",
              db.get_contact(ids[3])["next_call_at"] is None)
        check("while a booked meeting kept its time",
              db.get_contact(ids[2])["next_call_at"] == "2099-03-04 14:30:00")

        print("\n11. THE SCRIPT IS THE OPERATOR'S, AND STARTS EMPTY")
        script = client.get("/api/call-script").get_json()
        check("a script exists on first use", script and "sections" in script)
        check("with section headings", len(script["sections"]) >= 4, str(len(script["sections"])))
        check("and no words put in your mouth",
              all(not s["body"].strip() for s in script["sections"]),
              str([s["body"] for s in script["sections"]][:3]))

        r = client.put("/api/call-script", json={"sections": [
            {"title": "Opening", "body": "Hi, is this {{company}}?"},
        ]}, headers=hdr)
        check("it saves", r.status_code == 200)
        again = client.get("/api/call-script").get_json()
        check("and round-trips", again["sections"][0]["body"] == "Hi, is this {{company}}?",
              str(again["sections"]))
        r = client.put("/api/call-script", json={"sections": "not a list"}, headers=hdr)
        check("garbage is refused", r.status_code == 400)

        print("\n12. A CLOSED-OUT LEAD IS FINDABLE, NOT GONE")
        # It leaves every calling queue by design, so Contacts is the only
        # place it can be seen -- and call_status is the only thing that
        # distinguishes it from a lead nobody has ever dialled.
        closed = db.get_contact(ids[3])          # marked not_interested earlier
        check("the outcome is recorded on the contact",
              closed["call_status"] == "not_interested", closed["call_status"])
        check("and 'not interested' does NOT unsubscribe them",
              closed["status"] != "unsubscribed", closed["status"])

        page = db.get_contacts_page(page=1, per_page=100, call_status="not_interested")
        check("filtering Contacts by call status finds it",
              any(r["id"] == ids[3] for r in page["rows"]), str(page["total"]))
        never = db.get_contacts_page(page=1, per_page=100, call_status="none")
        check("'never called' excludes it",
              all(r["id"] != ids[3] for r in never["rows"]))
        any_called = db.get_contacts_page(page=1, per_page=100, call_status="any")
        check("'called, any outcome' includes it",
              any(r["id"] == ids[3] for r in any_called["rows"]))

        worked = db.get_call_queue("worked")
        check("the Worked bucket lists it", any(l["id"] == ids[3] for l in worked))
        check("but the callable buckets still do not",
              all(l["id"] != ids[3] for l in db.get_call_queue("all")))

        print("\n13. A LEAD CLOSED BY MISTAKE CAN BE REOPENED")
        r = client.post(f"/api/calls/{ids[3]}/reopen", headers=hdr)
        check("reopen succeeds", r.status_code == 200, str(r.status_code))
        check("it is callable again",
              any(l["id"] == ids[3] for l in db.get_call_queue("all")))
        check("and the call history is kept, not rewritten",
              len(db.get_call_history(ids[3])) >= 1)

        print("\n14. CALL CAMPAIGNS GROUP LEADS BY DECISION, NOT BY SCRAPE")
        camp = db.create_call_campaign("St Johns dentists", "first batch")
        added = db.add_to_call_campaign(camp, [ids[0], ids[1], ids[2]])
        check("leads are added", added == 3, str(added))
        check("adding the same lead twice is a no-op",
              db.add_to_call_campaign(camp, [ids[0]]) == 0)

        scoped = db.get_call_queue("all", call_campaign_id=camp)
        check("the queue can be scoped to the campaign",
              scoped and all(l["id"] in (ids[0], ids[1], ids[2]) for l in scoped),
              str([l["id"] for l in scoped]))
        unscoped = db.get_call_queue("all")
        check("and is wider without it", len(unscoped) > len(scoped),
              f"{len(unscoped)} vs {len(scoped)}")

        stats = {c["id"]: c for c in db.get_call_campaigns()}[camp]
        check("the overview counts its leads", stats["total"] == 3, str(stats["total"]))
        check("and reports what is left", stats["remaining"] == stats["total"] - stats["closed"],
              str(stats))

        print("\n15. A CALL IS ATTRIBUTED TO THE CAMPAIGN IT WAS MADE UNDER")
        # Assert the movement, not absolute numbers: an earlier section already
        # booked one of these leads, and hardcoding totals would make this test
        # break whenever anything above it changes.
        before_stats = {c["id"]: c for c in db.get_call_campaigns()}[camp]
        r = client.post("/api/calls/log", json={
            "contact_id": ids[0], "outcome": "not_interested",
            "notes": "no thanks", "call_campaign_id": camp,
        }, headers=hdr)
        check("logging with a campaign works", r.status_code == 200, str(r.get_json()))
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT call_campaign_id FROM call_log WHERE contact_id=? "
                "ORDER BY id DESC LIMIT 1", (ids[0],)).fetchone()
        check("the campaign is stored on the call", row["call_campaign_id"] == camp,
              str(row["call_campaign_id"]))
        after = {c["id"]: c for c in db.get_call_campaigns()}[camp]
        check("one more lead counts as closed",
              after["closed"] == before_stats["closed"] + 1,
              f"{before_stats['closed']} -> {after['closed']}")
        check("and one fewer remains",
              after["remaining"] == before_stats["remaining"] - 1,
              f"{before_stats['remaining']} -> {after['remaining']}")
        check("the not-interested tally moved too",
              after["not_interested"] == before_stats["not_interested"] + 1,
              f"{before_stats['not_interested']} -> {after['not_interested']}")
        check("the total is unchanged — closing a lead does not remove it",
              after["total"] == before_stats["total"], str(after["total"]))

        print("\n16. DELETING A CAMPAIGN KEEPS THE LEADS")
        before_contacts = db.get_contacts_page(page=1, per_page=500)["total"]
        r = client.delete(f"/api/call-campaigns/{camp}", headers=hdr)
        check("the campaign goes", r.status_code == 200)
        check("no campaign remains", all(c["id"] != camp for c in db.get_call_campaigns()))
        check("every contact survives",
              db.get_contacts_page(page=1, per_page=500)["total"] == before_contacts)
        check("and so does the call history", len(db.get_call_history(ids[0])) >= 1)

        print("\n17. THE ROUTES ARE ADMIN-ONLY")
        anon = app_mod.app.test_client()
        for path in ("/api/calls/queue", "/api/call-script", "/api/call-campaigns",
                     f"/api/calls/{ids[0]}/ics"):
            check(f"{path} needs a session", anon.get(path).status_code in (401, 403))

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
