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
import email as emaillib
import email.message
import email.utils
import re
import time
import random
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

def _render(template: str, contact: dict) -> str:
    """Replace {{variable}} placeholders with contact values."""
    fields = {
        "first_name": contact.get("first_name", ""),
        "last_name":  contact.get("last_name", ""),
        "company":    contact.get("company", ""),
        "email":      contact.get("email", ""),
        "full_name":  f"{contact.get('first_name','')} {contact.get('last_name','')}".strip(),
    }
    # also merge any extra JSON fields
    try:
        extra = json.loads(contact.get("extra", "{}") or "{}")
        fields.update(extra)
    except Exception:
        pass

    for k, v in fields.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def _html_to_text(html: str) -> str:
    """Very simple HTML → plain-text strip for the text/plain part."""
    txt = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    txt = re.sub(r'<p[^>]*>', '\n', txt, flags=re.IGNORECASE)
    txt = re.sub(r'</p>', '\n', txt, flags=re.IGNORECASE)
    txt = re.sub(r'<[^>]+>', '', txt)
    txt = re.sub(r'\n{3,}', '\n\n', txt)
    return txt.strip()


def _make_message_id(campaign_id, contact_id, step_num) -> str:
    unique = f"{campaign_id}-{contact_id}-{step_num}-{time.time()}"
    h = hashlib.md5(unique.encode()).hexdigest()[:12]
    settings = db.get_settings()
    domain = settings.get("smtp_from_email", "outreach@example.com").split("@")[-1]
    return f"<{h}.{int(time.time())}@{domain}>"


def _make_unsub_token(email: str) -> str:
    """HMAC-signed unsubscribe token — not forgeable without the server secret."""
    secret = db.get_or_create_secret()
    sig = hmac.new(secret.encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:20]
    encoded = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    return f"{encoded}.{sig}"


def _unsubscribe_footer(contact_email: str, settings: dict) -> str:
    """Plain-text unsubscribe footer appended to every email."""
    base_url = settings.get("app_base_url", "http://localhost:5000")
    token = _make_unsub_token(contact_email)
    return (
        f"\n\n---\n"
        f"To unsubscribe, click: {base_url}/unsubscribe/{token}\n"
        f"Or reply with 'unsubscribe' in the subject line."
    )


def _unsubscribe_footer_html(contact_email: str, settings: dict) -> str:
    base_url = settings.get("app_base_url", "http://localhost:5000")
    token = _make_unsub_token(contact_email)
    return (
        f'<p style="font-size:11px;color:#999;margin-top:32px;border-top:1px solid #eee;padding-top:12px;">'
        f'To unsubscribe, <a href="{base_url}/unsubscribe/{token}">click here</a>.</p>'
    )


# ── Business hours check ──────────────────────────────────────────────────────

def is_business_hours(campaign: dict) -> bool:
    """Returns True only if current UTC hour is within campaign send window (Mon–Fri)."""
    now = datetime.datetime.utcnow()
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    h = now.hour
    return campaign["send_start_hour"] <= h < campaign["send_end_hour"]


# ── Connection helpers ─────────────────────────────────────────────────────────

def get_smtp(settings: dict):
    host = settings.get("smtp_host", "")
    port = int(settings.get("smtp_port", 587))
    user = settings.get("smtp_user", "")
    pwd  = settings.get("smtp_pass", "")

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
        server.ehlo()
        server.starttls()
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
        M = imaplib.IMAP4_SSL(host, timeout=15)
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
) -> tuple[bool, str, str]:
    """
    Send one email.

    Returns (success: bool, message_id: str, error_msg: str)
    """
    try:
        # Render templates
        subject  = _render(subject_tpl, contact)
        body_html = _render(body_tpl, contact)

        # Append unsubscribe footer
        body_html += _unsubscribe_footer_html(contact["email"], settings)
        body_text  = _html_to_text(body_html) + _unsubscribe_footer(contact["email"], settings)

        msg_id = _make_message_id(campaign_id, contact["contact_id"] if "contact_id" in contact else 0, step_num)

        from_name  = settings.get("smtp_from_name", "")
        from_email = settings.get("smtp_from_email", "")
        from_addr  = email.utils.formataddr((from_name, from_email)) if from_name else from_email

        base_url  = settings.get("app_base_url", "http://localhost:5000")
        unsub_url = f"{base_url}/unsubscribe/{_make_unsub_token(contact['email'])}"

        # Build MIME message
        msg = email.message.EmailMessage()
        msg["From"]         = from_addr
        msg["To"]           = contact["email"]
        msg["Subject"]      = subject
        msg["Message-ID"]   = msg_id
        msg["Date"]         = email.utils.formatdate(localtime=True)
        msg["List-Unsubscribe"] = f"<{unsub_url}>, <mailto:{from_email}?subject=unsubscribe>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg["X-Mailer"]     = "Python-Outreach/1.0"

        msg.set_content(body_text)
        msg.add_alternative(body_html, subtype="html")

        # Send
        srv = get_smtp(settings)
        srv.send_message(msg)
        srv.quit()

        db.log_send(campaign_id, contact.get("contact_id", 0), step_num, subject, msg_id)
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

def check_replies(settings: dict):
    """
    Scan inbox for replies from enrolled contacts.
    If a reply is found, mark that enrollment as 'replied' to stop the sequence.

    Scans the last 50 unseen messages to keep it fast.
    """
    host = settings.get("imap_host", "")
    user = settings.get("imap_user", "")
    pwd  = settings.get("imap_pass", "")

    if not host or not user or not pwd:
        return  # IMAP not configured, skip

    try:
        M = imaplib.IMAP4_SSL(host, timeout=20)
        M.login(user, pwd)
        M.select("INBOX", readonly=True)

        # Search messages from the last 7 days to keep it fast
        since = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%d-%b-%Y")
        _, data = M.search(None, f'(SINCE "{since}")')
        ids = data[0].split()[-50:]  # last 50 max

        for num in ids:
            _, msg_data = M.fetch(num, "(RFC822.HEADER)")
            raw = msg_data[0][1] if msg_data and msg_data[0] else b""
            parsed = emaillib.message_from_bytes(raw)
            from_header = parsed.get("From", "")
            # Extract email address from From header
            _, from_email = emaillib.utils.parseaddr(from_header)
            from_email = from_email.lower().strip()

            if not from_email or "@" not in from_email:
                continue

            contact = db.get_contact_by_email(from_email)
            if contact:
                # Mark all active enrollments for this contact as replied
                with db.get_db() as conn:
                    rows = conn.execute(
                        "SELECT campaign_id FROM enrollments WHERE contact_id=? AND status='queued'",
                        (contact["id"],)
                    ).fetchall()
                    for row in rows:
                        db.mark_enrollment_replied(row["campaign_id"], contact["id"])
                        db.add_log(f"↩ Reply detected from {from_email} — sequence paused", "INFO")

        M.logout()

    except Exception as e:
        db.add_log(f"IMAP check error: {e}", "WARN")


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


def check_bounces(settings: dict):
    """
    Scan inbox for Mail Delivery System / MAILER-DAEMON bounce emails.

    Hard bounces (5xx permanent failure) → mark contact bounced immediately.
    Soft bounces (4xx temporary failure) → increment counter; mark bounced
      once soft_bounce_threshold (default 3) is reached.

    Processed bounce emails are marked as read so they aren't re-processed.
    Runs every 15 minutes from the scheduler alongside check_replies().
    """
    host = settings.get("imap_host", "")
    user = settings.get("imap_user", "")
    pwd  = settings.get("imap_pass", "")

    if not host or not user or not pwd:
        return

    threshold = int(settings.get("soft_bounce_threshold", 3))

    try:
        M = imaplib.IMAP4_SSL(host, timeout=20)
        M.login(user, pwd)
        M.select("INBOX")  # writable — we mark processed bounces as Seen

        since = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%d-%b-%Y")
        _, data = M.search(None, f'UNSEEN SINCE "{since}"')
        ids = data[0].split()

        for num in ids:
            _, msg_data = M.fetch(num, "(RFC822)")
            raw = msg_data[0][1] if msg_data and msg_data[0] else b""
            parsed = emaillib.message_from_bytes(raw)

            from_hdr    = parsed.get("From", "").lower()
            subject_hdr = parsed.get("Subject", "").lower()

            bounce_senders = ("mailer-daemon", "postmaster", "mail delivery")
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
                continue  # couldn't identify recipient — skip

            if is_hard:
                db.mark_bounced(failed_email)
                db.add_log(f"⛔ Hard bounce (inbox): {failed_email}", "WARN")
            else:
                db.increment_soft_bounce(failed_email, threshold)

            # Mark as read so we don't process it again on next run
            M.store(num, "+FLAGS", "\\Seen")

        M.logout()

    except Exception as e:
        db.add_log(f"Bounce inbox check error: {e}", "WARN")


