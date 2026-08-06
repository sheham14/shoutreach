"""Server-side contact paging, filtering and the lead-list ("cabinet") view.

Run:  python tests/test_contacts_paging.py

The Contacts tab used to fetch every contact (capped at 500) and filter in the
browser. Past that cap it silently showed only the newest 500, and a per-scrape
filter applied to that truncated window under-reported the list with no
warning. These tests pin the replacement: filtering happens in SQL, over the
whole table, and select-all can reach every matching row rather than one page.
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
    """Fresh DB + app, logged in as admin."""
    os.environ["DB_PATH"] = os.path.join(work, "contacts_test.db")
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "test-password-123"

    import db
    importlib.reload(db)
    import app as app_mod
    importlib.reload(app_mod)

    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["is_admin"] = True
        sess["csrf_token"] = "test-csrf"
    return db, app_mod, client


def main():
    work = tempfile.mkdtemp(prefix="shoutreach-contacts-")
    try:
        db, app_mod, client = boot(work)

        # Two scrapes, so the lead lists have something to separate.
        job_a = db.create_scrape_job("dentists", "Toronto Canada", 50, True)
        job_b = db.create_scrape_job("plumbers", "Calgary Canada", 50, True)

        print("\n1. SOURCE TAGGING")
        db.upsert_contacts([
            {"email": f"a{i}@clinic{i}.ca", "company": f"Clinic {i}",
             "website": f"https://clinic{i}.ca", "phone": f"555-000{i}",
             "rating": 4.5, "review_count": 10 + i, "category": "Dentist",
             "source_job_id": job_a}
            for i in range(60)
        ])
        db.upsert_contacts([
            {"email": f"b{i}@pipes{i}.ca", "company": f"Pipes {i}",
             "website": f"https://pipes{i}.ca", "source_job_id": job_b}
            for i in range(20)
        ])
        # Hand-added: no source job at all.
        db.upsert_contacts([{"email": "manual@byhand.ca", "company": "By Hand"}])

        page = db.get_contacts_page(page=1, per_page=50)
        check("all contacts counted", page["total"] == 81, f"total={page['total']}")
        check("page size respected", len(page["rows"]) == 50, f"rows={len(page['rows'])}")
        check("page count is right", page["pages"] == 2, f"pages={page['pages']}")

        first = db.get_contacts_page(page=1, per_page=50, source_job_id=job_a)
        check("filter by scrape narrows the set", first["total"] == 60, f"total={first['total']}")
        check("every row belongs to that scrape",
              all(r["source_job_id"] == job_a for r in first["rows"]))

        manual = db.get_contacts_page(page=1, per_page=50, source_job_id=db.SOURCE_MANUAL)
        check("manual bucket holds only untagged rows", manual["total"] == 1,
              f"total={manual['total']}")
        check("and it is the hand-added one",
              manual["rows"][0]["email"] == "manual@byhand.ca")

        print("\n2. QUALIFYING FIELDS ARE REAL COLUMNS, NOT `extra`")
        row = first["rows"][0]
        check("phone stored in its own column", bool(row["phone"]), f"phone={row['phone']}")
        check("rating stored as a number", isinstance(row["rating"], (int, float)),
              f"rating={row['rating']}")
        check("review_count stored", row["review_count"] is not None,
              f"review_count={row['review_count']}")
        check("category stored", row["category"] == "Dentist")
        check("extra left empty", row["extra"] in ("{}", ""), f"extra={row['extra']}")

        print("\n3. PAGING DOES NOT LOSE OR REPEAT ROWS")
        p1 = db.get_contacts_page(page=1, per_page=30)
        p2 = db.get_contacts_page(page=2, per_page=30)
        p3 = db.get_contacts_page(page=3, per_page=30)
        ids = [r["id"] for r in p1["rows"] + p2["rows"] + p3["rows"]]
        check("three pages cover everything", len(ids) == 81, f"got {len(ids)}")
        check("no row appears twice", len(set(ids)) == 81, f"unique={len(set(ids))}")

        print("\n4. SEARCH RUNS IN SQL, ACROSS THE WHOLE TABLE")
        # Clinic 57 sits well past the old 500-row/first-page window.
        hit = db.get_contacts_page(page=1, per_page=10, q="clinic57")
        check("finds a row that is not on page 1", hit["total"] == 1, f"total={hit['total']}")
        check("and returns it", hit["rows"] and hit["rows"][0]["company"] == "Clinic 57")

        by_phone = db.get_contacts_page(page=1, per_page=10, q="555-0003")
        check("search covers phone too", by_phone["total"] >= 1, f"total={by_phone['total']}")

        combined = db.get_contacts_page(page=1, per_page=10, q="pipes", source_job_id=job_a)
        check("search and list filter combine (AND, not OR)", combined["total"] == 0,
              f"total={combined['total']}")

        print("\n5. SORTING IS WHITELISTED")
        asc = db.get_contacts_page(page=1, per_page=5, sort_col="company", sort_dir="asc")
        desc = db.get_contacts_page(page=1, per_page=5, sort_col="company", sort_dir="desc")
        check("ascending sort applied", asc["sort_col"] == "company" and asc["sort_dir"] == "asc")
        check("ascending and descending differ",
              [r["id"] for r in asc["rows"]] != [r["id"] for r in desc["rows"]])
        bad = db.get_contacts_page(page=1, per_page=5, sort_col="company; DROP TABLE contacts")
        check("an unknown sort column is ignored, not interpolated", bad["sort_col"] == "")
        with db.get_db() as conn:
            still_there = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        check("contacts table survived the injection attempt", still_there == 81)

        print("\n6. LEAD LISTS")
        sources = db.get_contact_sources()
        by_id = {str(s["job_id"]): s for s in sources}
        check("one entry per scrape plus manual", len(sources) == 3, f"got {len(sources)}")
        check("scrape A counted", by_id[str(job_a)]["count"] == 60)
        check("scrape B counted", by_id[str(job_b)]["count"] == 20)
        check("manual bucket present", by_id[db.SOURCE_MANUAL]["count"] == 1)
        check("label carries niche and city",
              "dentists" in by_id[str(job_a)]["label"] and "Toronto" in by_id[str(job_a)]["label"],
              by_id[str(job_a)]["label"])

        print("\n7. SELECT-ALL REACHES EVERY MATCHING ROW")
        ids_a = db.get_contact_ids_matching(source_job_id=job_a)
        check("ids span the whole filter, not one page", len(ids_a) == 60, f"got {len(ids_a)}")
        ids_q = db.get_contact_ids_matching(q="pipes")
        check("ids respect the search too", len(ids_q) == 20, f"got {len(ids_q)}")

        print("\n8. DELETED ROWS ARE HIDDEN UNLESS ASKED FOR")
        target = ids_q[0]
        db.delete_contact(target)
        check("soft-deleted row drops out of the default view",
              db.get_contacts_page(page=1, per_page=100, q="pipes")["total"] == 19)
        check("and comes back with include_deleted",
              db.get_contacts_page(page=1, per_page=100, q="pipes",
                                   include_deleted=True)["total"] == 20)

        print("\n9. THE HTTP ROUTES AGREE WITH THE DB LAYER")
        r = client.get("/api/contacts?per_page=10")
        body = r.get_json()
        check("GET /api/contacts returns a page object", r.status_code == 200 and "rows" in body)
        check("with the paging metadata the UI needs",
              all(k in body for k in ("total", "page", "pages", "per_page")))
        check("page size honoured over the wire", len(body["rows"]) == 10)

        r = client.get(f"/api/contacts?per_page=5&source_job_id={job_a}")
        check("list filter works over the wire", r.get_json()["total"] == 60)

        r = client.get("/api/contacts/sources")
        check("sources route returns the lists", len(r.get_json()) == 3)

        r = client.get(f"/api/contacts/ids?source_job_id={job_a}")
        check("ids route returns every matching id", r.get_json()["total"] == 60)

        print("\n10. BULK DELETE OVER A FILTERED SELECTION")
        # One job_b lead was soft-deleted in step 8, so select-all sees 19 of
        # 20 -- it must not reach rows the filter is hiding, or "select all
        # matching" would quietly delete more than the screen ever showed.
        ids = client.get(f"/api/contacts/ids?source_job_id={job_b}").get_json()["ids"]
        check("select-all skips rows hidden by the filter", len(ids) == 19, f"got {len(ids)}")
        r = client.post("/api/contacts/bulk-delete", json={"ids": ids},
                        headers={"X-CSRF-Token": "test-csrf"})
        check("bulk delete accepts the expanded selection", r.status_code == 200,
              f"got {r.status_code}")
        check("every selected lead is gone",
              db.get_contacts_page(page=1, per_page=100, source_job_id=job_b)["total"] == 0)
        check("the hidden soft-deleted row was left alone, not swept up",
              db.get_contacts_page(page=1, per_page=100, source_job_id=job_b,
                                   include_deleted=True)["total"] == 1)
        check("the other list is untouched",
              db.get_contacts_page(page=1, per_page=100, source_job_id=job_a)["total"] == 60)

        print("\n11. UNAUTHENTICATED ACCESS IS STILL REFUSED")
        anon = app_mod.app.test_client()
        for path in ("/api/contacts", "/api/contacts/sources", "/api/contacts/ids"):
            check(f"{path} requires a session", anon.get(path).status_code == 401)

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
