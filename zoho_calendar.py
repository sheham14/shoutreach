"""
zoho_calendar.py — booked meetings onto Zoho Calendar, with an invite if asked.

Each operator connects their own Zoho login (an OAuth "server-based
application" an admin registers once in Zoho's API Console), so an event, and
the invite the client gets, comes from whoever booked it. Where it lands is
each operator's choice of calendar -- normally one the two of them share.

Nothing here runs on its own. Every call to Zoho is the direct result of an
operator booking, retrying, inviting or cancelling, and a booking never waits
on Zoho to be saved: if Zoho is down, the meeting is kept here with Zoho's
error and a Retry.

The client is only ever emailed (by Zoho, as the invite) when the operator
ticks a box that says so. Changing or clearing the pipeline stage never
cancels the event -- a mis-click must not email a client a cancellation.

Every network call goes through _http, which the tests replace.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone as _utc_tz
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

import db

logger = logging.getLogger("zoho")

# Zoho runs separate data centres, and a token from one is no good at another.
# Accounts and Calendar both follow the zoho.<suffix> pattern.
REGIONS = {
    "sa": "Saudi Arabia", "com": "United States", "eu": "Europe", "in": "India",
    "com.au": "Australia", "jp": "Japan", "ca": "Canada", "com.cn": "China",
}
DEFAULT_REGION = "sa"

SCOPES = ",".join((
    "ZohoCalendar.calendar.READ",   # list calendars to pick from
    "ZohoCalendar.event.ALL",       # create, move and cancel the meeting
    "ZohoMeeting.meeting.ALL",      # conference "zmeeting": a room per meeting
    "AaaServer.profile.Read",       # "Connected as you@company"
))

TIMEOUT = 15


class ZohoError(Exception):
    """Something Zoho refused or couldn't be reached for, in words an
    operator can act on."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# ── Configuration ────────────────────────────────────────────────────────────

def region() -> str:
    r = (db.get_settings().get("zoho_region") or DEFAULT_REGION).strip()
    return r if r in REGIONS else DEFAULT_REGION


def accounts_host(r: str = None) -> str:
    return f"https://accounts.zoho.{r or region()}"


def calendar_api(r: str = None) -> str:
    return f"https://calendar.zoho.{r or region()}/api/v1"


def client_config() -> tuple:
    s = db.get_settings()
    return (s.get("zoho_client_id") or "").strip(), (s.get("zoho_client_secret") or "").strip()


def is_configured() -> bool:
    cid, secret = client_config()
    return bool(cid and secret)


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _http(method: str, url: str, **kw):
    """The one door to the network."""
    return requests.request(method, url, timeout=TIMEOUT, **kw)


def _json(resp) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {"data": body}


def _zoho_message(body: dict, status: int) -> str:
    """Zoho's errors come in several shapes; find the sentence in them."""
    err = body.get("error")
    if isinstance(err, list) and err:
        err = err[0]
    if isinstance(err, dict):
        text = err.get("description") or err.get("message") or err.get("code")
        if text:
            return str(text)
    if isinstance(err, str) and err:
        return err.replace("_", " ")
    for key in ("message", "description"):
        if body.get(key):
            return str(body[key])
    return f"Zoho answered {status}"


def _post_accounts(path: str, data: dict) -> dict:
    try:
        resp = _http("POST", accounts_host() + path, data=data)
    except requests.RequestException as exc:
        raise ZohoError(f"Couldn't reach Zoho ({exc.__class__.__name__})")
    return _json(resp)


# ── Connecting an operator ───────────────────────────────────────────────────

def authorize_url(state: str, redirect_uri: str) -> str:
    cid, _ = client_config()
    return accounts_host() + "/oauth/v2/auth?" + urlencode({
        "scope": SCOPES, "client_id": cid, "response_type": "code",
        # offline + consent: a refresh token, so the connection outlives the
        # hour an access token lasts.
        "access_type": "offline", "prompt": "consent",
        "redirect_uri": redirect_uri, "state": state,
    })


def finish_connect(owner_id: int, code: str, redirect_uri: str) -> dict:
    """Swap the code Zoho sent back for tokens and remember them. Keeps a
    calendar already chosen on reconnect; otherwise starts on the operator's
    Zoho default calendar."""
    cid, secret = client_config()
    body = _post_accounts("/oauth/v2/token", {
        "grant_type": "authorization_code", "code": code, "client_id": cid,
        "client_secret": secret, "redirect_uri": redirect_uri,
    })
    if not body.get("access_token"):
        raise ZohoError(f"Zoho didn't accept the sign-in: {_zoho_message(body, 400)}")
    if not body.get("refresh_token"):
        raise ZohoError("Zoho signed you in but didn't grant a lasting connection — try Connect again")
    previous = db.get_zoho_connection(owner_id) or {}
    keep_calendar = previous.get("region") == region()
    db.save_zoho_connection(
        owner_id, region=region(), refresh_token=body["refresh_token"],
        access_token=body["access_token"],
        access_expires=int(time.time()) + int(body.get("expires_in") or 3600),
        zoho_email=_profile_email(body["access_token"]),
        calendar_uid=previous.get("calendar_uid", "") if keep_calendar else "",
        calendar_name=previous.get("calendar_name", "") if keep_calendar else "",
    )
    if not (keep_calendar and previous.get("calendar_uid")):
        try:
            default = next((c for c in list_calendars(owner_id) if c["isdefault"]), None)
            if default:
                db.save_zoho_connection(owner_id, calendar_uid=default["uid"],
                                        calendar_name=default["name"])
        except ZohoError as exc:
            logger.warning("Connected, but couldn't list calendars: %s", exc)
    return status(owner_id)


def _profile_email(access_token: str) -> str:
    """Best effort: which Zoho account this is, for "Connected as"."""
    try:
        resp = _http("GET", accounts_host() + "/oauth/user/info",
                     headers={"Authorization": f"Zoho-oauthtoken {access_token}"})
        body = _json(resp)
    except requests.RequestException:
        return ""
    return str(body.get("Email") or body.get("email") or "")


def disconnect(owner_id: int):
    """Forget this operator's tokens, telling Zoho to revoke them if it can.
    Meetings already on the calendar stay there."""
    conn = db.get_zoho_connection(owner_id)
    if conn and conn.get("refresh_token") and conn.get("region") == region():
        try:
            _http("POST", accounts_host() + "/oauth/v2/token/revoke",
                  params={"token": conn["refresh_token"]})
        except requests.RequestException:
            pass
    db.delete_zoho_connection(owner_id)


def status(owner_id: int) -> dict:
    """What the page may know about a connection. No tokens."""
    conn = db.get_zoho_connection(owner_id) or {}
    connected = bool(conn.get("refresh_token"))
    return {
        "configured": is_configured(),
        "region": region(),
        "region_name": REGIONS[region()],
        "connected": connected and conn.get("region") == region(),
        # Connected under a region the install has since moved away from.
        "needs_reconnect": connected and conn.get("region") != region(),
        "zoho_email": conn.get("zoho_email", ""),
        "calendar_uid": conn.get("calendar_uid", ""),
        "calendar_name": conn.get("calendar_name", ""),
        "prefs": db.get_meeting_prefs(owner_id),
    }


def _access_token(owner_id: int) -> str:
    conn = db.get_zoho_connection(owner_id)
    if not conn or not conn.get("refresh_token"):
        raise ZohoError("Zoho Calendar isn't connected — connect it in Settings → My settings")
    if conn.get("region") != region():
        raise ZohoError("The Zoho region changed since you connected — connect again in My settings")
    if conn.get("access_token") and int(conn.get("access_expires") or 0) > time.time() + 60:
        return conn["access_token"]
    cid, secret = client_config()
    body = _post_accounts("/oauth/v2/token", {
        "grant_type": "refresh_token", "refresh_token": conn["refresh_token"],
        "client_id": cid, "client_secret": secret,
    })
    if not body.get("access_token"):
        raise ZohoError(f"Zoho wouldn't renew your connection ({_zoho_message(body, 400)}) — "
                        "connect again in My settings")
    db.save_zoho_connection(owner_id, access_token=body["access_token"],
                            access_expires=int(time.time()) + int(body.get("expires_in") or 3600))
    return body["access_token"]


def _api(owner_id: int, method: str, path: str, params: dict = None, headers: dict = None) -> dict:
    sent = {"Authorization": f"Zoho-oauthtoken {_access_token(owner_id)}", **(headers or {})}
    try:
        resp = _http(method, calendar_api() + path, params=params, headers=sent)
    except requests.RequestException as exc:
        raise ZohoError(f"Couldn't reach Zoho ({exc.__class__.__name__})")
    body = _json(resp)
    if resp.status_code >= 400 or body.get("error"):
        raise ZohoError(_zoho_message(body, resp.status_code), status=resp.status_code)
    return body


def list_calendars(owner_id: int) -> list:
    """Every calendar this login can see, shared ones included."""
    body = _api(owner_id, "GET", "/calendars", params={"category": "all"})
    out = []
    for c in body.get("calendars") or []:
        if not isinstance(c, dict) or not c.get("uid"):
            continue
        out.append({"uid": str(c["uid"]), "name": str(c.get("name") or "Calendar"),
                    "privilege": str(c.get("privilege") or ""),
                    "isdefault": bool(c.get("isdefault"))})
    return out


# ── Preferences ──────────────────────────────────────────────────────────────

def _valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False


def _clean_link(url: str) -> str:
    url = (url or "").strip()
    if url and not re.match(r"^https?://\S+$", url):
        raise ValueError("A meeting link has to start with https://")
    return url[:500]


def clean_prefs(raw: dict) -> dict:
    """My settings → Meetings, checked. Raises ValueError with the reason."""
    out = {}
    if "timezone" in raw:
        tz = str(raw.get("timezone") or "").strip()
        if tz and not _valid_timezone(tz):
            raise ValueError(f"Unknown timezone: {tz}")
        out["timezone"] = tz
    if "minutes" in raw:
        out["minutes"] = _clean_minutes(raw.get("minutes"))
    if "invite_title" in raw:
        out["invite_title"] = str(raw.get("invite_title") or "").strip()[:200] \
            or db.MEETING_DEFAULT_TITLE
    if "link_kind" in raw:
        if raw.get("link_kind") not in db.MEETING_LINK_KINDS:
            raise ValueError("Pick Zoho Meeting, your own link, or no link")
        out["link_kind"] = raw["link_kind"]
    if "own_link" in raw:
        out["own_link"] = _clean_link(raw.get("own_link"))
    return out


def _clean_minutes(value) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ValueError("The length has to be a number of minutes")
    if not 5 <= minutes <= 480:
        raise ValueError("A meeting has to be between 5 minutes and 8 hours")
    return minutes


def clean_meeting_input(raw: dict) -> dict:
    """
    The meeting half of the booking dialog, checked before anything is saved
    so a bad link or address refuses the whole save rather than half of it.
    Keys not sent are left out, so the existing meeting or the operator's
    preferences fill them.
    """
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    if raw.get("timezone"):
        if not _valid_timezone(str(raw["timezone"])):
            raise ValueError(f"Unknown timezone: {raw['timezone']}")
        out["timezone"] = str(raw["timezone"])
    if raw.get("minutes") not in (None, ""):
        out["minutes"] = _clean_minutes(raw["minutes"])
    if raw.get("link_kind"):
        if raw["link_kind"] not in db.MEETING_LINK_KINDS:
            raise ValueError("Pick Zoho Meeting, your own link, or no link")
        out["link_kind"] = raw["link_kind"]
    if "own_link" in raw:
        out["own_link"] = _clean_link(raw.get("own_link"))
    if out.get("link_kind") == "own" and not out.get("own_link"):
        raise ValueError("Paste the link for the meeting, or pick Zoho Meeting")
    if "invite_email" in raw:
        email = str(raw.get("invite_email") or "").strip().lower()
        if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise ValueError("That invite address isn't an email address")
        out["invite_email"] = email
    if raw.get("send_invite") and not out.get("invite_email"):
        raise ValueError("Type the client's email to send them an invite")
    if raw.get("calendar_uid"):
        out["calendar_uid"] = str(raw["calendar_uid"])[:200]
        out["calendar_name"] = str(raw.get("calendar_name") or "")[:200]
    for flag in ("send_invite", "notify_change", "save_email"):
        out[flag] = bool(raw.get(flag))
    return out


# ── Meetings ─────────────────────────────────────────────────────────────────

def render_title(template: str, business: dict) -> str:
    name = (business or {}).get("name") or "your business"
    title = re.sub(r"\{\{\s*(business|company)\s*\}\}", name, template or db.MEETING_DEFAULT_TITLE)
    return title.strip()[:250]


def utc_range(starts_at: str, tz_name: str, minutes: int) -> tuple:
    """A wall-clock start in `tz_name` as Zoho's GMT stamps, start and end."""
    text = str(starts_at or "").strip().replace("T", " ")
    if len(text) == 16:
        text += ":00"
    try:
        local = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ZohoError("The meeting has no time of day — set one first")
    if not _valid_timezone(tz_name):
        raise ZohoError(f"Unknown timezone: {tz_name or '(none)'}")
    start = local.replace(tzinfo=ZoneInfo(tz_name)).astimezone(_utc_tz.utc)
    end = start + timedelta(minutes=int(minutes or db.MEETING_DEFAULT_MINUTES))
    return start.strftime("%Y%m%dT%H%M%SZ"), end.strftime("%Y%m%dT%H%M%SZ")


def event_payload(m: dict, notify: bool) -> dict:
    """
    The event as Zoho wants it. Only what the client should see: the title,
    the time and the link. No phone number, notes or "booked via WhatsApp"
    -- that is internal, and an invited client sees the whole event.
    """
    start, end = utc_range(m["starts_at"], m["timezone"], m["minutes"])
    data = {
        "title": m["title"],
        "dateandtime": {"timezone": m["timezone"], "start": start, "end": end},
        "isallday": False,
        "conference": "zmeeting" if m["link_kind"] == "zmeeting" else "none",
    }
    if m["link_kind"] == "own" and m["own_link"]:
        data["location"] = m["own_link"][:255]
        data["description"] = f"Join the call: {m['own_link']}"
    # On the event once the client has been invited, or is being invited now.
    # An update replaces the whole event, so a client invited before has to
    # be sent again or they'd drop off it.
    invited = bool(m["invite_email"]) and (notify or bool(m["invited_at"]))
    if invited:
        data["attendees"] = [{"email": m["invite_email"], "attendance": 1, "permission": 0}]
    data["notify_attendee"] = 1 if (notify and invited) else 0
    return data


def _find_url(obj, depth=0) -> str:
    """The join link somewhere in Zoho's conference data. Its exact shape
    isn't pinned down in Zoho's docs, so look for it rather than assume."""
    if depth > 5:
        return ""
    if isinstance(obj, str):
        return obj if obj.startswith("https://") else ""
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else []
    found = []
    for key, value in items:
        url = _find_url(value, depth + 1)
        if url:
            found.append((str(key).lower(), url))
    for key, url in found:
        if "join" in key or "meeting" in url:
            return url
    return found[0][1] if found else ""


def _join_url(event: dict, m: dict) -> str:
    if m["link_kind"] == "own":
        return m["own_link"]
    if m["link_kind"] == "none":
        return ""
    return _find_url(event.get("conference_data") or {}) or _find_url(event.get("app_data") or {})


def _now() -> str:
    return datetime.now(_utc_tz.utc).strftime("%Y-%m-%d %H:%M:%S")


def meeting_view(m: dict) -> dict:
    """A meeting for the page: no etag, plus a status word."""
    if not m:
        return None
    out = {k: v for k, v in m.items() if k != "event_etag"}
    if m.get("cancelled_at"):
        out["status"] = "cancelled"
    elif m.get("sync_error"):
        out["status"] = "failed"
    elif m.get("event_uid"):
        out["status"] = "on_calendar"
    else:
        out["status"] = "not_on_calendar"
    return out


def sync_meeting(business_id: int, owner_id: int, notify: bool = False) -> dict:
    """
    Put the meeting on the calendar, or bring the event there up to date.
    Never raises for a Zoho problem: the error is kept on the meeting so the
    page can show it with a Retry. Without a connection this is a no-op --
    the meeting stays here, not on a calendar.
    """
    m = db.get_meeting(business_id)
    if not m or m["cancelled_at"]:
        return m
    conn = db.get_zoho_connection(owner_id)
    if not conn or not conn.get("refresh_token"):
        return m
    calendar = m["calendar_uid"] or conn.get("calendar_uid") or ""
    calendar_name = m["calendar_name"] if m["calendar_uid"] else conn.get("calendar_name", "")
    if not calendar:
        return db.save_meeting(business_id, sync_error="Pick a calendar in Settings → My settings")
    try:
        data = event_payload(m, notify)
        if m["event_uid"]:
            data["etag"] = int(m["event_etag"]) if m["event_etag"].isdigit() else m["event_etag"]
            body = _api(owner_id, "PUT",
                        f"/calendars/{quote(calendar, safe='')}/events/{quote(m['event_uid'], safe='')}",
                        params={"eventdata": json.dumps(data)},
                        headers={"etag": str(m["event_etag"])})
        else:
            body = _api(owner_id, "POST", f"/calendars/{quote(calendar, safe='')}/events",
                        params={"eventdata": json.dumps(data)})
    except ZohoError as exc:
        logger.warning("Calendar sync failed for business %s: %s", business_id, exc)
        return db.save_meeting(business_id, sync_error=str(exc)[:300])
    event = (body.get("events") or [{}])[0] if isinstance(body.get("events"), list) else {}
    if not event.get("uid") and not m["event_uid"]:
        return db.save_meeting(business_id, sync_error="Zoho didn't say what it created — try again")
    return db.save_meeting(
        business_id,
        event_uid=str(event.get("uid") or m["event_uid"]),
        event_etag=str(event.get("etag") or m["event_etag"]),
        calendar_uid=calendar, calendar_name=calendar_name,
        join_url=_join_url(event, m) or m["join_url"],
        synced_at=_now(), sync_error="",
        invited_at=_now() if data["notify_attendee"] else m["invited_at"],
    )


def book(business_id: int, owner_id: int, starts_at: str, fallback_timezone: str = "",
         **choices) -> dict:
    """
    A business was set to 'booked' at `starts_at`: record the meeting and put
    it on the calendar. `choices` are clean_meeting_input's keys; anything
    missing comes from the meeting already booked, then the operator's
    preferences.

    The client is emailed only when asked: send_invite on a meeting they
    haven't been invited to, or notify_change when the time of one they have
    moves.
    """
    business = db.get_business(business_id)
    prefs = db.get_meeting_prefs(owner_id)
    old = db.get_meeting(business_id)
    live = old if old and not old["cancelled_at"] else None
    tz = (choices.get("timezone") or (live and live["timezone"]) or prefs["timezone"]
          or fallback_timezone or "UTC")
    if not _valid_timezone(tz):
        tz = "UTC"
    fields = {
        "starts_at": db._clean_date(starts_at),
        "timezone": tz,
        "minutes": choices.get("minutes") or (live and live["minutes"]) or prefs["minutes"],
        "link_kind": choices.get("link_kind") or (live and live["link_kind"]) or prefs["link_kind"],
        "invite_email": choices["invite_email"] if "invite_email" in choices
                        else (live["invite_email"] if live else ""),
    }
    fields["own_link"] = (choices["own_link"] if "own_link" in choices
                          else (live["own_link"] if live else prefs["own_link"]))
    if fields["link_kind"] == "own" and not fields["own_link"]:
        fields["link_kind"] = "zmeeting"
    if not live:
        # A fresh meeting -- first booking, or booking again after a cancel.
        # Nothing of a cancelled one's event carries over.
        fields.update(title=render_title(prefs["invite_title"], business), join_url="",
                      event_uid="", event_etag="", invited_at=None, synced_at=None,
                      sync_error="", cancelled_at=None,
                      calendar_uid=choices.get("calendar_uid", ""),
                      calendar_name=choices.get("calendar_name", ""))
    elif choices.get("calendar_uid") and not live["event_uid"]:
        # Only before it's on a calendar: moving a live event between
        # calendars would leave the client holding an invite to the old one.
        fields.update(calendar_uid=choices["calendar_uid"],
                      calendar_name=choices.get("calendar_name", ""))
    moved = bool(live) and any(str(live[k]) != str(fields[k])
                               for k in ("starts_at", "timezone", "minutes"))
    emailed_before = bool(live and live["invited_at"])
    if live and emailed_before and fields["invite_email"] != live["invite_email"]:
        # A different address is a new person: they need an invite of their own.
        fields["invited_at"] = None
        emailed_before = False
    notify = (bool(choices.get("send_invite")) and not emailed_before) \
        or (emailed_before and moved and bool(choices.get("notify_change")))
    db.save_meeting(business_id, **fields)
    return sync_meeting(business_id, owner_id, notify=notify)


def invite(business_id: int, owner_id: int, email: str) -> dict:
    """Send the client an invite to a meeting already booked."""
    m = db.get_meeting(business_id)
    if not m or m["cancelled_at"]:
        raise ZohoError("There's no meeting booked for this lead")
    if not (db.get_zoho_connection(owner_id) or {}).get("refresh_token"):
        raise ZohoError("Connect Zoho Calendar in Settings → My settings first")
    email = clean_meeting_input({"invite_email": email})["invite_email"]
    if not email:
        raise ZohoError("Type the client's email")
    if email != m["invite_email"]:
        db.save_meeting(business_id, invite_email=email, invited_at=None)
    m = sync_meeting(business_id, owner_id, notify=True)
    if m.get("sync_error"):
        raise ZohoError(m["sync_error"])
    return m


def cancel(business_id: int, owner_id: int, notify: bool) -> dict:
    """
    Take the meeting off the calendar and mark it cancelled -- the only path
    that does. `notify` emails the client a cancellation, and only matters if
    they were invited. Zoho failing leaves the meeting as it was, so it can't
    look cancelled here while still sitting on the calendar.
    """
    m = db.get_meeting(business_id)
    if not m or m["cancelled_at"]:
        raise ZohoError("There's no meeting booked for this lead")
    if m["event_uid"] and m["calendar_uid"]:
        if not (db.get_zoho_connection(owner_id) or {}).get("refresh_token"):
            raise ZohoError("Connect Zoho Calendar again to take it off the calendar")
        etag = m["event_etag"]
        data = {"etag": int(etag) if etag.isdigit() else etag,
                "notify_attendee": 1 if (notify and m["invited_at"]) else 0}
        try:
            _api(owner_id, "DELETE",
                 f"/calendars/{quote(m['calendar_uid'], safe='')}/events/{quote(m['event_uid'], safe='')}",
                 params={"eventdata": json.dumps(data)}, headers={"etag": str(etag)})
        except ZohoError as exc:
            # Already gone from the calendar is the result we wanted.
            if exc.status != 404:
                raise
    return db.save_meeting(business_id, cancelled_at=_now(), event_uid="", event_etag="",
                           join_url="", sync_error="")
