"""Duplicate-handling tests.

Run:  python tests/test_duplicates.py

Covers the five ways the same business could be contacted twice, and -- most
importantly -- proves that suppressing a duplicate never cancels the follow-ups
of a contact already mid-sequence.
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


def contact_by_email(db, email):
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
        return dict(row) if row else None


def test_domain_canonicalisation(db):
    print("\n1. DOMAIN CANONICALISATION")
    cases = [
        ("http://x.ca", "x.ca"),
        ("https://www.x.ca/", "x.ca"),
        ("http://x.ca/?utm_source=gmb", "x.ca"),
        ("https://www.x.ca/contact-us#form", "x.ca"),
        ("http://X.CA:8080/page", "x.ca"),
        ("x.ca", "x.ca"),
        ("", ""),
    ]
    ok = all(db.canonical_domain(url) == want for url, want in cases)
    check("all URL shapes reduce to one host", ok,
          str([(u, db.canonical_domain(u)) for u, w in cases if db.canonical_domain(u) != w]))


def test_prospect_dedupe(db):
    print("\n2. PROSPECTS DEDUPE BY DOMAIN, NOT URL STRING")
    db.upsert_contacts([
        {"email": "", "company": "Village Dental", "website": "http://village.ca",
         "status": "no_email"},
        {"email": "", "company": "Village Dental", "website": "https://www.village.ca/",
         "status": "no_email"},
        {"email": "", "company": "Village Dental",
         "website": "http://village.ca/?utm_source=gmb", "status": "form_only"},
    ])
    with db.get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE domain='village.ca' "
            "AND (email IS NULL OR email='')"
        ).fetchone()[0]
    check("three URL variants make one prospect row", n == 1, f"rows={n}")


def test_multi_address_ranking(db):
    print("\n3. MULTIPLE ADDRESSES AT ONE BUSINESS")
    db.upsert_contacts([
        {"email": "info@clinic.ca",     "company": "Clinic", "website": "http://clinic.ca"},
        {"email": "payments@clinic.ca", "company": "Clinic", "website": "http://clinic.ca"},
        {"email": "drsmith@clinic.ca",  "company": "Clinic", "website": "http://clinic.ca"},
    ])
    winner = contact_by_email(db, "drsmith@clinic.ca")
    info   = contact_by_email(db, "info@clinic.ca")
    billing = contact_by_email(db, "payments@clinic.ca")

    check("all three addresses are kept", all([winner, info, billing]))
    check("the personal address wins", winner["duplicate_of"] is None,
          f"duplicate_of={winner['duplicate_of']}")
    check("the role address is suppressed", info["duplicate_of"] == winner["id"])
    check("the billing address is suppressed", billing["duplicate_of"] == winner["id"])
    check("ranking order is personal < role < billing",
          db.email_rank("drsmith@x.ca") < db.email_rank("info@x.ca") < db.email_rank("payments@x.ca"))
    return winner, info


def test_company_not_overwritten(db):
    print("\n4. SHARED ADDRESS DOES NOT RENAME THE BUSINESS")
    db.upsert_contacts([{"email": "payments@ganderdental.com", "company": "Kenmount Court",
                         "website": "http://kenmount.ca"}])
    db.upsert_contacts([{"email": "payments@ganderdental.com", "company": "Parkdale Family",
                         "website": "http://parkdale.ca"}])
    row = contact_by_email(db, "payments@ganderdental.com")
    check("company keeps the first business seen", row["company"] == "Kenmount Court",
          f"company={row['company']}")
    import json
    extra = json.loads(row["extra"] or "{}")
    check("the other business is recorded, not lost",
          "Parkdale Family" in (extra.get("also_seen_at") or []), str(extra))


def test_cross_campaign(db):
    print("\n5. ONE CONTACT, TWO CAMPAIGNS")
    a = db.create_campaign("Campaign A")
    b = db.create_campaign("Campaign B")
    db.upsert_contacts([{"email": "solo@alpha.ca", "company": "Alpha", "website": "http://alpha.ca"}])
    cid = contact_by_email(db, "solo@alpha.ca")["id"]

    enrolled, _ = db.enroll_contacts_bulk(a, [cid])
    check("enrolls into the first campaign", enrolled == 1)

    enrolled, skipped = db.enroll_contacts_bulk(b, [cid])
    check("refused by the second campaign", enrolled == 0, f"enrolled={enrolled}")
    check("and the reason is reported", skipped.get("other_campaign") == 1, str(skipped))
    return a


def test_followups_survive_dedupe(db, campaign_id):
    """The regression that would hurt most: silently killing steps 2 and 3."""
    print("\n6. FOLLOW-UPS SURVIVE A LATER DEDUPE")
    db.upsert_contacts([{"email": "info@bravo.ca", "company": "Bravo", "website": "http://bravo.ca"}])
    enrolled_id = contact_by_email(db, "info@bravo.ca")["id"]
    db.enroll_contacts_bulk(campaign_id, [enrolled_id])

    # Simulate step 1 having gone out and step 2 being scheduled.
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM enrollments WHERE contact_id=?", (enrolled_id,)
        ).fetchone()
        db.advance_enrollment(row["id"], 2, "2000-01-01 00:00:00")

    # Now a re-scrape finds a better-ranked address at the same business.
    db.upsert_contacts([{"email": "drjones@bravo.ca", "company": "Bravo",
                         "website": "http://bravo.ca"}])

    still = contact_by_email(db, "info@bravo.ca")
    newer = contact_by_email(db, "drjones@bravo.ca")
    check("the mid-sequence contact is NOT suppressed",
          still["duplicate_of"] is None,
          "suppressing it would cancel steps 2 and 3 with no error")
    check("the newly-found address is suppressed instead",
          newer["duplicate_of"] == still["id"], f"duplicate_of={newer['duplicate_of']}")

    due = db.get_due_enrollments(campaign_id, limit=50)
    check("the contact is still returned as due for its follow-up",
          any(d["contact_id"] == enrolled_id for d in due),
          f"due contact_ids={[d['contact_id'] for d in due]}")


def test_suppressed_not_enrollable(db):
    print("\n7. SUPPRESSED ADDRESSES CANNOT BE ENROLLED")
    c = db.create_campaign("Campaign C")
    suppressed = contact_by_email(db, "payments@clinic.ca")
    enrolled, skipped = db.enroll_contacts_bulk(c, [suppressed["id"]])
    check("a duplicate address is refused", enrolled == 0, f"enrolled={enrolled}")
    check("and the reason is reported", skipped.get("duplicate_address") == 1, str(skipped))


def main():
    work = tempfile.mkdtemp(prefix="dupes_")
    try:
        db = fresh(work, "dupes.db")
        test_domain_canonicalisation(db)
        test_prospect_dedupe(db)
        test_multi_address_ranking(db)
        test_company_not_overwritten(db)
        campaign_a = test_cross_campaign(db)
        test_followups_survive_dedupe(db, campaign_a)
        test_suppressed_not_enrollable(db)
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
