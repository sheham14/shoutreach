"""Cross-channel duplicate confirmation.

Run:  python tests/test_cross_channel.py

Identity is shared (one `businesses` row), but presence on a channel is not --
a clinic already mid-sequence on email should not silently become a call lead
too. This pins the "confirm before proceeding" behaviour: a row or business id
that resolves to a business already active on a different channel is held
back rather than attached, reported to the caller, and only goes through on an
explicit confirm_conflicts resubmission. The unattended scrape worker is the
one caller exempt from this -- there is nobody there to confirm anything.
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
    os.environ["DB_PATH"] = os.path.join(work, "xchan.db")
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
    return db, app_mod, client, token


def api(client, token, method, url, **kw):
    fn = getattr(client, method)
    headers = kw.pop("headers", {})
    headers["X-CSRF-Token"] = token
    r = fn(url, headers=headers, **kw)
    return r.status_code, r.get_json()


def main():
    work = tempfile.mkdtemp(prefix="xchan-")
    try:
        db, app_mod, client, token = boot(work)

        print("\n1. DB LAYER: get_channel_presence")
        db.upsert_businesses([{"email": "a@onlyemail.ca", "company": "Only Email"}])
        db.upsert_businesses([{"company": "Only Call", "phone": "709-555-0001"}])
        email_biz = db.get_email_lead_by_email("a@onlyemail.ca")["business_id"]
        with db.get_db() as conn:
            call_biz = conn.execute(
                "SELECT id FROM businesses WHERE name='Only Call'").fetchone()["id"]
            db.get_or_create_call_lead(conn, call_biz)
            presence = db.get_channel_presence(conn, [email_biz, call_biz])
        check("email-only business shows email presence",
              presence[email_biz] == {"email": True, "call": False, "whatsapp": False},
              str(presence[email_biz]))
        check("call-only business shows call presence",
              presence[call_biz] == {"email": False, "call": True, "whatsapp": False},
              str(presence[call_biz]))

        print("\n2. find_cross_channel_conflicts SEES THROUGH TO THE BUSINESS")
        # This clinic already has calling activity; importing its email should
        # be flagged, not silently attached.
        conflicts = db.find_cross_channel_conflicts(
            [{"email": "front@onlycall.ca", "company": "Only Call", "phone": "709-555-0001"}],
            channel="email",
        )
        check("a business already on calling is flagged for an email import",
              len(conflicts) == 1 and conflicts[0]["business_id"] == call_biz,
              str(conflicts))
        check("the OTHER channel is named, not the one being imported into",
              conflicts[0]["channels"] == ["call"], str(conflicts[0]["channels"]))

        # Re-importing on the SAME channel is not a conflict -- it's a normal
        # update, not a cross-channel duplicate.
        conflicts = db.find_cross_channel_conflicts(
            [{"email": "a@onlyemail.ca", "company": "Only Email"}], channel="email",
        )
        check("re-importing the same channel is not a conflict", conflicts == [], str(conflicts))

        # A genuinely new business is never a conflict.
        conflicts = db.find_cross_channel_conflicts(
            [{"email": "new@brandnew.ca", "company": "Brand New"}], channel="email",
        )
        check("a brand-new business has nothing to conflict with", conflicts == [])

        print("\n3. channel_conflicts_for_businesses (the call-campaign side)")
        conflicts = db.channel_conflicts_for_businesses([email_biz, call_biz], channel="call")
        check("only the email-active business is flagged for a calling add",
              [c["business_id"] for c in conflicts] == [email_biz], str(conflicts))
        check("its OTHER channel is named", conflicts[0]["channels"] == ["email"])

        print("\n4. HTTP: /api/contacts/import HOLDS BACK A CONFLICTING ROW")
        s, body = api(client, token, "post", "/api/contacts/import", json={"rows": [
            {"email": "front@onlycall.ca", "company": "Only Call", "phone": "709-555-0001"},
            {"email": "clean@fresh.ca", "company": "Fresh Co", "phone": "709-555-9999"},
        ]})
        check("the request still succeeds", s == 200, str(s))
        check("exactly one row is held back", len(body["conflicts"]) == 1, str(body["conflicts"]))
        check("held back for the right reason",
              body["conflicts"][0]["channels"] == ["Calling"], str(body["conflicts"]))
        check("the clean row was imported immediately", body["inserted"] == 1, str(body))
        s, page = api(client, token, "get", "/api/contacts?per_page=10")
        check("the flagged address is NOT in Contacts yet",
              all(r["email"] != "front@onlycall.ca" for r in page["rows"]),
              str([r["email"] for r in page["rows"]]))
        check("the clean one is",
              any(r["email"] == "clean@fresh.ca" for r in page["rows"]))

        print("\n5. CONFIRMING THE RESUBMISSION LETS IT THROUGH")
        s, body2 = api(client, token, "post", "/api/contacts/import", json={
            "rows": [body["conflicts"][0]["row"]], "confirm_conflicts": True,
        })
        check("the confirmed resubmission succeeds", s == 200 and body2["inserted"] == 1,
              str(body2))
        check("no conflicts on the confirmed pass", body2["conflicts"] == [], str(body2))
        s, page = api(client, token, "get", "/api/contacts?per_page=10")
        check("the address is in Contacts now",
              any(r["email"] == "front@onlycall.ca" for r in page["rows"]))

        print("\n6. THE SCRAPE WORKER IS EXEMPT")
        worker_key = db.get_or_create_worker_api_key()
        db.upsert_businesses([{"company": "Worker Call Co", "phone": "709-555-0002"}])
        with db.get_db() as conn:
            wbiz = conn.execute(
                "SELECT id FROM businesses WHERE name='Worker Call Co'").fetchone()["id"]
            db.get_or_create_call_lead(conn, wbiz)
        r = client.post("/api/contacts/import", json={"rows": [
            {"email": "auto@workercall.ca", "company": "Worker Call Co", "phone": "709-555-0002"},
        ]}, headers={"X-API-Key": worker_key})
        wbody = r.get_json()
        check("the worker's import is never held back",
              r.status_code == 200 and wbody["inserted"] == 1 and wbody["conflicts"] == [],
              str(wbody))

        print("\n7. HTTP: ADDING TO A CALL CAMPAIGN HOLDS BACK AN EMAIL-ACTIVE BUSINESS")
        # Fresh businesses here rather than reusing email_biz/call_biz from
        # earlier sections -- section 5 deliberately gave call_biz an email
        # too, so by now it is genuinely active on both and reusing it would
        # be testing yesterday's state, not this section's setup.
        db.upsert_businesses([{"email": "c@onlyemail3.ca", "company": "Only Email 3"}])
        db.upsert_businesses([{"company": "Only Call 2", "phone": "709-555-0003"}])
        email_biz3 = db.get_email_lead_by_email("c@onlyemail3.ca")["business_id"]
        with db.get_db() as conn:
            call_biz2 = conn.execute(
                "SELECT id FROM businesses WHERE name='Only Call 2'").fetchone()["id"]
            db.get_or_create_call_lead(conn, call_biz2)

        s, camp = api(client, token, "post", "/api/call-campaigns", json={"name": "Batch"})
        cid = camp["id"]
        s, body = api(client, token, "post", f"/api/call-campaigns/{cid}/members",
                     json={"contact_ids": [email_biz3, call_biz2]})
        check("the request succeeds", s == 200, str(s))
        check("the email-active business is held back",
              len(body["conflicts"]) == 1 and body["conflicts"][0]["business_id"] == email_biz3,
              str(body))
        check("the already-on-calling business was added directly",
              body["added"] == 1, str(body))

        s, confirmed = api(client, token, "post", f"/api/call-campaigns/{cid}/members", json={
            "contact_ids": [c["business_id"] for c in body["conflicts"]],
            "confirm_conflicts": True,
        })
        check("confirming adds the held-back business too", confirmed["added"] == 1,
              str(confirmed))

        s, campaigns = api(client, token, "get", "/api/call-campaigns")
        total = next(c["total"] for c in campaigns if c["id"] == cid)
        check("both businesses ended up in the campaign", total == 2, str(total))

        print("\n8. CREATING A CAMPAIGN WITH INITIAL LEADS USES THE SAME CHECK")
        db.upsert_businesses([{"email": "b@onlyemail2.ca", "company": "Only Email 2"}])
        biz2 = db.get_email_lead_by_email("b@onlyemail2.ca")["business_id"]
        s, body = api(client, token, "post", "/api/call-campaigns",
                     json={"name": "Batch 2", "contact_ids": [biz2]})
        check("the new campaign holds back the email-active business",
              len(body["conflicts"]) == 1 and body["added"] == 0, str(body))

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
