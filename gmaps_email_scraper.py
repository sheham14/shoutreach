"""
╔══════════════════════════════════════════════════════════════╗
║           Google Maps → Email Scraper  v3                   ║
║                                                              ║
║  What it does:                                               ║
║    1. Opens Google Maps and searches your niche + city       ║
║    2. Clicks each result, grabs the website + address        ║
║    3. Visits each website and scrapes any email addresses    ║
║    4. Saves everything to a CSV, one row per business        ║
║                                                              ║
║  Key features:                                               ║
║    - Stealth mode  : hides bot signals from Google           ║
║    - Cookie persist: looks like a returning browser          ║
║    - Resume support: won't re-scrape already-done rows       ║
║    - CAPTCHA pause : script waits while you solve it         ║
║    - Email status  : tells you WHY an email wasn't found     ║
╚══════════════════════════════════════════════════════════════╝

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 INSTALL (run once in your terminal)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  pip install playwright requests beautifulsoup4 playwright-stealth
  playwright install chromium

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 RUN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python gmaps_email_scraper.py

  A real Chrome window opens automatically. Keep it visible.
  If a CAPTCHA appears: solve it in the browser, then press
  ENTER in the terminal to resume.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 CHANGING CITY / NICHE  ← READ THIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Find the ══ CONFIG ══ block below (around line 75).
  Edit NICHE and CITY. The search query is built automatically.

  Example:
    NICHE = "dental clinics"   →  change to "physiotherapy clinics"
    CITY  = "Brampton Canada"   →  change to "Vancouver Canada"

  Also update OUTPUT_FILE each time so you don't mix cities.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 OUTPUT CSV COLUMNS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  name              Business name from Google Maps
  website           Website URL from Google Maps listing
  address           Address from Google Maps listing
  emails            Semicolon-separated emails (if found)
  email_status      One of:

    found              Email(s) are in the emails column
    no_website         Google Maps had no website listed
    site_blocked       Got a 403/429 or connection refused
    site_timeout       Website didn't respond in time
    contact_form_only  Page loaded but only has a contact form
    no_email_on_page   Page loaded, no email found anywhere

  TIP: filter your CSV for contact_form_only rows — those are
  your best manual lookup targets. Site exists, owner is
  findable, email just isn't public. Worth 10 mins each.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 COOKIE FILES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  After each run the script saves cookies_<city>.json.
  Next run it loads those cookies so Google sees a returning
  browser with history — not a fresh bot session.
  Do NOT delete these files between runs.
"""

import argparse
import csv
import json
import re
import threading
import time
import random
import logging
from pathlib import Path
from collections import Counter

import email_validator as _ev

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ══════════════════════════════════════════════════════════════
#  CONFIG  ← only section you need to edit
# ══════════════════════════════════════════════════════════════

NICHE  = "dental clinics"    # ← niche / business type
CITY   = "Brampton Canada"    # ← city + country

MAX_RESULTS  = 70         # leads to collect per run
OUTPUT_FILE  = "brampton_dental.csv"   # ← change per city/run

# Timing — leave these alone, they mimic human behaviour
MAP_SCROLL_DELAY   = (1.2, 2.5)   # seconds between Map scrolls (random range)
SITE_REQUEST_DELAY = (1.0, 3.0)   # seconds between website visits (random range)
REQUEST_TIMEOUT    = 10            # seconds before giving up on a website

# Pages checked per website (stops the moment emails are found)
CONTACT_PATHS = [
    "",
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/reach-us",
]

# ══════════════════════════════════════════════════════════════
#  END OF CONFIG
# ══════════════════════════════════════════════════════════════

# Search query assembled from NICHE + CITY
SEARCH_QUERY = f"{NICHE} in {CITY}"

# Cookie file named per city so each city keeps its own session
COOKIE_FILE = f"cookies_{CITY.lower().replace(' ', '_')}.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Domains that produce false-positive email matches
EMAIL_BLACKLIST = {
    "example.com", "yourdomain.com", "sentry.io",
    "wixpress.com", "squarespace.com", "wordpress.com",
    "googletagmanager.com", "google.com", "schema.org",
    "w3.org", "jquery.com", "cloudflare.com", "amazonaws.com",
}

# File extensions that look like TLDs but are never real email domains
_FAKE_TLDS = {
    "webp", "png", "jpg", "jpeg", "gif", "svg", "ico", "bmp",
    "pdf", "zip", "js", "css", "html", "xml", "json", "woff",
    "woff2", "ttf", "eot", "mp4", "mp3", "wav", "webm",
}

# Status constants written to the email_status CSV column
STATUS_FOUND      = "found"
STATUS_NO_WEBSITE = "no_website"
STATUS_BLOCKED    = "site_blocked"
STATUS_TIMEOUT    = "site_timeout"
STATUS_FORM_ONLY  = "contact_form_only"
STATUS_NO_EMAIL   = "no_email_on_page"

CSV_FIELDS = ["name", "website", "address", "emails", "email_status"]


class ScraperJob:
    """State container for a web-UI-triggered scrape run."""

    def __init__(self, niche, city, max_results, auto_import=True):
        self.niche        = niche
        self.city         = city
        self.max_results  = max_results
        self.auto_import  = auto_import

        self.stop_event    = threading.Event()
        self.captcha_event = threading.Event()

        self.status   = "running"   # running | captcha | done | stopped | error
        self.logs     = []          # [{msg, level}, ...]
        self.progress = 0           # businesses whose email phase is done
        self.total    = 0           # businesses found on Maps
        self.found    = 0           # businesses with at least one email
        self.imported = 0           # contacts upserted into the DB

    def log(self, msg, level="INFO"):
        self.logs.append({"msg": msg, "level": level})
        if len(self.logs) > 300:
            self.logs = self.logs[-300:]

    def stop(self):
        self.stop_event.set()

    def resume(self):
        """Called by the web UI when the user has solved a CAPTCHA."""
        self.captcha_event.set()


# ──────────────────────────────────────────────────────────────
#  CSV HELPERS
# ──────────────────────────────────────────────────────────────

def load_existing_records(path: str) -> dict:
    """
    Reads the output CSV if it exists.
    Returns a dict {business_name: row_dict}.
    Used so resumed runs can skip already-processed businesses.
    """
    existing = {}
    p = Path(path)
    if not p.exists():
        return existing
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            existing[row["name"]] = row
    log.info(f"Resume: loaded {len(existing)} existing records from '{path}'")
    return existing


def append_to_csv(record: dict, path: str):
    """
    Appends one row immediately after processing that business.
    Progress is saved to disk even if the script crashes mid-run.
    """
    write_header = not Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(record)


# ──────────────────────────────────────────────────────────────
#  COOKIE HELPERS
#  Persists the Google Maps browser session between runs.
#  Google treats a browser with cookie history as a real user.
# ──────────────────────────────────────────────────────────────

def save_cookies(context, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(context.cookies(), f)
    log.info(f"Session cookies saved → {path}")


def load_cookies(context, path: str):
    p = Path(path)
    if not p.exists():
        log.info("No saved cookies found — starting fresh session.")
        return
    with open(p, encoding="utf-8") as f:
        context.add_cookies(json.load(f))
    log.info(f"Session cookies loaded from {path}")


# ──────────────────────────────────────────────────────────────
#  CAPTCHA HANDLER
#  Checks the page body for CAPTCHA signals after every major
#  action. Pauses the script and waits for you to solve it
#  manually in the browser window.
# ──────────────────────────────────────────────────────────────

CAPTCHA_SIGNALS = [
    "recaptcha",
    "unusual traffic",
    "verify you're not a robot",
    "captcha",
]

def handle_captcha_if_present(page, job=None):
    try:
        body = page.inner_text("body").lower()
    except Exception:
        return
    if any(s in body for s in CAPTCHA_SIGNALS):
        if job:
            job.status = "captcha"
            job.log("⚠ CAPTCHA detected — solve it in the Chrome window, then click Resume in the app.", "WARN")
            job.captcha_event.wait()   # blocks until resume() is called from the web UI
            job.captcha_event.clear()  # reset for the next CAPTCHA
            job.status = "running"
            job.log("Resuming after CAPTCHA...")
        else:
            log.warning("")
            log.warning("⚠  CAPTCHA detected!")
            log.warning("   Solve it in the Chrome window, then come back here.")
            input("   Press ENTER to continue → ")
            time.sleep(2)
            log.info("Resuming...")


# ──────────────────────────────────────────────────────────────
#  STEP 1 — GOOGLE MAPS SCRAPER
#
#  How it works:
#    - Launches a real visible Chrome window (headless=False)
#    - Applies playwright-stealth to hide ~20 bot fingerprints
#      (most importantly: navigator.webdriver = false)
#    - Loads saved cookies so Google sees a returning user
#    - Scrolls the results sidebar with randomised amounts
#      and timing so scroll behaviour looks human
#    - Clicks each card, waits for the detail panel, grabs
#      website URL and address
#    - Saves cookies on exit for the next run
# ──────────────────────────────────────────────────────────────

def scrape_google_maps(query: str, max_results: int, skip_names: set, job=None) -> list:
    businesses = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        load_cookies(context, COOKIE_FILE)
        page = context.new_page()

        # Stealth patch — hides navigator.webdriver and other
        # signals that Google's JS probes for to detect bots
        Stealth().apply_stealth_sync(page)

        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        log.info(f"Opening: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        handle_captcha_if_present(page, job)

        results_panel = page.query_selector('div[role="feed"]')
        if not results_panel:
            log.error(
                "Results sidebar not found — Google may have updated its layout.\n"
                "Check the browser window and see what's on screen."
            )
            browser.close()
            return []

        seen_names        = set(skip_names)
        consecutive_empty = 0

        while len(businesses) < max_results:
            handle_captcha_if_present(page, job)

            cards           = page.query_selector_all('a.hfpxzc')
            new_this_scroll = 0

            for card in cards:
                if len(businesses) >= max_results:
                    break
                if job and job.stop_event.is_set():
                    break

                name = card.get_attribute("aria-label") or ""
                if not name or name in seen_names:
                    continue

                seen_names.add(name)
                new_this_scroll += 1

                try:
                    card.click()
                    time.sleep(random.uniform(*MAP_SCROLL_DELAY))

                    page.wait_for_timeout(2000)
                    handle_captcha_if_present(page, job)

                    # Website link on the Maps detail panel
                    website_el = page.query_selector('a[data-item-id="authority"]')
                    website = (
                        (website_el.get_attribute("href") or "").rstrip("/")
                        if website_el else ""
                    )

                    # Address button on the Maps detail panel
                    address_el = page.query_selector('button[data-item-id="address"]')
                    address = (
                        address_el.inner_text().replace("\n", " ")
                        if address_el else ""
                    )

                    businesses.append({
                        "name":    name,
                        "website": website,
                        "address": address,
                    })
                    log.info(
                        f"  [{len(businesses)}/{max_results}] "
                        f"{name} → {website or 'NO WEBSITE'}"
                    )

                except Exception as e:
                    log.warning(f"  Skipped '{name}': {e}")

            # Randomised scroll — amount and a mid-scroll micro-pause
            # so the pattern never looks like a bot looping 1000px
            scroll_px = random.randint(600, 1400)
            results_panel.evaluate(f"el => el.scrollBy(0, {scroll_px})")
            time.sleep(random.uniform(0.3, 0.8))
            results_panel.evaluate(f"el => el.scrollBy(0, {random.randint(30, 120)})")
            time.sleep(random.uniform(*MAP_SCROLL_DELAY))

            if new_this_scroll == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0

            if consecutive_empty >= 5:
                log.info("5 scrolls with no new results — end of list reached.")
                break

            end_el = page.query_selector("p.fontBodyMedium > span")
            if end_el and "end of results" in (end_el.inner_text() or "").lower():
                log.info("Google confirmed: end of results.")
                break

        save_cookies(context, COOKIE_FILE)
        browser.close()

    log.info(f"Maps scrape complete. {len(businesses)} businesses collected.")
    return businesses


# ──────────────────────────────────────────────────────────────
#  STEP 2 — EMAIL SCRAPER
#
#  How it works:
#    - Uses plain HTTP requests (no browser needed — clinic
#      sites are mostly simple WordPress/Wix pages)
#    - Tries up to 6 pages per site (homepage → /contact etc.)
#    - Stops as soon as emails are found on any page
#    - If no email found, classifies why using classify_page()
#    - Returns both the email string AND a status label
# ──────────────────────────────────────────────────────────────

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Signals that indicate a contact form is present but no email
FORM_SIGNALS = [
    "contact form", "send us a message", "get in touch",
    "<form", 'type="submit"', "contact-form",
    "wpcf7", "gravityforms", "ninja-forms",
]


def classify_page(soup: BeautifulSoup) -> str:
    """
    Called when no email is found on a page.
    Returns contact_form_only if a form is detected,
    otherwise no_email_on_page.
    """
    page_str = str(soup).lower()
    if any(sig in page_str for sig in FORM_SIGNALS):
        return STATUS_FORM_ONLY
    return STATUS_NO_EMAIL


def fetch_emails_from_url(url: str) -> tuple:
    """
    GETs a single URL and returns (set_of_emails, status).
    Searches both rendered text and raw HTML source so it
    catches emails inside href="mailto:..." links too.
    """
    try:
        resp = requests.get(
            url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT
        )
    except requests.exceptions.Timeout:
        return set(), STATUS_TIMEOUT
    except Exception:
        return set(), STATUS_BLOCKED

    if resp.status_code in (403, 429, 503):
        return set(), STATUS_BLOCKED

    try:
        resp.raise_for_status()
    except Exception:
        return set(), STATUS_BLOCKED

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strip <style> blocks before extracting visible text — they contain
    # font license comments with author emails (e.g. Raleway, Lato).
    for tag in soup.find_all("style"):
        tag.decompose()

    page_text = soup.get_text(separator=" ")

    # Strip <style>...</style> blocks from raw HTML too before regex scan.
    raw_no_css = re.sub(r"<style[\s\S]*?</style>", "", resp.text, flags=re.IGNORECASE)

    # Search rendered text + raw HTML (catches obfuscated/encoded emails)
    raw = (
        set(EMAIL_REGEX.findall(page_text))
        | set(EMAIL_REGEX.findall(raw_no_css))
    )

    clean = set()
    for e in raw:
        e = e.lower()
        domain = e.split("@")[-1]
        tld = domain.rsplit(".", 1)[-1]
        if tld in _FAKE_TLDS:
            continue
        if any(domain == b or domain.endswith("." + b) for b in EMAIL_BLACKLIST):
            continue
        clean.add(e)

    if clean:
        return clean, STATUS_FOUND

    return set(), classify_page(soup)


def get_emails_for_business(website: str) -> tuple:
    """
    Iterates through CONTACT_PATHS for one business website.
    Returns (emails_string, status_string).
    Stops early if emails are found or site is unreachable.
    """
    if not website:
        return "", STATUS_NO_WEBSITE

    last_status = STATUS_NO_EMAIL

    for path in CONTACT_PATHS:
        emails, status = fetch_emails_from_url(website + path)
        last_status    = status

        if emails:
            return "; ".join(sorted(emails)), STATUS_FOUND

        # Site is down or actively blocking — no point trying more pages
        if status in (STATUS_BLOCKED, STATUS_TIMEOUT):
            return "", status

        time.sleep(random.uniform(*SITE_REQUEST_DELAY))

    return "", last_status


# ──────────────────────────────────────────────────────────────
#  SUMMARY
# ──────────────────────────────────────────────────────────────

def print_summary(output_file: str):
    records = load_existing_records(output_file)
    counts  = Counter(r["email_status"] for r in records.values())
    total   = len(records)

    log.info("")
    log.info("══════════════════════════════════════════")
    log.info("  Run Summary")
    log.info(f"  Total rows in CSV : {total}")
    log.info("")
    for status, n in sorted(counts.items(), key=lambda x: -x[1]):
        pct = int(20 * n / total) if total else 0
        log.info(f"  {status:<22} {n:>4}  {'█' * pct}")
    manual = counts.get(STATUS_FORM_ONLY, 0) + counts.get(STATUS_NO_EMAIL, 0)
    log.info("")
    log.info(f"  Manual lookup candidates: {manual}")
    log.info(f"  Output: {output_file}")
    log.info("══════════════════════════════════════════")


# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────

def run_scraper_job(job: ScraperJob):
    """
    Full scrape orchestrated by a ScraperJob.
    Runs in a background thread launched by the web app.
    Progress, logs, and CAPTCHA state are written to the job object
    and polled by the frontend every 2 seconds.
    """
    import db as _db

    try:
        search_query = f"{job.niche} in {job.city}"
        cookie_file  = f"cookies_{job.city.lower().replace(' ', '_')}.json"
        output_file  = (
            f"{job.city.lower().replace(' ', '_')}_"
            f"{job.niche.lower().replace(' ', '_')}.csv"
        )

        job.log(f"Starting: {search_query}")
        job.log(f"Target: {job.max_results} results → {output_file}")

        # ── Step 1: Google Maps ─────────────────────────────────────────────
        businesses = scrape_google_maps(search_query, job.max_results, set(), job)

        if job.stop_event.is_set():
            job.log("Stopped by user.")
            job.status = "stopped"
            return

        if not businesses:
            job.log("No businesses found. Check niche/city and try again.", "WARN")
            job.status = "done"
            return

        job.total = len(businesses)
        job.log(f"Maps done — {job.total} businesses found. Scraping emails...")

        # ── Step 2: Email scraping ──────────────────────────────────────────
        contacts_to_import = []

        for i, biz in enumerate(businesses):
            if job.stop_event.is_set():
                job.log("Stopped by user.")
                break

            emails, status = get_emails_for_business(biz["website"])
            biz["emails"]       = emails
            biz["email_status"] = status
            job.progress = i + 1

            if status == STATUS_FOUND:
                job.found += 1
                job.log(f"[{i+1}/{job.total}] ✓ {biz['name']} — {emails}")
                for raw in emails.split(";"):
                    raw = raw.strip()
                    if not raw:
                        continue
                    mx_ok = _ev.check_mx(raw)
                    if not mx_ok:
                        job.log(f"  ↳ {raw} — invalid MX (saved for review)")
                    contacts_to_import.append({
                        "email":      raw,
                        "company":    biz.get("name", ""),
                        "first_name": "",
                        "last_name":  "",
                        "website":    biz.get("website", ""),
                        "address":    biz.get("address", ""),
                        "mx_valid":   1 if mx_ok else 0,
                    })
            elif biz.get("website") and status != STATUS_NO_WEBSITE:
                # Store as a prospect so the user can follow up manually
                prospect_status = "form_only" if status == STATUS_FORM_ONLY else "no_email"
                contacts_to_import.append({
                    "email":   "",
                    "company": biz.get("name", ""),
                    "website": biz.get("website", ""),
                    "address": biz.get("address", ""),
                    "status":  prospect_status,
                })
                job.log(f"[{i+1}/{job.total}] – {biz['name']} — {status} (saved as prospect)")
            else:
                job.log(f"[{i+1}/{job.total}] ✗ {biz['name']} — {status}")

            append_to_csv({k: biz.get(k, "") for k in CSV_FIELDS}, output_file)
            time.sleep(random.uniform(*SITE_REQUEST_DELAY))

        # ── Step 3: Auto-import to DB ───────────────────────────────────────
        if job.auto_import and contacts_to_import:
            job.imported = _db.upsert_contacts(contacts_to_import)
            job.log(f"✓ {job.imported} contacts imported into the database.")
        elif contacts_to_import:
            job.log(f"{len(contacts_to_import)} emails found — saved to {output_file} (auto-import was off).")

        job.log(
            f"Complete. {job.found}/{job.total} businesses had emails. "
            f"{job.imported} contacts imported."
        )
        job.status = "done"

    except Exception as e:
        job.log(f"Scraper error: {e}", "ERROR")
        job.status = "error"


def main():
    global NICHE, CITY, MAX_RESULTS, OUTPUT_FILE, SEARCH_QUERY, COOKIE_FILE

    parser = argparse.ArgumentParser(description="Google Maps Email Scraper")
    parser.add_argument("--niche",  default=NICHE,        help="Business type, e.g. 'dental clinics'")
    parser.add_argument("--city",   default=CITY,         help="City + country, e.g. 'Toronto Canada'")
    parser.add_argument("--max",    default=MAX_RESULTS,  type=int, help="Max results to collect")
    parser.add_argument("--output", default=None,         help="Output CSV filename (auto-generated if omitted)")
    args = parser.parse_args()

    NICHE       = args.niche
    CITY        = args.city
    MAX_RESULTS = args.max
    OUTPUT_FILE = args.output or f"{CITY.lower().replace(' ', '_')}_{NICHE.lower().replace(' ', '_')}.csv"
    SEARCH_QUERY = f"{NICHE} in {CITY}"
    COOKIE_FILE  = f"cookies_{CITY.lower().replace(' ', '_')}.json"

    log.info("══════════════════════════════════════════")
    log.info("  Google Maps Email Scraper  v3")
    log.info(f"  Query  : {SEARCH_QUERY}")
    log.info(f"  Target : {MAX_RESULTS} results")
    log.info(f"  Output : {OUTPUT_FILE}")
    log.info("══════════════════════════════════════════")
    log.info("")
    log.info("Chrome will open. Solve any CAPTCHAs there,")
    log.info("then press ENTER here to resume.")
    log.info("")

    # Load already-processed businesses for resume support
    existing   = load_existing_records(OUTPUT_FILE)
    skip_names = set(existing.keys())

    if skip_names:
        log.info(f"Resuming — skipping {len(skip_names)} already-processed entries.")

    # ── Step 1: Google Maps ───────────────────────────────────
    businesses = scrape_google_maps(SEARCH_QUERY, MAX_RESULTS, skip_names)

    if not businesses:
        if existing:
            log.info("No new businesses found. Existing CSV is up to date.")
        else:
            log.error("Nothing found. Check your NICHE/CITY config and try again.")
        print_summary(OUTPUT_FILE)
        return

    # ── Step 2: Scrape emails from each website ───────────────
    log.info("")
    log.info(f"Scraping emails from {len(businesses)} websites...")
    log.info("")

    for i, biz in enumerate(businesses, 1):
        log.info(f"[{i}/{len(businesses)}] {biz['name']}")
        log.info(f"  → {biz['website'] or 'NO WEBSITE'}")

        emails, status = get_emails_for_business(biz["website"])

        if status == STATUS_FOUND:
            log.info(f"  ✓ {emails}")
        else:
            log.info(f"  ✗ {status}")

        append_to_csv(
            {**biz, "emails": emails, "email_status": status},
            OUTPUT_FILE,
        )
        time.sleep(random.uniform(*SITE_REQUEST_DELAY))

    print_summary(OUTPUT_FILE)


if __name__ == "__main__":
    main()
