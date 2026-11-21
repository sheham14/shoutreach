"""Signing out really signs out (audit B-3).

Run:  python tests/test_sessions.py     (exits 0 on pass, 1 on failure)

A sign-in used to be a signed cookie the server trusted without looking the
user up, with "is admin" read from the cookie too. Deleting an account or
changing its password left an open session working. Now every request loads
the user row: a deleted user is refused, a cookie whose session version is
behind the account's is refused, and admin comes from the row.
"""
import importlib
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SHOUTREACH_NO_SCHEDULER"] = "1"

_failures = []
PW = "test-password-123"


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def main():
    work = tempfile.mkdtemp(prefix="sessions_")
    try:
        path = os.path.join(work, "sessions.db")
        os.environ["DB_PATH"] = path
        os.environ["SECRET_KEY"] = "test-secret"
        os.environ["SHOUTREACH_INSECURE_COOKIES"] = "1"
        import db
        importlib.reload(db)
        db.init_db()
        alice = db.create_user("alice", PW, is_admin=True)
        bob = db.create_user("bob", PW, is_admin=False)
        carol = db.create_user("carol", PW, is_admin=True)
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True

        def sign_in(name, password=PW):
            """A real sign-in through the login form."""
            c = app_mod.app.test_client()
            c.get("/login")
            with c.session_transaction() as s:
                token = s.get("csrf_token", "")
            c.post("/login", data={"username": name, "password": password, "csrf_token": token})
            with c.session_transaction() as s:
                tok = s.get("csrf_token", "")
            return c, {"X-CSRF-Token": tok}

        print("\n1. A DELETED ACCOUNT'S SESSION IS REFUSED")
        ca, ha = sign_in("alice")
        cc, hc = sign_in("carol")
        check("carol is signed in", cc.get("/api/users").status_code == 200)
        r = ca.delete(f"/api/users/{carol}", headers=ha)
        check("alice deletes carol", r.status_code == 200, str(r.status_code))
        r = cc.get("/api/users")
        check("carol's open session now gets 401", r.status_code == 401, str(r.status_code))
        r = cc.post(f"/api/users/{bob}/password", headers=hc, json={"password": "x" * 14})
        check("and can't reset anyone's password", r.status_code in (401, 403), str(r.status_code))
        with cc.session_transaction() as s:
            check("the stale session is cleared", "user_id" not in s)
        r = cc.get("/")
        check("a page request is sent to sign in", r.status_code == 302 and "/login" in r.location)

        print("\n2. A PASSWORD CHANGED BY SOMEONE ELSE SIGNS THE OLD SESSION OUT")
        cb, hb = sign_in("bob")
        check("bob is signed in", cb.get("/api/users/me").get_json().get("id") == bob)
        r = ca.post(f"/api/users/{bob}/password", headers=ha, json={"password": "new-password-456"})
        check("alice resets bob's password", r.status_code == 200, str(r.status_code))
        check("bob's old session gets 401", cb.get("/api/users/me").status_code == 401)
        check("alice's own session is untouched", ca.get("/api/users").status_code == 200)
        cb, hb = sign_in("bob", "new-password-456")
        check("bob signs in again with the new one", cb.get("/api/users/me").status_code == 200)

        print("\n3. CHANGING YOUR OWN PASSWORD KEEPS THIS SESSION, ENDS THE OTHERS")
        phone, hp = sign_in("alice")
        r = ca.post(f"/api/users/{alice}/password", headers=ha,
                    json={"password": "alice-new-pass-789", "current_password": PW})
        check("alice changes her own password", r.status_code == 200, str(r.status_code))
        check("the device she did it on stays signed in", ca.get("/api/users").status_code == 200)
        check("her other session is signed out", phone.get("/api/users").status_code == 401)

        print("\n4. A DEMOTED ADMIN LOSES ADMIN ON THE NEXT REQUEST")
        dave = db.create_user("dave", PW, is_admin=True)
        cd, hd = sign_in("dave")
        check("dave is an admin", cd.get("/api/users").status_code == 200)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE users SET is_admin=0 WHERE id=?", (dave,))
        conn.commit()
        conn.close()
        check("demoted: the next request is refused", cd.get("/api/users").status_code == 403)
        check("/api/users/me says so", cd.get("/api/users/me").get_json().get("is_admin") is False)
        check("but he is still signed in", cd.get("/api/users/me").status_code == 200)

        print("\n5. A COOKIE THAT CLAIMS ADMIN ISN'T BELIEVED")
        frank = db.create_user("frank", PW, is_admin=False)
        forged = app_mod.app.test_client()
        with forged.session_transaction() as s:
            s["user_id"] = frank
            s["is_admin"] = True
            s["csrf_token"] = "t"
        check("a hand-made session with is_admin=True for a non-admin gets 403",
              forged.get("/api/users").status_code == 403)

        print("\n6. A SESSION FROM BEFORE VERSIONS EXISTED STILL WORKS")
        erin = db.create_user("erin", PW, is_admin=False)
        old = app_mod.app.test_client()
        with old.session_transaction() as s:
            s["user_id"] = erin
            s["csrf_token"] = "t"
        check("no 'sv' in the cookie, version 0 on the account: allowed",
              old.get("/api/users/me").status_code == 200)
        db.change_password(erin, "erin-new-password-1")
        check("until the password changes", old.get("/api/users/me").status_code == 401)

        print("\n7. RE-HASHING A PASSWORD AT SIGN-IN DOESN'T SIGN ANYONE OUT")
        v = db.get_user_by_id(alice)["session_version"]
        db.change_password(alice, "alice-new-pass-789", end_sessions=False)
        check("end_sessions=False leaves the version alone",
              db.get_user_by_id(alice)["session_version"] == v)
        check("and alice's session keeps working", ca.get("/api/users").status_code == 200)

        print("\n8. reset_password.py SIGNS THE ACCOUNT OUT TOO")
        src = open(os.path.join(os.path.dirname(db.__file__), "reset_password.py")).read()
        check("it uses db.change_password, which bumps the version",
              "db.change_password(" in src and "end_sessions=False" not in src)
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
