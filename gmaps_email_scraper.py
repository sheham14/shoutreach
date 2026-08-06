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
import sys
import threading
import time
import random
import logging
import urllib.parse
from pathlib import Path
from collections import Counter

import email_validator as _ev

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# Small-business sites routinely have expired or misconfigured certificates.
# We retry those once with verification off (see _fetch), so silence the noise.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# The console on Windows defaults to cp1252, which cannot encode the check
# marks and block characters used in the log output below. Without this the
# logging module swallows a UnicodeEncodeError per line and mangles the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # already UTF-8, or not a real stream (captured output)

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
# Split out of the old catch-all site_blocked so the CSV says what actually
# happened. Previously an expired certificate and a Cloudflare 403 both read
# as "blocked", which made the failures look unfixable when most were not.
STATUS_SSL_ERROR  = "site_ssl_error"
STATUS_DNS_ERROR  = "site_dns_error"
STATUS_DEAD       = "site_unreachable"

# Statuses worth a second attempt through a real browser: the site answered
# (or refused) in a way that a plain HTTP client often cannot get past.
BROWSER_RETRY_STATUSES = (STATUS_BLOCKED, STATUS_NO_EMAIL, STATUS_FORM_ONLY)

CSV_FIELDS = [
    "name", "website", "address", "phone", "rating", "reviews", "category",
    "emails", "email_status",
]


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

def safe_filename(name: str) -> str:
    """
    Turn a niche/city string into a filesystem-safe filename fragment.
    'St. John's Newfoundland' previously produced st_john's_...csv -- legal on
    Windows but awkward to quote in every shell and script that touches it.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip().lower())
    return cleaned.strip("_") or "output"


def read_csv_rows(path: str) -> list:
    """
    Every row in the output CSV, duplicates included.

    print_summary used to count via load_existing_records(), which is keyed by
    business name -- so two rows for the same business collapsed into one and
    the totals silently under-reported.
    """
    p = Path(path)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [row for row in csv.DictReader(f)]


def load_existing_records(path: str) -> dict:
    """
    Reads the output CSV if it exists.
    Returns a dict {business_name: row_dict}.
    Used so resumed runs can skip already-processed businesses.
    """
    existing = {}
    for row in read_csv_rows(path):
        name = row.get("name")
        if name:
            existing[name] = row
    if existing:
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


def rewrite_csv_rows(records: list, path: str):
    """
    Replace the rows written during this run with their final state.

    Rows are appended as each business is processed so a crash never loses the
    run, but the browser fallback resolves some of them afterwards. Rather than
    leave the file disagreeing with what was imported, rewrite it once at the
    end -- preserving any rows from earlier runs that are not in this batch.
    """
    names_now = {r.get("name") for r in records}
    kept = [r for r in read_csv_rows(path) if r.get("name") not in names_now]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in kept + records:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


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

# Phrases that only appear on a genuine Google challenge page. The previous
# version substring-matched "captcha" against the whole page body, which fires
# on any listing whose text happens to mention it -- and this check runs after
# every card click, so a false positive stalls the run waiting for a CAPTCHA
# that was never there.
CAPTCHA_TEXT_SIGNALS = [
    "unusual traffic from your computer network",
    "verify you're not a robot",
    "our systems have detected unusual traffic",
]


def _captcha_present(page) -> bool:
    """
    Structure first, text second. Google serves its interstitial from
    /sorry/ and mounts the widget in a recaptcha iframe -- both are far more
    reliable than reading the body text.
    """
    try:
        if "/sorry/" in (page.url or ""):
            return True
    except Exception:
        pass
    try:
        if page.query_selector('iframe[src*="recaptcha"], form#captcha-form, div#recaptcha'):
            return True
    except Exception:
        pass
    try:
        body = page.inner_text("body").lower()
    except Exception:
        return False
    return any(s in body for s in CAPTCHA_TEXT_SIGNALS)


def handle_captcha_if_present(page, job=None):
    if _captcha_present(page):
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

# Google's obfuscated class names change without notice. If a run suddenly
# collects zero businesses, check this selector against the live DOM first.
RESULT_CARD_SELECTOR = "a.hfpxzc"


def _text_of(page, selector: str) -> str:
    try:
        el = page.query_selector(selector)
        return (el.inner_text() or "").strip() if el else ""
    except Exception:
        return ""


PANEL_MATCH_TIMEOUT = 10.0   # seconds to wait for the panel to catch up
PANEL_RETRY_LIMIT   = 2      # attempts before giving up on a business


def _wait_for_detail_panel(page, name: str, timeout: float = PANEL_MATCH_TIMEOUT) -> bool:
    """
    Wait until the open detail panel actually belongs to `name`.

    Clicking a result card swaps the panel contents asynchronously. The old
    code slept a fixed ~3-4s and then read whatever was on screen, so a slow
    panel meant reading the PREVIOUS business's website, address and phone and
    filing them under the new business's name -- the name comes from the card's
    aria-label and is always right, which is what made the mismatch invisible.

    Observed live: "Highgate Health" was recorded with Polygon Health's website
    and email, and "MSK Health and Performance Clinic" with Physio Train's.

    Returns False on timeout so the caller can skip rather than record a
    business stapled to someone else's contact details.
    """
    target = (name or "").strip()
    if not target:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # The detail pane carries the business name as its aria-label.
            # Preferred over the obfuscated heading class, which changes.
            panel = page.query_selector('div[role="main"][aria-label]')
            if panel:
                label = (panel.get_attribute("aria-label") or "").strip()
                if label == target:
                    return True
            # Fallback: the visible <h1> title.
            heading = page.query_selector('div[role="main"] h1')
            if heading and (heading.inner_text() or "").strip() == target:
                return True
        except Exception:
            pass
        page.wait_for_timeout(200)
    return False


def _read_detail_panel(page) -> dict:
    """
    Scrape the open Maps detail panel.

    Rating, review count and phone are all sitting there already and cost
    nothing extra to read. They are what let you qualify a list before
    spending sends on it -- and for a web agency, a business with no website
    is a lead rather than a failure, which needs the phone number to act on.
    """
    website_el = None
    try:
        website_el = page.query_selector('a[data-item-id="authority"]')
    except Exception:
        pass
    website = (website_el.get_attribute("href") or "") if website_el else ""

    phone = ""
    try:
        phone_el = page.query_selector('button[data-item-id^="phone"]')
        if phone_el:
            phone = (phone_el.get_attribute("data-item-id") or "").split(":")[-1].strip()
            if not phone:
                phone = (phone_el.inner_text() or "").strip()
    except Exception:
        pass

    # The rating block reads "4.8 stars 127 reviews" via aria-label.
    rating, reviews = "", ""
    try:
        el = page.query_selector('div.F7nice span[aria-hidden="true"]')
        if el:
            rating = (el.inner_text() or "").strip()
        el = page.query_selector('div.F7nice span[aria-label*="review"]')
        if el:
            label = el.get_attribute("aria-label") or ""
            digits = re.sub(r"[^\d]", "", label)
            reviews = digits
    except Exception:
        pass

    return {
        "website":  website.rstrip("/"),
        "address":  _text_of(page, 'button[data-item-id="address"]').replace("\n", " "),
        "phone":    phone,
        "rating":   rating,
        "reviews":  reviews,
        "category": _text_of(page, "button.DkEaL"),
    }


def scrape_google_maps(query: str, max_results: int, skip_names: set, job=None) -> list:
    businesses = []

    with sync_playwright() as p:
        # Prefer the real installed Chrome: it carries a genuine, current UA
        # and build fingerprint, which bundled Chromium does not. Fall back if
        # Chrome is not on this machine.
        try:
            browser = p.chromium.launch(
                headless=False, channel="chrome", args=["--start-maximized"]
            )
        except Exception:
            log.info("Chrome not available — falling back to bundled Chromium")
            browser = p.chromium.launch(headless=False, args=["--start-maximized"])

        context = browser.new_context(
            user_agent=BROWSER_UA,
            # Let the viewport follow the real (maximized) window. Setting an
            # explicit viewport alongside --start-maximized produced a maximized
            # window rendering at a fixed 1280x900 -- a mismatch bot detection
            # can see.
            no_viewport=True,
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
        panel_failures    = {}   # name -> attempts, bounds the retry above

        while len(businesses) < max_results:
            handle_captcha_if_present(page, job)

            # Read the names up front, then re-fetch each card by index right
            # before clicking it. Clicking opens the detail panel and re-renders
            # the feed, which invalidates every handle captured beforehand --
            # the old code held the whole list across clicks, so later cards
            # raised on .click() and were swallowed as "Skipped '<name>'".
            cards           = page.query_selector_all(RESULT_CARD_SELECTOR)
            pending         = []
            for idx, card in enumerate(cards):
                try:
                    label = card.get_attribute("aria-label") or ""
                except Exception:
                    continue
                if label:
                    pending.append((idx, label))

            new_this_scroll = 0

            for idx, name in pending:
                if len(businesses) >= max_results:
                    break
                if job and job.stop_event.is_set():
                    break

                if name in seen_names:
                    continue

                seen_names.add(name)
                new_this_scroll += 1

                try:
                    fresh = page.query_selector_all(RESULT_CARD_SELECTOR)
                    if idx >= len(fresh):
                        continue          # feed shrank under us; catch it next scroll
                    fresh[idx].click()
                    time.sleep(random.uniform(*MAP_SCROLL_DELAY))
                    handle_captcha_if_present(page, job)

                    # Confirm the panel is showing THIS business before reading
                    # it. Never fall back to a fixed sleep: that is what caused
                    # businesses to be saved with the previous one's website.
                    if not _wait_for_detail_panel(page, name):
                        panel_failures[name] = panel_failures.get(name, 0) + 1
                        if panel_failures[name] < PANEL_RETRY_LIMIT:
                            # Put it back so a later scroll pass can retry --
                            # but bounded, or a permanently broken panel would
                            # loop forever and the scrape would never finish.
                            seen_names.discard(name)
                            log.info(f"  Panel slow for '{name}' — will retry")
                        else:
                            log.warning(
                                f"  Skipped '{name}': detail panel never loaded. "
                                f"Recording it would risk the previous business's details."
                            )
                        continue

                    record = {"name": name}
                    record.update(_read_detail_panel(page))

                    businesses.append(record)
                    log.info(
                        f"  [{len(businesses)}/{max_results}] "
                        f"{name} → {record['website'] or 'NO WEBSITE'}"
                        + (f"  ({record['rating']}★ {record['reviews']})"
                           if record.get("rating") else "")
                    )

                except Exception as e:
                    log.warning(f"  Skipped '{name}': {e}")

            # Randomised scroll — amount and a mid-scroll micro-pause
            # so the pattern never looks like a bot looping 1000px.
            # Re-query the panel: the handle taken before the click loop goes
            # stale for the same reason the card handles do.
            results_panel = page.query_selector('div[role="feed"]') or results_panel
            try:
                scroll_px = random.randint(600, 1400)
                results_panel.evaluate(f"el => el.scrollBy(0, {scroll_px})")
                time.sleep(random.uniform(0.3, 0.8))
                results_panel.evaluate(f"el => el.scrollBy(0, {random.randint(30, 120)})")
            except Exception as exc:
                log.warning(f"  Scroll failed ({type(exc).__name__}) — retrying next pass")
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

# Keep the Chrome version roughly current -- a years-old UA string is itself a
# bot signal to WAFs. Bump it when you notice more 403s than usual.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    # Some WAFs flag requests that ask for a bare document with no navigation
    # context. These are what a real Chrome tab sends on a top-level load.
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
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


# ──────────────────────────────────────────────────────────────
#  HTTP SESSION
#
#  One pooled session per thread, with retries. Roughly half the
#  sites previously written off as "site_blocked" were actually
#  transient failures that a single retry recovers.
# ──────────────────────────────────────────────────────────────

_thread_local = threading.local()


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    retry = Retry(
        total=2,
        backoff_factor=1,               # 0s, 1s, 2s between attempts
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _session() -> requests.Session:
    """Sessions are not thread-safe, and the recall harness probes in parallel."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = _build_session()
    return _thread_local.session


def _normalize_entry_url(website: str) -> str:
    """
    Strip the query string and fragment off a Google Maps website link.

    Maps hands out tracking-tagged URLs like
    'http://avalondental.ca/?utm_source=google&utm_campaign=gmb'. The old code
    concatenated paths onto that string, producing
    '...&utm_campaign=gmb/contact' -- so every contact page fetch hit garbage
    and only the homepage ever loaded.
    """
    website = (website or "").strip()
    if not website:
        return ""
    if "//" not in website:
        website = "https://" + website
    parts = urllib.parse.urlsplit(website)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _is_dns_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(s in text for s in (
        "nodename nor servname", "name or service not known",
        "getaddrinfo failed", "failed to resolve", "no address associated",
        "temporary failure in name resolution",
    ))


# requests' `timeout` is a per-socket-read deadline, NOT a total one: it resets
# every time a byte arrives. A server that trickles the body a byte at a time
# keeps the connection alive indefinitely, and a scrape parked on one such site
# stalls the whole queue with no error and no way out. These two ceilings are
# what actually bound the work.
MAX_RESPONSE_BYTES = 3 * 1024 * 1024   # pages are text; 3 MB is already absurd
BODY_READ_BUDGET   = 20                # seconds to stream one body, start to finish
BUSINESS_BUDGET    = 90                # seconds for a whole business, all pages
FETCH_HARD_DEADLINE = 30               # seconds before one request is abandoned


def _force_close(resp):
    """Tear the socket down so a blocked read raises instead of waiting."""
    for closer in (getattr(resp, "raw", None), resp):
        try:
            if closer is not None:
                closer.close()
        except Exception:
            pass


def _read_body(resp) -> bool:
    """
    Stream the body under a hard wall-clock and size ceiling.

    The deadline is enforced by a watchdog that closes the socket, NOT by
    checking the clock between chunks. Checking between chunks looks correct
    and does nothing: iter_content bottoms out in BufferedReader.read(n), which
    blocks until it has all n bytes or hits EOF, so a server dribbling one byte
    every couple of seconds never yields control back to the loop. Closing the
    socket from another thread is what actually interrupts it.

    Returns False if either ceiling was hit. The response keeps whatever
    arrived -- a truncated page is still worth scanning for an address.
    Populating `_content` is what lets `resp.text` work downstream despite
    stream=True.
    """
    deadline = time.monotonic() + BODY_READ_BUDGET
    watchdog = threading.Timer(BODY_READ_BUDGET, _force_close, args=(resp,))
    watchdog.daemon = True
    watchdog.start()

    chunks, total, complete = [], 0, True
    try:
        # Modest chunks so normal pages still stream efficiently while the
        # size cap stays responsive.
        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                complete = False
                break
    except Exception:
        # Includes the watchdog yanking the socket out from under the read.
        complete = False
    finally:
        watchdog.cancel()
        resp._content = b"".join(chunks)
        resp._content_consumed = True
        _force_close(resp)
    return complete


def _fetch(url: str):
    """
    GET one URL under a hard deadline. Returns (response, error_status).

    The request runs on a daemon thread we are willing to abandon. Nothing else
    reliably bounds it: requests' timeout resets on every byte received, and
    closing the socket from another thread does not unblock a pending recv on
    Windows. If the thread does not finish in time we walk away and let it die
    with the process -- one stalled lead must never hold up the queue, and the
    observed failure was a site that parked a scrape for ten minutes.
    """
    box = {}

    def _work():
        try:
            box["result"] = _fetch_blocking(url)
        except BaseException as exc:      # noqa: BLE001 - reported, not raised
            box["error"] = exc

    thread = threading.Thread(target=_work, name=f"fetch-{url[:40]}", daemon=True)
    thread.start()
    thread.join(FETCH_HARD_DEADLINE)

    if thread.is_alive():
        log.debug("Abandoned a stalled request after %ss: %s",
                  FETCH_HARD_DEADLINE, url)
        return None, STATUS_TIMEOUT
    if "error" in box:
        return None, STATUS_DEAD
    return box.get("result", (None, STATUS_DEAD))


def _fetch_blocking(url: str):
    """The actual request. Always call it through _fetch, never directly."""
    session = _session()
    # (connect timeout, read timeout) -- a slow server should not cost the
    # same budget as an unreachable one. See _read_body for the total ceiling.
    timeouts = (5, REQUEST_TIMEOUT)
    try:
        resp = session.get(url, timeout=timeouts, allow_redirects=True, stream=True)
    except requests.exceptions.SSLError:
        # Expired/mismatched certs are routine on small business sites and are
        # not a reason to drop a lead. Retry once without verification.
        try:
            resp = session.get(url, timeout=timeouts, allow_redirects=True,
                               verify=False, stream=True)
        except Exception:
            return None, STATUS_SSL_ERROR
    except requests.exceptions.Timeout:
        return None, STATUS_TIMEOUT
    except requests.exceptions.ConnectionError as exc:
        return None, (STATUS_DNS_ERROR if _is_dns_failure(exc) else STATUS_DEAD)
    except Exception:
        return None, STATUS_DEAD

    if not _read_body(resp):
        log.debug("Body truncated at the size/time ceiling: %s", url)

    if resp.status_code in (401, 403, 429):
        return resp, STATUS_BLOCKED
    if resp.status_code >= 400:
        # A missing subpage must not condemn the whole site -- the caller
        # decides whether this was the homepage or just one candidate link.
        return resp, STATUS_NO_EMAIL
    return resp, None


# ──────────────────────────────────────────────────────────────
#  EMAIL EXTRACTION
# ──────────────────────────────────────────────────────────────

# Cloudflare's email-obfuscation writes <a href="/cdn-cgi/l/email-protection#hex">
# and decodes it client-side. Plain HTTP scraping sees only the hex, so any site
# using it looked email-free. The first byte is the XOR key.
_CFEMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')

# name [at] domain [dot] com  /  name(at)domain(dot)com
_OBFUSCATED_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*[\[\(]?\s*(?:at|@)\s*[\]\)]?\s*"
    r"([a-zA-Z0-9.\-]+)\s*[\[\(]\s*(?:dot|\.)\s*[\]\)]\s*([a-zA-Z]{2,})",
    re.IGNORECASE,
)


def _decode_cfemail(hex_str: str) -> str:
    try:
        raw = bytes.fromhex(hex_str)
        key = raw[0]
        return "".join(chr(b ^ key) for b in raw[1:])
    except Exception:
        return ""


def _is_real_email(email: str) -> bool:
    domain = email.split("@")[-1]
    if domain.rsplit(".", 1)[-1] in _FAKE_TLDS:
        return False
    return not any(domain == b or domain.endswith("." + b) for b in EMAIL_BLACKLIST)


def _clean_emails(candidates) -> set:
    return {e.lower() for e in candidates if _is_real_email(e.lower())}


def extract_emails(html: str, soup: BeautifulSoup) -> set:
    """
    Pull every address off one page, most reliable sources first.
    Returns already-filtered addresses.
    """
    # 1. mailto: links -- unambiguous, an author put it there deliberately.
    found = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            addr = urllib.parse.unquote(href[7:].split("?")[0]).strip()
            found.update(EMAIL_REGEX.findall(addr))

    # 2. Structured data -- schema.org LocalBusiness blocks carry a clean
    #    "email" field on a good number of agency-built sites.
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        for match in EMAIL_REGEX.findall(tag.get_text() or ""):
            found.add(match)

    # 3. Cloudflare-obfuscated addresses.
    for hex_str in _CFEMAIL_RE.findall(html):
        decoded = _decode_cfemail(hex_str)
        if decoded:
            found.update(EMAIL_REGEX.findall(decoded))

    # 4. Human obfuscation: "info [at] clinic [dot] ca".
    for user, domain, tld in _OBFUSCATED_RE.findall(html):
        found.add(f"{user}@{domain}.{tld}")

    clean = _clean_emails(found)
    if clean:
        return clean

    # 5. Fall back to scanning the whole page.
    #
    #    Strip <style> only, never <script>. Inline stylesheets carry bundled
    #    font licences whose author addresses look completely real -- that is
    #    where team@latofonts.com came from, and no domain blacklist can catch
    #    a font designer's gmail. Script blocks are the opposite: CMS config
    #    objects genuinely hold the business address (Drupal's
    #    "practice_email", Wix and Squarespace contact settings), and the junk
    #    they carry -- Sentry DSNs and analytics IDs -- is already covered by
    #    EMAIL_BLACKLIST. Dropping scripts loses real leads.
    for tag in soup.find_all("style"):
        tag.decompose()
    stripped = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    return _clean_emails(
        set(EMAIL_REGEX.findall(soup.get_text(separator=" ")))
        | set(EMAIL_REGEX.findall(stripped))
    )


# ──────────────────────────────────────────────────────────────
#  CONTACT PAGE DISCOVERY
# ──────────────────────────────────────────────────────────────

_CONTACT_LINK_RE = re.compile(
    r"contact|about|team|staff|reach|connect|our-office|meet-us", re.IGNORECASE
)
MAX_DISCOVERED_LINKS = 4


def discover_contact_links(soup: BeautifulSoup, base_url: str) -> list:
    """
    Find the site's real contact pages by reading its own navigation, rather
    than guessing at /contact, /contact-us and hoping. Guessing misses
    /contact-us-today, /en/contact, /book-appointment and every non-English
    variant; the nav does not.
    """
    base_host = urllib.parse.urlsplit(base_url).netloc.lower()
    scored = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text = (a.get_text() or "").strip().lower()
        if not (_CONTACT_LINK_RE.search(text) or _CONTACT_LINK_RE.search(href)):
            continue
        full = urllib.parse.urljoin(base_url, href)
        full = urllib.parse.urldefrag(full)[0]
        if urllib.parse.urlsplit(full).netloc.lower() != base_host:
            continue
        # "contact" outranks "about" -- it is likelier to carry an address.
        rank = 0 if "contact" in text or "contact" in href.lower() else 1
        scored.append((rank, full))

    ordered, seen = [], set()
    for _, url in sorted(scored, key=lambda x: x[0]):
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered[:MAX_DISCOVERED_LINKS]


# ──────────────────────────────────────────────────────────────
#  PER-BUSINESS ORCHESTRATION
# ──────────────────────────────────────────────────────────────

def _soup_of(resp) -> BeautifulSoup:
    return BeautifulSoup(resp.text, "html.parser")


def get_emails_for_business(website: str) -> tuple:
    """
    Resolve one business website to (emails_string, status).

    Fetches the homepage, follows the site's own contact links, and falls back
    to guessed paths only when the page exposes no usable navigation.
    """
    entry = _normalize_entry_url(website)
    if not entry:
        return "", STATUS_NO_WEBSITE

    # Second ceiling, on top of the per-body one: five individually-legal but
    # slow pages still add up, and no single lead is worth stalling the queue.
    give_up_at = time.monotonic() + BUSINESS_BUDGET

    resp, err = _fetch(entry)
    if resp is None:
        return "", err                       # timeout / DNS / SSL / unreachable
    if err == STATUS_BLOCKED:
        return "", STATUS_BLOCKED            # queued for the browser fallback

    # Every later URL resolves against where we actually landed, so www ->
    # apex and http -> https redirects do not silently break path joining.
    base_url = resp.url
    soup = _soup_of(resp)
    home_html = resp.text

    emails = extract_emails(home_html, soup)
    if emails:
        return "; ".join(sorted(emails)), STATUS_FOUND

    # Re-parse: extract_emails() decomposes <script>/<style> from the soup.
    nav_soup = _soup_of(resp)
    candidates = discover_contact_links(nav_soup, base_url)

    if not candidates:
        root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(base_url))
        candidates = [urllib.parse.urljoin(root, p) for p in CONTACT_PATHS if p]

    last_status = classify_page(nav_soup)
    for url in candidates:
        if time.monotonic() > give_up_at:
            log.debug("Budget exhausted for %s — moving on", entry)
            break
        time.sleep(random.uniform(*SITE_REQUEST_DELAY))
        sub_resp, sub_err = _fetch(url)
        if sub_resp is None:
            continue                          # one bad subpage is not a dead site
        if sub_err == STATUS_BLOCKED:
            last_status = STATUS_BLOCKED
            continue
        sub_soup = _soup_of(sub_resp)
        sub_emails = extract_emails(sub_resp.text, sub_soup)
        if sub_emails:
            return "; ".join(sorted(sub_emails)), STATUS_FOUND
        if sub_err is None:
            found_form = classify_page(_soup_of(sub_resp))
            if found_form == STATUS_FORM_ONLY:
                last_status = STATUS_FORM_ONLY

    return "", last_status


# ──────────────────────────────────────────────────────────────
#  BROWSER FALLBACK
#
#  Last resort for sites the HTTP client cannot read: WAFs that
#  refuse non-browser clients, and pages that inject the contact
#  details with JavaScript after load. Runs once per scrape over
#  the whole batch rather than per site, so the browser start-up
#  cost is paid a single time.
# ──────────────────────────────────────────────────────────────

BROWSER_FALLBACK_TIMEOUT = 25000   # ms per navigation
BROWSER_SETTLE_MS        = 2000    # let late JS paint the contact block


def _browser_emails_on_page(page) -> set:
    """Extract from the rendered DOM. Mirrors extract_emails' source order."""
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    emails = extract_emails(html, soup)
    if emails:
        return emails
    # The rendered text can hold addresses that never appear in the markup,
    # e.g. assembled from JS string fragments to defeat exactly this scrape.
    try:
        return _clean_emails(set(EMAIL_REGEX.findall(page.inner_text("body"))))
    except Exception:
        return set()


def _browser_contact_links(page, base_url: str, limit: int = 2) -> list:
    try:
        pairs = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => [ (e.innerText||'').toLowerCase(), e.href ])",
        )
    except Exception:
        return []
    base_host = urllib.parse.urlsplit(base_url).netloc.lower()
    out = []
    for text, href in pairs:
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        if not (_CONTACT_LINK_RE.search(text) or _CONTACT_LINK_RE.search(href)):
            continue
        if urllib.parse.urlsplit(href).netloc.lower() != base_host:
            continue
        href = urllib.parse.urldefrag(href)[0]
        if href not in out:
            out.append(href)
    return out[:limit]


def browser_fallback(websites: list, log_fn=None) -> dict:
    """
    Retry a batch of sites in a real browser.

    `websites` is a list of website strings. Returns
    {original_website: (emails_string, status)} for the ones that resolved --
    sites that stay empty are simply absent, so the caller keeps its existing
    status for them.

    `log_fn` lets the web-UI job route these lines into its own live log;
    without it they go to the module logger.
    """
    results = {}
    targets = [w for w in websites if (w or "").strip()]
    if not targets:
        return results

    def _say(msg):
        if log_fn:
            log_fn(msg)
        else:
            log.info(msg)

    _say(f"Browser fallback: retrying {len(targets)} site(s) that HTTP could not read")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=BROWSER_UA,
                viewport={"width": 1366, "height": 900},
                ignore_https_errors=True,   # matches the HTTP path's cert leniency
            )
            page = context.new_page()

            # Images and fonts are pure cost here -- we only read text.
            def _block(route):
                if route.request.resource_type in ("image", "media", "font"):
                    route.abort()
                else:
                    route.continue_()
            try:
                page.route("**/*", _block)
            except Exception:
                pass

            for site in targets:
                entry = _normalize_entry_url(site)
                try:
                    page.goto(entry, wait_until="domcontentloaded",
                              timeout=BROWSER_FALLBACK_TIMEOUT)
                    page.wait_for_timeout(BROWSER_SETTLE_MS)
                except Exception as exc:
                    _say(f"  browser: {entry} unreachable ({type(exc).__name__})")
                    continue

                emails = _browser_emails_on_page(page)
                if not emails:
                    for link in _browser_contact_links(page, page.url):
                        try:
                            page.goto(link, wait_until="domcontentloaded",
                                      timeout=BROWSER_FALLBACK_TIMEOUT)
                            page.wait_for_timeout(BROWSER_SETTLE_MS)
                        except Exception:
                            continue
                        emails = _browser_emails_on_page(page)
                        if emails:
                            break

                if emails:
                    results[site] = ("; ".join(sorted(emails)), STATUS_FOUND)
                    _say(f"  browser recovered: {sorted(emails)[0]}")

            browser.close()
    except Exception as exc:
        _say(f"Browser fallback unavailable ({type(exc).__name__}: {exc})")

    _say(f"Browser fallback recovered {len(results)} of {len(targets)} site(s)")
    return results


# ──────────────────────────────────────────────────────────────
#  SUMMARY
# ──────────────────────────────────────────────────────────────

def print_summary(output_file: str):
    # Count every row, not the name-keyed dict -- two rows for the same
    # business are two leads, and collapsing them under-reported the run.
    records = read_csv_rows(output_file)
    counts  = Counter(r.get("email_status", "") for r in records)
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
        output_file  = f"{safe_filename(job.city)}_{safe_filename(job.niche)}.csv"

        job.log(f"Starting: {search_query}")
        job.log(f"Target: {job.max_results} results → {output_file}")

        # ── Resume set ──────────────────────────────────────────────────────
        # This used to pass an empty set, so the web UI had no resume support
        # at all: running "dentists in St John's" after "dental clinics in
        # St John's" re-scraped every overlapping business from scratch.
        skip_names = set(load_existing_records(output_file).keys())
        try:
            skip_names |= _db.get_known_company_names()
        except Exception as exc:
            job.log(f"Could not read known companies from the database: {exc}", "WARN")
        if skip_names:
            job.log(f"Resume: skipping {len(skip_names)} business(es) already collected")

        # ── Step 1: Google Maps ─────────────────────────────────────────────
        businesses = scrape_google_maps(search_query, job.max_results, skip_names, job)

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

        # ── Step 2: Email scraping over HTTP ────────────────────────────────
        processed = []

        for i, biz in enumerate(businesses):
            if job.stop_event.is_set():
                job.log("Stopped by user.")
                break

            emails, status = get_emails_for_business(biz["website"])
            biz["emails"]       = emails
            biz["email_status"] = status
            job.progress = i + 1
            processed.append(biz)

            if status == STATUS_FOUND:
                job.found += 1
                job.log(f"[{i+1}/{job.total}] ✓ {biz['name']} — {emails}")
            else:
                job.log(f"[{i+1}/{job.total}] ✗ {biz['name']} — {status}")

            # Write as we go: a crash mid-run must not cost the whole scrape.
            # The file is rewritten with final values once the fallback ends.
            append_to_csv({k: biz.get(k, "") for k in CSV_FIELDS}, output_file)
            time.sleep(random.uniform(*SITE_REQUEST_DELAY))

        # ── Step 2b: Browser fallback ───────────────────────────────────────
        # One browser session for everything HTTP could not read, rather than
        # paying browser start-up per site.
        retry_sites = [
            b["website"] for b in processed
            if b.get("website") and b["email_status"] in BROWSER_RETRY_STATUSES
        ]
        if retry_sites and not job.stop_event.is_set():
            recovered = browser_fallback(retry_sites, log_fn=job.log)
            for biz in processed:
                hit = recovered.get(biz.get("website"))
                if hit:
                    biz["emails"], biz["email_status"] = hit
                    job.found += 1
            if recovered:
                rewrite_csv_rows(processed, output_file)

        # ── Step 3: Build contacts from the final state ─────────────────────
        contacts_to_import = []
        for biz in processed:
            status = biz["email_status"]
            if status == STATUS_FOUND:
                for raw in biz["emails"].split(";"):
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
            elif biz.get("website"):
                # No email, but a real site -- keep it as a prospect to chase
                # by hand rather than dropping the lead entirely.
                prospect_status = "form_only" if status == STATUS_FORM_ONLY else "no_email"
                contacts_to_import.append({
                    "email":   "",
                    "company": biz.get("name", ""),
                    "website": biz.get("website", ""),
                    "address": biz.get("address", ""),
                    "status":  prospect_status,
                })

        # ── Step 4: Auto-import to DB ───────────────────────────────────────
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
    OUTPUT_FILE  = args.output or f"{safe_filename(CITY)}_{safe_filename(NICHE)}.csv"
    SEARCH_QUERY = f"{NICHE} in {CITY}"
    COOKIE_FILE  = f"cookies_{safe_filename(CITY)}.json"

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
        biz["emails"]       = emails
        biz["email_status"] = status

        if status == STATUS_FOUND:
            log.info(f"  ✓ {emails}")
        else:
            log.info(f"  ✗ {status}")

        append_to_csv({k: biz.get(k, "") for k in CSV_FIELDS}, OUTPUT_FILE)
        time.sleep(random.uniform(*SITE_REQUEST_DELAY))

    # One browser pass over everything the HTTP client could not read.
    retry_sites = [
        b["website"] for b in businesses
        if b.get("website") and b.get("email_status") in BROWSER_RETRY_STATUSES
    ]
    if retry_sites:
        recovered = browser_fallback(retry_sites)
        for biz in businesses:
            hit = recovered.get(biz.get("website"))
            if hit:
                biz["emails"], biz["email_status"] = hit
                log.info(f"  ✓ (browser) {biz['name']} — {biz['emails']}")
        if recovered:
            rewrite_csv_rows(businesses, OUTPUT_FILE)

    print_summary(OUTPUT_FILE)


if __name__ == "__main__":
    main()
