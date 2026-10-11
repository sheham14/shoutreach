"""WhatsApp follow-ups as a sequence that never stops.

Run:  python tests/test_wa_sequence.py

A lead's follow-ups go: its own (as many as it brought), then the campaign's
list of follow-ups from the top, round and round until it replies or is
paused. Nobody gets one message on repeat. An optional long-term gap slows
the cadence once everything written for a lead has gone.
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
    work = tempfile.mkdtemp(prefix="wa-sequence-")
    try:
        os.environ["DB_PATH"] = os.path.join(work, "seq.db")
        import db
        importlib.reload(db)
        db.init_db()
        me = db.create_user("admin", "test-password-123", is_admin=True)
        camp = db.create_wa_campaign("Clinics", owner_id=me, country="AE")
        db.update_wa_campaign(camp, templates={"followups": [["Step one"], ["Step two"], ["Step three"]]})

        def lead_named(name):
            with db.get_db() as conn:
                wid = conn.execute("""SELECT w.id FROM wa_leads w JOIN businesses b ON b.id=w.business_id
                                       WHERE b.name=?""", (name,)).fetchone()[0]
            return db.get_wa_lead(wid)

        def sequence(lead, n):
            c = db.get_wa_campaign(lead["wa_campaign_id"])
            return [db.wa_message_for({**lead, "followup_count": i}, c, "followup") for i in range(n)]

        print("\n1. THE CAMPAIGN HOLDS A LIST OF FOLLOW-UPS")
        c = db.get_wa_campaign(camp)
        check("three follow-ups saved, the first still readable as 'followup'",
              c["templates"]["followups"] == [["Step one"], ["Step two"], ["Step three"]]
              and c["templates"]["followup"] == ["Step one"] and c["followup_steps"] == 3, str(c["templates"]))
        db.update_wa_campaign(camp, templates={"followup": ["Step one, reworded"]})
        c = db.get_wa_campaign(camp)
        check("saving the older one-follow-up shape replaces only the first",
              [s[0] for s in c["templates"]["followups"]] == ["Step one, reworded", "Step two", "Step three"])
        db.update_wa_campaign(camp, templates={"followups": [["Step one"], ["Step two"], ["Step three"]]})
        try:
            db.update_wa_campaign(camp, templates={"followups": [[""], []]})
            refused = False
        except ValueError:
            refused = True
        check("a campaign can't be left with no follow-up", refused)

        print("\n2. A LEAD WITH ITS OWN FOLLOW-UPS, THEN THE CAMPAIGN'S, ROUND AND ROUND")
        rows = [
            {"company": "Five Own", "phone": "050 100 0001", "message": "Hi",
             **{f"followup_{n}": f"Own {n}" for n in range(1, 6)}},
            {"company": "Gappy", "phone": "050 100 0002", "followup_1": "Own 1", "followup_3": "Own 3"},
            {"company": "None Own", "phone": "050 100 0003"},
        ]
        accepted, _ids, report = db.upsert_wa_leads(rows, default_country="AE", owner_id=me, wa_campaign_id=camp)
        check("all three import", accepted == 3)
        check("the report counts all five follow-up positions", report["followups"] == [2, 1, 2, 1, 1],
              str(report["followups"]))
        five = lead_named("Five Own")
        check("a lead keeps all five of its own", five["own_followups"] == 5
              and db.parse_lead_followups(five["draft_followups"])[4] == "Own 5")
        seq = sequence(five, 12)
        check("its own five, then the campaign's three, then round again",
              seq == ["Own 1", "Own 2", "Own 3", "Own 4", "Own 5",
                      "Step one", "Step two", "Step three", "Step one", "Step two", "Step three", "Step one"], str(seq))
        seq = sequence(lead_named("Gappy"), 6)
        check("a gap in its own takes the campaign's follow-up for that position",
              seq == ["Own 1", "Step two", "Own 3", "Step one", "Step two", "Step three"], str(seq))
        seq = sequence(lead_named("None Own"), 7)
        check("a lead with none cycles through the campaign's",
              seq == ["Step one", "Step two", "Step three", "Step one", "Step two", "Step three", "Step one"], str(seq))
        check("never the same message twice in a row",
              all(a != b for a, b in zip(sequence(five, 40), sequence(five, 40)[1:])))
        src = db.wa_followup_source(five, db.get_wa_campaign(camp), 6)
        check("the page can say where a follow-up comes from", src == {"own": False, "step": 2, "of": 3}, str(src))

        print("\n3. WRITING ONE FOR A LEAD BY HAND, PAST THE THIRD")
        none = lead_named("None Own")
        check("a fourth can be written", db.set_wa_followup_draft(none["id"], 3, "Hand-written 4"))
        none = db.get_wa_lead(none["id"])
        seq = sequence(none, 6)
        check("positions before it come from the campaign, then the campaign's list from the top",
              none["own_followups"] == 4 and seq == ["Step one", "Step two", "Step three", "Hand-written 4",
                                                     "Step one", "Step two"], str(seq))
        check("the ceiling is enforced", not db.set_wa_followup_draft(none["id"], db.WA_MAX_LEAD_FOLLOWUPS, "x"))

        print("\n4. THE LONG-TERM GAP")
        def due_names():
            return {r["company"] for r in db.get_wa_followups_due(owner_id=me, wa_campaign_id=camp)}
        with db.get_db() as conn:
            conn.execute("""UPDATE wa_leads SET wa_status='sent', sent_date=datetime('now','-5 days'),
                                   followup_count=CASE (SELECT name FROM businesses b WHERE b.id=business_id)
                                     WHEN 'Five Own' THEN 8 WHEN 'Gappy' THEN 1 ELSE 2 END""")
        check("off by default: every lead past the 3-day gap is due",
              due_names() == {"Five Own", "Gappy", "None Own"}, str(due_names()))
        db.update_wa_campaign(camp, long_followup_days=30)
        check("on: a lead that has had everything written for it waits the long gap",
              due_names() == {"Gappy", "None Own"}, str(due_names()))
        with db.get_db() as conn:
            conn.execute("UPDATE wa_leads SET sent_date=datetime('now','-31 days')")
        check("...and is due again after it", "Five Own" in due_names())
        db.update_wa_campaign(camp, long_followup_days=0)
        check("turning it off puts everyone back on the usual gap", len(due_names()) == 3)

        print("\n5. A COPIED CAMPAIGN KEEPS ITS FOLLOW-UPS")
        db.update_wa_campaign(camp, long_followup_days=45)
        copy = db.get_wa_campaign(db.create_wa_campaign("Copy", owner_id=me, copy_from=camp))
        check("the list, its count and the long gap come along",
              len(copy["templates"]["followups"]) == 3 and copy["followup_steps"] == 3
              and copy["long_followup_days"] == 45, str(copy["templates"]))

        print("\n6. DOWNLOADING THEM")
        cols, out = db.export_leads("wa", [five["id"]], owner_id=me)
        check("the CSV has a column for every follow-up written for the lead (5 own + 3)",
              "followup_8" in cols and "followup_9" not in cols and out[0]["followup_6"] == "Step one", str(cols))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
