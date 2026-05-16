"""
scheduler.py — Background job engine.

Runs two recurring jobs:
  1. process_queue()  — every 60s  — sends due emails with random human-like delays
  2. check_replies()  — every 15m  — scans IMAP for replies, pauses sequences

Anti-spam protections enforced here:
  ✓ Business hours gate       — no sends outside Mon-Fri configured window
  ✓ Daily cap hard stop       — stops when campaign daily_limit reached
  ✓ Random inter-email delay  — 45–120s (configurable) between sends
  ✓ Bounce rate circuit-breaker — auto-pauses campaign if bounce rate > threshold
  ✓ Reply detection            — stops sequences for contacts who replied
"""

import time
import random
import datetime
import logging
import threading

import db
import sender as email_sender

logger = logging.getLogger("scheduler")

_scheduler_thread: threading.Thread = None
_stop_event = threading.Event()


# ── Main queue processor ──────────────────────────────────────────────────────

def process_queue():
    """
    Core loop: called every ~60 seconds.
    For each active campaign, finds contacts due for their next send,
    checks all gates, then fires emails with random delays.
    """
    try:
        settings  = db.get_settings()
        campaigns = db.get_campaigns()
        today_total = db.get_today_count()

        # Global daily cap across all campaigns (from settings, default 200)
        global_daily_cap = int(settings.get("global_daily_cap", 200))
        if today_total >= global_daily_cap:
            db.add_log(f"⛔ Global daily cap ({global_daily_cap}) reached — pausing all sends", "WARN")
            return

        for campaign in campaigns:
            if campaign["status"] != "active":
                continue

            cid = campaign["id"]

            # ── Business hours gate ──────────────────────────────────────────
            if not email_sender.is_business_hours(campaign):
                continue

            # ── Per-campaign daily cap ───────────────────────────────────────
            today_campaign_count = _get_campaign_today_count(cid)
            if today_campaign_count >= campaign["daily_limit"]:
                continue

            # ── Bounce rate circuit-breaker ──────────────────────────────────
            bounce_rate = db.get_bounce_rate(cid)
            if bounce_rate >= campaign["bounce_pause_pct"]:
                db.update_campaign(cid, status="paused")
                db.add_log(
                    f"⛔ Campaign '{campaign['name']}' AUTO-PAUSED — "
                    f"bounce rate {bounce_rate:.1f}% exceeded threshold "
                    f"{campaign['bounce_pause_pct']}%",
                    "ERROR"
                )
                continue

            # ── Find due contacts ────────────────────────────────────────────
            remaining = campaign["daily_limit"] - today_campaign_count
            due = db.get_due_enrollments(cid, limit=min(remaining, 10))

            if not due:
                continue

            raw_steps = db.get_steps(cid)
            for s in raw_steps:
                s["variants"] = db.get_step_variants(s["id"])
            steps = {s["step_num"]: s for s in raw_steps}
            if not steps:
                continue

            for enrollment in due:
                # Re-check cap inside loop (other threads could have sent)
                if _get_campaign_today_count(cid) >= campaign["daily_limit"]:
                    break

                step_num = enrollment["current_step"]
                step = steps.get(step_num)
                if not step:
                    db.complete_enrollment(enrollment["enroll_id"])
                    continue

                # ── Send the email ───────────────────────────────────────────
                contact = dict(enrollment)
                contact["contact_id"] = enrollment["contact_id"]

                # Pick subject/body: use variant if the enrollment has one
                subject_tpl = step["subject"]
                body_tpl    = step["body_html"]
                variant_label = enrollment.get("variant_label")
                if variant_label and step.get("variants"):
                    v = next((x for x in step["variants"] if x["label"] == variant_label), None)
                    if v:
                        subject_tpl = v["subject"]
                        body_tpl    = v["body_html"]

                # Round-robin through campaign's assigned accounts (falls back
                # to global settings inside send_email when account is None)
                account = db.get_next_account_for_campaign(cid)

                success, msg_id, err = email_sender.send_email(
                    contact=contact,
                    subject_tpl=subject_tpl,
                    body_tpl=body_tpl,
                    campaign_id=cid,
                    step_num=step_num,
                    settings=settings,
                    account=account,
                )

                if success:
                    # Advance to next step or mark complete
                    next_step = step_num + 1
                    if next_step in steps:
                        delay_days = steps[next_step]["delay_days"]
                        next_dt = (
                            datetime.datetime.utcnow() +
                            datetime.timedelta(days=delay_days)
                        ).replace(
                            hour=campaign["send_start_hour"],
                            minute=random.randint(0, 30),   # jitter ±30 min
                            second=0
                        ).strftime("%Y-%m-%d %H:%M:%S")
                        db.advance_enrollment(enrollment["enroll_id"], next_step, next_dt)
                    else:
                        db.complete_enrollment(enrollment["enroll_id"])

                elif err == "bounce":
                    pass  # already handled in sender.send_email

                # ── Random inter-email delay ─────────────────────────────────
                delay = random.randint(
                    campaign["min_delay_secs"],
                    campaign["max_delay_secs"]
                )
                logger.debug(f"Sleeping {delay}s before next send...")
                time.sleep(delay)

    except Exception as e:
        db.add_log(f"Scheduler error: {e}", "ERROR")
        logger.exception("process_queue error")


def _get_campaign_today_count(campaign_id):
    today = datetime.date.today().isoformat()
    with db.get_db() as conn:
        row = conn.execute("""
            SELECT COUNT(*) FROM sends
            WHERE campaign_id=?
              AND DATE(sent_at) = ?
        """, (campaign_id, today)).fetchone()
        return row[0] if row else 0


# ── Reply checker ─────────────────────────────────────────────────────────────

def run_reply_check():
    try:
        settings = db.get_settings()
        email_sender.check_replies(settings)
    except Exception as e:
        db.add_log(f"Reply check error: {e}", "ERROR")


def run_bounce_check():
    try:
        settings = db.get_settings()
        email_sender.check_bounces(settings)
    except Exception as e:
        db.add_log(f"Bounce check error: {e}", "ERROR")


# ── Background thread runner ──────────────────────────────────────────────────

def _run_loop():
    """
    Infinite loop running in a daemon thread.
    - Processes queue every 60 seconds
    - Checks replies every 15 minutes
    """
    last_reply_check = 0

    while not _stop_event.is_set():
        process_queue()

        now = time.time()
        if now - last_reply_check > 900:  # 15 minutes
            run_reply_check()
            run_bounce_check()
            last_reply_check = now

        # Sleep in small chunks so we can respond to stop event quickly
        for _ in range(60):
            if _stop_event.is_set():
                break
            time.sleep(1)


def start():
    global _scheduler_thread
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_run_loop, daemon=True, name="outreach-scheduler")
    _scheduler_thread.start()
    db.add_log("⚡ Scheduler started", "INFO")
    logger.info("Scheduler started")


def stop():
    _stop_event.set()
    db.add_log("⏹ Scheduler stopped", "INFO")
    logger.info("Scheduler stopped")


def is_running():
    return _scheduler_thread is not None and _scheduler_thread.is_alive()
