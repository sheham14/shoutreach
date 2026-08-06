"""Unit tests for URL handling and email extraction. No network access.

Run:  python tests/test_extraction.py

The recall harness measures these end-to-end against live sites, which is slow
and can drift as those sites change. These pin the specific behaviours that
regressed before.
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup  # noqa: E402
import gmaps_email_scraper as scraper  # noqa: E402

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def soup_of(html):
    return BeautifulSoup(html, "html.parser")


def test_url_normalisation():
    """The bug: Maps hands out UTM-tagged URLs and paths were concatenated."""
    print("\n1. URL NORMALISATION")

    utm = "http://avalondental.ca/?utm_source=google&utm_medium=profile&utm_campaign=gmb"
    base = scraper._normalize_entry_url(utm)
    joined = urllib.parse.urljoin(base, "/contact")
    check("query string dropped from the entry URL", "?" not in base, base)
    check("contact path joins cleanly", joined == "http://avalondental.ca/contact", joined)
    check("old concatenation would have been wrong",
          (utm.rstrip("/") + "/contact") != joined)

    deep = "https://www.airportheightsdental.com/site/home?utm_source=G&utm_medium=LPM"
    check("path is preserved, query is not",
          scraper._normalize_entry_url(deep) == "https://www.airportheightsdental.com/site/home")

    check("fragment dropped",
          scraper._normalize_entry_url("https://x.ca/page#team") == "https://x.ca/page")
    check("scheme-less input gets https",
          scraper._normalize_entry_url("x.ca").startswith("https://"))
    check("empty input stays empty", scraper._normalize_entry_url("") == "")


def test_cloudflare_decoding():
    print("\n2. CLOUDFLARE OBFUSCATION")
    # "dieb@email.com" encoded with the standard first-byte-XOR scheme.
    decoded = scraper._decode_cfemail("7c1815191e3c19111d1510521f1311")
    check("data-cfemail decodes", decoded == "dieb@email.com", decoded)
    check("malformed hex is ignored, not raised", scraper._decode_cfemail("zzz") == "")

    html = '<a href="/cdn-cgi/l/email-protection" data-cfemail="7c1815191e3c19111d1510521f1311">[email&#160;protected]</a>'
    check("extracted from a page", "dieb@email.com" in scraper.extract_emails(html, soup_of(html)))


def test_source_priority():
    print("\n3. EXTRACTION SOURCES")

    html = '<a href="mailto:hello@clinic.ca?subject=Hi">Email us</a>'
    check("mailto is read and query-stripped",
          scraper.extract_emails(html, soup_of(html)) == {"hello@clinic.ca"})

    html = '<script type="application/ld+json">{"@type":"Dentist","email":"info@clinic.ca"}</script>'
    check("JSON-LD email is read",
          "info@clinic.ca" in scraper.extract_emails(html, soup_of(html)))

    html = '<p>Write to us at reception [at] clinic [dot] ca today</p>'
    check("human obfuscation is decoded",
          "reception@clinic.ca" in scraper.extract_emails(html, soup_of(html)))

    # A CMS config object -- this is where First Street Dental's address lives.
    # Stripping <script> to reduce junk silently cost real leads.
    html = '<script>var settings = {"practice_email":"office@clinic.ca"};</script>'
    check("script blocks are still scanned",
          "office@clinic.ca" in scraper.extract_emails(html, soup_of(html)))

    # ...whereas inline CSS carries font-licence addresses that look real and
    # cannot be blacklisted by domain (the author's personal gmail).
    html = ('<style>/* Lato by Pablo Impallari, impallari@gmail.com, '
            'team@latofonts.com */</style><p>Welcome</p>')
    check("style blocks are not scanned",
          scraper.extract_emails(html, soup_of(html)) == set())


def test_filtering():
    print("\n4. JUNK FILTERING")

    html = '<img src="logo@2x.webp"><p>group-103@2x.webp</p>'
    check("file names are not emails", scraper.extract_emails(html, soup_of(html)) == set())

    html = '<script>Sentry.init({dsn:"605a7ba@sentry-next.wixpress.com"})</script>'
    check("blacklist matches subdomains",
          scraper.extract_emails(html, soup_of(html)) == set())

    html = '<p>Contact citysmilesnl@gmail.com for bookings</p>'
    check("a real gmail still passes",
          scraper.extract_emails(html, soup_of(html)) == {"citysmilesnl@gmail.com"})


def test_link_discovery():
    print("\n5. CONTACT LINK DISCOVERY")

    html = """
      <a href="/contact-us-today">Contact Us</a>
      <a href="/about">About</a>
      <a href="https://facebook.com/x">Facebook</a>
      <a href="#top">Top</a>
      <a href="mailto:a@b.ca">Mail</a>
      <a href="/services">Services</a>
    """
    links = scraper.discover_contact_links(soup_of(html), "https://clinic.ca/")
    check("finds a non-guessable contact path",
          "https://clinic.ca/contact-us-today" in links, str(links))
    check("contact outranks about",
          links[0].endswith("contact-us-today"), str(links))
    check("off-site links excluded", not any("facebook" in l for l in links))
    check("anchors and mailto excluded", not any("#" in l or "mailto" in l for l in links))
    check("unrelated pages excluded", not any("services" in l for l in links))


def test_filenames():
    print("\n6. FILENAME SAFETY")
    check("apostrophes removed",
          scraper.safe_filename("St. John's Newfoundland") == "st_john_s_newfoundland")
    check("no leading/trailing separators",
          not scraper.safe_filename("  ...Toronto...  ").strip("_").startswith("_"))
    check("empty input has a fallback", scraper.safe_filename("") == "output")


def main():
    test_url_normalisation()
    test_cloudflare_decoding()
    test_source_priority()
    test_filtering()
    test_link_discovery()
    test_filenames()

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
