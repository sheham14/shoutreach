"""The WhatsApp reply checklist, and Settings -> Stages & outcomes.

Run:  python tests/test_replies_and_stages.py

The properties worth protecting:
  - a reply tick only lands on a lead you own that was actually messaged,
    records when, and comes off cleanly -- and un-ticking never pushes an
    unsent lead out of Ready to send;
  - the checklist's counts, the tab badge and the reply rate are all counted
    the same way, so they never disagree with the lists on screen;
  - the period on the rate is a cohort by first message, not by reply;
  - hiding a built-in stage or outcome hides it for the person who hid it
    and nobody else, and leaves everything already on it alone;
  - deleting a stage or outcome that's in use archives it instead, so no
    lead loses where it's up to;
  - the stages only the website sets are flagged so the picker can skip them.
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
    os.environ["DB_PATH"] = os.path.join(work, "replies.db")
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["SHOUTREACH_NO_SCHEDULER"] = "1"
    import db
    importlib.reload(db)
    db.init_db()
    alice = db.create_user("alice", "testpassword123", is_admin=True)
    bob = db.create_user("bob", "testpassword123", is_admin=False)
    import app as app_mod
    importlib.reload(app_mod)
    app_mod.app.config["TESTING"] = True

    def client_for(uid, admin):
        c = app_mod.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = uid
            s["is_admin"] = admin
            s["csrf_token"] = "t"
        return c
    return db, app_mod, alice, bob, client_for(alice, True), client_for(bob, False)


def call(client, method, url, **kw):
    r = getattr(client, method)(url, headers={"X-CSRF-Token": "t"}, **kw)
    return r.status_code, r.get_json()


def lead(db, owner, name):
    return next(l for l in db.get_wa_leads(owner_id=owner, include_inactive=True) if l["company"] == name)


def names(page):
    return sorted(r["company"] for r in page["rows"])


def test_reply_ticks(db, alice, bob, ca, cb):
    print("\n1. TICKING A REPLY")
    camp = db.create_wa_campaign("Replies", owner_id=alice, country="AE")
    db.upsert_wa_leads([{"company": n, "phone": f"050 300 000{i}"} for i, n in
                        enumerate(["Amber Dental", "Birch Dental", "Cedar Dental", "Dune Dental"], 1)],
                       default_country="AE", wa_campaign_id=camp, owner_id=alice)
    bob_camp = db.create_wa_campaign("Bob's", owner_id=bob, country="AE")
    db.upsert_wa_leads([{"company": "Bob Dental", "phone": "050 300 0099"}],
                       default_country="AE", wa_campaign_id=bob_camp, owner_id=bob)
    A, B, C, D = (lead(db, alice, n) for n in ("Amber Dental", "Birch Dental", "Cedar Dental", "Dune Dental"))
    X = lead(db, bob, "Bob Dental")
    for l in (A, B, C):
        db.mark_wa_sent(l["id"], "hello", kind="opener")
    db.mark_wa_sent(C["id"], "following up", kind="followup")
    db.mark_wa_sent(X["id"], "hello", kind="opener")

    s = db.get_wa_summary(owner_id=alice)
    check("the summary counts three waiting for a reply", s["awaiting_reply"] == 3, str(s["awaiting_reply"]))

    st, body = call(ca, "post", "/api/wa/leads/bulk",
                    json={"action": "replied", "wa_lead_ids": [A["id"], D["id"], X["id"]]})
    check("a bulk tick lands only on the messaged lead you own", st == 200 and body["updated"] == 1, str(body))
    a = db.get_wa_lead(A["id"])
    check("it's replied, out of the cadence, with a time",
          a["replied"] == 1 and a["wa_status"] == "replied" and a["replied_at"], str(dict(a))[:0])
    check("an unsent lead can't be ticked", db.get_wa_lead(D["id"])["replied"] == 0)
    check("nor another operator's", db.get_wa_lead(X["id"])["replied"] == 0)

    waiting = db.get_wa_leads_page(stage="awaiting", owner_id=alice)
    replied = db.get_wa_leads_page(stage="replied", owner_id=alice)
    check("Waiting lists the other two messaged leads", names(waiting) == ["Birch Dental", "Cedar Dental"],
          str(names(waiting)))
    check("Replied lists the ticked one", names(replied) == ["Amber Dental"], str(names(replied)))
    check("rows carry replied_at", replied["rows"][0]["replied_at"] is not None)
    ids = db.get_wa_leads_page(stage="awaiting", owner_id=alice, ids_only=True)
    check("Select all on Waiting selects the same two", sorted(ids) == sorted([B["id"], C["id"]]), str(ids))

    print("\n   Un-ticking")
    st, body = call(ca, "post", "/api/wa/leads/bulk", json={"action": "unreplied", "wa_lead_ids": [A["id"]]})
    a = db.get_wa_lead(A["id"])
    check("it comes off", st == 200 and body["updated"] == 1 and a["replied"] == 0, str(body))
    check("back in the cadence, with no reply time", a["wa_status"] == "sent" and a["replied_at"] is None)
    check("and back on Waiting", "Amber Dental" in names(db.get_wa_leads_page(stage="awaiting", owner_id=alice)))

    st, _ = call(ca, "post", f"/api/wa/leads/{D['id']}/replied", json={"replied": False})
    check("un-ticking an unsent lead leaves it ready to send",
          st == 200 and db.get_wa_lead(D["id"])["wa_status"] == "drafted",
          db.get_wa_lead(D["id"])["wa_status"])

    print("\n   The single tick keeps the first time")
    call(ca, "post", f"/api/wa/leads/{C['id']}/replied", json={"replied": True})
    with db.get_db() as conn:
        conn.execute("UPDATE wa_leads SET replied_at='2026-01-01 09:00:00' WHERE id=?", (C["id"],))
    call(ca, "post", f"/api/wa/leads/{C['id']}/replied", json={"replied": True})
    check("ticking twice doesn't move replied_at",
          db.get_wa_lead(C["id"])["replied_at"] == "2026-01-01 09:00:00", db.get_wa_lead(C["id"])["replied_at"])

    print("\n   A stage marks the reply too")
    db.set_business_pipeline(B["business_id"], "proposal_due", channel="whatsapp", owner_id=alice)
    b = db.get_wa_lead(B["id"])
    check("setting a stage records when", b["replied"] == 1 and b["replied_at"] is not None)
    return camp, A, B, C, D, X


def test_reply_rate(db, alice, bob, ca, cb, camp, A, B, C, D, X):
    print("\n2. THE REPLY RATE")
    # Now: A waiting (1 message), B replied (1 message), C replied (2 messages).
    st, s = call(ca, "get", "/api/wa/reply-stats")
    check("three messaged, two replied", st == 200 and (s["messaged"], s["replied"], s["waiting"]) == (3, 2, 1), str(s))
    check("the rate is two in three", s["reply_rate"] == 66.7, str(s["reply_rate"]))
    check("split by how many follow-ups went first", s["after"] == [1, 1, 0, 0], str(s["after"]))
    summary = db.get_wa_summary(owner_id=alice)
    check("the tab badge agrees with Waiting", summary["awaiting_reply"] == s["waiting"],
          f"{summary['awaiting_reply']} vs {s['waiting']}")

    st, s = call(ca, "get", f"/api/wa/reply-stats?wa_campaign_id={camp}")
    check("a campaign narrows it", st == 200 and s["messaged"] == 3, str(s))
    st, s = call(cb, "get", f"/api/wa/reply-stats?wa_campaign_id={camp}")
    check("another operator's campaign id shows them nothing of it", st == 200 and s["messaged"] == 0, str(s))
    st, s = call(cb, "get", "/api/wa/reply-stats")
    check("each operator counts their own", st == 200 and (s["messaged"], s["replied"]) == (1, 0), str(s))

    print("\n   A period is a cohort by first message")
    with db.get_db() as conn:
        conn.execute("UPDATE wa_log SET sent_at=datetime('now','-40 days') WHERE wa_lead_id=?", (C["id"],))
    st, s = call(ca, "get", "/api/wa/reply-stats?days=30")
    check("a lead first messaged 40 days ago is outside the last 30", st == 200 and s["messaged"] == 2, str(s))
    check("even though it replied (and was messaged) later",
          s["replied"] == 1 and s["days"] == 30, str(s))
    st, _ = call(ca, "get", "/api/wa/reply-stats?days=soon")
    check("a period that isn't a number is refused", st == 400)

    print("\n   What counts as waiting")
    db.set_wa_paused(A["id"], True)
    check("paused after a send is still waiting for a reply",
          "Amber Dental" in names(db.get_wa_leads_page(stage="awaiting", owner_id=alice))
          and db.get_wa_summary(owner_id=alice)["awaiting_reply"] == 1)
    db.set_wa_no_whatsapp([A["id"]], True, owner_id=alice)
    s = db.get_wa_reply_stats(owner_id=alice)
    check("marked not on WhatsApp drops out of the rate and the list",
          s["messaged"] == 2 and not db.get_wa_leads_page(stage="awaiting", owner_id=alice)["rows"], str(s))
    check("and out of the badge", db.get_wa_summary(owner_id=alice)["awaiting_reply"] == 0)


def test_stage_hiding(db, alice, bob, ca, cb):
    print("\n3. HIDING STAGES")
    stages = db.get_pipeline_stages(include_archived=True, owner_id=alice)
    check("the website's stages are flagged as system",
          all(stages[k]["system"] == 1 for k in ("enquired", "booked_elsewhere", "enquired_elsewhere")))
    check("the rest aren't", stages["booked"]["system"] == 0 and stages["replied"]["system"] == 0)

    st, body = call(cb, "post", "/api/pipeline-stages/won/hidden", json={"hidden": True})
    check("anyone can hide a built-in, admin or not", st == 200, str(body))
    check("it leaves the hider's pickers", "won" not in db.get_pipeline_stages(owner_id=bob))
    check("and reads as hidden in their full list",
          db.get_pipeline_stages(include_archived=True, owner_id=bob)["won"]["archived"] == 1)
    check("the other operator still has it", "won" in db.get_pipeline_stages(owner_id=alice))
    check("and the row itself isn't archived", db.get_pipeline_stage("won")["archived"] == 0)

    db.upsert_wa_leads([{"company": "Won Clinic", "phone": "050 300 0077"}],
                       default_country="AE", owner_id=bob)
    won = lead(db, bob, "Won Clinic")
    r = db.set_business_pipeline(won["business_id"], "won", channel="whatsapp", owner_id=bob)
    check("a hidden stage can still be set (the website and calls do)", r.get("ok"), str(r))
    check("and is counted as in use",
          db.get_pipeline_stages(include_archived=True, owner_id=bob)["won"]["in_use"] == 1)
    check("in use counts only your own businesses",
          db.get_pipeline_stages(include_archived=True, owner_id=alice)["won"]["in_use"] == 0)

    st, _ = call(cb, "post", "/api/pipeline-stages/won/hidden", json={"hidden": False})
    check("showing it again brings it back", st == 200 and "won" in db.get_pipeline_stages(owner_id=bob))

    bob_key = db.create_pipeline_stage("Bob's stage", owner_id=bob)
    st, _ = call(ca, "post", f"/api/pipeline-stages/{bob_key}/hidden", json={"hidden": True})
    check("nobody can hide another operator's own stage", st == 404, str(st))
    st, _ = call(ca, "post", "/api/pipeline-stages/no_such_stage/hidden", json={"hidden": True})
    check("or one that doesn't exist", st == 404)
    st, _ = call(cb, "post", f"/api/pipeline-stages/{bob_key}/hidden", json={"hidden": True})
    check("hiding your own archives it", st == 200 and db.get_pipeline_stage(bob_key)["archived"] == 1)

    print("\n4. DELETING, RE-ORDERING AND EDITING STAGES")
    used = db.create_pipeline_stage("Samples sent", owner_id=bob)
    unused = db.create_pipeline_stage("Never used", owner_id=bob)
    db.set_business_pipeline(won["business_id"], used, channel="whatsapp", owner_id=bob)
    st, body = call(cb, "delete", f"/api/pipeline-stages/{used}")
    check("a stage in use is archived, not deleted", st == 200 and body["result"] == "archived", str(body))
    check("and the business keeps it", db.get_business(won["business_id"])["pipeline_stage"] == used)
    st, body = call(cb, "delete", f"/api/pipeline-stages/{unused}")
    check("an unused one is deleted for good", st == 200 and body["result"] == "deleted"
          and db.get_pipeline_stage(unused) is None, str(body))
    st, _ = call(cb, "delete", "/api/pipeline-stages/booked")
    check("a built-in can't be deleted", st == 400)
    st, _ = call(ca, "delete", f"/api/pipeline-stages/{used}")
    check("nor someone else's", st == 400 and db.get_pipeline_stage(used) is not None)

    st, _ = call(cb, "patch", f"/api/pipeline-stages/{used}", json={"sort_order": 35.5})
    check("your own stage moves between two others", st == 200 and db.get_pipeline_stage(used)["sort_order"] == 35.5,
          str(db.get_pipeline_stage(used)["sort_order"]))
    st, _ = call(cb, "patch", f"/api/pipeline-stages/{used}", json={"sort_order": "first"})
    check("a position that isn't a number is ignored", st == 400 and db.get_pipeline_stage(used)["sort_order"] == 35.5)
    st, _ = call(cb, "patch", "/api/pipeline-stages/won", json={"sort_order": 1})
    check("only an admin re-orders a built-in", st == 403)
    st, _ = call(ca, "patch", "/api/pipeline-stages/won", json={"sort_order": 55})
    check("and an admin can", st == 200 and db.get_pipeline_stage("won")["sort_order"] == 55)
    st, _ = call(cb, "patch", f"/api/pipeline-stages/{used}", json={"tone": "rainbow"})
    check("a colour that isn't one of ours is ignored", db.get_pipeline_stage(used)["tone"] == "neutral")
    st, body = call(cb, "post", "/api/pipeline-stages", json={"label": "Odd", "tone": "<b>"})
    check("including on create", st == 200 and db.get_pipeline_stage(body["key"])["tone"] == "neutral")


def test_outcome_hiding(db, alice, bob, ca, cb):
    print("\n5. HIDING CALL OUTCOMES")
    st, _ = call(cb, "post", "/api/call-outcomes/voicemail/hidden", json={"hidden": True})
    check("a built-in outcome can be hidden", st == 200)
    st, q = call(cb, "get", "/api/calls/queue")
    check("it's gone from the hider's dialler",
          st == 200 and "voicemail" not in [o["key"] for o in q["outcomes"]], str(st))
    check("but its label still resolves for call history", q["outcome_labels"].get("voicemail") == "Left voicemail")
    st, q = call(ca, "get", "/api/calls/queue")
    check("the other operator's dialler still has it", "voicemail" in [o["key"] for o in q["outcomes"]])
    st, rows = call(cb, "get", "/api/call-outcomes")
    vm = next(o for o in rows if o["key"] == "voicemail")
    check("the editor lists it as hidden, with a use count", vm["archived"] == 1 and vm["in_use"] == 0, str(vm))
    bob_oc = db.create_call_outcome("Gatekeeper", owner_id=bob)
    st, _ = call(ca, "post", f"/api/call-outcomes/{bob_oc}/hidden", json={"hidden": True})
    check("nobody hides another operator's own outcome", st == 404)
    st, _ = call(cb, "post", "/api/call-outcomes/voicemail/hidden", json={"hidden": False})
    check("and it comes back", "voicemail" in db.get_call_outcomes(owner_id=bob))


def test_user_delete_cleans_up(db, ca):
    print("\n6. A DELETED USER'S HIDDEN LIST GOES WITH THEM")
    carol = db.create_user("carol", "testpassword123")
    db.set_choice_hidden("stage", "won", True, owner_id=carol)
    ok, err = db.delete_user(carol)
    with db.get_db() as conn:
        left = conn.execute("SELECT COUNT(*) FROM hidden_choices WHERE owner_id=?", (carol,)).fetchone()[0]
    check("deleted, and nothing left behind", ok and left == 0, str(err))


def main():
    work = tempfile.mkdtemp(prefix="replies-")
    try:
        db, app_mod, alice, bob, ca, cb = boot(work)
        camp, A, B, C, D, X = test_reply_ticks(db, alice, bob, ca, cb)
        test_reply_rate(db, alice, bob, ca, cb, camp, A, B, C, D, X)
        test_stage_hiding(db, alice, bob, ca, cb)
        test_outcome_hiding(db, alice, bob, ca, cb)
        test_user_delete_cleans_up(db, ca)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
