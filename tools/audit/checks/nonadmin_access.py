"""Verify, against the harness server, what a NON-admin can reach."""
import re, requests
B = "http://127.0.0.1:5055"
s = requests.Session()
r = s.get(B + "/login")
csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
s.post(B + "/login", data={"username": "cofounder", "password": "audit-password-123", "csrf_token": csrf})
me = s.get(B + "/api/users/me").json()
print("me:", {k: me.get(k) for k in ("username", "is_admin")})
r = s.get(B + "/api/logs")
print("GET /api/logs ->", r.status_code, "rows:", len(r.json()) if r.ok else r.text[:80])
if r.ok:
    for row in r.json()[:3]:
        print("   ", str(row.get("message"))[:110])
tok = s.get(B + "/api/csrf").json()["csrf_token"]
r = s.post(B + "/api/scheduler/run", headers={"X-CSRF-Token": tok})
print("POST /api/scheduler/run as non-admin ->", r.status_code, r.text[:80])
for path in ("/api/accounts", "/api/settings", "/api/users", "/api/db/tables", "/api/accounts/names"):
    r = s.get(B + path)
    print(f"GET {path} ->", r.status_code, (r.text[:90].replace("\n", " ")))
d = s.get(B + "/api/dashboard").json()
print("dashboard email block for cofounder:", d.get("email"))
