import os, re, requests, sqlite3, tempfile
B = "http://127.0.0.1:5055"
s = requests.Session()
csrf = re.search(r'name="csrf_token" value="([^"]+)"', s.get(B + "/login").text).group(1)
s.post(B + "/login", data={"username": "sheham", "password": "audit-password-123", "csrf_token": csrf})
tok = s.get(B + "/api/csrf").json()["csrf_token"]
_db = os.path.join(os.path.join(tempfile.gettempdir(), "shoutreach-audit"), "audit.db").replace(os.sep, "/")
con = sqlite3.connect(f"file:{_db}?mode=ro", uri=True)
rows = con.execute("""SELECT cl.id, cl.business_id, cl.call_status FROM call_leads cl JOIN businesses b ON b.id=cl.business_id
                      WHERE b.owner_id=1 AND cl.call_status IN ('not_interested','wrong_number','booked') ORDER BY cl.id""").fetchall()
mx = con.execute("SELECT MAX(id) FROM call_leads").fetchone()[0]
print("closed-out leads (call_lead id, business id, status):", rows[:8], "| highest call_lead id:", mx)
for clid, bid, st in rows[:6]:
    own = con.execute("SELECT 1 FROM call_leads c JOIN businesses b ON b.id=c.business_id WHERE c.id=? AND b.owner_id=1", (bid,)).fetchone()
    r = s.post(f"{B}/api/calls/{bid}/reopen", headers={"X-CSRF-Token": tok})
    print(f"  reopen business {bid} (its call_lead is {clid}); a call_lead with id {bid} of mine exists: {bool(own)} -> {r.status_code} {r.text.strip()[:60]}")
