"""The 'automatically check for replies & bounces' toggle.

Run:  python tests/test_email_checking_toggle.py

Reply checking logs into IMAP every 5 minutes whether or not any campaign
is running, and logs an unconditional line every time -- real cost and real
log volume for nothing, on an operator's smallest GCP tier, with no
campaigns active. This pins that the toggle actually gates the periodic
check, that a manual "Check for replies & send" always runs regardless of
it, and that it defaults to on so no existing install's behavior changes
just by upgrading.
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


def boot(work):
    os.environ["DB_PATH"] = os.path.join(work, "t.db")
    os.environ["SECRET_KEY"] = "test-secret"
    import db
    importlib.reload(db)
    db.init_db()
    db.create_user("admin", "testpassword123", is_admin=True)
    import scheduler
    importlib.reload(scheduler)
    import app as app_mod
    importlib.reload(app_mod)
    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()
    r = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.get_data(as_text=True)).group(1)
    client.post("/login", data={"username": "admin", "password": "testpassword123",
                                "csrf_token": csrf})
    token = client.get("/api/csrf").get_json()["csrf_token"]
    return db, scheduler, client, token


def api(client, token, method, url, **kw):
    fn = getattr(client, method)
    headers = kw.pop("headers", {})
    headers["X-CSRF-Token"] = token
    r = fn(url, headers=headers, **kw)
    return r.status_code, r.get_json()


def main():
    work = tempfile.mkdtemp(prefix="checktoggle-")
    try:
        db, scheduler, client, token = boot(work)

        print("\n1. DEFAULTS TO ON")
        check("a fresh install checks by default (no upgrade should go quiet)",
              scheduler.email_checking_enabled() is True)

        print("\n2. THE DASHBOARD TOGGLE ROUND-TRIPS OVER HTTP")
        s, body = api(client, token, "get", "/api/settings")
        check("it's readable and not masked as a secret",
              s == 200 and body.get("email_checking_enabled") in ("1", None), str(body))

        s, body = api(client, token, "post", "/api/settings",
                     json={"email_checking_enabled": "0"})
        check("turning it off succeeds", s == 200, str(body))
        check("the scheduler sees it off", scheduler.email_checking_enabled() is False)

        s, body = api(client, token, "get", "/api/settings")
        check("and it reads back off", body.get("email_checking_enabled") == "0", str(body))

        s, body = api(client, token, "post", "/api/settings",
                     json={"email_checking_enabled": "1"})
        check("turning it back on succeeds", s == 200, str(body))
        check("the scheduler sees it on again", scheduler.email_checking_enabled() is True)

        print("\n3. THE PERIODIC CHECK IS GATED; A MANUAL RUN IS NOT")
        db.save_settings({"email_checking_enabled": "0"})
        calls = []
        real_reply, real_bounce = scheduler.run_reply_check, scheduler.run_bounce_check
        scheduler.run_reply_check = lambda: calls.append("reply")
        scheduler.run_bounce_check = lambda: calls.append("bounce")
        try:
            # Reproduce _run_loop's own gating condition exactly, rather than
            # running the infinite loop itself.
            do_reply_check = False
            last_reply_check = 0
            now = last_reply_check + 301  # well past the 5-minute mark
            if do_reply_check or (scheduler.email_checking_enabled() and now - last_reply_check > 300):
                scheduler.run_reply_check()
                scheduler.run_bounce_check()
            check("disabled + only the timer being due does NOT run the checker",
                  calls == [], str(calls))

            do_reply_check = True  # the manual "Check for replies & send" path
            if do_reply_check or (scheduler.email_checking_enabled() and now - last_reply_check > 300):
                scheduler.run_reply_check()
                scheduler.run_bounce_check()
            check("but a manual trigger runs it even while disabled",
                  calls == ["reply", "bounce"], str(calls))
        finally:
            scheduler.run_reply_check, scheduler.run_bounce_check = real_reply, real_bounce

        print("\n4. THE GATE LIVES WHERE THE LOOP ACTUALLY RUNS, NOT JUST IN A HELPER")
        import inspect
        loop_src = inspect.getsource(scheduler._run_loop)
        check("_run_loop actually calls the gate rather than re-deriving it inline",
              "email_checking_enabled()" in loop_src, loop_src[:200])
        check("a manual trigger is still checked first (or'd), so it can't be gated out",
              "do_reply_check or" in loop_src.replace("\n", " ").replace("  ", " "),
              "expected 'do_reply_check or (...)' shape")

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
