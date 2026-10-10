"""Duplicate matching across phone formats, named imports and the "From"
filter, "Download CSV only" scrapes, CSV downloads of selected leads, and
Recently deleted.

Run:  python tests/test_lead_batches.py

Prompted by a real double import: a WhatsApp scrape stored "050 123 4567",
the same leads came back from a chat as "+971 50 123 4567", and the
phone match (the last ten digits) didn't see them as the same number.
"""
import csv
import importlib
import io
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


def read_csv(resp):
    text = resp.get_data(as_text=True).lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text)))


def main():
    work = tempfile.mkdtemp(prefix="lead-batches-")
    try:
        os.environ["DB_PATH"] = os.path.join(work, "batches.db")
        os.environ["SECRET_KEY"] = "test-secret"
        import db
        importlib.reload(db)
        db.init_db()
        me = db.create_user("admin", "test-password-123", is_admin=True)
        other = db.create_user("other", "test-password-123", is_admin=False)
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
        client, theirs = client_for(me, True), client_for(other, False)
        worker = app_mod.app.test_client()
        hdr = {"X-CSRF-Token": "t"}
        wh = {"X-API-Key": db.get_or_create_worker_key(me)}
        camp = db.create_wa_campaign("Dubai clinics", owner_id=me)
        camp = camp[0] if isinstance(camp, tuple) else camp

        def count_businesses(owner):
            with db.get_db() as conn:
                return conn.execute("SELECT COUNT(*) FROM businesses WHERE owner_id=?",
                                    (owner,)).fetchone()[0]

        def sources(c=client):
            return c.get("/api/contacts/sources").get_json()

        print("\n1. ONE NUMBER, TWO FORMATS")
        n = db.normalize_phone
        check("UAE mobile: local and international agree",
              n("050 123 4567", "AE") == n("+971 50 123 4567") == n("050 123 4567") == "501234567")
        check("UAE landline", n("04 123 4567", "AE") == n("+971 4 123 4567"))
        check("Qatar (no trunk 0)", n("4412 3456", "QA") == n("+974 4412 3456"))
        check("international without a +", n("971501234567") == n("00971 50 123 4567") == "501234567")
        check("North American keys unchanged",
              n("(709) 555-0123") == n("+1 709-555-0123") == n("17095550123") == "7095550123")
        check("too short is unusable", n("123") == "")

        print("\n2. A WHATSAPP SCRAPE, THEN THE SAME LEADS AS JSON")
        job = db.create_scrape_job("dentists", "Dubai", owner_id=me, destination="whatsapp",
                                   country="AE", campaign_id=camp)
        db.claim_scrape_job(me)
        scraped = [{"company": f"Clinic {i}", "phone": f"050 123 45{i:02d}", "address": "Dubai",
                    "source_job_id": job} for i in range(5)]
        r = worker.post("/api/contacts/import", headers=wh, json={"rows": scraped})
        check("the scrape imports", r.status_code == 200 and r.get_json()["inserted"] == 5, str(r.get_json()))
        before = count_businesses(me)
        as_json = [{"company": f"Clinic {i}", "phone": f"+971 50 123 45{i:02d}",
                    "message": f"Hi Clinic {i}!"} for i in range(5)]
        as_json.append({"company": "Brand New", "phone": "+971 55 000 0001", "message": "Hi!"})
        r = client.post("/api/wa/import", headers=hdr,
                        json={"rows": as_json, "country": "AE", "wa_campaign_id": camp,
                              "source": "JSON"})
        body = r.get_json()
        check("the JSON imports", r.status_code == 200 and body.get("batch_id"), str(body))
        check("only the one genuinely new lead was created", count_businesses(me) == before + 1,
              f"{before} -> {count_businesses(me)}")
        with db.get_db() as conn:
            msg = conn.execute("""SELECT w.draft_message FROM wa_leads w JOIN businesses b ON b.id=w.business_id
                                   WHERE b.name='Clinic 3' AND b.owner_id=?""", (me,)).fetchone()[0]
        check("the JSON's message landed on the scraped lead", msg == "Hi Clinic 3!", msg)

        print("\n3. EACH IMPORT IS ITS OWN ENTRY")
        src = {s["kind"] + ":" + str(s.get("scrape_job_id") or s["base"]): s for s in sources()}
        scrape = src.get(f"scrape:{job}")
        imp = next((s for s in sources() if s["kind"] == "import"), None)
        check("the scrape is listed with its 5 leads, all new",
              scrape and scrape["count"] == 5 and scrape["new_count"] == 5, str(scrape))
        check("the JSON import is listed with all 6 it brought, 1 new",
              imp and imp["count"] == 6 and imp["new_count"] == 1 and imp["base"] == "JSON import", str(imp))
        check("a lead in both shows under both",
              db.get_wa_leads_page(source=scrape["id"], owner_id=me)["total"] == 5
              and db.get_wa_leads_page(source=imp["id"], owner_id=me)["total"] == 6)
        check("Contacts filters by it",
              client.get(f"/api/businesses?source_job_id={imp['id']}").get_json()["total"] == 6)
        ids = client.get(f"/api/wa/leads/ids?source_job_id={scrape['id']}").get_json()["ids"]
        check("WhatsApp can select every lead in one import", len(ids) == 5, str(ids))
        check("the manual bucket is empty", not any(s["kind"] == "manual" for s in sources()))

        r = client.put(f"/api/lead-batches/{imp['id']}", headers=hdr, json={"name": "Dubai JSON v1"})
        check("an import can be renamed", r.status_code == 200
              and next(s for s in sources() if s["id"] == imp["id"])["name"] == "Dubai JSON v1")
        r = theirs.put(f"/api/lead-batches/{imp['id']}", headers=hdr, json={"name": "mine now"})
        check("...but not by the other operator", r.status_code == 404)
        check("the other operator sees none of these", sources(theirs) == [])
        r = theirs.post("/api/lists/add-to", headers=hdr, json={"source_job_id": imp["id"], "channel": "calling"})
        check("...and can't add them to a channel", r.status_code == 404)

        print("\n4. A CONFIRMED RE-SUBMISSION JOINS ITS IMPORT")
        db.add_to_calling([db.find_existing_business(db.get_db(), phone="+971 55 000 0001", owner_id=me)["id"]],
                          owner_id=me)
        rows = [{"company": "Brand New", "phone": "+971 55 000 0001"},
                {"company": "Second New", "phone": "+971 55 000 0002"}]
        r = client.post("/api/wa/import", headers=hdr,
                        json={"rows": rows, "country": "AE", "wa_campaign_id": camp, "source": "Paste"})
        first = r.get_json()
        check("one row is held as already on Calling", len(first["conflicts"]) == 1, str(first))
        r = client.post("/api/wa/import", headers=hdr,
                        json={"rows": [c["row"] for c in first["conflicts"]], "country": "AE",
                              "wa_campaign_id": camp, "confirm_conflicts": True,
                              "batch_id": first["batch_id"], "source": "Paste"})
        check("the confirmed half joins the same import", r.get_json()["batch_id"] == first["batch_id"])
        check("...which then holds both",
              next(s for s in sources() if s["id"] == first["batch_id"])["count"] == 2)
        r = theirs.post("/api/wa/import", headers=hdr, json={
            "rows": [{"company": "Theirs", "phone": "+971 55 999 0000"}], "country": "AE",
            "wa_campaign_id": db.create_wa_campaign("x", owner_id=other)[0]
            if isinstance(db.create_wa_campaign("y", owner_id=other), tuple)
            else db.create_wa_campaign("x", owner_id=other),
            "batch_id": first["batch_id"]})
        check("another operator can't join it", r.get_json().get("batch_id") != first["batch_id"],
              str(r.get_json()))

        print("\n5. A 'DOWNLOAD CSV ONLY' SCRAPE")
        with db.get_db() as conn:   # one scrape at a time: finish the first
            conn.execute("UPDATE scrape_jobs SET status='done' WHERE id=?", (job,))
        r = client.post("/api/scraper/start", headers=hdr,
                        json={"niche": "gyms", "city": "Doha", "destination": "csv", "auto_import": False})
        csv_job = r.get_json()["job_id"]
        check("it starts", r.status_code == 200, str(r.get_json()))
        spec = worker.post("/api/scraper/claim", headers=wh).get_json()
        check("the worker is told to search for emails", spec["destination"] == "email"
              and spec["auto_import"] is True, str(spec))
        before = count_businesses(me)
        rows = [{"company": "Gym A", "phone": "4412 0001", "email": "a@gyma.qa", "website": "https://gyma.qa",
                 "source_job_id": csv_job},
                {"company": "Gym A", "phone": "4412 0001", "email": "boss@gyma.qa", "website": "https://gyma.qa",
                 "source_job_id": csv_job},
                {"company": "Gym B", "phone": "", "website": "", "address": "Pearl, Doha",
                 "status": "no_website", "source_job_id": csv_job}]
        r = worker.post("/api/contacts/import", headers=wh, json={"rows": rows})
        check("the worker's rows are accepted", r.status_code == 200 and r.get_json()["inserted"] == 2,
              str(r.get_json()))
        check("nothing was filed as a lead", count_businesses(me) == before)
        r = client.get(f"/api/scraper/jobs/{csv_job}/csv")
        got = read_csv(r)
        check("the CSV downloads, one row per business", r.status_code == 200 and len(got) == 2, str(got))
        a = next((g for g in got if g["company"] == "Gym A"), {})
        check("both emails are kept", a.get("email") == "a@gyma.qa" and a.get("other_emails") == "boss@gyma.qa",
              str(a))
        check("the city comes from the scrape", a.get("city") == "Doha")
        check("the other operator can't download it",
              theirs.get(f"/api/scraper/jobs/{csv_job}/csv").status_code == 404)
        held = client.get("/api/scraper/held").get_json()
        check("it's listed among held scrapes", held and held[0]["id"] == csv_job and held[0]["count"] == 2,
              str(held))

        print("\n6. DOWNLOAD SELECTED LEADS")
        wa_ids = client.get(f"/api/wa/leads/ids?source_job_id={scrape['id']}").get_json()["ids"]
        r = client.post("/api/leads/export", headers=hdr, json={"kind": "wa", "ids": wa_ids})
        got = read_csv(r)
        check("WhatsApp leads download with their messages", r.status_code == 200 and len(got) == 5
              and all(g["message"] for g in got) and "followup_1" in got[0], str(got[:1]))
        check("a + phone isn't mangled by the formula guard", got[0]["wa_number"][:1] != "'"
              and all(not g["phone"].startswith("'") for g in got), str([g["phone"] for g in got]))
        r = theirs.post("/api/leads/export", headers=hdr, json={"kind": "wa", "ids": wa_ids})
        check("another operator's ids come back empty", read_csv(r) == [])
        biz_ids = client.get(f"/api/businesses/ids?source_job_id={imp['id']}").get_json()["ids"]
        got = read_csv(client.post("/api/leads/export", headers=hdr, json={"kind": "business", "ids": biz_ids}))
        check("Contacts downloads too", len(got) == 6 and "channels" in got[0])
        db.upsert_businesses([{"company": "=HYPERLINK(\"x\")", "email": "f@formula.ca"}], owner_id=me)
        fid = db.find_existing_business(db.get_db(), email="f@formula.ca", owner_id=me)["id"]
        got = read_csv(client.post("/api/leads/export", headers=hdr, json={"kind": "business", "ids": [fid]}))
        check("a formula in a name is neutralised", got[0]["company"].startswith("'="), got[0]["company"])

        print("\n7. RECENTLY DELETED")
        target = db.find_existing_business(db.get_db(), phone="050 123 4503", owner_id=me, country="AE")["id"]
        with db.get_db() as conn:
            wa_before = dict(conn.execute("SELECT * FROM wa_leads WHERE business_id=?", (target,)).fetchone())
        r = client.post("/api/businesses/delete", headers=hdr, json={"business_ids": [target]})
        check("a delete goes through", r.get_json()["deleted"] == 1, str(r.get_json()))
        check("it's gone from the list",
              db.get_wa_leads_page(source=scrape["id"], owner_id=me)["total"] == 4)
        trash = client.get("/api/trash").get_json()
        check("it's in Recently deleted, with 30 days left",
              trash["groups"] and trash["groups"][0]["count"] == 1
              and trash["groups"][0]["days_left"] == 30
              and "whatsapp" in trash["groups"][0]["items"][0]["channels"], str(trash))
        check("the other operator doesn't see it", theirs.get("/api/trash").get_json()["groups"] == [])
        r = theirs.post("/api/trash/restore", headers=hdr, json={"group": trash["groups"][0]["group"]})
        check("...and can't restore it", r.get_json()["restored"] == 0)
        r = client.post("/api/trash/restore", headers=hdr, json={"group": trash["groups"][0]["group"]})
        check("it restores", r.get_json()["restored"] == 1, str(r.get_json()))
        with db.get_db() as conn:
            wa_after = conn.execute("SELECT * FROM wa_leads WHERE business_id=?", (target,)).fetchone()
        check("with its WhatsApp lead, message and campaign intact",
              wa_after and dict(wa_after) == wa_before, str(dict(wa_after) if wa_after else None))
        check("back in its imports", db.get_wa_leads_page(source=scrape["id"], owner_id=me)["total"] == 5
              and db.get_wa_leads_page(source=imp["id"], owner_id=me)["total"] == 6)
        check("and out of Recently deleted", client.get("/api/trash").get_json()["groups"] == [])

        # Deleted, then re-imported, then restored: restoring would duplicate it.
        client.post("/api/businesses/delete", headers=hdr, json={"business_ids": [target]})
        client.post("/api/wa/import", headers=hdr, json={
            "rows": [{"company": "Clinic 3", "phone": "+971 50 123 4503"}], "country": "AE",
            "wa_campaign_id": camp})
        group = client.get("/api/trash").get_json()["groups"][0]["group"]
        r = client.post("/api/trash/restore", headers=hdr, json={"group": group})
        check("a lead that's back already isn't restored twice",
              r.get_json() == {"ok": True, "restored": 0, "exists": 1}, str(r.get_json()))
        r = client.post("/api/trash/forget", headers=hdr, json={"group": group})
        check("delete forever empties it", r.get_json()["deleted"] == 1
              and client.get("/api/trash").get_json()["groups"] == [])

        # The 30 days.
        b2 = db.find_existing_business(db.get_db(), phone="+971 50 123 4504", owner_id=me)["id"]
        client.post("/api/businesses/delete", headers=hdr, json={"business_ids": [b2]})
        with db.get_db() as conn:
            conn.execute("UPDATE deleted_leads SET deleted_at = datetime('now', '-31 days')")
        check("after 30 days it's gone for good", db.purge_deleted_leads() == 1
              and client.get("/api/trash").get_json()["groups"] == [])

        print("\n8. EXISTING PHONE KEYS ARE RE-KEYED ONCE")
        with db.get_db() as conn:
            conn.execute("UPDATE businesses SET phone_normalized='1501234500' WHERE name='Clinic 0'")
            conn.execute("DELETE FROM settings WHERE key='_migrated_phone_keys'")
            db._migrate_phone_keys(conn)
            key = conn.execute("SELECT phone_normalized FROM businesses WHERE name='Clinic 0'").fetchone()[0]
        check("an old-style key is rewritten", key == "501234500", key)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
