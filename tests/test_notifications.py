"""Telling the team (Plan 2 package 7).

Run:  python tests/test_notifications.py     (exits 0 on pass, 1 on failure)

An operator who gives a notification address gets one plain email when a
website enquiry is filed for them and when a booking is made: the subject and
the business's name, nothing else. The inbound routes only queue it; the
scheduler's pass sends it through a sending account. The real SMTP sender is
never used here -- a recorder stands in for it.
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
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


def main():
    work = tempfile.mkdtemp(prefix="notify_")
    try:
        path = os.path.join(work, "n.db")
        os.environ["DB_PATH"] = path
        os.environ["SECRET_KEY"] = "test-secret"
        import db
        importlib.reload(db)
        db.init_db()
        sheham = db.create_user("sheham", "test-password-123", is_admin=True)
        anas = db.create_user("anas", "test-password-123", is_admin=False)
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True

        def client_for(uid):
            c = app_mod.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = uid
                s["csrf_token"] = "t"
            return c
        cs, ca = client_for(sheham), client_for(anas)
        hdr = {"X-CSRF-Token": "t"}
        sent = []

        def fake_send(to, subject, body, settings, account):
            sent.append((to, subject, body, account["email"]))

        print("\n1. EACH OPERATOR'S OWN ADDRESS")
        check("unset by default", ca.get("/api/notify-email").get_json() == {"email": ""})
        r = ca.put("/api/notify-email", headers=hdr, json={"email": "anas@hexiv.co"})
        check("an operator sets theirs", r.status_code == 200 and ca.get("/api/notify-email").get_json()["email"] == "anas@hexiv.co")
        check("and only theirs", cs.get("/api/notify-email").get_json() == {"email": ""})
        check("a bad address is refused", ca.put("/api/notify-email", headers=hdr, json={"email": "nope"}).status_code == 400)

        print("\n2. AN ENQUIRY AND A BOOKING ARE QUEUED, NOT SENT, BY THE ROUTES")
        db.save_website_config({"sites": {"hexiv.co.uk": {"owner_id": anas, "country": "GB"}}})
        key = db.rotate_website_key()
        lead = {"site": "hexiv.co.uk", "name": "Sam Booker", "email": "sam.booker@example.com",
                "business": "Booker Dental", "phone": "07700 900123",
                "answers": {"Biggest challenge": "Private answer"}, "summary": "private summary"}
        anon = app_mod.app.test_client()
        anon.post("/api/inbound/website-lead", headers={"X-API-Key": key}, json=lead)
        anon.post("/api/inbound/website-lead", headers={"X-API-Key": key}, json={**lead, "summary": "again"})
        rows = q(path, "SELECT owner_id, to_email, subject, sent_at FROM team_notifications")
        check("one email queued for the enquiry, not one per post",
              rows == [(anas, "anas@hexiv.co", "New website enquiry: Booker Dental", None)], str(rows))
        check("nothing was sent by the route", sent == [])
        secret = db.rotate_cal_secret()
        with open(os.path.join(HERE, "fixtures", "cal_booking_created.json"), encoding="utf-8") as f:
            raw = f.read().encode()
        anon.post("/api/inbound/cal", data=raw, headers={
            "X-Cal-Signature-256": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()})
        check("the booking queues one more",
              [r[0] for r in q(path, "SELECT subject FROM team_notifications ORDER BY id")]
              == ["New website enquiry: Booker Dental", "Booked on Cal.com: Booker Dental"])

        print("\n3. THE SCHEDULER'S PASS SENDS THEM")
        counts = db.run_team_notifications(send=fake_send)
        check("with no sending account, nothing is sent and the reason is kept",
              counts == {"sent": 0, "failed": 0} and sent == []
              and q(path, "SELECT DISTINCT error FROM team_notifications") == [("No active sending account",)])
        db.create_smtp_account({"name": "main", "email": "hello@hexiv.co", "smtp_host": "smtp.invalid",
                                "smtp_user": "hello@hexiv.co", "smtp_pass": "x"})
        counts = db.run_team_notifications(send=fake_send)
        check("with one, both are sent through it", counts["sent"] == 2 and len(sent) == 2
              and all(s[0] == "anas@hexiv.co" and s[3] == "hello@hexiv.co" for s in sent), str(sent))
        everything = json.dumps(sent)
        check("subject and business name only: no answers, summary, email or phone",
              not any(x in everything for x in ("Private answer", "private summary", "sam.booker", "07700", "Sam ")),
              everything[:200])
        db.run_team_notifications(send=fake_send)
        check("each is sent once", len(sent) == 2)

        print("   a business name with line breaks (review item 9)")
        with db.get_db() as conn:
            conn.execute("INSERT INTO businesses(owner_id, name) VALUES(?, ?)", (anas, "Bad\r\nBcc: x@evil.example\nName"))
            odd = conn.execute("SELECT MAX(id) FROM businesses").fetchone()[0]
        db.queue_team_notification(anas, "enquiry", odd)
        subj = q(path, "SELECT subject FROM team_notifications ORDER BY id DESC LIMIT 1")[0][0]
        check("the subject is one line", "\n" not in subj and "\r" not in subj
              and subj == "New website enquiry: Bad Bcc: x@evil.example Name", subj)
        import sender as real_sender
        importlib.reload(real_sender)
        captured = {}

        class Capture:
            def sendmail(self, frm, to, text):
                captured["text"] = text

            def quit(self):
                pass
        real_sender.get_smtp = lambda cfg: Capture()
        db.run_team_notifications(send=real_sender.send_notification)
        check("and the real sender sends it (it used to fail five times and stop)",
              "New website enquiry: Bad Bcc" in captured.get("text", "")
              and q(path, "SELECT sent_at IS NOT NULL FROM team_notifications ORDER BY id DESC LIMIT 1") == [(1,)])
        check("no header was injected", "\nBcc:" not in captured.get("text", ""))

        print("\n4. FAILURES ARE RETRIED, THEN LEFT")
        db.queue_team_notification(anas, "enquiry", q(path, "SELECT id FROM businesses")[0][0])

        def broken(*a):
            raise OSError("connection refused")
        for _ in range(7):
            db.run_team_notifications(send=broken)
        check("a failure is kept with its reason, tried five times at most",
              q(path, "SELECT attempts, error, sent_at FROM team_notifications ORDER BY id DESC LIMIT 1")
              == [(5, "connection refused", None)])

        print("\n5. NO ADDRESS, NO EMAIL; THE SCHEDULER RUNS IT SAFELY")
        before = q(path, "SELECT COUNT(*) FROM team_notifications")[0][0]
        db.save_website_config({"sites": {"hexiv.co.uk": {"owner_id": sheham, "country": "GB"}}})
        anon.post("/api/inbound/website-lead", headers={"X-API-Key": key},
                  json={**lead, "email": "other@example.com", "business": "Other Clinic", "phone": "07700 900999"})
        check("an operator with no address gets nothing queued",
              q(path, "SELECT COUNT(*) FROM team_notifications")[0][0] == before)
        import scheduler
        importlib.reload(scheduler)
        check("the pass calls it", "run_team_notifications()" in open(scheduler.__file__, encoding="utf-8").read().split("def _one_pass")[1])
        real = db.run_team_notifications
        db.run_team_notifications = lambda send=None: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            scheduler.run_team_notifications()
            check("a failing job can't stop the loop", True)
        except Exception as exc:
            check("a failing job can't stop the loop", False, repr(exc))
        finally:
            db.run_team_notifications = real

        print("\n6. THE REAL SENDER BUILDS A PLAIN EMAIL (fake SMTP server)")
        import sender
        importlib.reload(sender)
        got = {}

        class FakeSMTP:
            def sendmail(self, frm, to, text):
                got.update(frm=frm, to=to, text=text)

            def quit(self):
                got["quit"] = True
        sender.get_smtp = lambda cfg: (got.update(host=cfg.get("smtp_host")) or FakeSMTP())
        sender.send_notification("anas@hexiv.co", "Booked on Cal.com: Booker Dental", "Body line\n",
                                 {}, {"email": "hello@hexiv.co", "smtp_host": "smtp.example"})
        check("through the account's server, from its address, to the operator",
              got.get("host") == "smtp.example" and got.get("frm") == "hello@hexiv.co"
              and got.get("to") == ["anas@hexiv.co"] and got.get("quit"))
        check("plain text with the subject, and no unsubscribe footer",
              "Subject: Booked on Cal.com: Booker Dental" in got.get("text", "")
              and "text/plain" in got.get("text", "") and "unsubscribe" not in got.get("text", "").lower())
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
