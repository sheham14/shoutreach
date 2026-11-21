"""Does the background scheduler thread survive a database that stays locked
for longer than its 10-second wait? Scratch database; nothing is sent."""
import os
import sqlite3
import sys
import tempfile
import threading
import time

work = tempfile.mkdtemp(prefix="verify-thread-")
os.environ["DB_PATH"] = os.path.join(work, "v.db")
os.environ["SECRET_KEY"] = "x"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
import db
assert db.DB_PATH.endswith("v.db")
db.init_db()
alice = db.create_user("alice", "password-123456", is_admin=True)
import scheduler
import sender

sender.send_email = lambda **k: (True, "<id>", None)
sender.is_business_hours = lambda c: True
scheduler.email_checking_enabled = lambda: False
# One campaign whose pass raises (see B-6), so the loop's own error handler
# has to write an Activity-log line.
cid = db.create_campaign("c", owner_id=alice)
db.update_campaign(cid, daily_limit="lots", status="active")

lock = sqlite3.connect(db.DB_PATH, check_same_thread=False, isolation_level=None)
lock.execute("BEGIN IMMEDIATE")          # another writer holds the database
t = threading.Thread(target=scheduler._run_loop, daemon=True)
started = time.time()
t.start()
t.join(timeout=16)
print(f"after {time.time() - started:.0f}s with the database held by another writer: scheduler thread alive = {t.is_alive()}")
lock.execute("COMMIT")
lock.close()
time.sleep(2)
print("after the lock is released: scheduler thread alive =", t.is_alive())
print("is_running() says:", scheduler.is_running() if hasattr(scheduler, "is_running") else "n/a")
scheduler._stop_event.set()
scheduler._wake_event.set()
