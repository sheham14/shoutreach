"""Pipeline stages: how far along a business is, shared by every channel.

Run:  python tests/test_pipeline.py

The properties worth protecting:
  - the stage is a fact about the BUSINESS, so it reads the same from
    WhatsApp, Calling and Email -- that is the whole reason it isn't stored
    per channel;
  - setting a stage stops that business's WhatsApp cadence, and clearing one
    does NOT start it again (a mis-click must never resume messaging someone
    you are mid-conversation with);
  - a terminal stage is not a suppression: "not interested" must not set
    do_not_contact, which spans every operator and every channel;
  - stages are walled per operator like everything else.
"""
import importlib
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
    os.environ["DB_PATH"] = os.path.join(work, "pipeline.db")
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


def wa_lead(db, name):
    return next(l for l in db.get_wa_leads(include_inactive=True) if l["company"] == name)


def test_stages_exist(db):
    print("\n1. THE STAGES YOU START WITH, AND THE ONES YOU INVENT")
    stages = db.get_pipeline_stages()
    for key in ("replied", "proposal_due", "proposal_sent", "booked", "won", "not_interested"):
        check(f"built-in '{key}' is there", key in stages)
    check("'proposal due' asks for a date", stages["proposal_due"]["wants_date"] == 1)
    check("'replied' does not", stages["replied"]["wants_date"] == 0)
    check("won and not-interested end the conversation",
          stages["won"]["is_terminal"] == 1 and stages["not_interested"]["is_terminal"] == 1)

    key = db.create_pipeline_stage("Sent samples", wants_date=True)
    check("a stage of your own can be added", key in db.get_pipeline_stages())
    check("its key comes from its label", key == "sent_samples", key)
    db.update_pipeline_stage(key, label="Samples posted")
    check("relabelling keeps the key", db.get_pipeline_stage(key)["label"] == "Samples posted")
    check("a built-in cannot be deleted", db.delete_pipeline_stage("booked") is False)
    check("your own can be", db.delete_pipeline_stage(key) is True)


def test_one_stage_every_channel(db, camp):
    print("\n2. ONE STAGE, READ THE SAME FROM EVERY CHANNEL")
    db.upsert_wa_leads([{"company": "Shared View Clinic", "phone": "050 200 0001",
                         "email": "hello@sharedview.ae", "website": "sharedview.ae"}],
                       default_country="AE", wa_campaign_id=camp)
    lead = wa_lead(db, "Shared View Clinic")
    bid = lead["business_id"]
    with db.get_db() as conn:
        db.get_or_create_call_lead(conn, bid)

    res = db.set_business_pipeline(bid, "booked", channel="whatsapp",
                                   next_action_at="2026-10-05")
    check("the stage is set", res["ok"] and res["pipeline_stage"] == "booked", str(res))
    check("it records which channel it came from", res["pipeline_channel"] == "whatsapp")
    check("and the date owed", res["next_action_at"] == "2026-10-05")

    check("WhatsApp's own view of the lead shows it",
          wa_lead(db, "Shared View Clinic")["pipeline_stage"] == "booked")
    check("the business record shows it",
          db.get_business(bid)["pipeline_stage"] == "booked")
    contacts = db.get_businesses_page(q="Shared View")["rows"]
    check("Contacts shows it, with the label resolved",
          contacts and contacts[0]["pipeline_stage"] == "booked"
          and contacts[0]["pipeline_label"] == "Meeting booked",
          str(contacts[0].get("pipeline_label") if contacts else None))


def test_cadence_rules(db, camp):
    print("\n3. SETTING A STAGE STOPS THE CADENCE; CLEARING ONE DOES NOT RESTART IT")
    db.upsert_wa_leads([{"company": "Cadence Co", "phone": "050 200 0002"}],
                       default_country="AE", wa_campaign_id=camp)
    lead = wa_lead(db, "Cadence Co")
    db.mark_wa_sent(lead["id"], "opener", kind="opener")
    check("it is in the cadence after sending",
          wa_lead(db, "Cadence Co")["replied"] == 0)

    db.set_business_pipeline(lead["business_id"], "booked", channel="whatsapp")
    after = wa_lead(db, "Cadence Co")
    check("booking it stops the follow-ups", after["replied"] == 1, str(after["replied"]))
    check("and its stage reads 'replied' to the queue", after["wa_status"] == "replied")
    due_ids = [d["id"] for d in db.get_wa_followups_due(days=0)]
    check("so it is not in the follow-up queue", after["id"] not in due_ids)

    db.set_business_pipeline(lead["business_id"], "")
    cleared = wa_lead(db, "Cadence Co")
    check("clearing the stage clears the stage",
          db.get_business(lead["business_id"])["pipeline_stage"] == "")
    check("but does NOT put them back in the cadence", cleared["replied"] == 1,
          "a mis-click must not resume messaging a live conversation")

    print("\n   A lead never messaged is left alone")
    db.upsert_wa_leads([{"company": "Untouched Co", "phone": "050 200 0003"}],
                       default_country="AE", wa_campaign_id=camp)
    fresh = wa_lead(db, "Untouched Co")
    db.set_business_pipeline(fresh["business_id"], "replied", channel="whatsapp")
    check("an unsent lead isn't marked replied by a stage change",
          wa_lead(db, "Untouched Co")["replied"] == 0)


def test_terminal_is_not_suppression(db, camp):
    print("\n4. 'NOT INTERESTED' IS NOT 'NEVER CONTACT US'")
    db.upsert_wa_leads([{"company": "Not Keen Ltd", "phone": "050 200 0004"}],
                       default_country="AE", wa_campaign_id=camp)
    lead = wa_lead(db, "Not Keen Ltd")
    db.set_business_pipeline(lead["business_id"], "not_interested", channel="whatsapp")
    biz = db.get_business(lead["business_id"])
    check("the stage is set", biz["pipeline_stage"] == "not_interested")
    check("do_not_contact is untouched", not biz["do_not_contact"],
          "that flag spans every operator and every channel")
    board = [r["business_id"] for r in db.get_pipeline_board()]
    check("a terminal stage drops out of conversations in progress",
          lead["business_id"] not in board)
    board_all = [r["business_id"] for r in db.get_pipeline_board(include_terminal=True)]
    check("but can still be listed on purpose", lead["business_id"] in board_all)


def test_board_order(db, camp):
    print("\n5. CONVERSATIONS IN PROGRESS: WHAT'S OWED, SOONEST FIRST")
    rows = [("Due Later", "050 200 0010", "2026-12-01"),
            ("Due Soon", "050 200 0011", "2026-09-01"),
            ("No Date", "050 200 0012", None)]
    for name, phone, when in rows:
        db.upsert_wa_leads([{"company": name, "phone": phone}],
                           default_country="AE", wa_campaign_id=camp)
        lead = wa_lead(db, name)
        db.set_business_pipeline(lead["business_id"], "proposal_due",
                                 channel="whatsapp", next_action_at=when)
    board = [r["company"] for r in db.get_pipeline_board()
             if r["company"] in ("Due Later", "Due Soon", "No Date")]
    check("the soonest thing owed comes first", board[0] == "Due Soon", str(board))
    check("undated conversations sort last", board[-1] == "No Date", str(board))


def test_messaged_filter_and_sent_log(db, camp):
    print("\n6. 'WHO HAVE I SENT TO' AND 'WHAT WENT OUT'")
    db.upsert_wa_leads([{"company": "Sent One", "phone": "050 200 0020"},
                        {"company": "Sent Two", "phone": "050 200 0021"},
                        {"company": "Never Sent", "phone": "050 200 0022"}],
                       default_country="AE", wa_campaign_id=camp)
    for name in ("Sent One", "Sent Two"):
        db.mark_wa_sent(wa_lead(db, name)["id"], f"hello {name}", kind="opener")

    messaged = [r["company"] for r in db.get_wa_leads_page(stage="messaged", per_page=200)["rows"]]
    check("both messaged leads are in one filter",
          "Sent One" in messaged and "Sent Two" in messaged, str(messaged[:6]))
    check("an unsent lead is not", "Never Sent" not in messaged)

    log = db.get_wa_sent_log()
    names = [r["company"] for r in log]
    check("the send log lists what went out",
          "Sent One" in names and "Sent Two" in names, str(names[:6]))
    check("newest first", log[0]["sent_at"] >= log[-1]["sent_at"])
    check("with what kind of message it was", log[0]["kind"] in ("opener", "followup"))
    check("a future 'since' returns nothing",
          db.get_wa_sent_log(since="2099-01-01 00:00:00") == [])

    db.set_business_pipeline(wa_lead(db, "Sent One")["business_id"], "booked", channel="whatsapp")
    deal = [r["company"] for r in db.get_wa_leads_page(stage="deal:booked", per_page=200)["rows"]]
    check("the deal half of the filter works too", "Sent One" in deal, str(deal[:6]))


def test_search_widened(db, camp):
    print("\n7. SEARCH FINDS WHAT YOU REMEMBER")
    db.upsert_wa_leads([{"company": "Findable Clinic", "phone": "050 200 0030",
                         "email": "reception@findable.ae"}],
                       default_country="AE", wa_campaign_id=camp)
    bid = wa_lead(db, "Findable Clinic")["business_id"]
    db.update_business(bid, {"notes": "spoke to Dr Aziz about the rebrand"})
    for term, why in [("Findable", "name"), ("reception@findable", "email"),
                      ("Aziz", "notes"), ("0030", "number")]:
        found = [r["company"] for r in db.get_wa_leads_page(q=term, per_page=50)["rows"]]
        check(f"searching by {why} finds it", "Findable Clinic" in found, term)


def test_conflict_shows_stage(db, camp):
    print("\n8. IMPORTING ELSEWHERE SHOWS WHAT'S ALREADY HAPPENING")
    db.upsert_wa_leads([{"company": "Crossover Dental", "phone": "050 200 0040",
                         "email": "hi@crossover.ae"}],
                       default_country="AE", wa_campaign_id=camp)
    bid = wa_lead(db, "Crossover Dental")["business_id"]
    db.set_business_pipeline(bid, "booked", channel="whatsapp")

    conflicts = db.find_cross_channel_conflicts(
        [{"company": "Crossover Dental", "phone": "050 200 0040"}], channel="call")
    check("the clash is reported", len(conflicts) == 1, str(len(conflicts)))
    check("it says which channels they're already on",
          "whatsapp" in conflicts[0]["channels"], str(conflicts[0]["channels"]))
    check("and how far along they already are",
          conflicts[0]["stage"] == "Meeting booked", str(conflicts[0].get("stage")))

    plain = db.find_cross_channel_conflicts(
        [{"company": "Never Sent", "phone": "050 200 0022"}], channel="call")
    check("a business with no stage says nothing rather than guessing",
          plain and plain[0]["stage"] == "", str(plain[0].get("stage") if plain else None))


def test_http_and_ownership(db, app_mod, client, token, camp):
    print("\n9. OVER HTTP, AND WALLED PER OPERATOR")
    db.upsert_wa_leads([{"company": "Http Clinic", "phone": "050 200 0050"}],
                       default_country="AE", wa_campaign_id=camp)
    lead = wa_lead(db, "Http Clinic")
    s, body = api(client, token, "put", f"/api/businesses/{lead['business_id']}/pipeline",
                  json={"stage": "proposal_due", "channel": "whatsapp",
                        "next_action_at": "2026-10-09"})
    check("a stage can be set over HTTP", s == 200 and body["pipeline_stage"] == "proposal_due",
          str(body)[:90])

    s, body = api(client, token, "put", f"/api/businesses/{lead['business_id']}/pipeline",
                  json={"stage": "not_a_real_stage"})
    check("an unknown stage is refused", s == 400, str(body))

    s, body = api(client, token, "post", "/api/wa/leads/bulk",
                  json={"action": "pipeline", "wa_lead_ids": [lead["id"]], "stage": "won"})
    check("bulk sets it too", s == 200 and body["updated"] == 1, str(body))
    check("and it landed", db.get_business(lead["business_id"])["pipeline_stage"] == "won")

    s, rows = api(client, token, "get", "/api/pipeline")
    check("the board is served", s == 200 and isinstance(rows, list))

    bob = db.create_user("bob", "testpassword123", is_admin=False)
    bob_camp = db.create_wa_campaign("Bob", owner_id=bob, country="AE")
    db.upsert_wa_leads([{"company": "Bob Clinic", "phone": "050 200 0060"}],
                       default_country="AE", owner_id=bob, wa_campaign_id=bob_camp)
    bob_lead = next(l for l in db.get_wa_leads(owner_id=bob, include_inactive=True)
                    if l["company"] == "Bob Clinic")
    s, _ = api(client, token, "put", f"/api/businesses/{bob_lead['business_id']}/pipeline",
               json={"stage": "booked"})
    check("another operator's business is a 404, not a 403", s == 404, str(s))
    check("and nothing was written",
          db.get_business(bob_lead["business_id"])["pipeline_stage"] == "")

    alice = db.get_user_by_username("admin")["id"]
    bob_board = [r["business_id"] for r in db.get_pipeline_board(owner_id=alice)]
    check("Bob's conversations are not on Alice's board",
          bob_lead["business_id"] not in bob_board)

    print("\n   A stage one operator invents is theirs")
    bob_key = db.create_pipeline_stage("Site audit sent", owner_id=bob)
    check("Bob sees his own", bob_key in db.get_pipeline_stages(owner_id=bob))
    check("Alice does not", bob_key not in db.get_pipeline_stages(owner_id=alice))
    check("and cannot edit it",
          db.update_pipeline_stage(bob_key, owner_id=alice, label="mine now") is False)


def test_migration_is_additive(work):
    print("\n10. MIGRATION: AN OLD DATABASE GAINS THE COLUMNS, KEEPS ITS ROWS")
    os.environ["DB_PATH"] = os.path.join(work, "old.db")
    import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    uid = db_mod.create_user("solo", "testpassword123", is_admin=True)
    camp = db_mod.create_wa_campaign("Old", owner_id=uid, country="AE")
    db_mod.upsert_wa_leads([{"company": "Old Lead", "phone": "050 200 0070"}],
                           default_country="AE", owner_id=uid, wa_campaign_id=camp)
    bid = next(l for l in db_mod.get_wa_leads(owner_id=uid)
               if l["company"] == "Old Lead")["business_id"]
    check("an existing business starts with no stage",
          db_mod.get_business(bid)["pipeline_stage"] == "")

    db_mod.set_business_pipeline(bid, "booked", channel="call", owner_id=uid)
    db_mod.init_db()
    check("re-running init_db keeps the stage",
          db_mod.get_business(bid)["pipeline_stage"] == "booked")
    check("and doesn't re-seed over a relabelled built-in",
          db_mod.update_pipeline_stage("booked", owner_id=uid, label="Call booked")
          and (db_mod.init_db() or db_mod.get_pipeline_stage("booked")["label"] == "Call booked"))


def main():
    work = tempfile.mkdtemp(prefix="pipeline-")
    try:
        db, app_mod, client, token = boot(work)
        camp = db.create_wa_campaign("Pipeline campaign", country="AE")

        test_stages_exist(db)
        test_one_stage_every_channel(db, camp)
        test_cadence_rules(db, camp)
        test_terminal_is_not_suppression(db, camp)
        test_board_order(db, camp)
        test_messaged_filter_and_sent_log(db, camp)
        test_search_widened(db, camp)
        test_conflict_shows_stage(db, camp)
        test_http_and_ownership(db, app_mod, client, token, camp)
        test_migration_is_additive(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
