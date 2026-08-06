"""Email-extraction recall harness.

Replays the websites in the scraped CSVs through the live extractor and reports
how many yield a REAL email -- deliberately not trusting the scraper's own
email_status column, because that column is what regressed: three rows in the
St. John's data are marked "found" while holding only .webp filenames and
Wixpress sentry IDs.

Run:  python tests/recall_harness.py [--csv PATTERN] [--workers N] [--limit N]

Baseline -- measured live against the repo extractor before the Phase 1 rework.
The two St. John's CSVs hold 40 rows but only 25 unique (name, website) pairs:

    businesses probed   25
    with a real email   13   (52%)
    site_blocked         7
    contact_form_only    2
    site_timeout         1
    no_website           2
    junk leaked          0   <-- the 64cffaa filter fix holds

(The historical CSV columns claim a higher hit rate, but three of those rows
hold only .webp filenames and Wixpress sentry IDs. They were produced by an
older fork that predates the filter fix -- do not use them as the baseline.)

After the Phase 1 rework (HTTP path only, before the browser fallback):

    with a real email   18   (72%)
    site_blocked         0
    contact_form_only    5
    no_website           2
    junk leaked          0

Regression guard: if `real` drops below 18 or junk goes above 0, something in
the extraction order broke. The subtle one to watch is <script> stripping --
removing script blocks costs First Street Dental, whose address lives in a
Drupal settings object, while keeping <style> stripping is what suppresses
bundled font-licence addresses like team@latofonts.com.
"""
import argparse
import csv
import glob
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gmaps_email_scraper as scraper  # noqa: E402

# ── Junk detection, independent of the scraper's own filters ─────────────────
# The harness must be able to catch junk the scraper failed to filter, so it
# cannot reuse the scraper's blacklist. These are the false positives actually
# observed in the St. John's output.
_FILE_EXT_TLDS = {
    "webp", "png", "jpg", "jpeg", "gif", "svg", "ico", "bmp", "pdf", "zip",
    "js", "css", "html", "xml", "json", "woff", "woff2", "ttf", "eot",
    "mp4", "mp3", "wav", "webm",
}
_JUNK_DOMAINS = {
    "wixpress.com", "sentry.io", "example.com", "yourdomain.com",
    "latofonts.com", "sentry-next.wixpress.com",
}


def is_junk(email: str) -> bool:
    domain = email.split("@")[-1].lower()
    if domain.rsplit(".", 1)[-1] in _FILE_EXT_TLDS:
        return True
    return any(domain == d or domain.endswith("." + d) for d in _JUNK_DOMAINS)


def load_rows(pattern):
    rows, seen = [], set()
    for path in glob.glob(pattern):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row.get("name", ""), row.get("website", ""))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    return rows


def probe(row):
    """Run one business through the extractor. Returns a result dict."""
    website = (row.get("website") or "").strip()
    name = row.get("name", "")
    if not website:
        return {"name": name, "website": "", "status": "no_website",
                "real": [], "junk": [], "was": row.get("email_status", "")}
    try:
        emails, status = scraper.get_emails_for_business(website)
    except Exception as exc:
        return {"name": name, "website": website,
                "status": f"EXC:{type(exc).__name__}", "real": [], "junk": [],
                "was": row.get("email_status", "")}
    found = [e.strip() for e in emails.split(";") if e.strip()]
    return {
        "name": name,
        "website": website,
        "status": status,
        "real": [e for e in found if not is_junk(e)],
        "junk": [e for e in found if is_junk(e)],
        "was": row.get("email_status", ""),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="scraper_output/*.csv", help="glob for input CSVs")
    ap.add_argument("--workers", type=int, default=8,
                    help="parallel sites; the extractor sleeps between pages, "
                         "so this is what keeps the run to a couple of minutes")
    ap.add_argument("--limit", type=int, default=0, help="probe only the first N rows")
    ap.add_argument("--verbose", action="store_true", help="print every row")
    ap.add_argument("--browser", action="store_true",
                    help="retry unresolved sites through the browser fallback")
    args = ap.parse_args()

    rows = load_rows(args.csv)
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print(f"No rows matched {args.csv!r}")
        return 1

    print(f"Probing {len(rows)} businesses with {args.workers} workers...\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(probe, rows))

    if args.browser:
        retry = [r["website"] for r in results
                 if not r["real"] and r["website"]
                 and r["status"] in scraper.BROWSER_RETRY_STATUSES]
        if retry:
            recovered = scraper.browser_fallback(retry)
            for r in results:
                if r["website"] in recovered:
                    emails, status = recovered[r["website"]]
                    found = [e.strip() for e in emails.split(";") if e.strip()]
                    r["real"] = [e for e in found if not is_junk(e)]
                    r["junk"] = [e for e in found if is_junk(e)]
                    r["status"] = status + " (browser)"
            print()

    real = [r for r in results if r["real"]]
    junk_only = [r for r in results if r["junk"] and not r["real"]]
    any_junk = [r for r in results if r["junk"]]
    statuses = Counter(r["status"] for r in results)

    if args.verbose:
        for r in sorted(results, key=lambda x: (not x["real"], x["name"])):
            mark = "OK " if r["real"] else "-- "
            detail = ", ".join(r["real"][:2]) if r["real"] else r["status"]
            print(f"  {mark}{r['name'][:34]:36} {detail}")
            if r["junk"]:
                print(f"      junk leaked: {', '.join(r['junk'][:3])}")
        print()

    total = len(results)
    print("=" * 58)
    print(f"  Businesses probed        {total}")
    print(f"  With a REAL email        {len(real)}   ({100*len(real)//total}%)")
    print(f"  Junk matches leaked      {len(any_junk)}"
          + ("   <-- must be 0" if any_junk else ""))
    print(f"  Rows that are junk-only  {len(junk_only)}"
          + ("   <-- false 'found'" if junk_only else ""))
    print()
    print("  Status histogram:")
    for status, n in statuses.most_common():
        print(f"    {status:<22} {n:>3}")
    print("=" * 58)

    # Regressions the Phase 1 rework was built to fix.
    blocked = statuses.get("site_blocked", 0)
    print()
    print(f"  vs baseline: real 22 -> {len(real)}   |   site_blocked 9 -> {blocked}")
    if any_junk:
        print("  WARNING: junk is still being extracted -- filters regressed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
