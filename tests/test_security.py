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
