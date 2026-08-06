"""
scraper_worker.py — runs Google Maps scrapes on YOUR machine.

Why this exists
───────────────
The scraper drives a real Chrome window, and Google will sometimes serve a
CAPTCHA that a human has to solve by looking at it. A server cannot provide
that: the GCP VM has no display, and even with one, the window would be on the
server's screen, not yours.

So the split is: ShoutReach (on GCP) owns the queue, the contacts and the
campaigns; this worker owns the browser. You press Start in the web UI, this
process picks the job up, and Chrome opens here.

Setup
─────
  pip install playwright playwright-stealth requests beautifulsoup4
  playwright install chromium

  set SHOUTREACH_URL=https://your-shoutreach-host
  set SHOUTREACH_API_KEY=<from Settings -> Lead Scraper in the web UI>

Run
───
  python scraper_worker.py

Leave it running. It polls for work every few seconds and idles quietly when
there is none. Ctrl-C to stop; a job in progress is reported as stopped.
"""

import argparse
import json
import logging
import os
import queue
import random
import signal
import sys
import threading
import time

import requests

import gmaps_email_scraper as scraper

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_SERVER    = os.environ.get("SHOUTREACH_URL", "http://localhost:5000")
API_KEY           = os.environ.get("SHOUTREACH_API_KEY", "")
POLL_SECONDS      = 5      # how often to ask for work when idle
PROGRESS_SECONDS  = 5      # how often to push progress while running
HTTP_TIMEOUT      = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("worker")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_shutdown = threading.Event()


# ── Server client ─────────────────────────────────────────────────────────────

class Server:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        })

    def _post(self, path, payload=None, quiet=False):
        try:
            resp = self.session.post(
                self.base + path,
                data=json.dumps(payload or {}),
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            if not quiet:
                log.warning(f"{path} unreachable: {type(exc).__name__}")
            return None
        if resp.status_code == 401:
            log.error("Server rejected the API key. Check SHOUTREACH_API_KEY.")
            return None
        if resp.status_code == 204:
            return {}
        if resp.status_code >= 400:
            if not quiet:
                log.warning(f"{path} -> HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        try:
            return resp.json()
        except ValueError:
            return {}

    def claim(self):
        job = self._post("/api/scraper/claim", quiet=True)
        return job or None

    def heartbeat(self):
        self._post("/api/scraper/heartbeat", quiet=True)

    def progress(self, job_id, **fields):
        """Push state and read back the stop/resume flags in one round trip."""
        return self._post(f"/api/scraper/jobs/{job_id}/progress", fields) or {}

    def import_contacts(self, rows):
        return self._post("/api/contacts/import", {"rows": rows})


# ── The job adapter ───────────────────────────────────────────────────────────

class RemoteScraperJob:
    """
    Stands in for the in-process ScraperJob the scraper was written against.

    It keeps the same surface -- .log(), .status, .progress, .stop_event,
    .captcha_event -- so run_scraper_job() needs no changes. The difference is
    that state goes over HTTP to the server instead of being read out of memory
    by Flask, and the CAPTCHA wait polls for the Resume flag rather than
    blocking on a local threading.Event.
    """

    def __init__(self, server: Server, job_id: int, niche, city,
                 max_results, auto_import):
        self.server      = server
        self.job_id      = job_id
        self.niche       = niche
        self.city        = city
        self.max_results = max_results
        self.auto_import = auto_import

        self.status   = "running"
        self.progress = 0
        self.total    = 0
        self.found    = 0
        self.imported = 0

        self.stop_event    = threading.Event()
        self.captcha_event = threading.Event()

        self._pending  = queue.Queue()
        self._last_push = 0.0
        self._lock = threading.Lock()

    # -- logging ---------------------------------------------------------
    def log(self, msg, level="INFO"):
        log.info(f"  {msg}")
        self._pending.put({"msg": str(msg), "level": level})
        self._maybe_push()

    def _drain_logs(self, limit=100):
        out = []
        while len(out) < limit:
            try:
                out.append(self._pending.get_nowait())
            except queue.Empty:
                break
        return out

    # -- server sync -----------------------------------------------------
    def _maybe_push(self, force=False):
        now = time.time()
        if not force and now - self._last_push < PROGRESS_SECONDS:
            return
        with self._lock:
            self._last_push = now
            control = self.server.progress(
                self.job_id,
                status=self.status,
                progress=self.progress,
                total=self.total,
                found=self.found,
                imported=self.imported,
                logs=self._drain_logs(),
            )
        if control.get("stop"):
            self.stop_event.set()
        return control

    def push(self, **extra):
        """Force a sync now — used at phase boundaries and on completion."""
        with self._lock:
            self._last_push = time.time()
            control = self.server.progress(
                self.job_id,
                status=self.status,
                progress=self.progress,
                total=self.total,
                found=self.found,
                imported=self.imported,
                logs=self._drain_logs(),
                **extra,
            )
        if control.get("stop"):
            self.stop_event.set()
        return control

    # -- CAPTCHA ---------------------------------------------------------
    def wait_for_resume(self):
        """
        Block until the operator clicks Resume in the web UI.

        The scraper's own handler waits on captcha_event; here that event is
        never set locally, so this polls the progress endpoint, which returns
        the resume flag the button sets.
        """
        self.status = "captcha"
        self.push()
        log.warning("CAPTCHA detected — solve it in the Chrome window here, "
                    "then click Resume in the web UI.")
        while not _shutdown.is_set():
            control = self.push()
            if control.get("resume"):
                break
            if control.get("stop"):
                self.stop_event.set()
                break
            time.sleep(2)
        self.status = "running"
        self.push()

    def stop(self):
        self.stop_event.set()

    def resume(self):
        self.captcha_event.set()


def _install_captcha_bridge(job: RemoteScraperJob):
    """
    Point the scraper's CAPTCHA handler at the remote Resume flag.

    run_scraper_job calls handle_captcha_if_present(page, job), which blocks on
    job.captcha_event.wait(). That event only ever gets set by an in-process
    Flask route, which no longer exists — so swap the wait for a poll of the
    server flag, and restore the original when the job ends.
    """
    original = scraper.handle_captcha_if_present

    def patched(page, job_arg=None):
        if scraper._captcha_present(page):
            job.wait_for_resume()

    scraper.handle_captcha_if_present = patched
    return original


# ── Running one job ───────────────────────────────────────────────────────────

def run_job(server: Server, spec: dict):
    job = RemoteScraperJob(
        server, spec["job_id"], spec["niche"], spec["city"],
        spec["max_results"], spec["auto_import"],
    )
    log.info(f"Claimed job {job.job_id}: {job.niche} in {job.city} "
             f"(max {job.max_results})")

    original_handler = _install_captcha_bridge(job)
    ticker = threading.Thread(target=_heartbeat_loop, args=(job,), daemon=True)
    ticker.start()

    try:
        _run_scrape(server, job)
    except Exception as exc:
        log.exception("Job failed")
        job.status = "error"
        job.push(error=str(exc)[:500], finished=True)
    finally:
        scraper.handle_captcha_if_present = original_handler


def _heartbeat_loop(job: RemoteScraperJob):
    """Keep the UI's 'worker online' indicator alive during long silences."""
    while not _shutdown.is_set() and job.status in ("running", "captcha"):
        time.sleep(PROGRESS_SECONDS)
        if job.status == "running":
            job._maybe_push(force=True)


def _run_scrape(server: Server, job: RemoteScraperJob):
    """The scrape itself. Mirrors run_scraper_job, reporting over HTTP."""
    search_query = f"{job.niche} in {job.city}"
    output_file  = f"{scraper.safe_filename(job.city)}_{scraper.safe_filename(job.niche)}.csv"

    job.log(f"Starting: {search_query}")
    job.log(f"Target: {job.max_results} results → {output_file}")

    skip_names = set(scraper.load_existing_records(output_file).keys())
    if skip_names:
        job.log(f"Resume: skipping {len(skip_names)} business(es) from a previous run")

    # ── Maps ────────────────────────────────────────────────────────────
    businesses = scraper.scrape_google_maps(
        search_query, job.max_results, skip_names, job
    )

    if job.stop_event.is_set():
        job.log("Stopped by user.")
        job.status = "stopped"
        job.push(finished=True)
        return

    if not businesses:
        job.log("No businesses found. Check the niche and city.", "WARN")
        job.status = "done"
        job.push(finished=True)
        return

    job.total = len(businesses)
    job.log(f"Maps done — {job.total} businesses. Scraping their websites...")
    job.push()

    # ── Websites ────────────────────────────────────────────────────────
    processed = []
    for i, biz in enumerate(businesses):
        if job.stop_event.is_set():
            job.log("Stopped by user.")
            break
        emails, status = scraper.get_emails_for_business(biz["website"])
        biz["emails"], biz["email_status"] = emails, status
        job.progress = i + 1
        processed.append(biz)

        if status == scraper.STATUS_FOUND:
            job.found += 1
            job.log(f"[{i+1}/{job.total}] ✓ {biz['name']} — {emails}")
        else:
            job.log(f"[{i+1}/{job.total}] ✗ {biz['name']} — {status}")

        scraper.append_to_csv(
            {k: biz.get(k, "") for k in scraper.CSV_FIELDS}, output_file
        )
        time.sleep(random.uniform(*scraper.SITE_REQUEST_DELAY))

    # ── Browser fallback ────────────────────────────────────────────────
    retry = [b["website"] for b in processed
             if b.get("website") and b["email_status"] in scraper.BROWSER_RETRY_STATUSES]
    if retry and not job.stop_event.is_set():
        recovered = scraper.browser_fallback(retry, log_fn=job.log)
        for biz in processed:
            hit = recovered.get(biz.get("website"))
            if hit:
                biz["emails"], biz["email_status"] = hit
                job.found += 1
        if recovered:
            scraper.rewrite_csv_rows(processed, output_file)
    job.push()

    # ── Push to the server ──────────────────────────────────────────────
    rows = _build_contact_rows(processed, job)
    if rows and job.auto_import:
        result = server.import_contacts(rows)
        if result and result.get("ok"):
            job.imported = result.get("inserted", 0)
            job.log(f"✓ {job.imported} contacts imported "
                    f"({result.get('invalid_mx', 0)} with no MX record)")
        else:
            # The CSV is already on disk, so nothing is lost -- say where.
            job.log(f"Could not reach the server to import. "
                    f"Leads are saved in {output_file}", "ERROR")
    elif rows:
        job.log(f"{len(rows)} leads saved to {output_file} (auto-import off)")

    job.status = "done"
    job.log(f"Complete. {job.found}/{job.total} businesses had emails.")
    job.push(finished=True)


def _build_contact_rows(processed, job) -> list:
    """
    Turn scrape results into contact rows.

    MX validation happens here rather than server-side: it is a blocking DNS
    lookup per domain, and doing it on the worker keeps it off the web app's
    single request thread.
    """
    import email_validator as _ev

    def _extra(biz):
        """Maps details that have no dedicated column, for qualifying leads."""
        return {k: v for k, v in {
            "phone":    biz.get("phone", ""),
            "rating":   biz.get("rating", ""),
            "reviews":  biz.get("reviews", ""),
            "category": biz.get("category", ""),
        }.items() if v}

    rows = []
    for biz in processed:
        status = biz["email_status"]
        if status == scraper.STATUS_FOUND:
            for raw in biz["emails"].split(";"):
                raw = raw.strip()
                if not raw:
                    continue
                mx_ok = _ev.check_mx(raw)
                rows.append({
                    "email":      raw,
                    "company":    biz.get("name", ""),
                    "first_name": "",
                    "last_name":  "",
                    "website":    biz.get("website", ""),
                    "address":    biz.get("address", ""),
                    "mx_valid":   1 if mx_ok else 0,
                    "extra":      _extra(biz),
                })
        elif biz.get("website"):
            rows.append({
                "email":   "",
                "company": biz.get("name", ""),
                "website": biz.get("website", ""),
                "address": biz.get("address", ""),
                "status":  "form_only" if status == scraper.STATUS_FORM_ONLY else "no_email",
                "extra":   _extra(biz),
            })
        else:
            # No website at all. These used to be logged and thrown away, but
            # for a web-design agency a business with no site is the strongest
            # lead on the list, not a failure -- the phone number makes it
            # actionable.
            rows.append({
                "email":   "",
                "company": biz.get("name", ""),
                "website": "",
                "address": biz.get("address", ""),
                "status":  "no_website",
                "extra":   _extra(biz),
            })
    return rows


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="ShoutReach local scrape worker")
    ap.add_argument("--server", default=DEFAULT_SERVER,
                    help="ShoutReach base URL (or set SHOUTREACH_URL)")
    ap.add_argument("--api-key", default=API_KEY,
                    help="Worker API key (or set SHOUTREACH_API_KEY)")
    ap.add_argument("--once", action="store_true",
                    help="Run a single job then exit, for testing")
    args = ap.parse_args()

    if not args.api_key:
        log.error("No API key. Set SHOUTREACH_API_KEY, or pass --api-key.")
        log.error("Find it in the web UI under Settings -> Lead Scraper.")
        return 2

    server = Server(args.server, args.api_key)

    def _on_signal(signum, frame):
        log.info("Shutting down after the current step...")
        _shutdown.set()
    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except (AttributeError, ValueError):
        pass

    log.info(f"Worker ready. Server: {args.server}")
    log.info("Waiting for jobs — press Start in the web UI.")

    idle_logged = False
    while not _shutdown.is_set():
        spec = server.claim()
        if spec and spec.get("job_id"):
            idle_logged = False
            run_job(server, spec)
            if args.once:
                break
        else:
            server.heartbeat()
            if not idle_logged:
                idle_logged = True
        _shutdown.wait(POLL_SECONDS)

    log.info("Worker stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
