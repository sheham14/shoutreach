"""End-to-end test of the scrape job queue and worker protocol.

Run:  python tests/test_worker_api.py

Exercises the full round trip against a real Flask test client on a temporary
database: queue a job, claim it as a worker, report progress, drive a CAPTCHA
through to Resume, stop a job, and confirm the auth boundaries hold.

No browser and no network -- the scraping itself is not what is under test here.
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
    os.environ["DB_PATH"] = os.path.join(work, "worker_test.db")
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "test-password-123"

    import db
    importlib.reload(db)
    import app as app_mod
    importlib.reload(app_mod)

    # Worker keys belong to a real account now, so the session faked below has
    # to name a user that exists.
    if not db.get_user_by_username("admin"):
        db.create_user("admin", "test-password-123", is_admin=True)

    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()

    # Log in so the browser-side routes are reachable.
    client.post("/login", data={
        "username": "admin",
        "password": "test-password-123",
        "csrf_token": "",
    }, follow_redirects=True)
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["is_admin"] = True
        sess["csrf_token"] = "test-csrf"

    return db, app_mod, client


def admin_post(client, path, payload=None):
    return client.post(path, json=payload or {}, headers={"X-CSRF-Token": "test-csrf"})


def main():
    work = tempfile.mkdtemp(prefix="worker_api_")
    try:
        db, app_mod, client = boot(work)
        alice = db.get_user_by_username("admin")["id"]
        key = db.get_or_create_worker_key(alice)
        wh = {"X-API-Key": key}

        print("\n1. AUTH BOUNDARIES")
        r = client.post("/api/scraper/claim")
        check("claim without a key is rejected", r.status_code == 401, f"got {r.status_code}")
        r = client.post("/api/scraper/claim", headers={"X-API-Key": "wrong"})
        check("claim with a bad key is rejected", r.status_code == 401, f"got {r.status_code}")
        r = client.post("/api/scraper/claim", headers=wh)
        check("claim with a good key is accepted", r.status_code in (200, 204), f"got {r.status_code}")
        check("the worker key never appears in /api/settings",
              key not in str(client.get("/api/settings").get_json()))

        print("\n2. QUEUE A JOB")
        # The claim above counted as a check-in, so clear it to test the
        # no-worker path -- pressing Start with nothing listening has to say so
        # rather than silently queueing into the void.
        with db.get_db() as conn:
            conn.execute("UPDATE worker_keys SET last_seen=NULL")
        r = admin_post(client, "/api/scraper/start",
                       {"niche": "HVAC", "city": "Calgary Canada", "max_results": 20})
        body = r.get_json()
        check("start queues a job", r.status_code == 200 and body.get("ok"), str(body))
        job_id = body.get("job_id")
        check("warns when no worker is connected", bool(body.get("warning")), str(body.get("warning")))
        check("job is queued anyway, to run when the worker appears",
              db.get_scrape_job(job_id)["status"] == "queued")

        r = admin_post(client, "/api/scraper/start", {"niche": "x", "city": "y"})
        check("a second concurrent job is refused", r.status_code == 409, f"got {r.status_code}")

        print("\n3. WORKER CLAIMS IT")
        r = client.post("/api/scraper/claim", headers=wh)
        spec = r.get_json()
        check("worker receives the job", spec and spec.get("job_id") == job_id, str(spec))
        check("job spec carries the search", spec.get("niche") == "HVAC" and spec.get("max_results") == 20)

        r = client.post("/api/scraper/claim", headers=wh)
        check("a claimed job is not handed out twice", r.status_code == 204, f"got {r.status_code}")

        print("\n4. PROGRESS AND THE ONLINE INDICATOR")
        r = client.post(f"/api/scraper/jobs/{job_id}/progress", headers=wh, json={
            "status": "running", "progress": 3, "total": 20, "found": 2,
            "logs": [{"msg": "scraping", "level": "INFO"}],
        })
        control = r.get_json()
        check("progress returns control flags",
              "stop" in control and "resume" in control, str(control))

        status = client.get("/api/scraper/status").get_json()
        check("status reflects progress", status.get("progress") == 3 and status.get("total") == 20)
        check("worker now shows online", status.get("worker_online") is True)
        check("logs surface to the UI", any(l["msg"] == "scraping" for l in status.get("logs", [])))

        print("\n5. CAPTCHA ROUND TRIP")
        client.post(f"/api/scraper/jobs/{job_id}/progress", headers=wh, json={"status": "captcha"})
        status = client.get("/api/scraper/status").get_json()
        check("UI sees the captcha state", status.get("status") == "captcha")

        control = client.post(f"/api/scraper/jobs/{job_id}/progress",
                              headers=wh, json={}).get_json()
        check("worker keeps waiting before Resume", control.get("resume") is False)

        admin_post(client, "/api/scraper/resume")
        control = client.post(f"/api/scraper/jobs/{job_id}/progress",
                              headers=wh, json={}).get_json()
        check("Resume reaches the worker", control.get("resume") is True)

        control = client.post(f"/api/scraper/jobs/{job_id}/progress",
                              headers=wh, json={}).get_json()
        check("resume is one-shot, not sticky", control.get("resume") is False,
              "a single click must not unblock every later CAPTCHA")

        print("\n6. STOP")
        admin_post(client, "/api/scraper/stop")
        control = client.post(f"/api/scraper/jobs/{job_id}/progress",
                              headers=wh, json={"status": "running"}).get_json()
        check("Stop reaches the worker", control.get("stop") is True)

        client.post(f"/api/scraper/jobs/{job_id}/progress", headers=wh,
                    json={"status": "stopped", "finished": True})
        r = admin_post(client, "/api/scraper/start", {"niche": "a", "city": "b"})
        check("a new job can start once the last one finished",
              r.status_code == 200, f"got {r.status_code}")

        print("\n7. STALE WORKER REAPING")
        new_id = r.get_json()["job_id"]
        client.post("/api/scraper/claim", headers=wh)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE scrape_jobs SET status='running', "
                "heartbeat_at=datetime('now','-1 day') WHERE id=?", (new_id,)
            )
        db.reap_stale_scrape_jobs()
        job = db.get_scrape_job(new_id)
        check("an abandoned job is failed, not left stuck",
              job["status"] == "error", f"status={job['status']}")

        print("\n8. WORKER PUSHES LEADS")
        r = client.post("/api/contacts/import", headers=wh, json={"rows": [
            {"email": "reception@avalondental.ca", "company": "Avalon Dental",
             "website": "http://avalondental.ca", "mx_valid": 1},
            {"email": "", "company": "Village Dental", "website": "http://village.ca",
             "status": "form_only"},
        ]})
        body = r.get_json()
        check("worker key is accepted by the import route",
              r.status_code == 200 and body.get("ok"), f"{r.status_code} {body}")
        check("both rows stored", body.get("inserted") == 2, str(body))

        r = client.post("/api/contacts/import", headers={"X-API-Key": "nope"},
                        json={"rows": [{"email": "x@y.ca"}]})
        check("a bad key cannot import", r.status_code in (401, 403), f"got {r.status_code}")

        r = client.post("/api/contacts/import", headers=wh,
                        json={"rows": [{"email": f"a{i}@b.ca"} for i in range(50_001)]})
        check("oversized JSON import is refused", r.status_code == 413, f"got {r.status_code}")

        print("\n9. A SCRAPE AIMED AT WHATSAPP LANDS THERE AND NOWHERE ELSE")
        r = admin_post(client, "/api/scraper/start",
                       {"niche": "dentists", "city": "Doha", "destination": "whatsapp"})
        check("a WhatsApp scrape needs a country", r.status_code == 400, f"got {r.status_code}")
        r = admin_post(client, "/api/scraper/start",
                       {"niche": "dentists", "city": "Doha", "destination": "fax"})
        check("an unknown destination is refused", r.status_code == 400, f"got {r.status_code}")

        r = admin_post(client, "/api/scraper/start", {"niche": "dentists", "city": "Doha",
                                                      "destination": "whatsapp", "country": "QA"})
        wa_job = (r.get_json() or {}).get("job_id")
        check("a WhatsApp scrape with a country queues", r.status_code == 200 and wa_job,
              f"{r.status_code} {r.get_json()}")

        spec = client.post("/api/scraper/claim", headers=wh).get_json() or {}
        check("the worker is told it's a WhatsApp scrape, and where",
              spec.get("destination") == "whatsapp" and spec.get("country") == "QA", str(spec))

        # A worker that hasn't been updated still hunts for emails and sends
        # them. They must not become email leads.
        r = client.post("/api/contacts/import", headers=wh, json={"rows": [
            {"email": "front@pearldental.qa", "company": "Pearl Dental", "phone": "5512 3456",
             "website": "http://pearldental.qa", "mx_valid": 1, "source_job_id": wa_job},
            {"email": "", "company": "Corniche Clinic", "phone": "4412 7788", "website": "",
             "status": "no_website", "source_job_id": wa_job},
        ]})
        body = r.get_json() or {}
        check("the worker's import succeeds", r.status_code == 200 and body.get("ok"),
              f"{r.status_code} {body}")
        check("and is filed as WhatsApp", body.get("destination") == "whatsapp", str(body))
        with db.get_db() as conn:
            wa = conn.execute("""
                SELECT w.wa_number FROM wa_leads w JOIN businesses b ON b.id = w.business_id
                 WHERE b.name IN ('Pearl Dental', 'Corniche Clinic')
            """).fetchall()
            stray_email = conn.execute(
                "SELECT COUNT(*) FROM email_leads WHERE email='front@pearldental.qa'"
            ).fetchone()[0]
        check("both became WhatsApp leads", len(wa) == 2, f"got {len(wa)}")
        check("with numbers formatted for Qatar",
              all(row["wa_number"].startswith("974") for row in wa), str([dict(x) for x in wa]))
        check("and the email a stale worker sent was not filed as an email lead",
              stray_email == 0, f"got {stray_email}")

        # The same job id arriving from a person, not the worker, means Contacts.
        r = client.post("/api/contacts/import", headers={"X-CSRF-Token": "test-csrf"}, json={"rows": [
            {"email": "hello@westbay.qa", "company": "West Bay Dental",
             "website": "http://westbay.qa", "mx_valid": 1, "source_job_id": wa_job},
        ]})
        body = r.get_json() or {}
        with db.get_db() as conn:
            westbay_email = conn.execute(
                "SELECT COUNT(*) FROM email_leads WHERE email='hello@westbay.qa'"
            ).fetchone()[0]
            westbay_wa = conn.execute("""
                SELECT COUNT(*) FROM wa_leads w JOIN businesses b ON b.id = w.business_id
                 WHERE b.name = 'West Bay Dental'
            """).fetchone()[0]
        check("a CSV imported by hand through Contacts still goes to Contacts",
              westbay_email == 1 and westbay_wa == 0,
              f"email={westbay_email} whatsapp={westbay_wa} body={body}")

        print("\n10. TWO OPERATORS, EACH WITH THEIR OWN WORKER")
        # Close out section 9's scrape so it isn't still counted as active.
        client.post(f"/api/scraper/jobs/{wa_job}/progress", headers=wh,
                    json={"status": "done", "finished": True})

        bob = db.create_user("bob", "test-password-456", is_admin=False)
        bob_key = db.get_or_create_worker_key(bob)
        bh = {"X-API-Key": bob_key}
        check("each operator gets their own key", bob_key != key)

        bob_client = app_mod.app.test_client()
        with bob_client.session_transaction() as sess:
            sess["user_id"] = bob
            sess["is_admin"] = False
            sess["csrf_token"] = "test-csrf"

        r = bob_client.get("/api/settings/worker-key")
        shown = (r.get_json() or {}).get("key")
        check("a non-admin can read their own key", r.status_code == 200 and shown == bob_key,
              f"{r.status_code}")
        check("and it is theirs, never the admin's", shown != key)

        r = admin_post(client, "/api/scraper/start", {"niche": "dentists", "city": "Halifax"})
        alice_job = (r.get_json() or {}).get("job_id")
        check("the admin queues a scrape", r.status_code == 200 and alice_job, str(r.get_json()))

        r = client.post("/api/scraper/claim", headers=bh)
        check("the other operator's worker does not pick it up", r.status_code == 204,
              f"got {r.status_code}")

        r = admin_post(bob_client, "/api/scraper/start", {"niche": "physio", "city": "Moncton"})
        bob_job = (r.get_json() or {}).get("job_id")
        check("the other operator can scrape at the same time, on their own worker",
              r.status_code == 200 and bob_job, f"{r.status_code} {r.get_json()}")

        spec = client.post("/api/scraper/claim", headers=bh).get_json() or {}
        check("each worker gets its own operator's scrape", spec.get("job_id") == bob_job, str(spec))
        spec = client.post("/api/scraper/claim", headers=wh).get_json() or {}
        check("and the admin's worker gets the admin's", spec.get("job_id") == alice_job, str(spec))

        r = client.post(f"/api/scraper/jobs/{alice_job}/progress", headers=bh,
                        json={"status": "running", "logs": [{"msg": "snooping", "level": "INFO"}]})
        check("a worker cannot report into another operator's scrape", r.status_code == 404,
              f"got {r.status_code}")
        check("so nothing reached that scrape's log",
              "snooping" not in (db.get_scrape_job(alice_job).get("logs") or ""))

        r = client.post("/api/contacts/import", headers=bh, json={"rows": [
            {"email": "desk@monctonphysio.ca", "company": "Moncton Physio",
             "website": "http://monctonphysio.ca", "mx_valid": 1, "source_job_id": alice_job},
        ]})
        check("the other operator's worker can import", r.status_code == 200,
              f"{r.status_code} {r.get_json()}")
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT owner_id FROM businesses WHERE name='Moncton Physio'").fetchone()
        check("its leads land in its own operator's account, even naming another's scrape",
              row is not None and row["owner_id"] == bob, str(dict(row) if row else None))
        sources = bob_client.get("/api/contacts/sources").get_json() or []
        check("and naming that scrape reveals nothing about it",
              "Halifax" not in str(sources) and "dentists" not in str(sources), str(sources))

        r = admin_post(bob_client, "/api/settings/worker-key")
        new_bob_key = (r.get_json() or {}).get("key")
        check("an operator can rotate their own key",
              r.status_code == 200 and new_bob_key and new_bob_key != bob_key, f"{r.status_code}")
        r = client.post("/api/scraper/heartbeat", headers=bh)
        check("their old key stops working", r.status_code == 401, f"got {r.status_code}")
        r = client.post("/api/scraper/heartbeat", headers=wh)
        check("while everyone else's keeps working", r.status_code == 200, f"got {r.status_code}")

        with db.get_db() as conn:
            conn.execute("UPDATE worker_keys SET last_seen=NULL WHERE owner_id=?", (bob,))
        client.post("/api/scraper/heartbeat", headers=wh)
        mine = client.get("/api/scraper/status").get_json() or {}
        theirs = bob_client.get("/api/scraper/status").get_json() or {}
        check("'worker connected' reflects your own worker, not someone else's",
              mine.get("worker_online") is True and theirs.get("worker_online") is False,
              f"admin={mine.get('worker_online')} other={theirs.get('worker_online')}")

        r = bob_client.post("/api/contacts/import", headers={"X-CSRF-Token": "test-csrf"},
                            json={"rows": [{"email": "hi@bobsown.ca", "company": "Bobs Own",
                                            "website": "http://bobsown.ca", "mx_valid": 1}]})
        check("a non-admin can import a CSV through the browser",
              r.status_code == 200 and (r.get_json() or {}).get("inserted") == 1,
              f"{r.status_code} {r.get_json()}")

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
