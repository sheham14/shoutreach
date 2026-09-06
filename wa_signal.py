"""
wa_signal.py — Cheap, lean booking-gap detection for WhatsApp leads.

One plain HTTP fetch of the homepage per lead, checked against a keyword and
known-widget list. Deliberately not an agent: no per-lead search calls, no
multi-step research, no rendered-browser fetch. A page that comes back too
short to judge honestly is flagged 'unclear' rather than escalated to a
second, heavier fetch -- a real gap a plain fetch can't see, accepted as a
"check by hand" case for now rather than standing up a second local worker
process. See docs/WhatsApp Module Handover.md for the reasoning.

Called only from scheduler.py's background loop, never from a request
thread -- the server runs gunicorn --workers 1, and blocking that worker on
network fetches for a large import would freeze the whole app.
"""
import re
import logging

import requests

logger = logging.getLogger("wa_signal")

REQUEST_TIMEOUT = 6            # seconds, per site
MAX_BYTES = 500_000            # stop reading a page well before it matters
MIN_BODY_CHARS = 200           # below this, a fetch is inconclusive, not "no gap"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (compatible; ShoutReachBot/1.0; "
                   "+https://shoutreach.hexiv.co)"),
}

# Phrases a clinic's own site uses to advertise booking. Checked against the
# raw (lowercased) HTML, not just visible text, so a keyword sitting in a
# button's alt text or aria-label still counts -- it is still evidence the
# business built or bought a booking flow.
BOOKING_KEYWORDS = [
    "book online", "book an appointment", "book appointment", "book now",
    "schedule appointment", "schedule an appointment", "make an appointment",
    "request an appointment", "online booking", "book a consultation",
    "book your appointment", "reserve your spot", "schedule your visit",
]

# Known booking-widget hosts/scripts. A hit here is stronger than a keyword
# match: the business isn't just claiming online booking, the widget is
# actually embedded and working.
BOOKING_WIDGET_SIGNATURES = [
    "calendly.com", "acuityscheduling.com", "squareup.com/appointments",
    "book.squareup.com", "fresha.com", "janeapp.com", "mindbodyonline.com",
    "vagaro.com", "setmore.com", "appointy.com", "simplybook.me",
    "zocdoc.com", "schedulicity.com",
]


def detect_signal(website: str) -> dict:
    """
    Returns {"signal_type": "gap_found"|"no_gap"|"unclear", "signal_detail": str}.

      gap_found -- no booking keyword or widget seen. The observation IS the
                   hook: "no visible way to book online" is itself the
                   specific, verifiable thing the opener references.
      no_gap    -- a keyword or widget was found. Nothing to pitch on this
                   axis; the opener should compliment instead.
      unclear   -- couldn't fetch, or the page was too thin to judge
                   honestly (most often a JS-rendered site a plain HTTP GET
                   can't see into). Surfaced for the operator to check by
                   hand rather than guessed at.
    """
    website = (website or "").strip()
    if not website:
        return {"signal_type": "unclear", "signal_detail": "No website on file"}

    url = website if website.startswith(("http://", "https://")) else f"https://{website}"

    try:
        resp = requests.get(
            url, headers=_HEADERS, timeout=REQUEST_TIMEOUT,
            allow_redirects=True, stream=True,
        )
        raw = resp.raw.read(MAX_BYTES + 1, decode_content=True) or b""
        if len(raw) > MAX_BYTES:
            raw = raw[:MAX_BYTES]
        html = raw.decode(resp.encoding or "utf-8", errors="replace")
    except requests.exceptions.RequestException as exc:
        logger.info("Signal fetch failed for %s: %s", url, exc)
        return {
            "signal_type": "unclear",
            "signal_detail": f"Site did not respond ({type(exc).__name__}) — check by hand",
        }

    html_lower = html.lower()

    for sig in BOOKING_WIDGET_SIGNATURES:
        if sig in html_lower:
            return {"signal_type": "no_gap", "signal_detail": f"Uses {sig} for online booking"}

    for kw in BOOKING_KEYWORDS:
        if kw in html_lower:
            return {"signal_type": "no_gap", "signal_detail": f'Site says "{kw}"'}

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < MIN_BODY_CHARS:
        return {
            "signal_type": "unclear",
            "signal_detail": f"Page returned little content ({len(text)} chars) — check by hand",
        }

    return {
        "signal_type": "gap_found",
        "signal_detail": "No online booking option found on the homepage",
    }
