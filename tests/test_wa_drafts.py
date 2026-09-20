"""Hyper-personalised WhatsApp copy: leads that arrive with their own opener
and follow-ups written for them, instead of reading the campaign's templates.

Run:  python tests/test_wa_drafts.py

The properties worth protecting here:
  - a lead's own follow-ups are used in order, and the campaign's template is
    still the floor once they run out -- the cadence never ends, so it has to
    have something to say forever;
  - an import replaces copy an earlier import wrote, and never replaces what
    the operator wrote here, which exists nowhere else;
  - nothing about importing copy re-queues a lead that has already been
    messaged. There is a real person on the other end of a second opener.
"""
import importlib
import json
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


def lead_named(db, name):
    return next(l for l in db.get_wa_leads(include_inactive=True, with_message=True)
                if l["company"] == name)


def row(name, phone, **extra):
    base = {"company": name, "phone": phone}
    base.update(extra)
    return base


def test_cadence_order(db, camp):
    print("\n1. A LEAD'S OWN FOLLOW-UPS ARE USED IN ORDER, THEN THE TEMPLATE")
    db.upsert_wa_leads([row(
        "Order Clinic", "050 100 0001",
        message="Opener for {{business_name}}",
        followup_1="Mine 1", followup_2="Mine 2", followup_3="Mine 3",
    )], default_country="AE", wa_campaign_id=camp)

    lead = lead_named(db, "Order Clinic")
    campaign = db.get_wa_campaign(camp)
    check("the opener is the imported one, placeholders filled",
          lead["message"] == "Opener for Order Clinic", lead["message"])

    seen = []
    for count in range(len(db.parse_lead_followups(lead["draft_followups"])) + 1):
        lead["followup_count"] = count
        seen.append(db.wa_message_for(lead, campaign, "followup"))
    check("follow-ups 1-3 are the lead's own, in order",
          seen[:3] == ["Mine 1", "Mine 2", "Mine 3"], str(seen[:3]))
    template_fu = campaign["templates"]["followup"][0]
    check("follow-up 4 falls back to the campaign template",
          seen[3] == db.render_wa_message(template_fu, lead, campaign["variables"]),
          seen[3][:48])
    check("the cadence still has something to say at follow-up 20",
          bool(db.wa_message_for({**lead, "followup_count": 20}, campaign, "followup").strip()))


def test_partial_and_placeholders(db, camp):
    print("\n2. A PARTLY-WRITTEN BATCH FALLS BACK PER SLOT")
    db.upsert_wa_leads([row(
        "Partial Clinic", "050 100 0002", city="Dubai",
        message="Hi {{business_name}} in {{city}}",
        followup_2="Only the second one",
    )], default_country="AE", wa_campaign_id=camp)
    lead = lead_named(db, "Partial Clinic")
    campaign = db.get_wa_campaign(camp)
    template_fu = db.render_wa_message(campaign["templates"]["followup"][0], lead,
                                       campaign["variables"])

    check("placeholders inside imported copy are filled",
          lead["message"] == "Hi Partial Clinic in Dubai", lead["message"])
    check("an empty slot uses the campaign template",
          db.wa_message_for({**lead, "followup_count": 0}, campaign, "followup") == template_fu)
    check("a filled slot is used even when its neighbours are empty",
          db.wa_message_for({**lead, "followup_count": 1}, campaign, "followup")
          == "Only the second one")

    print("\n   A placeholder that can't be filled degrades, never leaks braces")
    db.upsert_wa_leads([row("Brace Clinic", "050 100 0003",
                            message="Hi {{business_name}}, about {{nonsense|your website}}")],
                       default_country="AE", wa_campaign_id=camp)
    msg = lead_named(db, "Brace Clinic")["message"]
    check("unknown placeholder uses its fallback", msg.endswith("about your website"), msg)
    check("no literal braces survive to the phone", "{{" not in msg and "}}" not in msg)


def test_import_never_clobbers_hand_edits(db, camp):
    print("\n3. AN IMPORT REPLACES IMPORTED COPY, NEVER THE OPERATOR'S OWN")
    db.upsert_wa_leads([row("Clobber Co", "050 100 0004", message="V1", followup_1="V1-F1")],
                       default_country="AE", wa_campaign_id=camp)
    lead = lead_named(db, "Clobber Co")
    check("imported copy is marked as such", lead["message_source"] == "import",
          lead["message_source"])

    _n, _ids, report = db.upsert_wa_leads(
        [row("Clobber Co", "050 100 0004", message="V2", followup_1="V2-F1")],
        default_country="AE", wa_campaign_id=camp)
    lead = lead_named(db, "Clobber Co")
    check("a re-import replaces the earlier import", lead["draft_message"] == "V2",
          lead["draft_message"])
    check("its follow-ups are replaced too",
          db.parse_lead_followups(lead["draft_followups"])[0] == "V2-F1")
    check("nothing is reported as preserved", report["kept_edits"] == 0)

    db.set_wa_message(lead["id"], "TYPED BY HAND")
    _n, _ids, report = db.upsert_wa_leads(
        [row("Clobber Co", "050 100 0004", message="V3", followup_1="V3-F1")],
        default_country="AE", wa_campaign_id=camp)
    lead = lead_named(db, "Clobber Co")
    check("a hand edit survives a re-import", lead["draft_message"] == "TYPED BY HAND",
          lead["draft_message"])
    check("so do the follow-ups on that lead",
          db.parse_lead_followups(lead["draft_followups"])[0] == "V2-F1")
    check("and the import says so rather than silently skipping",
          report["kept_edits"] == 1, str(report))

    print("\n   Editing a follow-up protects the lead the same way")
    db.upsert_wa_leads([row("Fu Edit Co", "050 100 0005", message="I1", followup_1="I1-F1")],
                       default_country="AE", wa_campaign_id=camp)
    fu_lead = lead_named(db, "Fu Edit Co")
    db.set_wa_followup_draft(fu_lead["id"], 0, "MY FOLLOW-UP")
    _n, _ids, report = db.upsert_wa_leads(
        [row("Fu Edit Co", "050 100 0005", message="I2", followup_1="I2-F1")],
        default_country="AE", wa_campaign_id=camp)
    fu_lead = lead_named(db, "Fu Edit Co")
    check("a hand-written follow-up is kept",
          db.parse_lead_followups(fu_lead["draft_followups"])[0] == "MY FOLLOW-UP")
    check("and the opener it was written to follow is kept with it",
          fu_lead["draft_message"] == "I1", fu_lead["draft_message"])

    print("\n   Clearing a follow-up puts that one back on the template")
    db.set_wa_followup_draft(fu_lead["id"], 0, "")
    fu_lead = lead_named(db, "Fu Edit Co")
    campaign = db.get_wa_campaign(camp)
    check("the slot is empty again",
          not db.parse_lead_followups(fu_lead["draft_followups"])[0])
    check("so the template is what would go",
          db.wa_message_for({**fu_lead, "followup_count": 0}, campaign, "followup")
          == db.render_wa_message(campaign["templates"]["followup"][0], fu_lead,
                                  campaign["variables"]))


def test_reimport_cannot_requeue(db, camp):
    print("\n4. A RE-IMPORT CANNOT RE-QUEUE A LEAD ALREADY MESSAGED")
    db.upsert_wa_leads([row("Sent Co", "050 100 0006", message="First contact")],
                       default_country="AE", wa_campaign_id=camp)
    lead = lead_named(db, "Sent Co")
    db.mark_wa_sent(lead["id"], "First contact", kind="opener")
    before = lead_named(db, "Sent Co")
    check("the lead is out of Ready to send once sent", before["wa_status"] == "sent")

    db.upsert_wa_leads([row("Sent Co", "050 100 0006", message="Second contact")],
                       default_country="AE", wa_campaign_id=camp)
    after = lead_named(db, "Sent Co")
    check("re-importing does not put it back in the queue",
          after["wa_status"] == "sent", after["wa_status"])
    check("its sent date is untouched", after["sent_date"] == before["sent_date"])
    ready_ids = [r["id"] for r in db.get_wa_ready()]
    check("and it is not in Ready to send", after["id"] not in ready_ids)
    check("the new copy is still stored for the follow-ups to come",
          after["draft_message"] == "Second contact", after["draft_message"])


def test_report_counts(db, camp):
    print("\n5. THE IMPORT SAYS WHAT ARRIVED, PER SLOT")
    _n, _ids, report = db.upsert_wa_leads([
        row("Cov A", "050 100 0010", message="m", followup_1="a", followup_2="b", followup_3="c"),
        row("Cov B", "050 100 0011", message="m", followup_1="a"),
        row("Cov C", "050 100 0012", message="m"),
        row("Cov D", "050 100 0013"),
    ], default_country="AE", wa_campaign_id=camp)
    check("openers counted", report["opener"] == 3, str(report))
    check("each follow-up slot counted separately",
          report["followups"] == [2, 1, 1], str(report["followups"]))

    _n, _ids, report = db.upsert_wa_leads(
        [row("No Phone Co", None, message="written but nowhere to put it")],
        default_country="AE", wa_campaign_id=camp)
    check("a row with copy but no phone is counted, not dropped in silence",
          report["no_phone"] == 1, str(report))

    _n, _ids, report = db.upsert_wa_leads(
        [row("Runaway Co", "050 100 0014", message="x" * (db.WA_MAX_DRAFT_CHARS + 1))],
        default_country="AE", wa_campaign_id=camp)
    check("an implausibly long message is rejected", report["too_long"] == 1, str(report))
    check("but the lead is still imported", report["opener"] == 0
          and lead_named(db, "Runaway Co") is not None)
    runaway = lead_named(db, "Runaway Co")
    check("and it falls back to the template rather than half a message",
          not runaway["message_edited"] and runaway["message"].strip() != "")


def test_stats_exclude_own_copy(db, camp):
    print("\n6. REPLY FIGURES DON'T CREDIT A VERSION FOR COPY IT NEVER SENT")
    other = db.create_wa_campaign("Stats campaign", country="AE")
    db.update_wa_campaign(other, templates={"opener": ["Version A copy", "Version B copy"],
                                            "followup": ["Follow up"]})
    db.upsert_wa_leads([row("Stats Template Co", "050 100 0020")],
                       default_country="AE", wa_campaign_id=other)
    db.upsert_wa_leads([row("Stats Own Co", "050 100 0021", message="bespoke")],
                       default_country="AE", wa_campaign_id=other)

    for name in ("Stats Template Co", "Stats Own Co"):
        lead = lead_named(db, name)
        db.mark_wa_sent(lead["id"], lead["message"], kind="opener",
                        template_variant=lead["template_variant"])
    own_lead = lead_named(db, "Stats Own Co")
    db.mark_wa_replied(own_lead["id"], True)

    stats = db.get_wa_variant_stats(wa_campaign_id=other)
    own_rows = [s for s in stats if s["own_copy"]]
    arm_rows = [s for s in stats if not s["own_copy"]]
    check("a lead with its own copy is reported apart", len(own_rows) == 1, str(stats))
    check("its reply is counted there", own_rows and own_rows[0]["replied"] == 1)
    check("and not against any template version",
          all(s["replied"] == 0 for s in arm_rows), str(arm_rows))
    check("the template leads are still measured",
          sum(s["sent"] for s in arm_rows) == 1, str(arm_rows))


def test_http_import(db, client, token, camp):
    print("\n7. OVER HTTP: JSON IN, COVERAGE BACK")
    payload = [
        {"company": "Json Clinic", "phone": "050 100 0030", "city": "Dubai",
         "message": "Hi {{business_name}}!\n\nA message with\nreal line breaks, and a comma.",
         "followup_1": "First", "followup_2": "Second", "followup_3": "Third"},
    ]
    s, body = api(client, token, "post", "/api/wa/import",
                  json={"rows": payload, "country": "AE", "wa_campaign_id": camp})
    check("the import is accepted", s == 200 and body.get("ok"), str(body)[:120])
    check("it reports the copy it got",
          body["drafts"]["opener"] == 1 and body["drafts"]["followups"] == [1, 1, 1],
          str(body.get("drafts")))
    lead = lead_named(db, "Json Clinic")
    check("multi-line copy survives the round trip",
          lead["message"].count("\n") == 3 and lead["message"].startswith("Hi Json Clinic!"),
          repr(lead["message"][:40]))

    s, body = api(client, token, "post", "/api/wa/import",
                  json={"rows": ["just a string"], "country": "AE", "wa_campaign_id": camp})
    check("a list of bare strings is refused with an explanation",
          s == 400 and "object" in (body.get("error") or "").lower(), str(body))

    print("\n   Editing one follow-up over HTTP")
    s, body = api(client, token, "put", f"/api/wa/leads/{lead['id']}/followup/1",
                  json={"message": "Edited second"})
    check("the slot is saved", s == 200 and body["followups"][1] == "Edited second", str(body))
    check("the lead now counts as the operator's own", body["message_source"] == "manual")
    s, body = api(client, token, "put", f"/api/wa/leads/{lead['id']}/followup/9",
                  json={"message": "nope"})
    check("a slot that doesn't exist is a 404, not a silent no-op", s == 404, str(body))


def test_ownership(db, app_mod, work):
    print("\n8. IMPORTED COPY IS WALLED LIKE EVERY OTHER LEAD")
    bob = db.create_user("bob", "testpassword123", is_admin=False)
    alice = db.get_user_by_username("admin")["id"]
    bob_camp = db.create_wa_campaign("Bob copy", owner_id=bob, country="AE")
    db.upsert_wa_leads([row("Bob Bespoke Co", "050 100 0040", message="Bob's own wording")],
                       default_country="AE", owner_id=bob, wa_campaign_id=bob_camp)

    alice_leads = [l["company"] for l in db.get_wa_leads(owner_id=alice, include_inactive=True)]
    check("Bob's lead is not in Alice's list", "Bob Bespoke Co" not in alice_leads)
    bob_leads = [l["company"] for l in db.get_wa_leads(owner_id=bob, include_inactive=True)]
    check("it is in Bob's", "Bob Bespoke Co" in bob_leads)

    bob_lead = next(l for l in db.get_wa_leads(owner_id=bob, include_inactive=True)
                    if l["company"] == "Bob Bespoke Co")
    client = app_mod.app.test_client()
    r = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.get_data(as_text=True)).group(1)
    client.post("/login", data={"username": "admin", "password": "testpassword123",
                                "csrf_token": csrf})
    token = client.get("/api/csrf").get_json()["csrf_token"]
    s, _body = api(client, token, "put", f"/api/wa/leads/{bob_lead['id']}/followup/0",
                   json={"message": "Alice writing on Bob's lead"})
    check("Alice cannot write copy onto Bob's lead (404, not 403)", s == 404, str(s))
    stored = db.parse_lead_followups(
        db.get_wa_lead(bob_lead["id"])["draft_followups"])
    check("and nothing was written", not stored[0], str(stored))


def test_migration(work):
    print("\n9. MIGRATION: AN OLD DATABASE GAINS THE COLUMNS AND KEEPS ITS COPY")
    os.environ["DB_PATH"] = os.path.join(work, "old.db")
    import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    uid = db_mod.create_user("solo", "testpassword123", is_admin=True)
    camp = db_mod.create_wa_campaign("Old", owner_id=uid, country="AE")
    db_mod.upsert_wa_leads([row("Old Hand Edit", "050 100 0050")],
                           default_country="AE", owner_id=uid, wa_campaign_id=camp)
    lead = next(l for l in db_mod.get_wa_leads(owner_id=uid) if l["company"] == "Old Hand Edit")

    # The shape a pre-message_source database is in: edited, with nothing
    # saying who edited it.
    with db_mod.get_db() as conn:
        conn.execute("UPDATE wa_leads SET draft_message=?, message_edited=1, paraphrased=0, "
                     "message_source='' WHERE id=?", ("Written before the column existed", lead["id"]))
        conn.execute("DELETE FROM settings WHERE key=?", (db_mod._WA_MESSAGE_SOURCE_MARKER,))

    db_mod.init_db()
    after = db_mod.get_wa_lead(lead["id"])
    check("the text is untouched", after["draft_message"] == "Written before the column existed")
    check("and is now recorded as the operator's own", after["message_source"] == "manual",
          after["message_source"])

    _n, _ids, report = db_mod.upsert_wa_leads(
        [row("Old Hand Edit", "050 100 0050", message="an import trying its luck")],
        default_country="AE", owner_id=uid, wa_campaign_id=camp)
    check("so an import leaves it alone",
          db_mod.get_wa_lead(lead["id"])["draft_message"] == "Written before the column existed")
    check("and reports it", report["kept_edits"] == 1)

    print("\n   Running it twice changes nothing more")
    db_mod.init_db()
    check("the marker keeps it one-shot",
          db_mod.get_wa_lead(lead["id"])["message_source"] == "manual")


def test_no_send_path(app_mod):
    print("\n10. NOTHING HERE SENDS ANYTHING")
    import inspect
    src = inspect.getsource(app_mod)
    for needle in ("whatsapp.send", "pywhatkit", "selenium", "web.whatsapp.com/send"):
        check(f"the server never reaches WhatsApp itself ({needle})", needle not in src)
    check("the follow-up route only stores text",
          "set_wa_followup_draft" in src and "requests.post" not in
          inspect.getsource(app_mod.api_wa_update_followup))


def main():
    work = tempfile.mkdtemp(prefix="wa-drafts-")
    try:
        db, app_mod, client, token = boot(work)
        camp = db.create_wa_campaign("Bespoke campaign", country="AE")

        test_cadence_order(db, camp)
        test_partial_and_placeholders(db, camp)
        test_import_never_clobbers_hand_edits(db, camp)
        test_reimport_cannot_requeue(db, camp)
        test_report_counts(db, camp)
        test_stats_exclude_own_copy(db, camp)
        test_http_import(db, client, token, camp)
        test_ownership(db, app_mod, work)
        test_no_send_path(app_mod)
        test_migration(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
