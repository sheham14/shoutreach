"""Re-prove the security reviewer's serious findings. Scratch database only;
the only network traffic is to a server this script starts on 127.0.0.1."""
import http.server
import os
import sys
import tempfile
import threading

work = tempfile.mkdtemp(prefix="verify-sec-")
os.environ["DB_PATH"] = os.path.join(work, "v.db")
os.environ["SECRET_KEY"] = "x"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
import scheduler
_real_start = scheduler.start
scheduler.start = lambda *a, **k: None
import db
assert db.DB_PATH.endswith("v.db")
db.init_db()
admin = db.create_user("alice", "password-123456", is_admin=True)
bob = db.create_user("bob", "password-123456", is_admin=False)
carol = db.create_user("carol", "password-123456", is_admin=True)
import app as app_mod
app_mod.app.config["TESTING"] = True; app_mod.app.config["PROPAGATE_EXCEPTIONS"] = False
H = {"X-CSRF-Token": "t"}


def client_for(uid, is_admin):
    c = app_mod.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
        s["is_admin"] = is_admin
        s["csrf_token"] = "t"
    return c


ca, cb, cc = client_for(admin, True), client_for(bob, False), client_for(carol, True)


def leads(email):
    with db.get_db() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT owner_id, status FROM email_leads WHERE email=? ORDER BY id", (email,))]


print("\n== S-3  a deleted account's old session")
r = ca.delete(f"/api/users/{carol}", headers=H)
print("alice deletes carol (an admin):", r.status_code, r.get_json())
print("carol still exists:", db.get_user_by_id(carol) is not None if hasattr(db, "get_user_by_id") else "n/a")
r = cc.get("/api/users")
print("carol's old session lists users:", r.status_code, [u["username"] for u in (r.get_json() or [])] if r.status_code == 200 else r.get_json())
r = cc.post(f"/api/users/{bob}/password", json={"password": "carol-set-this-1234"}, headers=H)
print("carol's old session resets bob's password:", r.status_code, r.get_json())
r = cb.get("/api/users/me")
print("bob's old session after his password was changed:", r.status_code, (r.get_json() or {}).get("username"))

print("\n== S-1a  restore from Recently deleted after the opt-out")
cb.post("/api/contacts/import", json={"rows": [{"company": "Trash Clinic", "email": "t@trash-clinic.ae", "mx_valid": 1}]}, headers=H)
cid = cb.post("/api/campaigns", json={"name": "bob camp"}, headers=H).get_json()["id"]
cb.post(f"/api/campaigns/{cid}/steps", json={"step_num": 1, "subject": "Hi", "body_html": "<p>Hi</p>", "delay_days": 0}, headers=H)
print("enrol:", cb.post(f"/api/campaigns/{cid}/contacts", json={"all": True}, headers=H).get_json().get("enrolled"))
with db.get_db() as conn:
    bid = conn.execute("SELECT id FROM businesses WHERE name='Trash Clinic'").fetchone()["id"]
print("delete the lead:", cb.post("/api/businesses/delete", json={"business_ids": [bid]}, headers=H).get_json())
import sender
r = app_mod.app.test_client().post(f"/unsubscribe/{sender._make_unsub_token('t@trash-clinic.ae')}")
print("recipient unsubscribes while it's in the trash:", r.status_code, "| rows now:", leads("t@trash-clinic.ae"))
trash = cb.get("/api/trash").get_json()
group = (trash.get("groups") or trash if isinstance(trash, dict) else trash)
gkey = None
try:
    gkey = (trash.get("groups") or [])[0].get("group") or (trash.get("groups") or [])[0].get("key")
except Exception:
    pass
print("trash payload keys:", list(trash.keys()) if isinstance(trash, dict) else type(trash), "| first group:", str((trash.get("groups") or [None])[0])[:160] if isinstance(trash, dict) else "")
with db.get_db() as conn:
    drow = conn.execute("SELECT * FROM deleted_leads LIMIT 1").fetchone()
    dkeys = drow.keys()
print("deleted_leads columns:", dkeys)
res = cb.post("/api/trash/restore", json={"ids": [drow["id"]]}, headers=H).get_json()
print("restore:", res, "| rows now:", leads("t@trash-clinic.ae"))
print("on the send loop's due list:", [d["email"] for d in db.get_due_enrollments(cid)])

print("\n== S-1c  the owner flips an unsubscribed address back to active")
cb.post("/api/contacts/import", json={"rows": [{"company": "Flip Clinic", "email": "f@flip-clinic.ae", "mx_valid": 1}]}, headers=H)
app_mod.app.test_client().post(f"/unsubscribe/{sender._make_unsub_token('f@flip-clinic.ae')}")
with db.get_db() as conn:
    lid = conn.execute("SELECT id FROM email_leads WHERE email='f@flip-clinic.ae'").fetchone()["id"]
print("after opt-out:", leads("f@flip-clinic.ae"))
r = cb.put(f"/api/contacts/{lid}", json={"status": "active"}, headers=H)
print("PUT status=active:", r.status_code, r.get_json(), "| rows now:", leads("f@flip-clinic.ae"))

print("\n== S-6  one campaign's bad setting and the shared send loop")
r = cb.patch(f"/api/campaigns/{cid}", json={"daily_limit": "lots"}, headers=H)
print("bob PATCHes daily_limit='lots':", r.status_code, r.get_json())
with db.get_db() as conn:
    print("stored as:", repr(conn.execute("SELECT daily_limit FROM campaigns WHERE id=?", (cid,)).fetchone()[0]))
    conn.execute("UPDATE campaigns SET status='active' WHERE id=?", (cid,))
    conn.execute("DELETE FROM logs")
fn = next((getattr(scheduler, n) for n in ("_run_cycle", "run_once", "_tick", "run_send_cycle", "process_campaigns", "_send_cycle") if hasattr(scheduler, n)), None)
print("scheduler entry points:", [n for n in dir(scheduler) if not n.startswith("__") and callable(getattr(scheduler, n))][:25])

print("\n== S-10  a backslash in a business name and booking")
cb.post("/api/contacts/import", json={"rows": [{"company": "AC\\DC Dental", "phone": "050 111 2222", "mx_valid": 1}]}, headers=H)
with db.get_db() as conn:
    b2 = conn.execute("SELECT id FROM businesses WHERE name LIKE 'AC%'").fetchone()["id"]
r = cb.put(f"/api/businesses/{b2}/pipeline", json={"stage": "booked", "channel": "whatsapp", "next_action_at": "2026-12-01T10:00"}, headers=H)
print("book it:", r.status_code, (r.get_json() or {}) if r.status_code != 500 else "500 Internal Server Error")
print("stage saved anyway:", db.get_business(b2, owner_id=bob)["pipeline_stage"] if "owner_id" in db.get_business.__code__.co_varnames else db.get_business(b2)["pipeline_stage"],
      "| meeting row:", db.get_meeting(b2))

print("\n== S-4  the audit fetching an address on this machine")
hits = []


class Hd(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        hits.append(self.path)
        body = b"<html><head><title>INTERNAL ADMIN PANEL</title></head><body>secret</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), Hd)
threading.Thread(target=srv.serve_forever, daemon=True).start()
import audit
out = audit.scan_site(f"http://127.0.0.1:{srv.server_port}/latest/meta-data/")
print("scan_site on a loopback address:", {k: out.get(k) for k in ("ok", "status", "title", "final_url")}, "| server saw:", hits)
srv.shutdown()
