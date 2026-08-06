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

import json
import time
import random
import datetime
import logging
import threading

import db
import sender as email_sender

logger = logging.getLogger("scheduler")

_scheduler_thread: threading.Thread = None
_scheduler_lock = threading.Lock()
_stop_event = threading.Event()
_wake_event = threading.Event()
_run_now_reply_check = False  # set by request_run_now(include_reply_check=True)


def request_run_now(include_reply_check: bool = False) -> None:
    """Ask the scheduler thread to run process_queue() on its next tick.
    Returns immediately — the actual work happens off the request thread."""
    global _run_now_reply_check
    if include_reply_check:
        _run_now_reply_check = True
    _wake_event.set()


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

                # Parse campaign-level template variables
                try:
                    campaign_vars = json.loads(campaign.get("variables") or "{}")
                except Exception:
                    campaign_vars = {}

                success, msg_id, err = email_sender.send_email(
                    contact=contact,
                    subject_tpl=subject_tpl,
                    body_tpl=body_tpl,
                    campaign_id=cid,
                    step_num=step_num,
                    settings=settings,
                    account=account,
                    campaign_vars=campaign_vars,
                )

                if success:
                    # Advance to next step or mark complete
                    next_step = step_num + 1
                    if next_step in steps:
                        delay_days = steps[next_step]["delay_days"]
                        # Schedule in campaign timezone so send_start_hour is local time
                        tz_name = (campaign.get("timezone") or "").strip()
                        try:
                            from zoneinfo import ZoneInfo
                            tz = ZoneInfo(tz_name) if tz_name else datetime.timezone.utc
                        except Exception:
                            tz = datetime.timezone.utc
                        now_local = datetime.datetime.now(tz)
                        next_local = (now_local + datetime.timedelta(days=delay_days)).replace(
                            hour=campaign["send_start_hour"],
                            minute=random.randint(0, 30),
                            second=0,
                            microsecond=0,
                        )
                        # Store as UTC string for consistent DB comparison
                        next_dt = next_local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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


LOG_RETENTION_DAYS = 60


def run_log_prune():
    try:
        deleted = db.prune_logs(LOG_RETENTION_DAYS)
        if deleted:
            logger.info(f"Pruned {deleted} log row(s) older than {LOG_RETENTION_DAYS} days")
    except Exception as e:
        logger.exception("Log prune error")


# ── Background thread runner ──────────────────────────────────────────────────

def _run_loop():
    """
    Infinite loop running in a daemon thread.
    - Processes queue every 60 seconds (or sooner when request_run_now fires)
    - Checks replies every 5 minutes
    - Prunes old log rows once a day
    """
    global _run_now_reply_check
    last_reply_check = 0
    last_log_prune   = 0

    while not _stop_event.is_set():
        process_queue()

        do_reply_check = _run_now_reply_check
        _run_now_reply_check = False
        now = time.time()
        if do_reply_check or now - last_reply_check > 300:  # 5 minutes
            run_reply_check()
            run_bounce_check()
            last_reply_check = now

        if now - last_log_prune > 86400:  # 24 hours
            run_log_prune()
            last_log_prune = now

        # Sleep up to 60s, but wake immediately on request_run_now() or stop().
        _wake_event.wait(timeout=60)
        _wake_event.clear()


def start():
    """Start the background scheduler. Safe to call more than once."""
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return
        _stop_event.clear()
        _wake_event.clear()
        _scheduler_thread = threading.Thread(
            target=_run_loop, daemon=True, name="shoutreach-scheduler"
        )
        _scheduler_thread.start()
    db.add_log("⚡ Scheduler started", "INFO")
    logger.info("Scheduler started")


def stop(timeout: float = 10.0):
    """Signal the scheduler to stop and wait briefly for it to exit."""
    global _scheduler_thread
    _stop_event.set()
    _wake_event.set()
    t = _scheduler_thread
    if t is not None:
        t.join(timeout=timeout)
        if t.is_alive():
            logger.warning("Scheduler thread did not exit within %.1fs", timeout)
    _scheduler_thread = None
    db.add_log("⏹ Scheduler stopped", "INFO")
    logger.info("Scheduler stopped")


def is_running():
    return _scheduler_thread is not None and _scheduler_thread.is_alive()
