"""A/B variant assignment.

Run:  python tests/test_variants.py

The behaviour these pin, and why:

Copy used to live in two places -- steps.subject/body_html plus an optional
variant list -- so a two-arm test presented three editors and the stats showed
three arms, the third being whoever was enrolled before the variants existed.
Worse, the variant was drawn at enrollment from step 1 only, so configuring
A/B *after* enrolling contacts did nothing at all: every contact kept a NULL
label and received the fallback copy, with no warning anywhere.

Now every step owns at least one variant, the draw considers every label the
campaign defines, and activation fills in contacts that predate the variants --
weighted, and only for enrollments that have not been sent to yet.
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


def fresh(work, name):
    os.environ["DB_PATH"] = os.path.join(work, name)
    import db
    importlib.reload(db)
    db.init_db()
    return db


def make_contacts(db, n, prefix="c"):
    """
    Distinct business per contact, and a distinct domain per prefix.

    Reusing one set of domains across groups let duplicate suppression file the
    later groups as duplicates of the earlier ones, so they silently refused to
    enroll and the variant assertions had nothing to measure.
    """
    db.upsert_contacts([
        {"email": f"{prefix}{i}@{prefix}{i}.ca", "company": f"{prefix} {i}",
         "website": f"https://{prefix}{i}.ca"}
        for i in range(n)
    ])
    with db.get_db() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM contacts WHERE email LIKE ? ORDER BY id", (f"{prefix}%@%",)
        ).fetchall()]


def labels_for(db, campaign_id):
    with db.get_db() as conn:
        return [r["variant_label"] for r in conn.execute(
            "SELECT variant_label FROM enrollments WHERE campaign_id=?", (campaign_id,)
        ).fetchall()]


def main():
    work = tempfile.mkdtemp(prefix="variants_")
    try:
        db = fresh(work, "variants.db")

        print("\n1. EVERY STEP OWNS ITS COPY AS A VARIANT")
        cid = db.create_campaign("Promotion test")
        db.upsert_step(cid, 1, "Base subject", "Base body", 0)
        step = db.get_steps(cid)[0]
        variants = db.get_step_variants(step["id"])
        check("a step with no A/B still has one variant", len(variants) == 1,
              str(variants))
        check("and it is labelled A", variants and variants[0]["label"] == "A")
        check("carrying the step's own copy",
              variants and variants[0]["subject"] == "Base subject"
              and variants[0]["body_html"] == "Base body")
        check("at full weight", variants and variants[0]["weight"] == 100,
              str(variants[0]["weight"] if variants else None))

        print("\n2. ONE ARM IS NOT A TEST")
        ids = make_contacts(db, 6, "solo")
        enrolled, _ = db.enroll_contacts_bulk(cid, ids)
        check("contacts enroll", enrolled == 6, f"enrolled={enrolled}")
        check("a single-arm campaign assigns no label",
              all(l is None for l in labels_for(db, cid)), str(labels_for(db, cid)))
        check("get_campaign_variants reports the one arm",
              len(db.get_campaign_variants(cid)) == 1)

        print("\n3. TWO ARMS SPLIT NEW ENROLLMENTS")
        cid2 = db.create_campaign("Split test")
        db.upsert_step(cid2, 1, "A subject", "A body", 0)
        s2 = db.get_steps(cid2)[0]
        db.save_step_variants(s2["id"], [
            {"label": "A", "subject": "A subject", "body_html": "A body", "weight": 50},
            {"label": "B", "subject": "B subject", "body_html": "B body", "weight": 50},
        ])
        ids2 = make_contacts(db, 40, "split")
        db.enroll_contacts_bulk(cid2, ids2)
        got = labels_for(db, cid2)
        check("every contact gets a label", all(l in ("A", "B") for l in got), str(set(got)))
        check("both arms are used", {"A", "B"} <= set(got), str(set(got)))

        print("\n4. WEIGHTS ARE RESPECTED")
        cid3 = db.create_campaign("Weighted test")
        db.upsert_step(cid3, 1, "x", "y", 0)
        s3 = db.get_steps(cid3)[0]
        db.save_step_variants(s3["id"], [
            {"label": "A", "subject": "A", "body_html": "A", "weight": 90},
            {"label": "B", "subject": "B", "body_html": "B", "weight": 10},
        ])
        ids3 = make_contacts(db, 200, "weight")
        db.enroll_contacts_bulk(cid3, ids3)
        got3 = labels_for(db, cid3)
        a_share = got3.count("A") / len(got3)
        check("a 90/10 split lands roughly on 90%", 0.78 <= a_share <= 0.98,
              f"A={a_share:.0%} of {len(got3)}")

        print("\n5. A VARIANT ON A LATER STEP IS NOT INERT")
        # Drawing only from step 1 meant B here could never be sent to anyone.
        cid4 = db.create_campaign("Later-step test")
        db.upsert_step(cid4, 1, "s1", "b1", 0)
        db.upsert_step(cid4, 2, "s2", "b2", 3)
        step2 = [s for s in db.get_steps(cid4) if s["step_num"] == 2][0]
        db.save_step_variants(step2["id"], [
            {"label": "A", "subject": "s2a", "body_html": "b2a", "weight": 50},
            {"label": "B", "subject": "s2b", "body_html": "b2b", "weight": 50},
        ])
        arms4 = db.get_campaign_variants(cid4)
        check("the campaign reports both arms",
              {v["label"] for v in arms4} == {"A", "B"}, str(arms4))
        # The split must come from the step where it was configured. Taking
        # each label's first appearance would pit step 1's auto-created A at
        # weight 100 against step 2's B at 50 -- a 50/50 running as 67/33.
        check("the weights are the ones actually configured, 50/50",
              {v["label"]: v["weight"] for v in arms4} == {"A": 50, "B": 50},
              str(arms4))
        ids4 = make_contacts(db, 30, "later")
        db.enroll_contacts_bulk(cid4, ids4)
        got4 = labels_for(db, cid4)
        check("contacts are split even though step 1 has one arm",
              {"A", "B"} <= set(got4), str(set(got4)))

        print("\n6. ACTIVATION BACKFILLS CONTACTS ENROLLED BEFORE THE VARIANTS")
        cid5 = db.create_campaign("Backfill test")
        db.upsert_step(cid5, 1, "only", "copy", 0)
        ids5 = make_contacts(db, 30, "backfill")
        db.enroll_contacts_bulk(cid5, ids5)
        check("they start with no label",
              all(l is None for l in labels_for(db, cid5)))

        s5 = db.get_steps(cid5)[0]
        db.save_step_variants(s5["id"], [
            {"label": "A", "subject": "A", "body_html": "A", "weight": 50},
            {"label": "B", "subject": "B", "body_html": "B", "weight": 50},
        ])
        assigned = db.assign_missing_variants(cid5)
        check("every unassigned contact is filled in", assigned == 30, f"assigned={assigned}")
        got5 = labels_for(db, cid5)
        check("none is left without a label", all(l in ("A", "B") for l in got5))
        check("the backfill is split, not dumped into A",
              {"A", "B"} <= set(got5) and got5.count("A") < 30,
              f"A={got5.count('A')} B={got5.count('B')}")

        check("running it again is a no-op", db.assign_missing_variants(cid5) == 0)

        print("\n7. CONTACTS ALREADY MID-SEQUENCE ARE LEFT ALONE")
        # Switching arms between steps of one thread would change the voice
        # mid-conversation and file the earlier sends under the wrong arm.
        cid6 = db.create_campaign("Mid-sequence test")
        db.upsert_step(cid6, 1, "one", "body", 0)
        ids6 = make_contacts(db, 4, "mid")
        db.enroll_contacts_bulk(cid6, ids6)
        with db.get_db() as conn:
            first = conn.execute(
                "SELECT id, contact_id FROM enrollments WHERE campaign_id=? ORDER BY id LIMIT 1",
                (cid6,)
            ).fetchone()
        db.log_send(cid6, first["contact_id"], 1, "one", "msg-1")

        s6 = db.get_steps(cid6)[0]
        db.save_step_variants(s6["id"], [
            {"label": "A", "subject": "A", "body_html": "A", "weight": 50},
            {"label": "B", "subject": "B", "body_html": "B", "weight": 50},
        ])
        assigned6 = db.assign_missing_variants(cid6)
        check("only the untouched enrollments are assigned", assigned6 == 3,
              f"assigned={assigned6}")
        with db.get_db() as conn:
            sent_label = conn.execute(
                "SELECT variant_label FROM enrollments WHERE id=?", (first["id"],)
            ).fetchone()["variant_label"]
        check("the one already sent to keeps no label", sent_label is None,
              str(sent_label))

        print("\n8. THE STEP'S OWN COPY MIRRORS THE FIRST ARM")
        # It is the fallback when a label cannot be resolved, so it must not be
        # left holding an earlier draft.
        cid7 = db.create_campaign("Mirror test")
        db.upsert_step(cid7, 1, "stale subject", "stale body", 0)
        s7 = db.get_steps(cid7)[0]
        db.save_step_variants(s7["id"], [
            {"label": "A", "subject": "fresh A", "body_html": "fresh A body", "weight": 50},
            {"label": "B", "subject": "fresh B", "body_html": "fresh B body", "weight": 50},
        ])
        s7b = db.get_steps(cid7)[0]
        check("the step's subject follows variant A", s7b["subject"] == "fresh A",
              s7b["subject"])
        check("and so does its body", s7b["body_html"] == "fresh A body")

        print("\n9. CLEARING VARIANTS LEAVES THE STEP WITH ITS COPY")
        db.save_step_variants(s7["id"], [])
        left = db.get_step_variants(s7["id"])
        check("a step never ends up with zero arms", len(left) == 1, str(left))
        check("the remaining arm keeps the copy", left[0]["subject"] == "fresh A",
              left[0]["subject"])

        print("\n10. A LEGACY STEP GAINS VARIANT A ON STARTUP")
        # Steps created before copy moved into variants have none. The startup
        # migration is what gives them one; insert the row directly to bypass
        # upsert_step and reproduce that state exactly.
        cid8 = db.create_campaign("Legacy campaign")
        with db.get_db() as conn:
            conn.execute("""
                INSERT INTO steps(campaign_id, step_num, subject, body_html, delay_days)
                VALUES(?, 1, 'legacy subject', 'legacy body', 0)
            """, (cid8,))
            legacy_id = conn.execute(
                "SELECT id FROM steps WHERE campaign_id=? AND step_num=1", (cid8,)
            ).fetchone()["id"]
            conn.execute("DELETE FROM step_variants WHERE step_id=?", (legacy_id,))
        check("the legacy step starts with no variants",
              db.get_step_variants(legacy_id) == [])

        db.init_db()          # idempotent; this is what runs at app startup

        promoted = db.get_step_variants(legacy_id)
        check("startup gives it exactly one arm", len(promoted) == 1, str(promoted))
        check("labelled A", promoted and promoted[0]["label"] == "A")
        check("holding the copy it already had",
              promoted and promoted[0]["subject"] == "legacy subject"
              and promoted[0]["body_html"] == "legacy body")
        check("and running startup again changes nothing",
              (db.init_db() or len(db.get_step_variants(legacy_id))) == 1)

        print("\n11. A STEP THAT ALREADY HAS A/B IS LEFT ALONE BY STARTUP")
        before = db.get_step_variants(s2["id"])
        db.init_db()
        after = db.get_step_variants(s2["id"])
        check("its arms are untouched", len(after) == len(before) == 2, str(after))
        check("and their copy is unchanged",
              [v["subject"] for v in after] == [v["subject"] for v in before])

        print("\n12. VARIANTS SURVIVE A SAVE / REOPEN / SAVE ROUND TRIP")
        # The bug: the step editor loads /steps, which returned bare step rows
        # with no variants attached -- only the campaign detail route attached
        # them. So variant B saved fine and then simply was not shown on
        # reopen, and saving again wrote back the single arm the editor could
        # see, deleting B from the database.
        import importlib as _il
        import app as app_mod
        _il.reload(app_mod)
        app_mod.app.config["TESTING"] = True
        client = app_mod.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["is_admin"] = True
            sess["csrf_token"] = "t"
        hdr = {"X-CSRF-Token": "t"}

        cid9 = db.create_campaign("Round trip")
        client.post(f"/api/campaigns/{cid9}/steps", json={
            "step_num": 1, "subject": "A subj", "body_html": "A body", "delay_days": 0,
            "variants": [
                {"label": "A", "subject": "A subj", "body_html": "A body", "weight": 50},
                {"label": "B", "subject": "B subj", "body_html": "B body", "weight": 50},
            ],
        }, headers=hdr)

        loaded = client.get(f"/api/campaigns/{cid9}/steps").get_json()
        check("the steps route returns the variants", "variants" in (loaded[0] or {}),
              str(list((loaded[0] or {}).keys())))
        got = {v["label"]: v for v in loaded[0].get("variants", [])}
        check("both arms come back", set(got) == {"A", "B"}, str(sorted(got)))
        check("variant B keeps its own copy",
              got.get("B", {}).get("subject") == "B subj", str(got.get("B")))

        # Re-save exactly what a correctly-populated editor would send back.
        client.post(f"/api/campaigns/{cid9}/steps", json={
            "step_num": 1, "subject": "A subj", "body_html": "A body", "delay_days": 0,
            "variants": [
                {"label": v["label"], "subject": v["subject"],
                 "body_html": v["body_html"], "weight": v["weight"]}
                for v in loaded[0]["variants"]
            ],
        }, headers=hdr)
        again = client.get(f"/api/campaigns/{cid9}/steps").get_json()
        check("both arms survive the second save",
              {v["label"] for v in again[0]["variants"]} == {"A", "B"},
              str(again[0]["variants"]))

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
