"""B-1 through the real routes: operator A's contact unsubscribes by the
public link's own route; operator B then imports the address through
/api/contacts/import and enrols it through the campaign route. Scratch DB."""
import os, sys, tempfile, importlib, json
work = tempfile.mkdtemp(prefix="verify-route-")
os.environ["DB_PATH"] = os.path.join(work, "v.db")
os.environ["SECRET_KEY"] = "x"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
import scheduler
scheduler.start = lambda *a, **k: None
import db
db.init_db()
a = db.create_user("opa", "password-123456", is_admin=True)
b = db.create_user("opb", "password-123456", is_admin=False)
import app as app_mod
app_mod.app.config["TESTING"] = True

def client_for(uid, admin):
    c = app_mod.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid; s["is_admin"] = admin; s["csrf_token"] = "t"
    return c
H = {"X-CSRF-Token": "t"}
ca, cb = client_for(a, True), client_for(b, False)

row = {"company": "Clinic One", "email": "owner@clinic-one.ae", "mx_valid": 1}
r = ca.post("/api/contacts/import", json={"rows": [row]}, headers=H); print("A imports:", r.status_code, r.get_json())
# the recipient clicks the unsubscribe link in A's email
import sender
tok = sender._make_unsub_token("owner@clinic-one.ae")
r = app_mod.app.test_client().post(f"/unsubscribe/{tok}"); print("unsubscribe link:", r.status_code)
def rows():
    with db.get_db() as conn:
        return [tuple(x) for x in conn.execute("SELECT owner_id, email, status FROM email_leads ORDER BY id")]
print("after opt-out:", rows())
r = cb.post("/api/contacts/import", json={"rows": [row]}, headers=H); print("B imports later:", r.status_code, json.dumps(r.get_json())[:300])
print("now:", rows())
r = cb.post("/api/campaigns", json={"name": "B campaign"}, headers=H); cid = r.get_json().get("id"); print("B campaign:", r.status_code, cid)
r = cb.post(f"/api/campaigns/{cid}/steps", json={"step_num": 1, "subject": "Hi", "body_html": "<p>Hi</p>", "delay_days": 0}, headers=H); print("step:", r.status_code)
r = cb.post(f"/api/campaigns/{cid}/contacts", json={"all": True}, headers=H); print("enrol all:", r.status_code, r.get_json())
r = cb.post(f"/api/campaigns/{cid}/activate", headers=H); print("activate:", r.status_code, r.get_json())
print("send loop's due list:", [(d["email"]) for d in db.get_due_enrollments(cid)])
# and what the meeting-invite path (which DOES check) says about the same address
print("address_suppression says:", repr(db.address_suppression("owner@clinic-one.ae")))
