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
        key = db.get_or_create_worker_api_key()
        wh = {"X-API-Key": key}

        print("\n1. AUTH BOUNDARIES")
        r = client.post("/api/scraper/claim")
        check("claim without a key is rejected", r.status_code == 401, f"got {r.status_code}")
        r = client.post("/api/scraper/claim", headers={"X-API-Key": "wrong"})
        check("claim with a bad key is rejected", r.status_code == 401, f"got {r.status_code}")
        r = client.post("/api/scraper/claim", headers=wh)
        check("claim with a good key is accepted", r.status_code in (200, 204), f"got {r.status_code}")
        check("worker key is masked in /api/settings",
              client.get("/api/settings").get_json().get("_worker_api_key") != key)

        print("\n2. QUEUE A JOB")
        # The claim above counted as a check-in, so clear it to test the
        # no-worker path -- pressing Start with nothing listening has to say so
        # rather than silently queueing into the void.
        with db.get_db() as conn:
            conn.execute("DELETE FROM settings WHERE key='_worker_last_seen'")
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
