"""
sender.py — Email sending engine.

Anti-spam mechanisms built in:
  ✓ Random per-email delay (humanises send cadence)
  ✓ Business-hours enforcement (Mon–Fri, configurable window)
  ✓ Daily send cap (hard stop at campaign limit)
  ✓ Bounce rate circuit-breaker (auto-pauses campaign)
  ✓ Multipart text+HTML (better deliverability than HTML-only)
  ✓ List-Unsubscribe header (required by Gmail/Yahoo 2024 sender policy)
  ✓ Custom Message-ID for reply threading
  ✓ IMAP reply detection — stops sequence on reply
  ✓ SPF/DKIM reminder — we can't enforce it here, but README covers it
"""

import smtplib
import imaplib
import ssl
import email as emaillib
import email.message
import email.utils
import re
import time
import random
import secrets
import datetime
import hashlib
import hmac
import base64
import json
import logging
from typing import Optional

import db

logger = logging.getLogger("sender")

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

# {{name}}, {{ name }}, or {{name|fallback text}}.
#
# The fallback is what makes a scraped list usable: most rows have a company
# but no first name, and "Hi ," in the opening line is worse than no
# personalisation at all. Anything after the pipe is used literally when the
# value is missing or blank, so {{first_name|there}} reads "Hi there,".
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|([^}]*))?\}\}")


def _render(template: str, contact: dict, campaign_vars: dict = None) -> str:
    """Replace {{variable}} placeholders with contact values.

    Priority (highest wins): standard contact fields > contact extra JSON > campaign variables.

    A placeholder with no value resolves to its fallback, or to nothing. It is
    never left in the text: the old substitution only replaced keys it knew
    about, so a typo like {{firstname}} was delivered to the prospect with the
    braces still around it.
    """
    fields = {}
    # 1. Campaign variables — lowest priority
    if campaign_vars:
        fields.update(campaign_vars)
    # 2. Contact-level extra JSON fields
    try:
        extra = json.loads(contact.get("extra", "{}") or "{}")
        fields.update(extra)
    except Exception:
        pass
    # 3. Standard contact fields — highest priority
    fields.update({
        "first_name": contact.get("first_name", ""),
        "last_name":  contact.get("last_name", ""),
        "company":    contact.get("company", ""),
        "email":      contact.get("email", ""),
        "full_name":  f"{contact.get('first_name','')} {contact.get('last_name','')}".strip(),
    })

    def _resolve(match):
        key      = match.group(1)
        fallback = match.group(2)
        value    = fields.get(key)
        if value is None or not str(value).strip():
            # Stripped, so the spaced-out style people are used to from other
            # template languages -- {{ first_name | there }} -- does not render
            # as "Hi  there ," with the padding baked into the sentence.
            return fallback.strip() if fallback is not None else ""
        return str(value).strip()

    return _PLACEHOLDER_RE.sub(_resolve, template or "")


def _plain_to_html(text: str) -> str:
    """
    Convert plain-text email body to minimal HTML.
    If the text already contains HTML tags, return it unchanged.
    Wraps in a simple div that looks like a personal email, not a newsletter.
    """
    if re.search(r'<[a-z][^>]*>', text, re.IGNORECASE):
        return text  # already HTML — leave it alone
    paragraphs = text.split('\n\n')
    inner = ''.join(
        f'<p style="margin:0 0 1em 0">{p.strip().replace(chr(10), "<br>")}</p>'
        for p in paragraphs if p.strip()
    )
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        'line-height:1.6;color:#222;">'
        + inner
        + '</div>'
    )


def _html_to_text(html: str) -> str:
    """Very simple HTML → plain-text strip for the text/plain part."""
    if not re.search(r'<[a-z][^>]*>', html, re.IGNORECASE):
        return html  # already plain text
    txt = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    txt = re.sub(r'<p[^>]*>', '\n', txt, flags=re.IGNORECASE)
    txt = re.sub(r'</p>', '\n', txt, flags=re.IGNORECASE)
    txt = re.sub(r'<[^>]+>', '', txt)
    txt = re.sub(r'\n{3,}', '\n\n', txt)
    return txt.strip()


def _make_message_id(campaign_id, contact_id, step_num, from_email="outreach@example.com") -> str:
    # Use a cryptographically random token to make collisions effectively
    # impossible even at high send rates. Domain must be the sender's domain
    # so receiving MTAs accept the Message-ID as well-formed.
    unique = secrets.token_urlsafe(16)
    domain = from_email.split("@")[-1] if "@" in from_email else "shoutreach.local"
    return f"<{campaign_id}.{contact_id}.{step_num}.{unique}@{domain}>"


def _make_unsub_token(email: str) -> str:
    """HMAC-signed unsubscribe token — not forgeable without the server secret."""
    secret = db.get_or_create_secret()
    sig = hmac.new(secret.encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:20]
    encoded = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    return f"{encoded}.{sig}"


def _unsubscribe_footer(contact_email: str, settings: dict, include_link: bool = True) -> str:
    """Plain-text unsubscribe footer appended to every email."""
    address = settings.get("company_address", "").strip()
    addr_line = f"\n{address}" if address else ""
    if include_link:
        base_url = settings.get("app_base_url", "http://localhost:5000")
        token = _make_unsub_token(contact_email)
        return (
            f"\n\n---\n"
            f"To unsubscribe, click: {base_url}/unsubscribe/{token}\n"
            f"Or reply with 'unsubscribe' in the subject line."
            f"{addr_line}"
        )
    return f"\n\n---\nTo opt out of future emails, reply with 'unsubscribe'.{addr_line}"


def _unsubscribe_footer_html(contact_email: str, settings: dict) -> str:
    base_url = settings.get("app_base_url", "http://localhost:5000")
    token = _make_unsub_token(contact_email)
    address = settings.get("company_address", "").strip()
    addr_html = f"<br>{address}" if address else ""
    return (
        f'<p style="font-size:11px;color:#999;margin-top:32px;border-top:1px solid #eee;padding-top:12px;">'
        f'To unsubscribe, <a href="{base_url}/unsubscribe/{token}">click here</a>.{addr_html}</p>'
    )


# ── Business hours check ──────────────────────────────────────────────────────

def is_business_hours(campaign: dict) -> bool:
    """Returns True only if current hour is within campaign send window (Mon–Fri).
    Uses campaign timezone if set, otherwise UTC."""
    tz_name = (campaign.get("timezone") or "").strip()
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name) if tz_name else datetime.timezone.utc
    except Exception:
        tz = datetime.timezone.utc
    now = datetime.datetime.now(tz)
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    h = now.hour
    return campaign["send_start_hour"] <= h < campaign["send_end_hour"]


# ── Connection helpers ─────────────────────────────────────────────────────────

# Python's smtplib and imaplib, given no SSL context, fall back to
# ssl._create_stdlib_context() -- which sets verify_mode=CERT_NONE and
# check_hostname=False. That means no certificate validation at all: anyone on
# the network path can present any certificate and receive the SMTP/IMAP
# password in the TLS session. create_default_context() verifies both the
# chain and the hostname.
_TLS_CONTEXT = ssl.create_default_context()


def get_smtp(settings: dict):
    host = settings.get("smtp_host", "")
    port = int(settings.get("smtp_port", 587))
    user = settings.get("smtp_user", "")
    pwd  = settings.get("smtp_pass", "")

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=15, context=_TLS_CONTEXT)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
        server.ehlo()
        server.starttls(context=_TLS_CONTEXT)
        server.ehlo()

    server.login(user, pwd)
    return server


def test_smtp(settings: dict) -> tuple[bool, str]:
    try:
        srv = get_smtp(settings)
        srv.quit()
        return True, "SMTP connection successful ✓"
    except Exception as e:
        return False, f"SMTP error: {e}"


def test_imap(settings: dict) -> tuple[bool, str]:
    try:
        host = settings.get("imap_host", "")
        user = settings.get("imap_user", "")
        pwd  = settings.get("imap_pass", "")
        if not host:
            return False, "IMAP host not configured"
        M = imaplib.IMAP4_SSL(host, ssl_context=_TLS_CONTEXT, timeout=15)
        M.login(user, pwd)
        M.logout()
        return True, "IMAP connection successful ✓"
    except Exception as e:
        return False, f"IMAP error: {e}"


# ── Core send function ─────────────────────────────────────────────────────────

def send_email(
    contact: dict,
    subject_tpl: str,
    body_tpl: str,
    campaign_id: int,
    step_num: int,
    settings: dict,
    account: dict = None,
    campaign_vars: dict = None,
) -> tuple[bool, str, str]:
    """
    Send one email.

    Returns (success: bool, message_id: str, error_msg: str)
    """
    try:
        # Merge account-level SMTP settings over global settings
        cfg = settings.copy()
        if account:
            cfg.update({
                "smtp_host":       account.get("smtp_host", ""),
                "smtp_port":       account.get("smtp_port", 587),
                "smtp_user":       account.get("smtp_user", ""),
                "smtp_pass":       account.get("smtp_pass", ""),
                "smtp_from_name":  account.get("from_name", ""),
                "smtp_from_email": account.get("email", ""),
            })

        # Render templates
        subject       = _render(subject_tpl, contact, campaign_vars)
        body_rendered = _render(body_tpl, contact, campaign_vars)

        # Build HTML and plain-text parts from the rendered body
        include_unsub = cfg.get("include_unsubscribe", "1") == "1"
        body_html  = _plain_to_html(body_rendered)
        body_text  = _html_to_text(body_rendered)
        if include_unsub:
            body_html += _unsubscribe_footer_html(contact["email"], cfg)
        # Plain-text notice always stays for CAN-SPAM compliance
        body_text += _unsubscribe_footer(contact["email"], cfg, include_link=include_unsub)

        from_name  = cfg.get("smtp_from_name", "")
        from_email = cfg.get("smtp_from_email", "")
        msg_id = _make_message_id(
            campaign_id,
            contact["contact_id"] if "contact_id" in contact else 0,
            step_num,
            from_email,
        )
        from_addr  = email.utils.formataddr((from_name, from_email)) if from_name else from_email

        base_url  = cfg.get("app_base_url", "http://localhost:5000")
        unsub_url = f"{base_url}/unsubscribe/{_make_unsub_token(contact['email'])}"

        # Build MIME message
        msg = email.message.EmailMessage()
        msg["From"]         = from_addr
        msg["To"]           = contact["email"]
        msg["Subject"]      = subject
        msg["Message-ID"]   = msg_id
        msg["Date"]         = email.utils.formatdate(localtime=True)
        if include_unsub:
            msg["List-Unsubscribe"] = f"<{unsub_url}>, <mailto:{from_email}?subject=unsubscribe>"
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        msg.set_content(body_text)
        msg.add_alternative(body_html, subtype="html")

        # Send
        srv = get_smtp(cfg)
        srv.send_message(msg)
        srv.quit()

        db.log_send(
            campaign_id, contact.get("contact_id", 0), step_num, subject, msg_id,
            account_id=account["id"] if account else None,
        )
        db.add_log(f"✉ Sent step {step_num} → {contact['email']} | {subject}")

        return True, msg_id, ""

    except smtplib.SMTPRecipientsRefused:
        db.mark_bounced(contact["email"])
        db.add_log(f"⚠ Bounced: {contact['email']}", "WARN")
        return False, "", "bounce"

    except Exception as e:
        db.add_log(f"✗ Send failed → {contact['email']}: {e}", "ERROR")
        return False, "", str(e)


# ── IMAP reply detection ───────────────────────────────────────────────────────

_AUTO_REPLY_SUBJECTS = (
    "out of office", "automatic reply", "auto-reply", "autoreply",
    "on vacation", "i am away", "i'm away", "i am out", "i'm out",
    "away from the office", "on leave", "annual leave", "maternity leave",
    "currently unavailable", "will be back", "absence notification",
)


def _is_auto_reply(parsed_msg) -> bool:
    """Return True if email headers indicate an auto-reply / OOO message."""
    # RFC 3834 standard header — most reliable signal
    auto_submitted = (parsed_msg.get("Auto-Submitted") or "").lower()
    if auto_submitted and auto_submitted != "no":
        return True

    # Non-standard but common headers
    if parsed_msg.get("X-Autoreply") or parsed_msg.get("X-Auto-Response-Suppress"):
        return True

    precedence = (parsed_msg.get("Precedence") or "").lower()
    if precedence in ("auto-reply", "bulk", "junk"):
        return True

    # Subject line heuristics as a last resort
    subject = (parsed_msg.get("Subject") or "").lower()
    if any(phrase in subject for phrase in _AUTO_REPLY_SUBJECTS):
        return True

    return False


def _scan_inbox_for_replies(host: str, user: str, pwd: str):
    if not host or not user or not pwd:
        return
    try:
        M = imaplib.IMAP4_SSL(host, ssl_context=_TLS_CONTEXT, timeout=20)
        M.login(user, pwd)
        M.select("INBOX", readonly=True)

        since = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%d-%b-%Y")
        _, data = M.search(None, f'(SINCE "{since}")')
        ids = data[0].split()[-50:]

        # Build msg_id → (campaign_id, contact_id) map from the last 30 days
        # of sends. Looking up the enrollment via the threading header is
        # strictly more accurate than matching on the reply's From address —
        # a reply forwarded by a delegate, sent from an alias, or routed
        # through an auto-responder still references our original Message-ID.
        msg_id_since = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        with db.get_db() as conn:
            msg_id_to_enroll = {
                row["msg_id"].strip("<>").lower(): (row["campaign_id"], row["contact_id"])
                for row in conn.execute(
                    "SELECT msg_id, campaign_id, contact_id FROM sends "
                    "WHERE msg_id IS NOT NULL AND sent_at >= ?",
                    (msg_id_since,),
                ).fetchall()
            }

        db.add_log(
            f"Reply check: scanning {len(ids)} inbox messages, "
            f"{len(msg_id_to_enroll)} known send IDs",
            "INFO",
        )

        for num in ids:
            _, msg_data = M.fetch(num, "(RFC822.HEADER)")
            raw = msg_data[0][1] if msg_data and msg_data[0] else b""
            parsed = emaillib.message_from_bytes(raw)

            from_header = parsed.get("From", "")
            _, from_email = emaillib.utils.parseaddr(from_header)
            from_email = (from_email or "").lower().strip()

            if _is_auto_reply(parsed):
                db.add_log(f"↩ Auto-reply ignored from {from_email}", "INFO")
                continue

            # Threading headers: In-Reply-To is a single Message-ID, References
            # is the chain. Either pointing at one of our sends proves this is
            # a reply to a ShoutReach campaign.
            in_reply_to = (parsed.get("In-Reply-To") or "").strip("<>").lower()
            references = [r.strip("<>").lower() for r in (parsed.get("References") or "").split()]
            linked_ids = {in_reply_to} | set(references)
            linked_ids.discard("")

            if not linked_ids:
                continue  # no threading headers — not a reply at all

            matched = [msg_id_to_enroll[mid] for mid in linked_ids if mid in msg_id_to_enroll]
            if not matched:
                db.add_log(
                    f"Reply check: unmatched In-Reply-To from {from_email} "
                    f"(refs: {list(linked_ids)[:3]})",
                    "INFO",
                )
                continue

            # A single reply can reference multiple of our messages (the whole
            # thread chain). Dedupe to (campaign_id, contact_id) pairs.
            for cid, contact_id in set(matched):
                updated = db.mark_enrollment_replied(cid, contact_id)
                if updated:
                    db.add_log(
                        f"↩ Reply detected from {from_email} (campaign {cid}, "
                        f"contact {contact_id}) — sequence stopped",
                        "INFO",
                    )

        M.logout()
    except Exception as e:
        db.add_log(f"IMAP reply check error ({user}): {e}", "WARN")


def check_replies(settings: dict):
    """Scan all configured inboxes for replies."""
    _scan_inbox_for_replies(
        settings.get("imap_host", ""),
        settings.get("imap_user", ""),
        settings.get("imap_pass", ""),
    )
    for acct in db.get_smtp_accounts():
        if acct.get("imap_host"):
            _scan_inbox_for_replies(acct["imap_host"], acct["imap_user"], acct["imap_pass"])


# ── Bounce parser ──────────────────────────────────────────────────────────────

def _parse_bounce(msg) -> tuple:
    """
    Parse a Mail Delivery System bounce email.
    Returns (failed_email: str, is_hard: bool).

    Strategy:
      1. Look for a structured message/delivery-status MIME part (RFC 3464).
         This is the most reliable — it has Final-Recipient and Status fields.
      2. Fall back to the X-Failed-Recipients header (Exchange/Outlook bounces).
      3. Fall back to regex scan of the plain-text body near failure keywords.
    """
    failed_email = ""
    is_hard = True  # default to hard if we find an address but no status code

    # ── 1. Structured delivery-status part ────────────────────────────────────
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            payload = part.get_payload()
            if isinstance(payload, list):
                status_block = "\n".join(
                    sub.as_string() if hasattr(sub, "as_string") else str(sub)
                    for sub in payload
                )
            else:
                status_block = str(payload or "")

            m = re.search(
                r"Final-Recipient\s*:.*?;\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
                status_block, re.IGNORECASE,
            )
            if m:
                failed_email = m.group(1).lower()

            s = re.search(r"Status\s*:\s*([45])\.", status_block, re.IGNORECASE)
            if s:
                is_hard = s.group(1) == "5"

            break  # only need the first delivery-status block

    # ── 2. X-Failed-Recipients header (Exchange / Outlook) ────────────────────
    if not failed_email:
        x_failed = msg.get("X-Failed-Recipients", "")
        m = _EMAIL_RE.search(x_failed)
        if m:
            failed_email = m.group(0).lower()

    # ── 3. Regex scan of plain-text body ──────────────────────────────────────
    if not failed_email:
        body = ""
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    pass

        failure_keywords = [
            "does not exist", "no such user", "invalid address",
            "user unknown", "account.*disabled", "rejected", "undeliverable",
        ]
        if any(re.search(kw, body, re.IGNORECASE) for kw in failure_keywords):
            # The failed address is usually mentioned early in the body.
            # Skip well-known MTA domains to avoid matching relay hops.
            mta_domains = {
                "googlemail.com", "google.com", "gmail.com",
                "yahoo.com", "outlook.com", "hotmail.com",
                "amazonses.com", "sendgrid.net",
            }
            for candidate in _EMAIL_RE.findall(body):
                domain = candidate.split("@")[-1].lower()
                if domain not in mta_domains:
                    failed_email = candidate.lower()
                    break

    return failed_email, is_hard


def _scan_inbox_for_bounces(host: str, user: str, pwd: str, threshold: int):
    if not host or not user or not pwd:
        return
    try:
        M = imaplib.IMAP4_SSL(host, ssl_context=_TLS_CONTEXT, timeout=20)
        M.login(user, pwd)
        M.select("INBOX")

        since = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%d-%b-%Y")
        _, data = M.search(None, f'UNSEEN SINCE "{since}"')
        ids = data[0].split()

        for num in ids:
            # BODY.PEEK[] reads without setting \Seen. Plain "(RFC822)" marks
            # the message read as a side effect, so scanning for bounces was
            # quietly marking every unread mail in the inbox from the last 7
            # days -- fine on a dedicated sending mailbox, disruptive on one a
            # human also reads. Only confirmed bounces get flagged, below.
            _, msg_data = M.fetch(num, "(BODY.PEEK[])")
            raw = msg_data[0][1] if msg_data and msg_data[0] else b""
            parsed = emaillib.message_from_bytes(raw)

            from_hdr    = parsed.get("From", "").lower()
            subject_hdr = parsed.get("Subject", "").lower()

            bounce_senders  = ("mailer-daemon", "postmaster", "mail delivery")
            bounce_subjects = (
                "undeliverable", "delivery failed", "delivery status notification",
                "returned mail", "mail delivery failure", "failure notice",
                "delivery failure",
            )

            is_bounce = (
                any(s in from_hdr for s in bounce_senders) or
                any(s in subject_hdr for s in bounce_subjects)
            )
            if not is_bounce:
                continue

            failed_email, is_hard = _parse_bounce(parsed)
            if not failed_email:
                continue

            if is_hard:
                db.mark_bounced(failed_email)
                db.add_log(f"⛔ Hard bounce (inbox): {failed_email}", "WARN")
            else:
                db.increment_soft_bounce(failed_email, threshold)

            M.store(num, "+FLAGS", "\\Seen")

        M.logout()
    except Exception as e:
        db.add_log(f"Bounce inbox check error ({user}): {e}", "WARN")


def check_bounces(settings: dict):
    """Scan all configured inboxes for bounces."""
    threshold = int(settings.get("soft_bounce_threshold", 3))
    _scan_inbox_for_bounces(
        settings.get("imap_host", ""),
        settings.get("imap_user", ""),
        settings.get("imap_pass", ""),
        threshold,
    )
    for acct in db.get_smtp_accounts():
        if acct.get("imap_host"):
            _scan_inbox_for_bounces(acct["imap_host"], acct["imap_user"], acct["imap_pass"], threshold)


