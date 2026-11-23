"""Regression tests for the closed audit findings.

Run:  python tests/test_security.py

Each test names the Fable Audit finding it pins, so a future change that
reintroduces one fails here rather than being rediscovered by the next audit.
"""
import importlib
import os
import shutil
import ssl
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def boot(work):
    os.environ["DB_PATH"] = os.path.join(work, "sec.db")
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["SHOUTREACH_INSECURE_COOKIES"] = "1"
    os.environ["SHOUTREACH_NO_SCHEDULER"] = "1"
    import db
    importlib.reload(db)
    import app as app_mod
    importlib.reload(app_mod)
    app_mod.app.config["TESTING"] = True
    return db, app_mod


def test_mail_tls():
    """Fable 2.2 — SMTP/IMAP handed credentials over an unverified channel."""
    print("\n1. MAIL TLS VERIFICATION (2.2)")
    import sender
    ctx = sender._TLS_CONTEXT
    check("certificate chain is verified", ctx.verify_mode == ssl.CERT_REQUIRED,
          str(ctx.verify_mode))
    check("hostname is checked", ctx.check_hostname is True)

    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sender.py"), encoding="utf-8").read()
    check("every IMAP4_SSL call passes the context",
          src.count("IMAP4_SSL(host, ssl_context=_TLS_CONTEXT")
          == src.count("IMAP4_SSL("))
    check("SMTP_SSL passes the context", "SMTP_SSL(host, port, timeout=15, context=_TLS_CONTEXT)" in src)
    check("starttls passes the context", "starttls(context=_TLS_CONTEXT)" in src)


def test_bounce_scan_peek():
    """Fable 3.5 — the bounce scan marked every unread message as read."""
    print("\n2. BOUNCE SCAN DOES NOT MARK MAIL READ (3.5)")
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sender.py"), encoding="utf-8").read()

    # Check the fetch calls themselves, not prose that happens to mention them.
    fetches = [line.strip() for line in src.splitlines()
               if "M.fetch(" in line and not line.strip().startswith("#")]
    check("the bounce scan fetches with BODY.PEEK[]",
          any("BODY.PEEK[]" in f for f in fetches), str(fetches))
    check("no fetch uses bare RFC822",
          not any('"(RFC822)"' in f for f in fetches), str(fetches))

    # The reply scanner may use RFC822.HEADER because it opens the mailbox
    # read-only, so the server never sets \Seen regardless of what is fetched.
    check("a read-only mailbox is opened for the reply scan",
          'M.select("INBOX", readonly=True)' in src)


def test_unsubscribe_post(app_mod):
    """Fable 2.3 — one-click unsubscribe was advertised but returned 405."""
    print("\n3. ONE-CLICK UNSUBSCRIBE ACCEPTS POST (2.3)")
    client = app_mod.app.test_client()
    r = client.post("/unsubscribe/definitely-not-a-valid-token")
    check("POST is routed, not 405", r.status_code != 405, f"got {r.status_code}")
    check("an invalid token is still rejected", r.status_code == 400, f"got {r.status_code}")

    rules = [r for r in app_mod.app.url_map.iter_rules() if "/unsubscribe/" in str(r)]
    check("route allows both GET and POST",
          rules and {"GET", "POST"}.issubset(rules[0].methods), str(rules))


def test_formula_injection(app_mod):
    """Fable 3.1 — scraped names could execute as spreadsheet formulas."""
    print("\n4. EXPORT FORMULA INJECTION (3.1)")
    f = app_mod._no_formula
    check("= is neutralised", f('=HYPERLINK("http://evil")').startswith("'"))
    check("+ is neutralised", f("+1+1").startswith("'"))
    check("- is neutralised", f("-2+3").startswith("'"))
    check("@ is neutralised", f("@SUM(A1)").startswith("'"))
    check("ordinary text is untouched", f("Avalon Dental") == "Avalon Dental")
    check("non-strings pass through", f(42) == 42)


def test_xff_ratelimit(app_mod):
    """Fable 3.4 — rotating X-Forwarded-For reset the login throttle."""
    print("\n5. X-FORWARDED-FOR RATE-LIMIT BYPASS (3.4)")
    with app_mod.app.test_request_context(
        "/login", headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}
    ):
        ip = app_mod._client_ip()
    check("takes the proxy-written entry, not the client's claim",
          ip == "10.0.0.1", f"got {ip}")

    with app_mod.app.test_request_context(
        "/login", headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8, 10.0.0.1"}
    ):
        spoofed = app_mod._client_ip()
    check("extra spoofed hops do not shift the result",
          spoofed == "10.0.0.1", f"got {spoofed}")


def test_worker_routes_still_authenticated(app_mod):
    """The CSRF exemption for worker routes must not become an auth hole."""
    print("\n6. WORKER CSRF EXEMPTION IS NOT AN AUTH BYPASS")
    client = app_mod.app.test_client()
    for path in ("/api/scraper/claim", "/api/scraper/heartbeat",
                 "/api/scraper/jobs/1/progress"):
        r = client.post(path, json={})
        check(f"{path} rejects an unauthenticated caller",
              r.status_code == 401, f"got {r.status_code}")

    r = client.post("/api/contacts/import", json={"rows": [{"email": "x@y.ca"}]})
    check("/api/contacts/import still needs auth without a key",
          r.status_code in (401, 403), f"got {r.status_code}")


def test_logs_admin_only(db, app_mod):
    """Full Audit 2026-11-21 B-2 -- the Activity log answered any signed-in user."""
    print("\n7. THE ACTIVITY LOG IS ADMIN-ONLY (B-2)")
    admin = db.create_user("logadmin", "test-password-123", is_admin=True)
    plain = db.create_user("logplain", "test-password-123", is_admin=False)
    db.add_log("Sent step 1 -> someone@clinic.ae | subject")

    def client_for(uid, is_admin):
        c = app_mod.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = uid
            s["is_admin"] = is_admin
            s["csrf_token"] = "t"
        return c
    r = client_for(plain, False).get("/api/logs")
    check("a non-admin gets 403", r.status_code == 403, f"got {r.status_code}")
    check("and no log lines", b"someone@clinic.ae" not in r.data)
    r = client_for(admin, True).get("/api/logs")
    check("an admin still reads it", r.status_code == 200 and b"someone@clinic.ae" in r.data,
          f"got {r.status_code}")
    r = app_mod.app.test_client().get("/api/logs")
    check("signed out gets 401", r.status_code == 401, f"got {r.status_code}")


def test_public_static(db, app_mod):
    """Plan 3 -- css, img and fonts load signed out (the sign-in page uses
    them); js and every /api/ route answer exactly as before."""
    print("\n8. SIGNED OUT: CSS, IMAGES AND FONTS LOAD; NOTHING ELSE DOES (Plan 3)")
    client = app_mod.app.test_client()
    for path in ("/static/css/main.css", "/static/img/hexiv-mark.svg",
                 "/static/fonts/InterVariable.woff2"):
        r = client.get(path)
        check(f"{path} answers 200 signed out", r.status_code == 200, f"got {r.status_code}")
        r.close()

    js_body = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "static", "js", "app.js"), "rb").read()
    climbs = (
        "/static/js/app.js",
        "/static/css/../js/app.js",
        "/static/img/../js/app.js",
        "/static/fonts/../js/app.js",
        "/static/css/./../js/app.js",
        "/static/css/%2e%2e/js/app.js",
        "/static/css/..%2fjs/app.js",
        "/static/css/%2E%2E%2Fjs%2Fapp.js",
        "/static/fonts/../../app.py",
        "/static/css/..\\js\\app.js",
    )
    for path in climbs:
        r = client.get(path)
        leaked = r.status_code == 200 or js_body[:200] in r.data
        check(f"{path} is not served signed out",
              not leaked and r.status_code == 302 and "/login" in r.headers.get("Location", ""),
              f"got {r.status_code}")
        r.close()

    # Every /api/ route, every method, signed out: the same answer with and
    # without the public-static rule, and 401 for all but the routes that
    # carry their own credential (worker key, website key, Cal.com signature).
    own_credential = set(app_mod._WORKER_PATHS) | set(app_mod._INBOUND_PATHS)
    real = app_mod._is_public_static
    checked, differ, open_routes = 0, [], []
    for rule in app_mod.app.url_map.iter_rules():
        if not rule.rule.startswith("/api/"):
            continue
        args = {}
        for name, conv in rule._converters.items():
            args[name] = 1 if type(conv).__name__ in ("IntegerConverter", "NumberConverter") else "x"
        with app_mod.app.test_request_context():
            url = app_mod.app.url_for(rule.endpoint, **args)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            answers = []
            for rule_on in (True, False):
                app_mod._is_public_static = real if rule_on else (lambda: False)
                r = client.open(url, method=method, json={})
                answers.append(r.status_code)
                r.close()
            app_mod._is_public_static = real
            checked += 1
            if answers[0] != answers[1]:
                differ.append(f"{method} {url} {answers}")
            path = url.split("?")[0]
            if path in own_credential or path.startswith("/api/scraper/jobs/"):
                if 200 <= answers[0] < 300:
                    open_routes.append(f"{method} {url} {answers[0]}")
            elif answers[0] != 401:
                open_routes.append(f"{method} {url} {answers[0]}")
    check(f"all {checked} /api/ route+method pairs answer the same as without the rule",
          checked > 100 and not differ, "; ".join(differ[:5]))
    check("and every one refuses a signed-out caller", not open_routes, "; ".join(open_routes[:5]))

    # Signed in, js still loads (the rule only adds, never removes).
    uid = db.create_user("staticuser", "test-password-123", is_admin=False)
    c = app_mod.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
        sess["csrf_token"] = "t"
    r = c.get("/static/js/app.js")
    check("signed in, /static/js/app.js still loads", r.status_code == 200, f"got {r.status_code}")
    r.close()


def main():
    work = tempfile.mkdtemp(prefix="sec_")
    try:
        db, app_mod = boot(work)
        test_mail_tls()
        test_bounce_scan_peek()
        test_unsubscribe_post(app_mod)
        test_formula_injection(app_mod)
        test_xff_ratelimit(app_mod)
        test_worker_routes_still_authenticated(app_mod)
        db.init_db()
        test_logs_admin_only(db, app_mod)
        test_public_static(db, app_mod)
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
