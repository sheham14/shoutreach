"""Sending-account credential handling.

Run:  python tests/test_accounts.py

The bug these pin: the Test SMTP / Test IMAP buttons sent only the account id,
so the server tested whatever was already SAVED. Pasting a new app password and
pressing Test reported "authentication failed" for the OLD password -- the new
one never left the browser -- and a brand-new account could not be tested at
all before saving. A correct credential looked rejected.

No network: email_sender.test_smtp / test_imap are replaced with capture stubs,
so what is asserted is which credentials the route resolved, not whether a real
mail server accepts them.
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
    os.environ["DB_PATH"] = os.path.join(work, "accounts_test.db")
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


def post(client, path, payload=None):
    return client.post(path, json=payload or {}, headers={"X-CSRF-Token": "test-csrf"})


def main():
    work = tempfile.mkdtemp(prefix="shoutreach-accounts-")
    try:
        db, app_mod, client = boot(work)

        seen = {}

        def fake_smtp(cfg):
            seen.clear(); seen.update(cfg)
            return True, "stub ok"

        def fake_imap(cfg):
            seen.clear(); seen.update(cfg)
            return True, "stub ok"

        app_mod.email_sender.test_smtp = fake_smtp
        app_mod.email_sender.test_imap = fake_imap

        aid = db.create_smtp_account({
            "name": "Primary", "email": "me@hexiv.co", "from_name": "Me",
            "smtp_host": "smtp.zoho.com", "smtp_port": 587,
            "smtp_user": "me@hexiv.co", "smtp_pass": "OLD-stored-password",
            "imap_host": "imap.zoho.com", "imap_user": "me@hexiv.co",
            "imap_pass": "OLD-stored-password",
        })

        print("\n1. A PASTED PASSWORD IS THE ONE TESTED")
        r = post(client, "/api/accounts/test-smtp", {
            "id": aid, "smtp_host": "smtp.zoho.com", "smtp_port": 587,
            "smtp_user": "me@hexiv.co", "smtp_pass": "NEW-app-password",
        })
        check("route succeeds", r.status_code == 200, f"got {r.status_code}")
        check("the NEW password is what gets tested",
              seen.get("smtp_pass") == "NEW-app-password", seen.get("smtp_pass"))
        check("not the stale saved one",
              seen.get("smtp_pass") != "OLD-stored-password")

        print("\n2. AN UNTOUCHED FIELD FALLS BACK TO THE SAVED PASSWORD")
        r = post(client, "/api/accounts/test-smtp", {
            "id": aid, "smtp_host": "smtp.zoho.com", "smtp_port": 587,
            "smtp_user": "me@hexiv.co", "smtp_pass": app_mod._SECRET_PLACEHOLDER,
        })
        check("the mask is never sent to the mail server",
              seen.get("smtp_pass") != app_mod._SECRET_PLACEHOLDER, seen.get("smtp_pass"))
        check("the stored password is used instead",
              seen.get("smtp_pass") == "OLD-stored-password", seen.get("smtp_pass"))

        print("\n3. A BRAND-NEW ACCOUNT CAN BE TESTED BEFORE SAVING")
        r = post(client, "/api/accounts/test-smtp", {
            "smtp_host": "smtp.zoho.com", "smtp_port": 465,
            "smtp_user": "new@hexiv.co", "smtp_pass": "fresh-app-password",
        })
        check("no id required", r.status_code == 200, f"got {r.status_code}")
        check("its own credentials are used",
              seen.get("smtp_user") == "new@hexiv.co"
              and seen.get("smtp_pass") == "fresh-app-password", str(seen))
        check("the port is carried through", seen.get("smtp_port") == 465,
              str(seen.get("smtp_port")))

        print("\n4. IMAP BEHAVES THE SAME")
        r = post(client, "/api/accounts/test-imap", {
            "id": aid, "imap_host": "imap.zoho.com",
            "imap_user": "me@hexiv.co", "imap_pass": "NEW-app-password",
        })
        check("the pasted IMAP password is tested",
              seen.get("imap_pass") == "NEW-app-password", seen.get("imap_pass"))
        r = post(client, "/api/accounts/test-imap", {
            "id": aid, "imap_host": "imap.zoho.com",
            "imap_user": "me@hexiv.co", "imap_pass": app_mod._SECRET_PLACEHOLDER,
        })
        check("an untouched IMAP field falls back to the saved one",
              seen.get("imap_pass") == "OLD-stored-password", seen.get("imap_pass"))

        print("\n5. INCOMPLETE INPUT IS REFUSED, NOT SENT TO THE SERVER")
        r = post(client, "/api/accounts/test-smtp", {"smtp_user": "x@y.co", "smtp_pass": "p"})
        check("a missing host is rejected", r.status_code == 400, f"got {r.status_code}")
        r = post(client, "/api/accounts/test-smtp",
                 {"smtp_host": "smtp.zoho.com", "smtp_user": "x@y.co"})
        check("a missing password is rejected", r.status_code == 400, f"got {r.status_code}")
        r = post(client, "/api/accounts/test-smtp", {
            "smtp_host": "smtp.zoho.com", "smtp_port": "not-a-number",
            "smtp_user": "x@y.co", "smtp_pass": "p",
        })
        check("a junk port falls back to 587 rather than erroring",
              r.status_code == 200 and seen.get("smtp_port") == 587, str(seen.get("smtp_port")))

        print("\n6. SAVING STILL DOES NOT CLOBBER A PASSWORD WITH THE MASK")
        client.put(f"/api/accounts/{aid}", json={
            "name": "Renamed", "smtp_pass": app_mod._SECRET_PLACEHOLDER,
            "imap_pass": app_mod._SECRET_PLACEHOLDER,
        }, headers={"X-CSRF-Token": "test-csrf"})
        acct = db.get_smtp_account(aid)
        check("the real password survives an edit that did not touch it",
              acct["smtp_pass"] == "OLD-stored-password", acct["smtp_pass"])
        check("and so does the IMAP one", acct["imap_pass"] == "OLD-stored-password")
        check("the rest of the edit applied", acct["name"] == "Renamed", acct["name"])

        print("\n7. A BLANK PASSWORD FIELD MEANS 'UNCHANGED', NOT 'CLEAR IT'")
        # The edit form leaves the password blank rather than prefilling the
        # mask, so an unrelated edit must not wipe the credentials.
        client.put(f"/api/accounts/{aid}", json={
            "name": "Renamed again", "smtp_pass": "", "imap_pass": "",
        }, headers={"X-CSRF-Token": "test-csrf"})
        acct = db.get_smtp_account(aid)
        check("a blank SMTP field keeps the stored password",
              acct["smtp_pass"] == "OLD-stored-password", acct["smtp_pass"])
        check("a blank IMAP field keeps the stored password",
              acct["imap_pass"] == "OLD-stored-password", acct["imap_pass"])
        check("the edit itself still applied", acct["name"] == "Renamed again")

        # ...but a real new password must still get through.
        client.put(f"/api/accounts/{aid}", json={"smtp_pass": "ROTATED-password"},
                   headers={"X-CSRF-Token": "test-csrf"})
        check("a typed password does replace the old one",
              db.get_smtp_account(aid)["smtp_pass"] == "ROTATED-password")

        print("\n8. PAUSING AN ACCOUNT ACTUALLY STOPS IT SENDING")
        cid = db.create_campaign("Paused-account campaign")
        db.set_campaign_smtp_accounts(cid, [aid])
        check("an active account is picked as the sender",
              (db.get_next_account_for_campaign(cid) or {}).get("id") == aid)

        r = client.put(f"/api/accounts/{aid}", json={"status": "paused"},
                       headers={"X-CSRF-Token": "test-csrf"})
        check("the pause is accepted", r.status_code == 200, f"got {r.status_code}")
        check("status is stored", db.get_smtp_account(aid)["status"] == "paused")
        check("a paused account is never handed out as a sender",
              db.get_next_account_for_campaign(cid) is None,
              str(db.get_next_account_for_campaign(cid)))
        check("pausing did not disturb the credentials",
              db.get_smtp_account(aid)["smtp_pass"] == "ROTATED-password")

        client.put(f"/api/accounts/{aid}", json={"status": "active"},
                   headers={"X-CSRF-Token": "test-csrf"})
        check("reactivating brings it back",
              (db.get_next_account_for_campaign(cid) or {}).get("id") == aid)

        print("\n9. THE TEST ROUTES ARE ADMIN-ONLY")
        anon = app_mod.app.test_client()
        for path in ("/api/accounts/test-smtp", "/api/accounts/test-imap"):
            code = anon.post(path, json={}).status_code
            check(f"{path} refuses an unauthenticated caller", code in (401, 403),
                  f"got {code}")

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
