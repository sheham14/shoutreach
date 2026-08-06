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


def main():
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
