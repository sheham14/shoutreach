"""Clearing the activity log on request.

Run:  python tests/test_clear_logs.py

Logs are an operational trail, not outreach data -- clearing them can never
lose a contact, a campaign, or a send. This pins that the wipe is complete,
admin-only, and leaves one entry behind explaining what happened (an empty
log with no explanation reads like something broke).
"""
import importlib
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


def main():
    work = tempfile.mkdtemp(prefix="clearlogs-")
    try:
        os.environ["DB_PATH"] = os.path.join(work, "t.db")
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

        print("\n1. CLEARING WIPES EVERYTHING AND REPORTS HOW MUCH")
        db.add_log("entry one")
        db.add_log("entry two", "WARN")
        before = len(db.get_logs(1000))
        check("at least the two we just added are there", before >= 2, str(before))

        r = client.delete("/api/logs", headers={"X-CSRF-Token": token})
        check("the request succeeds", r.status_code == 200, str(r.status_code))
        body = r.get_json()
        check("it reports the count it deleted", body.get("deleted") == before, str(body))

        print("\n2. AN EMPTY LOG WOULD LOOK LIKE SOMETHING BROKE -- ONE ENTRY EXPLAINS ITSELF")
        after = db.get_logs(1000)
        check("exactly one entry remains", len(after) == 1, str(len(after)))
        # Printed with backslashreplace: the message legitimately starts with
        # an emoji, and a console on a non-UTF-8 codepage (Windows cp1252,
        # common enough not to assume around) would otherwise crash this
        # check on the print itself rather than report a result.
        safe_msg = after[0]["message"].encode("ascii", "backslashreplace").decode("ascii")
        check("and it explains what happened",
              "cleared" in after[0]["message"].lower(), safe_msg)

        print("\n3. IT NEVER TOUCHES ANYTHING BUT THE LOG TABLE")
        db.upsert_businesses([{"email": "a@keepme.ca", "company": "Keep Me"}])
        cid = db.create_campaign("Survives a log clear")
        db.clear_logs()
        check("a business/email lead survives", db.get_email_lead_by_email("a@keepme.ca") is not None)
        check("a campaign survives", db.get_campaign(cid) is not None)

        print("\n4. IT IS ADMIN-ONLY")
        anon = app_mod.app.test_client()
        r = anon.delete("/api/logs")
        check("an unauthenticated request is refused", r.status_code == 401, str(r.status_code))

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
