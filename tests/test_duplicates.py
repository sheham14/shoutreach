"""Duplicate-handling tests.

Run:  python tests/test_duplicates.py

Covers the five ways the same business could be contacted twice, and -- most
importantly -- proves that suppressing a duplicate never cancels the follow-ups
of an address already mid-sequence. Identity is a business now, not a contact
row, so most of this file is really testing find_or_create_business.
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


def lead_by_email(db, email):
    """An email_lead row, joined to its business (company, domain, etc.)."""
    return db.get_email_lead_by_email(email)


def business_extra(db, business_id):
    import json
    with db.get_db() as conn:
        row = conn.execute("SELECT extra FROM businesses WHERE id=?", (business_id,)).fetchone()
    return json.loads(row["extra"] or "{}") if row else {}


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
    print("\n2. NO-EMAIL PROSPECTS DEDUPE BY DOMAIN, NOT URL STRING")
    db.upsert_businesses([
        {"email": "", "company": "Village Dental", "website": "http://village.ca",
         "status": "no_email"},
        {"email": "", "company": "Village Dental", "website": "https://www.village.ca/",
         "status": "no_email"},
        {"email": "", "company": "Village Dental",
         "website": "http://village.ca/?utm_source=gmb", "status": "form_only"},
    ])
    with db.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM businesses WHERE domain='village.ca'").fetchone()[0]
    check("three URL variants make one business row", n == 1, f"rows={n}")


def test_multi_address_ranking(db):
    print("\n3. MULTIPLE ADDRESSES AT ONE BUSINESS")
    db.upsert_businesses([
        {"email": "info@clinic.ca",     "company": "Clinic", "website": "http://clinic.ca"},
        {"email": "payments@clinic.ca", "company": "Clinic", "website": "http://clinic.ca"},
        {"email": "drsmith@clinic.ca",  "company": "Clinic", "website": "http://clinic.ca"},
    ])
    winner  = lead_by_email(db, "drsmith@clinic.ca")
    info    = lead_by_email(db, "info@clinic.ca")
    billing = lead_by_email(db, "payments@clinic.ca")

    check("all three addresses are kept", all([winner, info, billing]))
    check("all three share one business",
          len({winner["business_id"], info["business_id"], billing["business_id"]}) == 1)
    check("the personal address wins", winner["duplicate_of"] is None,
          f"duplicate_of={winner['duplicate_of']}")
    check("the role address is suppressed", info["duplicate_of"] == winner["id"])
    check("the billing address is suppressed", billing["duplicate_of"] == winner["id"])
    check("ranking order is personal < role < billing",
          db.email_rank("drsmith@x.ca") < db.email_rank("info@x.ca") < db.email_rank("payments@x.ca"))
    return winner, info


def test_company_not_overwritten(db):
    print("\n4. SHARED ADDRESS DOES NOT RENAME THE BUSINESS")
    db.upsert_businesses([{"email": "payments@ganderdental.com", "company": "Kenmount Court",
                           "website": "http://kenmount.ca"}])
    db.upsert_businesses([{"email": "payments@ganderdental.com", "company": "Parkdale Family",
                           "website": "http://parkdale.ca"}])
    row = lead_by_email(db, "payments@ganderdental.com")
    check("company keeps the first business seen", row["company"] == "Kenmount Court",
          f"company={row['company']}")
    extra = business_extra(db, row["business_id"])
    check("the other business is recorded, not lost",
          "Parkdale Family" in (extra.get("also_seen_as") or []), str(extra))


def test_cross_campaign(db):
    print("\n5. ONE ADDRESS, TWO CAMPAIGNS")
    a = db.create_campaign("Campaign A")
    b = db.create_campaign("Campaign B")
    db.upsert_businesses([{"email": "solo@alpha.ca", "company": "Alpha", "website": "http://alpha.ca"}])
    lead_id = lead_by_email(db, "solo@alpha.ca")["id"]

    enrolled, _ = db.enroll_contacts_bulk(a, [lead_id])
    check("enrolls into the first campaign", enrolled == 1)

    enrolled, skipped = db.enroll_contacts_bulk(b, [lead_id])
    check("refused by the second campaign", enrolled == 0, f"enrolled={enrolled}")
    check("and the reason is reported", skipped.get("other_campaign") == 1, str(skipped))
    return a


def test_followups_survive_dedupe(db, campaign_id):
    """The regression that would hurt most: silently killing steps 2 and 3."""
    print("\n6. FOLLOW-UPS SURVIVE A LATER DEDUPE")
    db.upsert_businesses([{"email": "info@bravo.ca", "company": "Bravo", "website": "http://bravo.ca"}])
    enrolled_id = lead_by_email(db, "info@bravo.ca")["id"]
    db.enroll_contacts_bulk(campaign_id, [enrolled_id])

    # Simulate step 1 having gone out and step 2 being scheduled.
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM enrollments WHERE email_lead_id=?", (enrolled_id,)
        ).fetchone()
        db.advance_enrollment(row["id"], 2, "2000-01-01 00:00:00")

    # Now a re-scrape finds a better-ranked address at the same business.
    db.upsert_businesses([{"email": "drjones@bravo.ca", "company": "Bravo",
                           "website": "http://bravo.ca"}])

    still = lead_by_email(db, "info@bravo.ca")
    newer = lead_by_email(db, "drjones@bravo.ca")
    check("the mid-sequence address is NOT suppressed",
          still["duplicate_of"] is None,
          "suppressing it would cancel steps 2 and 3 with no error")
    check("the newly-found address is suppressed instead",
          newer["duplicate_of"] == still["id"], f"duplicate_of={newer['duplicate_of']}")

    due = db.get_due_enrollments(campaign_id, limit=50)
    check("the address is still returned as due for its follow-up",
          any(d["email_lead_id"] == enrolled_id for d in due),
          f"due email_lead_ids={[d['email_lead_id'] for d in due]}")


def test_suppressed_not_enrollable(db):
    print("\n7. SUPPRESSED ADDRESSES CANNOT BE ENROLLED")
    c = db.create_campaign("Campaign C")
    suppressed = lead_by_email(db, "payments@clinic.ca")
    enrolled, skipped = db.enroll_contacts_bulk(c, [suppressed["id"]])
    check("a duplicate address is refused", enrolled == 0, f"enrolled={enrolled}")
    check("and the reason is reported", skipped.get("duplicate_address") == 1, str(skipped))


def test_freemail_is_not_a_business(db):
    """
    The bug this pins: an address with no website fell back to its own email
    domain as its identity, so every gmail.com lead was arbitrated against
    every other one and all but the first were suppressed as duplicates.

    Worst where it matters most -- a business with no website is the strongest
    lead for a web-design offer, and it is also the one most likely to publish
    a Gmail address and have no domain of its own.
    """
    print("\n8. FREEMAIL IS A MAILBOX PROVIDER, NOT A BUSINESS")

    db.upsert_businesses([
        {"email": "friend.one@gmail.com",   "first_name": "One"},
        {"email": "friend.two@gmail.com",   "first_name": "Two"},
        {"email": "friend.three@gmail.com", "first_name": "Three"},
        {"email": "mate@outlook.com",       "first_name": "Four"},
        {"email": "other@yahoo.co.uk",      "first_name": "Five"},
    ])

    freemail = ["friend.one@gmail.com", "friend.two@gmail.com",
                "friend.three@gmail.com", "mate@outlook.com",
                "other@yahoo.co.uk"]
    leads = [lead_by_email(db, e) for e in freemail]
    check("every freemail lead is kept", all(leads), str(freemail))
    check("none is suppressed as a duplicate",
          all(r["duplicate_of"] is None for r in leads),
          str([(r["email"], r["duplicate_of"]) for r in leads]))
    check("each is its own business, not one shared gmail.com identity",
          len({r["business_id"] for r in leads}) == len(leads),
          str([(r["email"], r["business_id"]) for r in leads]))
    check("and none claims a freemail domain as its identity",
          all(r["domain"] == "" for r in leads),
          str([(r["email"], r["domain"]) for r in leads]))

    # All of them must actually be enrollable -- suppression is invisible until
    # you try to send, which is exactly how this stayed hidden.
    c = db.create_campaign("Freemail campaign")
    enrolled, skipped = db.enroll_contacts_bulk(c, [r["id"] for r in leads])
    check("all five enroll", enrolled == 5, f"enrolled={enrolled} skipped={skipped}")

    # A real business domain must still dedupe exactly as before.
    db.upsert_businesses([
        {"email": "info@realclinic.ca",    "company": "Real", "website": "https://realclinic.ca"},
        {"email": "billing@realclinic.ca", "company": "Real", "website": "https://realclinic.ca"},
    ])
    kept = lead_by_email(db, "info@realclinic.ca")
    dropped = lead_by_email(db, "billing@realclinic.ca")
    check("a real business still keeps one address", kept["duplicate_of"] is None)
    check("and suppresses the weaker one", dropped["duplicate_of"] == kept["id"],
          f"duplicate_of={dropped['duplicate_of']}")

    check("is_freemail knows the common providers",
          all(db.is_freemail(d) for d in
              ("gmail.com", "GMAIL.COM", "outlook.com", "yahoo.ca", "icloud.com")))
    check("and does not flag a business domain",
          not any(db.is_freemail(d) for d in ("realclinic.ca", "hexiv.co", "")))


def test_pasted_pair_still_dedupes_by_email_domain(db):
    """
    A business pasted in as "name, email" with no separate website field must
    still dedupe on the email's own domain -- this is the fallback
    find_or_create_business needs and the freemail test above must not
    accidentally take away.
    """
    print("\n9. NO EXPLICIT WEBSITE STILL DEDUPES VIA THE EMAIL'S DOMAIN")
    db.upsert_businesses([
        {"email": "owner@pastedclinic.ca", "company": "Pasted Clinic"},
        {"email": "info@pastedclinic.ca",  "company": "Pasted Clinic"},
    ])
    a = lead_by_email(db, "owner@pastedclinic.ca")
    b = lead_by_email(db, "info@pastedclinic.ca")
    check("both addresses resolved to one business",
          a and b and a["business_id"] == b["business_id"],
          f"a={a and a['business_id']} b={b and b['business_id']}")
    check("the business picked up the email's domain",
          a and a["domain"] == "pastedclinic.ca", f"domain={a and a['domain']}")


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
        test_freemail_is_not_a_business(db)
        test_pasted_pair_still_dedupes_by_email_domain(db)
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
