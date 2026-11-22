"""The two doors in from outside: website enquiries and Cal.com (Plan 2).

Run:  python tests/test_inbound.py     (exits 0 on pass, 1 on failure)

Contract A: a website posts a lead with its own key (X-API-Key). Contract B:
Cal.com posts a booking with the body signed (X-Cal-Signature-256, HMAC-SHA256
with the webhook secret). Neither has a session or a CSRF token; each checks
its own credential, and neither credential works anywhere else.
"""
import hashlib
import hmac
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SHOUTREACH_NO_SCHEDULER"] = "1"

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def q(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


LEAD = {"site": "hexiv.co.uk", "name": "Sam Smith", "email": "Sam@Example.co.uk",
        "business": "Smith Dental", "phone": "07700 900123", "website": "smithdental.co.uk",
        "country": "GB", "answers": {"What do you do?": "Dental clinic"},
        "summary": "Dental clinic, wants more bookings"}


def main():
    work = tempfile.mkdtemp(prefix="inbound_")
    try:
        path = os.path.join(work, "inbound.db")
        os.environ["DB_PATH"] = path
        os.environ["SECRET_KEY"] = "test-secret"
        os.environ["SHOUTREACH_INSECURE_COOKIES"] = "1"
        import db
        importlib.reload(db)
        db.init_db()
        admin = db.create_user("admin", "test-password-123", is_admin=True)
        other = db.create_user("other", "test-password-123", is_admin=False)
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True
        anon = app_mod.app.test_client()

        def client_for(uid):
            c = app_mod.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = uid
                s["csrf_token"] = "t"
            return c
        ca, co = client_for(admin), client_for(other)
        hdr = {"X-CSRF-Token": "t"}

        print("\n1. THE WEBSITE KEY")
        r = anon.post("/api/inbound/website-lead", json=LEAD)
        check("before any key exists, nothing gets in", r.status_code == 401, str(r.status_code))
        r = co.post("/api/website-settings/key", headers=hdr)
        check("a non-admin can't make the key", r.status_code == 403, str(r.status_code))
        r = ca.post("/api/website-settings/key", headers=hdr)
        key = (r.get_json() or {}).get("key", "")
        check("an admin makes one, shown once", r.status_code == 200 and key.startswith("web_"))
        stored = q(path, "SELECT value FROM settings WHERE key='_website_key_sha256'")
        check("only its digest is stored", stored and stored[0][0] == hashlib.sha256(key.encode()).hexdigest()
              and key not in json.dumps(q(path, "SELECT key, value FROM settings")))
        check("GET /api/settings shows neither the key nor the secret's setting",
              not any(k.startswith("_website") or k.startswith("_cal") for k in ca.get("/api/settings").get_json()))
        check("the settings card says it is set, without showing it",
              ca.get("/api/website-settings").get_json().get("has_website_key") is True
              and key not in ca.get("/api/website-settings").get_data(as_text=True))

        print("\n2. WHO MAY POST A LEAD")
        r = anon.post("/api/inbound/website-lead", json=LEAD)
        check("no key -> 401", r.status_code == 401, str(r.status_code))
        r = anon.post("/api/inbound/website-lead", json=LEAD, headers={"X-API-Key": "web_wrong"})
        check("wrong key -> 401", r.status_code == 401, str(r.status_code))
        worker_key = db.get_or_create_worker_key(admin)
        r = anon.post("/api/inbound/website-lead", json=LEAD, headers={"X-API-Key": worker_key})
        check("a worker key -> 401", r.status_code == 401, str(r.status_code))
        r = anon.post("/api/contacts/import", json={"rows": [{"email": "x@y.co"}]}, headers={"X-API-Key": key})
        check("and the website key doesn't work on the worker's import", r.status_code in (401, 403), str(r.status_code))
        r = anon.post("/api/scraper/claim", json={}, headers={"X-API-Key": key})
        check("nor on the worker's claim", r.status_code == 401, str(r.status_code))
        r = co.post("/api/inbound/website-lead", json=LEAD, headers=hdr)
        check("a signed-in session is not a website key", r.status_code == 401, str(r.status_code))

        web = {"X-API-Key": key}
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "site": "evil.example"}, headers=web)
        check("an unknown site -> 400", r.status_code == 400, str(r.status_code))
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "email": ""}, headers=web)
        check("no email -> 400", r.status_code == 400, str(r.status_code))
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "name": ""}, headers=web)
        check("no name -> 400", r.status_code == 400, str(r.status_code))
        r = anon.post("/api/inbound/website-lead", data="not json", headers={**web, "Content-Type": "application/json"})
        check("not JSON -> 400", r.status_code == 400, str(r.status_code))
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "summary": "x" * 40000}, headers=web)
        check("over 32 KB -> 413", r.status_code == 413, str(r.status_code))

        print("\n3. A GOOD POST, AND THE SAME ONE AGAIN")
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "unknown_field": "ignored",
                                                         "business": "B" * 999}, headers=web)
        check("accepted with {'ok': true} and nothing echoed", r.status_code == 200 and r.get_json() == {"ok": True},
              f"{r.status_code} {r.get_data(as_text=True)[:80]}")
        rows = q(path, "SELECT site, email, name, business_name, country, answers FROM website_enquiries")
        check("one enquiry, email lower-cased, strings capped",
              len(rows) == 1 and rows[0][1] == "sam@example.co.uk" and len(rows[0][3]) == 200, str(rows)[:160])
        r = anon.post("/api/inbound/website-lead", json={**LEAD, "phone": "", "summary": "Second look"}, headers=web)
        rows = q(path, "SELECT phone, summary FROM website_enquiries")
        check("a repeat post doesn't duplicate", r.status_code == 200 and len(rows) == 1, str(rows))
        check("it updates, keeping what the second post left empty",
              rows and rows[0] == ("07700 900123", "Second look"), str(rows))
        anon.post("/api/inbound/website-lead", json={**LEAD, "site": "hexiv.co"}, headers=web)
        check("the same email from the other site is its own enquiry",
              q(path, "SELECT COUNT(*) FROM website_enquiries")[0][0] == 2)

        print("\n4. CAL.COM'S SIGNATURE")
        body = json.dumps({"hello": "cal"}).encode()
        r = anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": "00"})
        check("no secret set yet -> 401", r.status_code == 401, str(r.status_code))
        r = co.post("/api/website-settings/cal-secret", headers=hdr)
        check("a non-admin can't make the secret", r.status_code == 403)
        secret = ca.post("/api/website-settings/cal-secret", headers=hdr).get_json().get("secret", "")
        check("an admin makes one, shown once", len(secret) > 20)
        good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": "0" * 64})
        check("a bad signature -> 401", r.status_code == 401, str(r.status_code))
        r = anon.post("/api/inbound/cal", data=body + b" ", headers={"X-Cal-Signature-256": good})
        check("a changed body -> 401", r.status_code == 401, str(r.status_code))
        r = anon.post("/api/inbound/cal", data=body)
        check("no signature -> 401", r.status_code == 401, str(r.status_code))
        r = anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": good})
        check("the right signature -> 200, no session or CSRF needed", r.status_code == 200, str(r.status_code))
        check("stored as received", q(path, "SELECT body FROM cal_events") == [(body.decode(),)])
        anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": good})
        check("Cal.com's retry of the same body is stored once",
              q(path, "SELECT COUNT(*) FROM cal_events")[0][0] == 1)
        big = b"{" + b" " * (300 * 1024) + b"}"
        r = anon.post("/api/inbound/cal", data=big,
                      headers={"X-Cal-Signature-256": hmac.new(secret.encode(), big, hashlib.sha256).hexdigest()})
        check("an oversize delivery -> 413", r.status_code == 413, str(r.status_code))
        r = anon.post("/api/inbound/cal", data=body, headers={"X-Cal-Signature-256": good, "X-API-Key": key})
        check("the website key isn't needed or used here", r.status_code == 200)
        r = anon.post("/api/inbound/website-lead", json=LEAD,
                      headers={"X-Cal-Signature-256": good})
        check("and a Cal.com signature doesn't open the website door", r.status_code == 401)

        print("\n5. NOTHING ELSE OPENED UP")
        for path_ in ("/api/inbound/website-leadx", "/api/inbound/", "/api/inbound/cal/extra"):
            r = anon.post(path_, json=LEAD, headers=web)
            check(f"{path_} still needs sign-in", r.status_code == 401, str(r.status_code))
        check("the website settings are admin-only", co.get("/api/website-settings").status_code == 403)
        check("signed out, they need sign-in", anon.get("/api/website-settings").status_code == 401)

        print("\n6. THE SETTINGS")
        r = ca.put("/api/website-settings", headers=hdr, json={
            "sites": {"hexiv.co.uk": {"owner_id": other, "country": "gb"}},
            "wa_wait_minutes": 45, "email_wait_hours": 12,
            "cal_booking_link": "https://cal.com/hexiv/free-strategy-call"})
        cfg = ca.get("/api/website-settings").get_json()
        check("an admin sets who receives a site's leads, its country, the waits and the link",
              r.status_code == 200 and cfg["sites"]["hexiv.co.uk"] == {"owner_id": other, "country": "GB"}
              and cfg["wa_wait_minutes"] == 45 and cfg["email_wait_hours"] == 12
              and cfg["cal_booking_link"].startswith("https://cal.com/"), str(cfg)[:200])
        check("the other site keeps its defaults", cfg["sites"]["hexiv.co"] == {"owner_id": 0, "country": ""})
        check("the defaults are 30 minutes and 24 hours",
              db.WEBSITE_DEFAULTS["wa_wait_minutes"] == 30 and db.WEBSITE_DEFAULTS["email_wait_hours"] == 24)
        for bad in ({"sites": {"hexiv.co": {"owner_id": 999}}}, {"sites": {"hexiv.co": {"country": "GBR"}}},
                    {"wa_wait_minutes": "soon"}, {"email_wait_hours": -1},
                    {"cal_booking_link": "http://insecure.example"}):
            r = ca.put("/api/website-settings", headers=hdr, json=bad)
            check(f"refused: {bad}", r.status_code == 400, str(r.get_json()))
        check("the addresses to paste are offered",
              cfg["cal_webhook_url"].endswith("/api/inbound/cal")
              and cfg["website_lead_url"].endswith("/api/inbound/website-lead"))

        print("\n7. EACH OPERATOR'S FOLLOW-UP CAMPAIGNS")
        mine = db.create_campaign("Mine", owner_id=other)
        theirs = db.create_campaign("Theirs", owner_id=admin)
        wa = db.create_wa_campaign("WA", owner_id=other)
        wa = wa[0] if isinstance(wa, tuple) else wa
        check("unset by default", co.get("/api/website-followup").get_json() ==
              {"email_campaign_id": None, "wa_campaign_id": None})
        r = co.put("/api/website-followup", headers=hdr, json={"email_campaign_id": mine, "wa_campaign_id": wa})
        check("an operator picks their own campaigns", r.status_code == 200 and
              co.get("/api/website-followup").get_json() == {"email_campaign_id": mine, "wa_campaign_id": wa})
        r = co.put("/api/website-followup", headers=hdr, json={"email_campaign_id": theirs})
        check("another operator's campaign is refused", r.status_code == 400, str(r.status_code))
        check("and each sees only their own choice",
              ca.get("/api/website-followup").get_json() == {"email_campaign_id": None, "wa_campaign_id": None})
        r = co.put("/api/website-followup", headers=hdr, json={"email_campaign_id": None, "wa_campaign_id": ""})
        check("either may be left unset", r.status_code == 200 and
              co.get("/api/website-followup").get_json() == {"email_campaign_id": None, "wa_campaign_id": None})

        print("\n8. LIMITS")
        app_mod._inbound_hits.clear()
        codes = [anon.post("/api/inbound/website-lead", json=LEAD, headers=web).status_code
                 for _ in range(app_mod._INBOUND_PER_MINUTE + 1)]
        check("more than the per-minute limit -> 429", codes[-1] == 429 and codes[0] == 200, str(codes[-3:]))
        app_mod._inbound_hits.clear()
        r = ca.post("/api/website-settings/key", headers=hdr)
        r2 = anon.post("/api/inbound/website-lead", json=LEAD, headers=web)
        check("rotating the key stops the old one", r2.status_code == 401, str(r2.status_code))
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
