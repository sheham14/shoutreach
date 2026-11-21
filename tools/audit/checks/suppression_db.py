"""Does an address that opted out under operator A stay suppressed when
operator B imports it afterwards? Scratch database only."""
import os, sys, tempfile
work = tempfile.mkdtemp(prefix="verify-supp-")
os.environ["DB_PATH"] = os.path.join(work, "v.db")
os.environ["SECRET_KEY"] = "x"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
import db
db.init_db()
a = db.create_user("opa", "password-123456", is_admin=True)
b = db.create_user("opb", "password-123456", is_admin=False)

def rows(email):
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, owner_id, email, status FROM email_leads WHERE email=? ORDER BY id", (email,))]

# 1. A imports, the person unsubscribes. B imports the same address AFTERWARDS.
db.upsert_businesses([{"company": "Clinic One", "email": "owner@clinic-one.ae", "mx_valid": 1}], owner_id=a)
db.unsubscribe_contact("owner@clinic-one.ae")
print("after A's unsubscribe:", rows("owner@clinic-one.ae"))
db.upsert_businesses([{"company": "Clinic One", "email": "owner@clinic-one.ae", "mx_valid": 1}], owner_id=b)
print("after B imports it later:", rows("owner@clinic-one.ae"))

# 2. Same with a hard bounce.
db.upsert_businesses([{"company": "Clinic Two", "email": "dead@clinic-two.ae", "mx_valid": 1}], owner_id=a)
db.mark_bounced("dead@clinic-two.ae")
db.upsert_businesses([{"company": "Clinic Two", "email": "dead@clinic-two.ae", "mx_valid": 1}], owner_id=b)
print("bounced under A, then imported by B:", rows("dead@clinic-two.ae"))

# 3. Would B's copy actually be picked up to send? Enrol it and ask the send loop's own query.
cid = db.create_campaign("B's campaign", owner_id=b)
db.upsert_step(cid, 1, "Hello", "<p>Hi</p>", 0)
with db.get_db() as conn:
    conn.execute("UPDATE campaigns SET status='active' WHERE id=?", (cid,))
    bids = [r["id"] for r in conn.execute("SELECT id FROM businesses WHERE owner_id=?", (b,))]
print("enrol result:", db.enroll_businesses(cid, bids, owner_id=b))
due = db.get_due_enrollments(cid, limit=20)
print("due to send from B's campaign:", [(d.get("email"), d.get("status")) for d in due])

# 4. The control: the order the tests cover (B already has it, THEN A's contact opts out).
db.upsert_businesses([{"company": "Clinic Three", "email": "x@clinic-three.ae", "mx_valid": 1}], owner_id=a)
db.upsert_businesses([{"company": "Clinic Three", "email": "x@clinic-three.ae", "mx_valid": 1}], owner_id=b)
db.unsubscribe_contact("x@clinic-three.ae")
print("control (both had it, then opt-out):", rows("x@clinic-three.ae"))

# 5. Re-import after the owner's own bulk hard delete of a bounced address.
ids = [r["id"] for r in rows("dead@clinic-two.ae") if r["owner_id"] == a]
try:
    print("bulk delete of A's bounced row:", db.delete_contacts_bulk(ids, owner_id=a))
except Exception as exc:
    print("bulk delete call failed:", type(exc).__name__, exc)
print("after bulk delete:", rows("dead@clinic-two.ae"))
