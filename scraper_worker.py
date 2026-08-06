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
from pathlib import Path

import requests

import gmaps_email_scraper as scraper

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_SERVER    = os.environ.get("SHOUTREACH_URL", "http://localhost:5000")
API_KEY           = os.environ.get("SHOUTREACH_API_KEY", "")
POLL_SECONDS      = 5      # how often to ask for work when idle
IMPORT_BATCH_SIZE = 20     # push leads every N businesses, not just at the end
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

# Remembers SHOUTREACH_URL / SHOUTREACH_API_KEY between terminal sessions so
# they only have to be set once. Lives next to this file, not the CSV output
# (which can reasonably live elsewhere) -- deliberately git-ignored since it
# holds a live credential.
CONFIG_PATH = Path(__file__).resolve().parent / ".worker_config.json"


def _load_cached_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cached_config(server: str, api_key: str):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"server": server, "api_key": api_key}, f, indent=2)
    except Exception as exc:
        log.warning(f"Could not save worker config to {CONFIG_PATH}: {exc}")


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
    The job object the scraper reports progress through.

    Originally this stood in for an in-process ScraperJob that Flask ran in a
    background thread and read out of memory; that never worked on a headless
    server and has been deleted, so this is now the only job type there is.

    It carries .log(), .status, .progress, .stop_event and .captcha_event, but
    state goes over HTTP to the server, and the CAPTCHA wait polls for the
    Resume flag rather than blocking on a local threading.Event.
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


# ── Running one job ───────────────────────────────────────────────────────────

def run_job(server: Server, spec: dict):
    job = RemoteScraperJob(
        server, spec["job_id"], spec["niche"], spec["city"],
        spec["max_results"], spec["auto_import"],
    )
    log.info(f"Claimed job {job.job_id}: {job.niche} in {job.city} "
             f"(max {job.max_results})")

    # No CAPTCHA monkeypatch here any more. This used to swap out
    # scraper.handle_captcha_if_present for the duration of a job, because that
    # function waited on a threading.Event only an in-process Flask job could
    # set. That job type is gone, so the scraper now polls wait_for_resume()
    # directly -- and the CAPTCHA warning reaches the live log, which the
    # patched version skipped straight past.
    ticker = threading.Thread(target=_heartbeat_loop, args=(job,), daemon=True)
    ticker.start()

    try:
        _run_scrape(server, job)
    except Exception as exc:
        log.exception("Job failed")
        job.status = "error"
        job.push(error=str(exc)[:500], finished=True)


def _heartbeat_loop(job: RemoteScraperJob):
    """Keep the UI's 'worker online' indicator alive during long silences."""
    while not _shutdown.is_set() and job.status in ("running", "captcha"):
        time.sleep(PROGRESS_SECONDS)
        if job.status == "running":
            job._maybe_push(force=True)


def _run_scrape(server: Server, job: RemoteScraperJob):
    """The scrape itself. Mirrors run_scraper_job, reporting over HTTP."""
    scraper.ensure_output_dirs()
    search_query = f"{job.niche} in {job.city}"
    output_file  = f"{scraper.OUTPUT_DIR}/{scraper.safe_filename(job.city)}_{scraper.safe_filename(job.niche)}.csv"

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
    unpushed = []          # awaiting the next incremental import
    for i, biz in enumerate(businesses):
        if job.stop_event.is_set():
            job.log("Stopped by user.")
            break
        emails, status = scraper.get_emails_for_business(biz["website"])
        biz["emails"], biz["email_status"] = emails, status
        job.progress = i + 1
        processed.append(biz)
        unpushed.append(biz)

        if status == scraper.STATUS_FOUND:
            job.found += 1
            job.log(f"[{i+1}/{job.total}] ✓ {biz['name']} — {emails}")
        else:
            job.log(f"[{i+1}/{job.total}] ✗ {biz['name']} — {status}")

        scraper.append_to_csv(
            {k: biz.get(k, "") for k in scraper.CSV_FIELDS}, output_file
        )

        # Push in batches rather than only at the end. A run that dies at
        # business 110 of 118 used to import nothing at all -- the leads
        # existed solely in the local CSV. upsert_contacts is idempotent on
        # email, so a partial batch costs nothing if the run is retried.
        if job.auto_import and len(unpushed) >= IMPORT_BATCH_SIZE:
            _push_batch(server, job, unpushed)
            unpushed = []

        time.sleep(random.uniform(*scraper.SITE_REQUEST_DELAY))

    if job.auto_import and unpushed and job.stop_event.is_set():
        # Interrupted mid-run: get what we have to the server before exiting.
        _push_batch(server, job, unpushed)
        unpushed = []

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

    # ── Push whatever is left ───────────────────────────────────────────
    # The browser fallback may have resolved sites that were already pushed as
    # prospects; re-sending them is safe because upsert_contacts merges on
    # email and only fills empty fields.
    if job.auto_import:
        remaining = unpushed or []
        recovered_after_push = [
            b for b in processed
            if b.get("email_status") == scraper.STATUS_FOUND and b not in remaining
        ]
        _push_batch(server, job, remaining + recovered_after_push)
    else:
        rows = _build_contact_rows(processed, job)
        job.log(f"{len(rows)} leads saved to {output_file} (auto-import off)")

    job.status = "done"
    job.log(f"Complete. {job.found}/{job.total} businesses had emails.")
    job.push(finished=True)


def _push_batch(server: Server, job, businesses: list):
    """
    Send one batch of finished businesses to the server.

    Failure is logged, not raised: the CSV on disk is the source of truth, and
    a network blip partway through a long scrape must not end the run.
    """
    if not businesses:
        return
    rows = _build_contact_rows(businesses, job)
    if not rows:
        return
    result = server.import_contacts(rows)
    if result and result.get("ok"):
        job.imported += result.get("inserted", 0)
        job.log(f"  ↑ imported {result.get('inserted', 0)} "
                f"({job.imported} so far)")
    else:
        job.log(f"  ↑ import failed for {len(rows)} lead(s) — they are still in "
                f"the CSV and will be re-sent on the next run", "WARN")


def _build_contact_rows(processed, job) -> list:
    """
    Turn scrape results into contact rows.

    MX validation happens here rather than server-side: it is a blocking DNS
    lookup per domain, and doing it on the worker keeps it off the web app's
    single request thread.
    """
    import email_validator as _ev

    def _qualifiers(biz):
        """
        Phone, category, rating and review count.

        These used to be packed into the `extra` JSON blob, which meant the
        Contacts table showed one opaque column instead of the fields you
        actually qualify a lead on. They are real columns now; `source_job_id`
        rides along so every lead remembers which scrape produced it.
        """
        # getattr, not job.job_id: an untagged lead is a cosmetic loss, but an
        # AttributeError here would throw away a whole finished batch.
        return {**scraper._qualifying_fields(biz),
                "source_job_id": getattr(job, "job_id", None)}

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
                    **_qualifiers(biz),
                })
        elif biz.get("website"):
            rows.append({
                "email":   "",
                "company": biz.get("name", ""),
                "website": biz.get("website", ""),
                "address": biz.get("address", ""),
                "status":  "form_only" if status == scraper.STATUS_FORM_ONLY else "no_email",
                **_qualifiers(biz),
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
                **_qualifiers(biz),
            })
    return rows


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    cached = _load_cached_config()

    ap = argparse.ArgumentParser(description="ShoutReach local scrape worker")
    ap.add_argument("--server", default=None,
                    help="ShoutReach base URL (or set SHOUTREACH_URL). "
                         "Remembered after the first successful run.")
    ap.add_argument("--api-key", default=None,
                    help="Worker API key (or set SHOUTREACH_API_KEY). "
                         "Remembered after the first successful run.")
    ap.add_argument("--once", action="store_true",
                    help="Run a single job then exit, for testing")
    ap.add_argument("--forget", action="store_true",
                    help="Clear the remembered server/API key and exit")
    args = ap.parse_args()

    if args.forget:
        try:
            CONFIG_PATH.unlink()
        except FileNotFoundError:
            pass
        log.info("Remembered worker config cleared.")
        return 0

    # Precedence: explicit flag > env var > what was remembered last time.
    server_url = args.server or os.environ.get("SHOUTREACH_URL") or cached.get("server") or DEFAULT_SERVER
    api_key    = args.api_key or os.environ.get("SHOUTREACH_API_KEY") or cached.get("api_key") or API_KEY

    if not api_key:
        log.error("No API key. Set SHOUTREACH_API_KEY, or pass --api-key, once --")
        log.error("it will be remembered and every run after that needs neither.")
        log.error("Find it in the web UI under Settings -> Lead Scraper.")
        return 2

    if cached.get("server") != server_url or cached.get("api_key") != api_key:
        _save_cached_config(server_url, api_key)
        log.info(f"Saved worker config to {CONFIG_PATH.name} "
                 "— future runs won't need SHOUTREACH_URL/SHOUTREACH_API_KEY "
                 "(use --forget to clear it).")

    server = Server(server_url, api_key)

    def _on_signal(signum, frame):
        # A second Ctrl-C exits immediately. The first only sets a flag, which
        # replaces Python's default of raising KeyboardInterrupt -- and that
        # exception is the thing that would have broken a blocking socket read.
        # Without this escape hatch, Ctrl-C cannot interrupt a hung request at
        # all, which is exactly how a stalled scrape becomes unkillable.
        if _shutdown.is_set():
            log.warning("Second interrupt — exiting now.")
            os._exit(130)
        log.info("Shutting down after the current step... (Ctrl-C again to force)")
        _shutdown.set()
    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except (AttributeError, ValueError):
        pass

    log.info(f"Worker ready. Server: {server_url}")
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
