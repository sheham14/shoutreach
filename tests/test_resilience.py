"""Tests for the ways a scrape run can stall or lose work.

Run:  python tests/test_resilience.py

These pin three failures seen on a real run of 118 physiotherapy clinics, where
the worker parked on one business for 6+ minutes and could not be interrupted:

  1. requests' timeout is per-read, not total -- a server that trickles bytes
     resets it forever, so one site could stall the whole queue.
  2. Leads were only pushed to the server at the very end, so a run that died
     at business 110 of 118 imported nothing.
  3. The SIGINT handler only set a flag, which replaces Python's default of
     raising KeyboardInterrupt -- the exception that would have broken the
     blocking read. Ctrl-C could not stop a hung scrape at all.

And, since the 2026-11-21 audit, the email sender's own resilience (B-6,
B-23): one campaign's error must not stop the others, bad campaign values are
refused when saved, and the background thread must survive anything short of
stop() -- and be restarted if it dies anyway.
"""
import http.server
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gmaps_email_scraper as scraper  # noqa: E402

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TricklingHandler(http.server.BaseHTTPRequestHandler):
    """Sends a byte every 2s forever -- the exact shape that hung the real run."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "100000000")
        self.end_headers()
        try:
            while True:
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(2)
        except Exception:
            pass

    def log_message(self, *args):
        pass


def serve(handler, port):
    # Threading + daemon_threads matters: the handler below never returns, and
    # a single-threaded HTTPServer would leave shutdown() waiting on it forever
    # -- the test would hang for the opposite reason to the one under test.
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_trickle_does_not_hang():
    """
    The real failure: a site trickled bytes and parked the scrape for 10
    minutes. Budgets are shrunk here so the test proves the mechanism in
    seconds rather than waiting out the production ceilings.
    """
    print("\n1. A TRICKLING SERVER CANNOT STALL THE QUEUE")
    port = free_port()
    srv = serve(TricklingHandler, port)
    real_body, real_biz = scraper.BODY_READ_BUDGET, scraper.BUSINESS_BUDGET
    real_hard = scraper.FETCH_HARD_DEADLINE
    scraper.BODY_READ_BUDGET, scraper.BUSINESS_BUDGET = 3, 8
    scraper.FETCH_HARD_DEADLINE = 4
    try:
        started = time.monotonic()
        emails, status = scraper.get_emails_for_business(f"http://127.0.0.1:{port}")
        elapsed = time.monotonic() - started

        # Without the ceiling this never returns at all. The bound is what is
        # under test, not the exact number.
        check("returns instead of hanging forever", True, f"took {elapsed:.1f}s")
        check("bounded by the configured budget",
              elapsed < 30, f"took {elapsed:.1f}s with a {scraper.BODY_READ_BUDGET}s body budget")
        check("no emails invented from a truncated body", emails == "", repr(emails))
        print(f"      elapsed {elapsed:.1f}s, status={status}")
    finally:
        scraper.BODY_READ_BUDGET, scraper.BUSINESS_BUDGET = real_body, real_biz
        scraper.FETCH_HARD_DEADLINE = real_hard
        # No shutdown(): the handler thread is deliberately stuck writing.
        # It is a daemon, so it dies with the process.
        srv.server_close()


def test_ceilings_are_configured():
    print("\n2. THE CEILINGS EXIST AND ARE SANE")
    check("a per-body time budget is set",
          isinstance(scraper.BODY_READ_BUDGET, (int, float)) and scraper.BODY_READ_BUDGET > 0)
    check("a per-body size cap is set",
          scraper.MAX_RESPONSE_BYTES >= 1024 * 1024)
    check("a whole-business budget is set",
          scraper.BUSINESS_BUDGET >= scraper.BODY_READ_BUDGET,
          "the business budget must not be tighter than one body read")


def test_incremental_import():
    print("\n3. LEADS ARE PUSHED IN BATCHES, NOT ONLY AT THE END")
    import scraper_worker

    check("a batch size is configured",
          getattr(scraper_worker, "IMPORT_BATCH_SIZE", 0) > 0,
          f"IMPORT_BATCH_SIZE={getattr(scraper_worker, 'IMPORT_BATCH_SIZE', None)}")
    check("_push_batch exists", hasattr(scraper_worker, "_push_batch"))

    pushed = []
    sent_rows = []

    class FakeServer:
        def import_contacts(self, rows):
            pushed.append(len(rows))
            sent_rows.extend(rows)
            return {"ok": True, "inserted": len(rows), "invalid_mx": 0}

    class FakeJob:
        imported = 0
        job_id   = 7      # every lead is tagged with the scrape that found it
        def log(self, msg, level="INFO"):
            pass

    job = FakeJob()
    scraper_worker._push_batch(FakeServer(), job, [
        {"name": "A", "website": "http://a.ca", "emails": "a@a.ca",
         "email_status": scraper.STATUS_FOUND, "address": "",
         "phone": "555-1234", "rating": "4.7", "reviews": "88",
         "category": "Dentist"},
        {"name": "B", "website": "http://b.ca", "emails": "",
         "email_status": scraper.STATUS_FORM_ONLY, "address": ""},
    ])
    check("a batch reaches the server", pushed == [2], str(pushed))
    check("the running total is updated", job.imported == 2, str(job.imported))

    # These four used to be buried in the `extra` JSON blob, so the Contacts
    # table showed one opaque column instead of the fields you qualify on --
    # and the web-triggered path was fixed while this one silently was not.
    lead = next((r for r in sent_rows if r.get("email") == "a@a.ca"), {})
    check("phone is sent as its own field", lead.get("phone") == "555-1234", str(lead.get("phone")))
    check("rating is coerced to a number", lead.get("rating") == 4.7, str(lead.get("rating")))
    check("review count is coerced to an int", lead.get("review_count") == 88,
          str(lead.get("review_count")))
    check("category is sent", lead.get("category") == "Dentist", str(lead.get("category")))
    check("the lead is tagged with its scrape", lead.get("source_job_id") == 7,
          str(lead.get("source_job_id")))
    check("prospects with no email are tagged too",
          all(r.get("source_job_id") == 7 for r in sent_rows), str(sent_rows))

    # A failing server must not end the run -- the CSV is the source of truth.
    class BrokenServer:
        def import_contacts(self, rows):
            return None

    job2 = FakeJob()
    try:
        scraper_worker._push_batch(BrokenServer(), job2, [
            {"name": "C", "website": "http://c.ca", "emails": "c@c.ca",
             "email_status": scraper.STATUS_FOUND, "address": ""},
        ])
        check("an import failure does not raise", True)
    except Exception as exc:
        check("an import failure does not raise", False, f"{type(exc).__name__}: {exc}")

    scraper_worker._push_batch(FakeServer(), FakeJob(), [])
    check("an empty batch is a no-op", pushed == [2], str(pushed))


def test_second_interrupt_forces_exit():
    print("\n4. A SECOND CTRL-C FORCES AN EXIT")
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scraper_worker.py"), encoding="utf-8").read()
    check("the handler has a force-quit path", "os._exit(130)" in src)
    handler = src.split("def _on_signal")[1].split("signal.signal")[0]
    check("it is gated on an already-set shutdown flag",
          "_shutdown.is_set()" in handler)
    check("the first interrupt tells the user about the second",
          "Ctrl-C again" in src)


class FakePanel:
    """Stands in for the Maps detail pane, which updates asynchronously."""

    def __init__(self, label):
        self.label = label

    def get_attribute(self, _name):
        return self.label

    def inner_text(self):
        return self.label


class FakePage:
    """Serves a stale panel for `stale_reads` polls, then the correct one."""

    def __init__(self, stale_label, real_label, stale_reads):
        self.stale_label, self.real_label = stale_label, real_label
        self.reads = 0

    def query_selector(self, selector):
        if "h1" in selector:
            return None          # force the aria-label path
        self.reads += 1
        return FakePanel(self.stale_label if self.reads <= self.stale_reads_left else self.real_label)

    @property
    def stale_reads_left(self):
        return self._stale

    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000.0)


def test_panel_race_guard():
    """
    The bug this pins: clicking a card swaps the detail pane asynchronously.
    Reading it too early captured the PREVIOUS business's website and address
    under the new business's name -- observed live as "Highgate Health" being
    saved with Polygon Health's website and email.
    """
    print("\n5. THE DETAIL PANEL MUST MATCH THE CLICKED BUSINESS")

    # Panel is stale for a few polls, then catches up: must wait, not accept.
    page = FakePage("Polygon Health Burnaby", "Highgate Health - pt Health", 0)
    page._stale = 3
    ok = scraper._wait_for_detail_panel(page, "Highgate Health - pt Health", timeout=5)
    check("waits through a stale panel and then matches", ok is True)

    # Panel never catches up: must refuse rather than record the wrong data.
    page = FakePage("Polygon Health Burnaby", "Polygon Health Burnaby", 0)
    page._stale = 10_000
    started = time.monotonic()
    ok = scraper._wait_for_detail_panel(page, "Highgate Health - pt Health", timeout=2)
    elapsed = time.monotonic() - started
    check("refuses a panel that never matches", ok is False)
    check("gives up at the timeout", elapsed < 4, f"{elapsed:.1f}s")

    check("an empty name is refused",
          scraper._wait_for_detail_panel(page, "", timeout=1) is False)

    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "gmaps_email_scraper.py"), encoding="utf-8").read()
    check("the scrape loop skips on a failed match rather than recording",
          "_wait_for_detail_panel(page, name)" in src and "panel_failures" in src)
    check("retries are bounded so a broken panel cannot loop forever",
          "PANEL_RETRY_LIMIT" in src)


def test_captcha_waits_on_the_remote_flag():
    """
    A CAPTCHA must block until Resume arrives from the server -- and must never
    block on a local threading.Event.

    The deleted in-process ScraperJob set captcha_event in the same process.
    Nothing does that any more, so waiting on it here would hang the scrape
    forever with no way out. The worker used to paper over this by monkey-
    patching the handler for the duration of a job; the handler now polls
    wait_for_resume() itself, so this pins the behaviour that replaced it.
    """
    print("\n6. A CAPTCHA WAITS ON THE SERVER'S RESUME FLAG")

    calls = {"waited": 0}
    logs = []

    class RemoteLikeJob:
        status = "running"
        def log(self, msg, level="INFO"):
            logs.append(msg)
        def wait_for_resume(self):
            calls["waited"] += 1

    class BlockingEventJob:
        """The old shape: only a threading.Event, which nobody would ever set."""
        status = "running"
        captcha_event = threading.Event()
        def log(self, msg, level="INFO"):
            logs.append(msg)

    original = scraper._captcha_present
    try:
        scraper._captcha_present = lambda page: True
        scraper.handle_captcha_if_present(object(), RemoteLikeJob())
        check("a remote job polls the server for Resume", calls["waited"] == 1,
              str(calls))
        check("the CAPTCHA is announced in the live log",
              any("CAPTCHA detected" in m for m in logs), str(logs))
        check("and so is the resume", any("Resuming" in m for m in logs), str(logs))

        # The event fallback must still work when a job really does provide one
        # -- set it first so the test cannot hang if the branch is taken.
        job = BlockingEventJob()
        job.captcha_event.set()
        scraper.handle_captcha_if_present(object(), job)
        check("an event-style job still resumes once its event is set",
              job.status == "running")

        scraper._captcha_present = lambda page: False
        before = calls["waited"]
        scraper.handle_captcha_if_present(object(), RemoteLikeJob())
        check("no CAPTCHA means no wait", calls["waited"] == before)
    finally:
        scraper._captcha_present = original

    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scraper_worker.py"), encoding="utf-8").read()
    check("the worker no longer monkeypatches the CAPTCHA handler",
          "_install_captcha_bridge" not in src)


def test_sender_survives():
    """Audit B-6 and B-23. Scratch database; send_email is replaced."""
    print("\n7. THE EMAIL SENDER CAN'T BE STOPPED BY ONE ERROR (B-6, B-23)")
    import importlib
    import sqlite3
    import tempfile
    work = tempfile.mkdtemp(prefix="sender_resilience_")
    os.environ["DB_PATH"] = os.path.join(work, "r.db")
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["SHOUTREACH_NO_SCHEDULER"] = "1"
    import db
    importlib.reload(db)
    import sender
    importlib.reload(sender)
    import scheduler
    importlib.reload(scheduler)
    db.init_db()
    alice = db.create_user("alice", "test-password-123", is_admin=True)
    bob = db.create_user("bob", "test-password-123", is_admin=False)

    sent = []

    def fake_send(contact, **kw):
        sent.append(contact["email"])
        db.log_send(kw["campaign_id"], contact["email_lead_id"], kw["step_num"], "s",
                    f"<{len(sent)}@x>")
        return True, "<id>", None
    sender.send_email = fake_send
    sender.is_business_hours = lambda campaign: True
    scheduler.email_checking_enabled = lambda: False

    def campaign(owner, name, email):
        db.upsert_businesses([{"company": name, "email": email, "mx_valid": 1}], owner_id=owner)
        cid = db.create_campaign(name, owner_id=owner, min_delay=0, max_delay=0)
        db.upsert_step(cid, 1, "Hi", "<p>Hi</p>", 0)
        with db.get_db() as conn:
            bids = [r["id"] for r in conn.execute(
                "SELECT id FROM businesses WHERE owner_id=? AND name=?", (owner, name))]
            conn.execute("UPDATE campaigns SET status='active' WHERE id=?", (cid,))
        db.enroll_businesses(cid, bids, owner_id=owner)
        return cid
    c_bob = campaign(bob, "Bob campaign", "b@bob-lead.ae")
    campaign(alice, "Alice campaign", "a@alice-lead.ae")
    first = db.get_campaigns(all_owners=True)[0]["id"]
    # Break whichever campaign the loop reaches first, as a value stored
    # before checks existed would be.
    with db.get_db() as conn:
        conn.execute("UPDATE campaigns SET daily_limit='lots' WHERE id=?", (first,))
    scheduler.process_queue()
    check("a campaign with a bad stored value doesn't stop the next one",
          len(sent) == 1, f"sent={sent}")
    check("the bad campaign is named in the Activity log",
          any("Scheduler error in campaign" in r["message"] for r in db.get_logs(10)))

    print("   saving bad values is refused")
    for bad in ({"daily_limit": "lots"}, {"daily_limit": -1}, {"daily_limit": 5000},
                {"send_start_hour": 25}, {"min_delay_secs": 99999}, {"bounce_pause_pct": 150},
                {"status": "sending"}, {"min_delay_secs": 200, "max_delay_secs": 100}):
        try:
            db.update_campaign(c_bob, **bad)
            check(f"refused: {bad}", False, "it was stored")
        except db.CampaignValueError as e:
            check(f"refused: {bad}", True, str(e))
    try:
        db.update_campaign(c_bob, max_delay_secs=10)  # min is 0
        db.update_campaign(c_bob, daily_limit="40", status="paused")
        check("good values still save, numbers as text included", True)
    except db.CampaignValueError as e:
        check("good values still save, numbers as text included", False, str(e))
    with db.get_db() as conn:   # a pair stored before checks existed
        conn.execute("UPDATE campaigns SET min_delay_secs=300, max_delay_secs=100 WHERE id=?", (c_bob,))
    try:
        db.update_campaign(c_bob, status="paused")
        check("an old bad delay pair doesn't block pausing or activating", True)
    except db.CampaignValueError as e:
        check("an old bad delay pair doesn't block pausing or activating", False, str(e))
    with db.get_db() as conn:
        conn.execute("UPDATE campaigns SET min_delay_secs=0, max_delay_secs=10 WHERE id=?", (c_bob,))
    try:
        db.create_campaign("Bad", daily_limit="lots", owner_id=alice)
        check("create_campaign refuses a bad value", False)
    except db.CampaignValueError:
        check("create_campaign refuses a bad value", True)

    print("   ...and through the PATCH route")
    import app as app_mod
    importlib.reload(app_mod)
    app_mod.app.config["TESTING"] = True
    c = app_mod.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = bob
        sess["csrf_token"] = "t"
    r = c.patch(f"/api/campaigns/{c_bob}", headers={"X-CSRF-Token": "t"}, json={"daily_limit": "lots"})
    check("PATCH with a bad value answers 400 with a sentence",
          r.status_code == 400 and "number" in (r.get_json() or {}).get("error", ""),
          f"{r.status_code} {r.get_json()}")
    check("and nothing was stored",
          db.get_campaign(c_bob)["daily_limit"] == 40, str(db.get_campaign(c_bob)["daily_limit"]))
    r = c.post("/api/campaigns", headers={"X-CSRF-Token": "t"}, json={"name": "x", "daily_limit": "lots"})
    check("creating one with a bad value answers 400", r.status_code == 400, str(r.status_code))
    check("the app didn't start the scheduler (SHOUTREACH_NO_SCHEDULER)", not scheduler.is_running())

    print("   the loop survives")
    calls = {"n": 0}
    real_pass = scheduler._one_pass

    def exploding_pass():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        scheduler._stop_event.set()
    scheduler._one_pass = exploding_pass
    scheduler._stop_event.clear()
    scheduler._wake_event.set()
    original_wait = scheduler._wake_event.wait
    scheduler._wake_event.wait = lambda timeout=None: True
    t = threading.Thread(target=scheduler._run_loop, daemon=True)
    t.start()
    t.join(timeout=5)
    scheduler._wake_event.wait = original_wait
    scheduler._one_pass = real_pass
    check("a pass that raises doesn't end the loop", calls["n"] == 2 and not t.is_alive(),
          f"passes={calls['n']} alive={t.is_alive()}")

    original_get_db = db.get_db

    def locked():
        raise sqlite3.OperationalError("database is locked")
    db.get_db = locked
    try:
        db.add_log("this write fails")
        check("add_log doesn't raise when its write fails", True)
    except Exception as e:
        check("add_log doesn't raise when its write fails", False, repr(e))
    finally:
        db.get_db = original_get_db

    # The audit's own reproduction: another writer holds the database past
    # the 10-second busy wait while the loop's error handlers try to log.
    with db.get_db() as conn:
        conn.execute("UPDATE campaigns SET daily_limit='lots' WHERE id=?", (c_bob,))
    scheduler._stop_event.clear()
    original_busy = db.get_db
    lock = sqlite3.connect(db.DB_PATH, check_same_thread=False, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    t = threading.Thread(target=scheduler._run_loop, daemon=True)
    started = time.time()
    t.start()
    t.join(timeout=13)
    alive_while_locked = t.is_alive()
    lock.execute("COMMIT")
    lock.close()
    scheduler._stop_event.set()
    scheduler._wake_event.set()
    t.join(timeout=15)
    check("the thread survives a database held locked past the busy wait",
          alive_while_locked, f"after {time.time() - started:.0f}s")

    print("   the watchdog")
    scheduler._stop_event.clear()
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    scheduler._scheduler_thread = dead
    started_threads = []
    real_thread = scheduler.threading.Thread

    class RecordingThread(real_thread):
        def start(self):
            started_threads.append(self)  # don't actually run a sender here
    scheduler.threading.Thread = RecordingThread
    try:
        restarted = scheduler.ensure_running()
    finally:
        scheduler.threading.Thread = real_thread
    check("ensure_running restarts a dead thread", restarted and len(started_threads) == 1)
    scheduler._scheduler_thread = None
    check("and does nothing when the scheduler was never started", not scheduler.ensure_running())

    print("   the Dashboard says when it last ran")
    scheduler._heartbeat()
    r = c.get("/api/dashboard")
    d = r.get_json() or {}
    check("/api/dashboard returns sender.alive and sender.last_pass_at",
          "sender" in d and set(d["sender"]) == {"alive", "last_pass_at"} and d["sender"]["last_pass_at"],
          str(d.get("sender")))


def main():
    test_sender_survives()
    test_ceilings_are_configured()
    test_incremental_import()
    test_second_interrupt_forces_exit()
    test_panel_race_guard()
    test_captcha_waits_on_the_remote_flag()
    test_trickle_does_not_hang()      # slowest, so last

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
