"""S-6: does one operator's malformed campaign setting stop the shared send
loop for the other operator? Scratch database; send_email is replaced, so
nothing leaves this machine."""
import os
import sys
import tempfile

work = tempfile.mkdtemp(prefix="verify-sched-")
os.environ["DB_PATH"] = os.path.join(work, "v.db")
os.environ["SECRET_KEY"] = "x"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
import scheduler
scheduler.start = lambda *a, **k: None
import db
assert db.DB_PATH.endswith("v.db")
db.init_db()
alice = db.create_user("alice", "password-123456", is_admin=True)
bob = db.create_user("bob", "password-123456", is_admin=False)
import sender

sent = []


def fake_send(contact, **kw):
    sent.append(contact["email"])
    db.log_send(kw["campaign_id"], contact["email_lead_id"], kw["step_num"], "s", f"<{len(sent)}@x>")
    return True, "<id>", None


sender.send_email = fake_send
sender.is_business_hours = lambda campaign: True
scheduler.time.sleep = lambda s: None
db.create_smtp_account({"name": "a", "email": "a@x.example", "smtp_host": "smtp.invalid", "smtp_user": "a", "smtp_pass": "p"})


def campaign(owner, name, email):
    db.upsert_businesses([{"company": name, "email": email, "mx_valid": 1}], owner_id=owner)
    cid = db.create_campaign(name, owner_id=owner, min_delay=0, max_delay=0)
    db.upsert_step(cid, 1, "Hi", "<p>Hi</p>", 0)
    with db.get_db() as conn:
        bids = [r["id"] for r in conn.execute("SELECT id FROM businesses WHERE owner_id=? AND name=?", (owner, name))]
        conn.execute("UPDATE campaigns SET status='active', send_days='0,1,2,3,4,5,6', send_start_hour=0, send_end_hour=24 WHERE id=?", (cid,))
    db.enroll_businesses(cid, bids, owner_id=owner)
    return cid


order = [c["name"] for c in db.get_campaigns(all_owners=True)]
c_bob = campaign(bob, "Bob campaign", "b@bob-lead.ae")
c_alice = campaign(alice, "Alice campaign", "a@alice-lead.ae")
print("loop order:", [(c["id"], c["name"]) for c in db.get_campaigns(all_owners=True)])

print("\n-- control: both campaigns healthy")
scheduler.process_queue()
print("sent:", sent)

# reset and break Bob's
with db.get_db() as conn:
    conn.execute("DELETE FROM sends")
    conn.execute("DELETE FROM daily_counts")
    conn.execute("UPDATE enrollments SET status='queued', current_step=1, next_send_at=NULL")
    conn.execute("DELETE FROM logs")
sent.clear()
try:
    db.update_campaign(c_bob, daily_limit="lots")
except ValueError as e:  # refused at save since Plan 1 package 4; plant it as an old row would hold it
    print("saving daily_limit='lots' is refused:", e)
    with db.get_db() as conn:
        conn.execute("UPDATE campaigns SET daily_limit='lots' WHERE id=?", (c_bob,))
print("\n-- Bob's campaign now has daily_limit='lots'")
scheduler.process_queue()
print("sent:", sent)
print("log:", [r["message"][:110] for r in db.get_logs(5)])
