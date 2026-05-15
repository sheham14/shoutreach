"""
app.py — Flask web application.
Run with:  python app.py
Then open: http://localhost:5000
"""

import csv
import io
import json
import base64
import hashlib
import hmac
import logging
import os
import sys
import threading
from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, make_response, Response
)

import db
import scheduler
import sender as email_sender

try:
    import gmaps_email_scraper as _scraper_mod
    _SCRAPER_AVAILABLE = True
except ImportError:
    _scraper_mod = None
    _SCRAPER_AVAILABLE = False

_scraper_job: object = None
_scraper_thread: threading.Thread = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)

app = Flask(__name__)

# ── Startup ───────────────────────────────────────────────────────────────────

db.init_db()
app.secret_key = os.environ.get("SECRET_KEY") or db.get_or_create_secret()

if not os.environ.get("ADMIN_PASS"):
    print(
        "\n  ⚠  No ADMIN_PASS set — dashboard is unprotected.\n"
        "     Set ADMIN_PASS (and optionally ADMIN_USER) to enable login.\n",
        file=sys.stderr,
    )


@app.before_request
def _ensure_scheduler():
    """Start the scheduler on first request if it isn't already running."""
    if not scheduler.is_running():
        scheduler.start()


@app.before_request
def _require_login():
    """HTTP Basic Auth gate. Skips the public unsubscribe endpoint."""
    admin_pass = os.environ.get("ADMIN_PASS", "")
    if not admin_pass:
        return  # No password configured — local/dev mode, allow all
    if request.path.startswith("/unsubscribe"):
        return  # Unsubscribe links must stay publicly accessible
    auth = request.authorization
    admin_user = os.environ.get("ADMIN_USER", "admin")
    if not auth or auth.username != admin_user or auth.password != admin_pass:
        return Response(
            "Outreach System — authentication required.\n"
            "Set ADMIN_PASS (and optionally ADMIN_USER) environment variables.",
            401,
            {"WWW-Authenticate": 'Basic realm="Outreach System"'},
        )


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


def _verify_unsub_token(token: str):
    """Verify the HMAC-signed unsubscribe token. Returns the email or None."""
    try:
        parts = token.rsplit(".", 1)
        if len(parts) != 2:
            return None
        encoded, sig = parts
        rem = len(encoded) % 4
        if rem:
            encoded += "=" * (4 - rem)
        email_addr = base64.urlsafe_b64decode(encoded.encode()).decode()
        secret = db.get_or_create_secret()
        expected = hmac.new(
            secret.encode(), email_addr.lower().encode(), hashlib.sha256
        ).hexdigest()[:20]
        if hmac.compare_digest(sig, expected):
            return email_addr
        return None
    except Exception:
        return None


@app.route("/unsubscribe/<token>")
def unsubscribe(token):
    email_addr = _verify_unsub_token(token)
    if not email_addr:
        return make_response("<p>Invalid or expired unsubscribe link.</p>", 400)
    db.unsubscribe_contact(email_addr)
    db.add_log(f"Unsubscribed: {email_addr}")
    return make_response(
        "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
        "<h2>✓ You've been unsubscribed</h2>"
        "<p>You won't receive any more emails from this sender.</p>"
        "</body></html>",
        200,
    )


# ── API: Settings ─────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    s = db.get_settings()
    # Never return the raw password to the frontend
    safe = {k: ("●●●●●●" if "pass" in k else v) for k, v in s.items()}
    return jsonify(safe)


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.json or {}
    # Don't overwrite passwords if the placeholder was sent back
    existing = db.get_settings()
    filtered = {}
    for k, v in data.items():
        if "pass" in k and v == "●●●●●●":
            filtered[k] = existing.get(k, "")
        else:
            filtered[k] = v
    db.save_settings(filtered)
    return jsonify({"ok": True})


@app.route("/api/settings/test-smtp", methods=["POST"])
def api_test_smtp():
    settings = db.get_settings()
    ok, msg = email_sender.test_smtp(settings)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/settings/test-imap", methods=["POST"])
def api_test_imap():
    settings = db.get_settings()
    ok, msg = email_sender.test_imap(settings)
    return jsonify({"ok": ok, "message": msg})


# ── API: Stats ────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_stats())


@app.route("/api/stats/<int:cid>")
def api_campaign_stats(cid):
    return jsonify(db.get_stats(cid))


# ── API: Campaigns ────────────────────────────────────────────────────────────

@app.route("/api/campaigns", methods=["GET"])
def api_get_campaigns():
    campaigns = db.get_campaigns()
    # Attach step count and contact count to each
    for c in campaigns:
        steps = db.get_steps(c["id"])
        stats = db.get_stats(c["id"])
        c["step_count"]    = len(steps)
        c["contact_count"] = stats["total"]
        c["sent_count"]    = stats["sent"]
        c["reply_rate"]    = stats["reply_rate"]
    return jsonify(campaigns)


@app.route("/api/campaigns", methods=["POST"])
def api_create_campaign():
    d = request.json or {}
    cid = db.create_campaign(
        name        = d.get("name", "New Campaign"),
        daily_limit = int(d.get("daily_limit", 30)),
        start_hour  = int(d.get("send_start_hour", 9)),
        end_hour    = int(d.get("send_end_hour", 17)),
        min_delay   = int(d.get("min_delay_secs", 45)),
        max_delay   = int(d.get("max_delay_secs", 120)),
    )
    return jsonify({"ok": True, "id": cid})


@app.route("/api/campaigns/<int:cid>", methods=["GET"])
def api_get_campaign(cid):
    c = db.get_campaign(cid)
    if not c:
        return jsonify({"error": "Not found"}), 404
    c["steps"]    = db.get_steps(cid)
    c["stats"]    = db.get_stats(cid)
    c["contacts"] = db.get_campaign_contacts(cid)
    return jsonify(c)


@app.route("/api/campaigns/<int:cid>", methods=["PATCH"])
def api_update_campaign(cid):
    d = request.json or {}
    db.update_campaign(cid, **d)
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:cid>/activate", methods=["POST"])
def api_activate(cid):
    steps = db.get_steps(cid)
    if not steps:
        return jsonify({"ok": False, "error": "Add at least one sequence step first"}), 400
    settings = db.get_settings()
    if not settings.get("smtp_host"):
        return jsonify({"ok": False, "error": "Configure SMTP settings first"}), 400
    db.update_campaign(cid, status="active")
    db.add_log(f"▶ Campaign {cid} activated")
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:cid>/pause", methods=["POST"])
def api_pause(cid):
    db.update_campaign(cid, status="paused")
    db.add_log(f"⏸ Campaign {cid} paused")
    return jsonify({"ok": True})


# ── API: Steps ────────────────────────────────────────────────────────────────

@app.route("/api/campaigns/<int:cid>/steps", methods=["GET"])
def api_get_steps(cid):
    return jsonify(db.get_steps(cid))


@app.route("/api/campaigns/<int:cid>/steps", methods=["POST"])
def api_upsert_step(cid):
    d = request.json or {}
    db.upsert_step(
        campaign_id = cid,
        step_num    = int(d["step_num"]),
        subject     = d.get("subject", ""),
        body_html   = d.get("body_html", ""),
        delay_days  = int(d.get("delay_days", 0)),
    )
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:cid>/steps/<int:step_num>", methods=["DELETE"])
def api_delete_step(cid, step_num):
    db.delete_step(cid, step_num)
    return jsonify({"ok": True})


# ── API: Contacts ─────────────────────────────────────────────────────────────

@app.route("/api/contacts", methods=["GET"])
def api_get_contacts():
    return jsonify(db.get_contacts(limit=500))


@app.route("/api/contacts/import", methods=["POST"])
def api_import_contacts():
    """
    Accepts JSON body: { "rows": [...] }
    or multipart form with a CSV file field named 'file'.
    """
    if request.content_type and "multipart" in request.content_type:
        f = request.files.get("file")
        if not f:
            return jsonify({"ok": False, "error": "No file"}), 400
        content = f.read().decode("utf-8", errors="replace")
        reader  = csv.DictReader(io.StringIO(content))
        rows    = list(reader)
    else:
        data = request.json or {}
        rows = data.get("rows", [])

    if not rows:
        return jsonify({"ok": False, "error": "No rows"}), 400

    inserted = db.upsert_contacts(rows)
    return jsonify({"ok": True, "inserted": inserted})


@app.route("/api/contacts", methods=["POST"])
def api_add_contact():
    d = request.json or {}
    contact_id, err = db.create_contact(
        email      = d.get('email', ''),
        first_name = d.get('first_name', ''),
        last_name  = d.get('last_name', ''),
        company    = d.get('company', ''),
        website    = d.get('website', ''),
        address    = d.get('address', ''),
        status     = d.get('status', 'active'),
    )
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    return jsonify({'ok': True, 'id': contact_id})


@app.route("/api/contacts/<int:cid>", methods=["PUT"])
def api_update_contact(cid):
    d = request.json or {}
    ok, err = db.update_contact(cid, d)
    if not ok:
        return jsonify({'ok': False, 'error': err}), 400
    return jsonify({'ok': True})


@app.route("/api/contacts/<int:cid>", methods=["DELETE"])
def api_delete_contact(cid):
    db.delete_contact(cid)
    return jsonify({'ok': True})


@app.route("/api/campaigns/<int:cid>/contacts", methods=["POST"])
def api_enroll_contacts(cid):
    """
    Enroll contacts from the global contacts pool into a campaign.
    Body: { "contact_ids": [1, 2, 3] }  OR  { "all": true }
    """
    d = request.json or {}

    if d.get("all"):
        with db.get_db() as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM contacts WHERE status='active'"
            ).fetchall()]
    else:
        ids = d.get("contact_ids", [])

    if not ids:
        return jsonify({"ok": False, "error": "No contact IDs"}), 400

    enrolled = db.enroll_contacts_bulk(cid, ids)
    return jsonify({"ok": True, "enrolled": enrolled})


@app.route("/api/campaigns/<int:cid>/contacts", methods=["GET"])
def api_campaign_contacts(cid):
    return jsonify(db.get_campaign_contacts(cid))


# ── API: Logs ─────────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    return jsonify(db.get_logs(100))


# ── API: Database viewer ───────────────────────────────────────────────────────

_VIEWER_TABLES = [
    "contacts", "campaigns", "enrollments", "sends",
    "steps", "daily_counts", "logs", "settings",
]
# Keys in the settings table that must never be shown in plaintext
_MASKED_KEYS = {"smtp_pass", "imap_pass", "_secret_key"}


@app.route("/api/db/tables")
def api_db_tables():
    result = []
    with db.get_db() as conn:
        for t in _VIEWER_TABLES:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                result.append({"name": t, "count": count})
            except Exception:
                pass
    return jsonify(result)


@app.route("/api/db/table/<name>")
def api_db_table(name):
    if name not in _VIEWER_TABLES:
        return jsonify({"error": "Table not allowed"}), 403

    page     = max(1, int(request.args.get("page", 1)))
    per_page = 50
    offset   = (page - 1) * per_page
    q        = request.args.get("q", "").strip()

    with db.get_db() as conn:
        columns = [
            d[0] for d in
            conn.execute(f"SELECT * FROM {name} LIMIT 0").description
        ]

        sort_col = request.args.get("sort_col", "").strip()
        sort_dir = request.args.get("sort_dir", "desc").lower()
        if sort_dir not in ("asc", "desc"):
            sort_dir = "desc"
        if sort_col in columns:
            order_by = f'"{sort_col}" {sort_dir.upper()}'
        else:
            sort_col = ""
            order_by = "rowid DESC"

        if q:
            conditions = " OR ".join(f'CAST("{col}" AS TEXT) LIKE ?' for col in columns)
            params     = [f"%{q}%" for _ in columns]
            where      = f"WHERE {conditions}"
        else:
            params = []
            where  = ""

        total = conn.execute(
            f"SELECT COUNT(*) FROM {name} {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT * FROM {name} {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()

        data = []
        for row in rows:
            r = dict(row)
            if name == "settings" and r.get("key") in _MASKED_KEYS:
                r["value"] = "●●●●●●"
            data.append(r)

    return jsonify({
        "columns":  columns,
        "rows":     data,
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "sort_col": sort_col,
        "sort_dir": sort_dir,
    })


@app.route("/api/db/table/<name>/export")
def api_db_table_export(name):
    if name not in _VIEWER_TABLES:
        return jsonify({"error": "Table not allowed"}), 403

    with db.get_db() as conn:
        cursor  = conn.execute(f"SELECT * FROM {name} ORDER BY rowid DESC")
        columns = [d[0] for d in cursor.description]
        rows    = cursor.fetchall()

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(columns)
    for row in rows:
        r = dict(row)
        if name == "settings" and r.get("key") in _MASKED_KEYS:
            r["value"] = "●●●●●●"
        w.writerow([r.get(col, "") for col in columns])

    resp = make_response(buf.getvalue())
    resp.headers["Content-Disposition"] = f"attachment; filename={name}.csv"
    resp.headers["Content-Type"] = "text/csv"
    return resp


# ── API: Scheduler status ──────────────────────────────────────────────────────

@app.route("/api/scheduler/status")
def api_scheduler_status():
    return jsonify({"running": scheduler.is_running()})


@app.route("/api/scheduler/trigger", methods=["POST"])
def api_scheduler_trigger():
    """Manually trigger one queue processing cycle (useful for testing)."""
    scheduler.process_queue()
    return jsonify({"ok": True, "message": "Queue processed"})


# ── API: Scraper ──────────────────────────────────────────────────────────────

@app.route("/api/scraper/status")
def api_scraper_status():
    if not _scraper_job:
        return jsonify({"status": "idle"})
    return jsonify({
        "status":   _scraper_job.status,
        "progress": _scraper_job.progress,
        "total":    _scraper_job.total,
        "found":    _scraper_job.found,
        "imported": _scraper_job.imported,
        "logs":     _scraper_job.logs[-80:],
    })


@app.route("/api/scraper/start", methods=["POST"])
def api_scraper_start():
    global _scraper_job, _scraper_thread
    if not _SCRAPER_AVAILABLE:
        return jsonify({"ok": False, "error":
            "Playwright not installed. Run: pip install playwright playwright-stealth && playwright install chromium"
        }), 501
    if _scraper_thread and _scraper_thread.is_alive():
        return jsonify({"ok": False, "error": "Scraper is already running"}), 409

    d           = request.json or {}
    niche       = d.get("niche", "").strip()
    city        = d.get("city", "").strip()
    max_results = int(d.get("max_results", 50))
    auto_import = bool(d.get("auto_import", True))

    if not niche or not city:
        return jsonify({"ok": False, "error": "Niche and city are required"}), 400

    _scraper_job    = _scraper_mod.ScraperJob(niche, city, max_results, auto_import)
    _scraper_thread = threading.Thread(
        target=_scraper_mod.run_scraper_job,
        args=(_scraper_job,),
        daemon=True,
        name="scraper",
    )
    _scraper_thread.start()
    return jsonify({"ok": True})


@app.route("/api/scraper/stop", methods=["POST"])
def api_scraper_stop():
    if _scraper_job:
        _scraper_job.stop()
    return jsonify({"ok": True})


@app.route("/api/scraper/resume", methods=["POST"])
def api_scraper_resume():
    if _scraper_job:
        _scraper_job.resume()
    return jsonify({"ok": True})


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "─" * 60)
    print("  📬  Outreach System")
    print("  ─────────────────────────────────────────────")
    print("  Dashboard: http://localhost:5000")
    print("  Press Ctrl+C to stop")
    print("─" * 60 + "\n")
    app.run(
        debug=True,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5000)),
        use_reloader=True,
    )
