"""
audit.py — what an agency wants to know about a business's online presence.

Runs only when the operator presses "Run checks" on one lead, never in the
background on its own, and never as research: each check is one request to a
free, public source, and the results are saved onto the business for later.

  site       one load of the homepage: https, what it's built on, analytics and
             ad pixels, mobile viewport, title and description, structured
             data, social profiles, how stale the footer looks
  pagespeed  Google PageSpeed Insights (mobile): performance, SEO, accessibility
             and best-practice scores, the key timings, and a screenshot
  ssl        whether https works and when the certificate expires
  email      who handles their email (Google Workspace, Microsoft 365, ...)
  archive    when the Internet Archive first and last saw the site
  domain     registration and expiry dates, where the registry publishes them

What it deliberately does not try: Google search results, AI answers, Meta
ads, LinkedIn or Instagram. Those sit behind logins or bot protection, or have
no free API for commercial ads outside the EU; the operator gets one-click
links for them instead (see the audit panel).

Called from a background thread in app.py, never on a request thread: the
server runs one gunicorn worker, and PageSpeed alone can take a minute.
"""
import datetime
import json
import re
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests

TIMEOUT = 12
PAGESPEED_TIMEOUT = 90
MAX_BYTES = 2_000_000

# A normal browser's user agent. Plenty of small-business hosts refuse
# anything that announces itself as a bot, and this is one visit, the same as
# the operator opening the page.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "en",
}

# (label, lowercase fragments any one of which identifies it in the HTML)
_BUILT_WITH = [
    ("WordPress",   ["wp-content/", "wp-includes/", 'content="wordpress']),
    ("Wix",         ["static.wixstatic.com", "wix.com website builder", "_wixcidx"]),
    ("Squarespace", ["static1.squarespace.com", "squarespace.com"]),
    ("Shopify",     ["cdn.shopify.com"]),
    ("Webflow",     ["data-wf-site", "webflow.js"]),
    ("GoDaddy Website Builder", ["img1.wsimg.com"]),
    ("Framer",      ["framerusercontent.com"]),
    ("Weebly",      ["weebly.com"]),
    ("Duda",        ["multiscreensite.com", "dudamobile"]),
    ("Joomla",      ['content="joomla', "/media/jui/"]),
    ("Drupal",      ["drupal-settings-json", "/sites/default/files/"]),
    ("HubSpot CMS", ["hs-sites.com", "hubspot-sites"]),
    ("Next.js",     ["__next_data__", "/_next/static/"]),
]

_TRACKING = [
    ("Google Tag Manager", ["googletagmanager.com/gtm.js", "googletagmanager.com/ns.html"]),
    ("Google Analytics",   ["googletagmanager.com/gtag/js?id=g-", "google-analytics.com/analytics.js",
                            "gtag('config', 'g-", 'gtag("config", "g-']),
    ("Google Ads tag",     ["googletagmanager.com/gtag/js?id=aw-", "gtag('config', 'aw-",
                            'gtag("config", "aw-', "googleadservices.com"]),
    ("Meta Pixel",         ["connect.facebook.net", "fbq('init'", 'fbq("init"']),
    ("TikTok Pixel",       ["analytics.tiktok.com"]),
    ("Snap Pixel",         ["sc-static.net/scevent"]),
    ("LinkedIn Insight",   ["snap.licdn.com"]),
    ("Hotjar",             ["static.hotjar.com"]),
    ("Microsoft Clarity",  ["clarity.ms/tag"]),
]

_ENGAGEMENT = [
    ("WhatsApp chat link", ["wa.me/", "api.whatsapp.com/send"]),
    ("Live chat",          ["embed.tawk.to", "widget.intercom.io", "client.crisp.chat",
                            "static.zdassets.com", "cdn.livechatinc.com", "tidio"]),
    ("Online booking",     ["calendly.com", "setmore.com", "okadoc", "vezeeta", "practo.com",
                            "fresha.com", "booksy.com", "simplybook", "acuityscheduling",
                            "book online", "book an appointment", "book now"]),
]

_SOCIAL = [
    ("Instagram", r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+/?"),
    ("Facebook",  r"https?://(?:www\.|m\.)?facebook\.com/(?!sharer|dialog|plugins|tr\b)[A-Za-z0-9_.\-/]+"),
    ("TikTok",    r"https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9_.]+"),
    ("LinkedIn",  r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in)/[A-Za-z0-9_\-%]+"),
    ("YouTube",   r"https?://(?:www\.)?youtube\.com/(?:@|channel/|c/|user/)[A-Za-z0-9_\-]+"),
    ("X",         r"https?://(?:www\.)?(?:twitter|x)\.com/(?!intent|share)[A-Za-z0-9_]+"),
]


def _url(website: str) -> str:
    website = (website or "").strip()
    if not website:
        return ""
    return website if website.startswith(("http://", "https://")) else f"https://{website}"


def _meta(html: str, name: str) -> str:
    m = re.search(
        rf'<meta[^>]+(?:name|property)\s*=\s*["\']{re.escape(name)}["\'][^>]*content\s*=\s*["\']([^"\']*)',
        html, re.I)
    if not m:
        m = re.search(
            rf'<meta[^>]+content\s*=\s*["\']([^"\']*)["\'][^>]*(?:name|property)\s*=\s*["\']{re.escape(name)}["\']',
            html, re.I)
    return (m.group(1).strip() if m else "")[:300]


def scan_site(website: str) -> dict:
    url = _url(website)
    if not url:
        return {"ok": False, "error": "No website on file"}
    started = datetime.datetime.now()
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=TIMEOUT, allow_redirects=True, stream=True)
        raw = resp.raw.read(MAX_BYTES + 1, decode_content=True) or b""
        html = raw[:MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")
    except requests.exceptions.SSLError:
        return {"ok": False, "error": "The site's security certificate is broken — browsers show a warning"}
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "error": f"The site didn't load ({type(exc).__name__})"}
    elapsed_ms = int((datetime.datetime.now() - started).total_seconds() * 1000)
    low = html.lower()

    def found(table):
        return [label for label, marks in table if any(m in low for m in marks)]

    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    schema_types = sorted({t for block in re.findall(
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.I | re.S)
        for t in re.findall(r'"@type"\s*:\s*"([^"]+)"', block)})
    socials = {}
    for label, pattern in _SOCIAL:
        m = re.search(pattern, html)
        if m:
            socials[label] = m.group(0).rstrip("/")
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20\d{2})", low)]
    generator = _meta(html, "generator")

    return {
        "ok": True,
        "status": resp.status_code,
        "final_url": resp.url,
        "https": resp.url.startswith("https://"),
        "load_ms": elapsed_ms,
        "page_kb": round(len(raw) / 1024),
        "title": re.sub(r"\s+", " ", title.group(1)).strip()[:200] if title else "",
        "description": _meta(html, "description"),
        "mobile_viewport": bool(re.search(r'<meta[^>]+name\s*=\s*["\']viewport', html, re.I)),
        "open_graph": bool(_meta(html, "og:title")),
        "noindex": "noindex" in _meta(html, "robots").lower(),
        "schema_types": schema_types[:12],
        "built_with": found(_BUILT_WITH) or ([generator] if generator else []),
        "tracking": found(_TRACKING),
        "engagement": found(_ENGAGEMENT),
        "socials": socials,
        "copyright_year": max(years) if years else None,
        "has_h1": bool(re.search(r"<h1[\s>]", html, re.I)),
        "favicon": bool(re.search(r'<link[^>]+rel\s*=\s*["\'][^"\']*icon', html, re.I)),
    }


def pagespeed(website: str, api_key: str = "") -> dict:
    url = _url(website)
    if not url:
        return {"ok": False, "error": "No website on file"}
    params = [("url", url), ("strategy", "mobile")]
    params += [("category", c) for c in ("performance", "seo", "accessibility", "best-practices")]
    if api_key:
        params.append(("key", api_key))
    try:
        resp = requests.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                            params=params, timeout=PAGESPEED_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "error": f"PageSpeed didn't answer ({type(exc).__name__})"}
    if resp.status_code == 429:
        return {"ok": False, "error": "Google's free PageSpeed limit is used up — add a Google API key in Settings"}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": f"PageSpeed returned an error ({resp.status_code})"}
    if resp.status_code != 200:
        message = ((data.get("error") or {}).get("message") or "").split("\n")[0][:200]
        return {"ok": False, "error": message or f"PageSpeed returned an error ({resp.status_code})"}
    lh = data.get("lighthouseResult") or {}
    cats = lh.get("categories") or {}
    audits = lh.get("audits") or {}

    def score(key):
        s = (cats.get(key) or {}).get("score")
        return None if s is None else round(s * 100)

    def shown(key):
        return (audits.get(key) or {}).get("displayValue") or ""

    shot = ((audits.get("final-screenshot") or {}).get("details") or {}).get("data") or ""
    return {
        "ok": True,
        "performance": score("performance"),
        "seo": score("seo"),
        "accessibility": score("accessibility"),
        "best_practices": score("best-practices"),
        "first_paint": shown("first-contentful-paint"),
        "largest_paint": shown("largest-contentful-paint"),
        "blocking_time": shown("total-blocking-time"),
        "layout_shift": shown("cumulative-layout-shift"),
        # A small JPEG as a data URI -- tens of KB, stored so the panel can
        # show how the site looks on a phone without running the test again.
        "screenshot": shot if shot.startswith("data:image/") and len(shot) < 400_000 else "",
    }


def ssl_certificate(website: str) -> dict:
    host = urlsplit(_url(website)).hostname
    if not host:
        return {"ok": False, "error": "No website on file"}
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        return {"ok": True, "valid": False, "problem": str(exc.verify_message or exc)[:160]}
    except (OSError, ssl.SSLError) as exc:
        return {"ok": True, "valid": False, "problem": f"No working https ({type(exc).__name__})"}
    expires = datetime.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
    issuer = dict(x[0] for x in cert.get("issuer", ()))
    return {"ok": True, "valid": True, "expires": expires.strftime("%Y-%m-%d"),
            "days_left": (expires - datetime.datetime.utcnow()).days,
            "issuer": issuer.get("organizationName", "")}


_MAIL_PROVIDERS = [
    ("Google Workspace", ["google.com", "googlemail.com"]),
    ("Microsoft 365",    ["outlook.com", "protection.outlook.com"]),
    ("Zoho Mail",        ["zoho.com", "zoho.eu", "zoho.in"]),
    ("GoDaddy email",    ["secureserver.net"]),
    ("Hostinger email",  ["hostinger.com"]),
    ("iCloud",           ["icloud.com"]),
    ("Yandex",           ["yandex.net"]),
]


def email_provider(domain: str) -> dict:
    if not domain:
        return {"ok": False, "error": "No website domain on file"}
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=6)
    except Exception as exc:  # NXDOMAIN, NoAnswer, timeout: all mean "no mail here we can see"
        name = type(exc).__name__
        if name in ("NXDOMAIN", "NoAnswer", "NoNameservers"):
            return {"ok": True, "provider": "No email set up on this domain", "hosts": []}
        return {"ok": False, "error": f"Couldn't look it up ({name})"}
    hosts = [str(r.exchange).rstrip(".").lower() for r in sorted(answers, key=lambda r: r.preference)]
    provider = next((label for label, marks in _MAIL_PROVIDERS
                     if any(h.endswith(m) for h in hosts for m in marks)), "")
    return {"ok": True, "provider": provider or f"Other ({hosts[0]})" if hosts else "None",
            "hosts": hosts[:3]}


def archive_history(domain: str) -> dict:
    if not domain:
        return {"ok": False, "error": "No website domain on file"}

    def one(limit):
        resp = requests.get("https://web.archive.org/cdx/search/cdx",
                            params={"url": domain, "output": "json", "fl": "timestamp",
                                    "limit": limit, "filter": "statuscode:200"},
                            headers=_HEADERS, timeout=TIMEOUT)
        rows = resp.json() if resp.text.strip() else []
        return rows[1][0] if len(rows) > 1 else None

    try:
        first, last = one(1), one(-1)
    except (requests.exceptions.RequestException, ValueError) as exc:
        return {"ok": False, "error": f"The Internet Archive didn't answer ({type(exc).__name__})"}

    def date(ts):
        return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if ts else None
    return {"ok": True, "first_seen": date(first), "last_seen": date(last)}


def domain_dates(domain: str) -> dict:
    if not domain:
        return {"ok": False, "error": "No website domain on file"}
    try:
        resp = requests.get(f"https://rdap.org/domain/{domain}", headers=_HEADERS, timeout=TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "error": f"The domain registry didn't answer ({type(exc).__name__})"}
    if resp.status_code != 200:
        return {"ok": False, "error": "Not published for this domain ending"}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": "Not published for this domain ending"}
    events = {e.get("eventAction"): (e.get("eventDate") or "")[:10] for e in data.get("events") or []}
    return {"ok": True, "registered": events.get("registration"), "expires": events.get("expiration")}


def run_checks(website: str, domain: str, google_api_key: str = "") -> dict:
    """Every automatic check, in parallel. One failing never stops the rest."""
    jobs = {
        "site": lambda: scan_site(website),
        "pagespeed": lambda: pagespeed(website, google_api_key),
        "ssl": lambda: ssl_certificate(website),
        "email": lambda: email_provider(domain),
        "archive": lambda: archive_history(domain),
        "domain": lambda: domain_dates(domain),
    }
    results = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {key: pool.submit(fn) for key, fn in jobs.items()}
        for key, future in futures.items():
            try:
                results[key] = future.result()
            except Exception as exc:  # a bug in one check must not lose the others
                results[key] = {"ok": False, "error": f"Check failed ({type(exc).__name__})"}
    results["checked_at"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    return results


def to_json(results: dict) -> str:
    return json.dumps(results, default=str)
