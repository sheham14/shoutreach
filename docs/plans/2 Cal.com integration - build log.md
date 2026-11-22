# Plan 2 · Cal.com integration — build log

A running log of the Plan 2 build, kept so a later session can pick up where
this one stopped. Newest entry last. Branch: `plan-2-cal-com`.

## How to resume

1. `git fetch origin plan-2-cal-com && git switch plan-2-cal-com`
2. Read the last entry below: it names the package in progress and what's left.
3. Environment set-up in a fresh container:
   `pip install --ignore-installed blinker -r requirements.txt Pillow beautifulsoup4 playwright playwright-stealth`

## Branch base

`plan-2-cal-com` starts at `bea9be1`, `master` with Plan 1 merged (PR #1,
deployed 2026-11-22 00:59 UTC, the deploy run green).

## Status

| Package | What | State | Commit |
|---|---|---|---|
| 1 | Settings and the two doors | done | `e9d6607` |
| 2 | An enquiry becomes a lead | done | `9a9625d` |
| 3 | A booking updates the lead | done | `e046fc6`, `48f13ab` |
| 4 | Following up people who didn't book | done | `5904f68` |
| 5 | One Meetings page | done | `2cffc4e` |
| 6 | The other operator's copy | done | `574cad8` |
| 7 | Telling the team | done | `62eb404` |
| 8 | Remove ShoutReach's Zoho booking | done | `fc6a8ed` |

## Log

### Package 1 — done (`e9d6607`)

- Tables: `website_enquiries` (full schema now, incl. owner_id and a nullable
  `business_id` FK ON DELETE CASCADE, so package 2 needs no ALTER if package 1
  is deployed alone, as the plan expects), `cal_events` (raw verified bodies,
  unique on SHA-256 of the body).
- Settings: `_website_config` (JSON: per-site owner + country, waits, booking
  link), `_website_key_sha256` (digest only; key shown once),
  `_cal_webhook_secret` (plain: HMAC needs it), `website_followup:<uid>`.
  All underscore/colon keys, so `GET /api/settings` never returns them.
- Routes: `/api/inbound/website-lead`, `/api/inbound/cal` (`_INBOUND_PATHS`,
  `_is_inbound_route` in both before_request hooks); `/api/website-settings`
  (+ `/key`, `/cal-secret`) admin; `/api/website-followup` own.
- Choice: the Cal route stores every verified delivery and answers 200; it
  does not parse yet. Idempotency in package 1 is per identical body;
  idempotency on Cal.com's booking id comes in package 3, from real payloads.
  This also makes the deployed route a safe place to capture real payloads.
- Choice: `X-Cal-Signature-256` is compared as lower-case hex, with an
  optional `sha256=` prefix tolerated. To confirm against a real delivery.
- Choice: an unknown site answers 400 before anything is stored; a site with
  no owner set still stores the enquiry (filed for no one until set).
- Tests: tests/test_inbound.py (56 checks). Browser: both cards, the
  show-once key dialog. Suite: 28 files pass.

### Package 2 — done (`9a9625d`)

- `file_website_enquiry` (called by `record_website_enquiry`), `_website_batch_id`
  (lead_batches kind `website`, name "Website — <site>", found by kind+source),
  `file_waiting_enquiries` (run by PUT /api/website-settings),
  `get_business_enquiries` (in `get_business_detail` as `enquiries`; answers
  as an ordered list of [question, answer] because Flask sorts JSON object
  keys).
- Stage `enquired` in `_LATER_PIPELINE_STAGES`, seeded by
  `_migrate_later_pipeline_stage` (marker `_seeded_pipeline_stage_enquired`),
  so a renamed or removed built-in is never put back. `PIPELINE_CHANNELS`
  gains `website`; the pill says "via the website".
- Choices: the business name falls back to the person's name when the form's
  business field is empty; an enquiry only sets the stage when the business
  has none (never moves a deal backwards); an enquiry filed once stays with
  its owner even if the site's owner setting changes later; a repeat post
  without answers keeps the first post's answers.
- Tests: tests/test_inbound.py §9. Browser: an enquiry posted to the harness
  the way a site would; Contacts → its panel shows the stage pill and the
  "Website enquiry" fold, answers in form order. Suite: 28 files pass.

### Stopped before package 3 — waiting for real Cal.com payloads

- `30b17d9`: admin download of received deliveries (`GET
  /api/website-settings/cal-events`, link on the Website and bookings card),
  to capture the payloads.
- Asked the operator (2026-11-22) to deploy packages 1–2, create the webhook,
  make a test booking from a site, reschedule it, cancel it, and send the
  download back. Packages 3–8 not started. When the file arrives: scrub it
  into `tests/fixtures/cal_*.json`, compare `payload.metadata` with the
  handoff, then build package 3 on it.

### Package 3 — created and rescheduled done (`e046fc6`); cancel pending

- Payloads captured on the live route (ping 01:28, created 01:32, rescheduled
  01:37 UTC, 22 Nov), scrubbed into `tests/fixtures/` (README there lists the
  replacements). Signature check worked on real deliveries.
- **Differences from the handoff:** metadata matches for hexiv.co.uk (site,
  phone, website, businessType, challenge, source, volume, ads, businessName)
  plus Cal.com's own `metadata.videoCallUrl`. The attendee's `phoneNumber` is
  null; the phone only comes via metadata. A reschedule is a NEW booking (new
  `uid`, new `bookingId`, new video link) with `rescheduleUid` /
  `rescheduleId` / `rescheduleStartTime` naming the old one. The operator's
  test phone had no `+` ("17097648718"), so it's read with the site's country.
- Code (db.py, end): `_cal_booking` (the only place payload fields are read),
  `_cal_booking_created`, `_cal_booking_rescheduled`, `process_cal_event(s)`,
  `CalWaiting` (site has no owner → kept unprocessed, retried on settings
  save). Meetings gained `source`, `cal_uid`, `state`, `starts_utc`.
  `_end_email_enrolments` + `_stop_followups_on_booking`, called from
  `set_business_pipeline` when the stage becomes booked; `log_call` uses the
  first.
- Choices: answers for a booking with no prior enquiry are parsed from the
  site's "Label: answer" notes, leaving out the business/website/phone lines;
  a booking whose metadata has no known site is recorded on the event as
  "not filed" (package 5's signed reference covers bookings from a copied
  link); a reschedule whose original never arrived is filed as a booking
  marked moved.
- Departure: package 3 lands in two commits, because the cancellation payload
  hadn't been captured yet.
- Note for the operator: the three real deliveries on the live server are
  still unread; the first delivery or settings save after this deploys reads
  them, so the test booking appears as a booked lead (delete it if unwanted).
- Tests: tests/test_cal_bookings.py. Suite: 29 files pass.

### Package 3 — cancel done (`48f13ab`); package 3 complete

- Cancellation captured 01:42 UTC from Cal.com's emailed cancel link (the
  operator's earlier "No" on the calendar invite RSVP went to the calendar,
  not Cal.com, so no webhook — expected). `BOOKING_CANCELLED` carries the uid
  of the booking it ends (the rescheduled one), `status` "CANCELLED",
  `cancellationReason` "" and `requestReschedule` false.
- `_cal_booking_cancelled`: meeting state `cancelled`, `cancelled_at` set,
  stage untouched; reason kept on the event's outcome.
- Panel: `calMeetingBoxHtml` for `source='cal'` meetings (package 5 reduces
  the Zoho box for the rest).
- Browser: the four scrubbed fixtures replayed, signed, into the harness;
  the lead read "Today 6:30 pm · Booked on Cal.com · from hexiv.co.uk ·
  Join", then "8:00 pm · Cancelled on Cal.com", no Join.

### Package 4 — done (`5904f68`)

- `db.run_website_followups(now=None)`; scheduler `run_website_followups()`
  and `run_cal_events()` in `_one_pass`, each in its own try.
- `enroll_contacts_bulk(..., not_before=None)`: first `next_send_at` is the
  later of now and `not_before` (UTC).
- Choices: "booked in between" = the enquiry has a Cal.com booking, or the
  business's stage is `booked` (or `booked_elsewhere`, package 6). A business
  marked do not contact (e.g. it unsubscribed) gets neither follow-up. Every
  enquiry past the wait is marked queued even when nothing was queued (booked,
  or the owner chose no campaigns), so it is considered once: choosing
  campaigns later doesn't reach back to older enquiries. WhatsApp uses the
  enquiry's country, else the site's.
- Tests: tests/test_website_followups.py (injected clock). Suite: 30 files.

### Package 5 — done (`2cffc4e`)

- db: `get_meetings`, `count_meetings`, `get_recent_enquiries`,
  `make_booking_ref` / `read_booking_ref` / `booking_link_for` (`CAL_REF_KEY`
  = `shoutreach_ref`, signed with the install's `_secret_key`); the Cal.com
  parser reads `metadata.shoutreach_ref` and, when it verifies, books exactly
  that lead (channel from the ref) instead of matching by site and email.
- Routes: `/api/meetings`, `/api/meetings/count`,
  `/api/businesses/<id>/booking-link?channel=`; `/api/dashboard` gains
  `website`.
- UI: `templates/sections/meetings.html`, `static/js/meetings.js`, sidebar
  link with count (`refreshMeetingCount()` in app.js at load), Dashboard
  "From the website" card, reduced meeting box, "Copy booking link".
  WhatsApp Booked tab and its JS removed.
- **To confirm with a real booking:** the prefill parameters of the copied
  link (`name`, `email`, `metadata[shoutreach_ref]` in the query string) are
  Cal.com's documented URL prefill, not something captured — a booking made
  from a copied link should arrive with `metadata.shoutreach_ref`. Asked the
  operator to try one.
- Departure: "setting booked by hand no longer touches any calendar" — the
  panel's box no longer offers calendar actions now, but the pipeline route's
  call into the Zoho code is removed with that code in package 8 (removing it
  here would break `tests/test_zoho_calendar.py`, which package 8 deletes).
- Choices: "Needs an outcome" = the meeting time has passed and the stage is
  still booked; a cancelled meeting stays listed (marked) while the stage is
  booked, and leaves the sidebar count; a meeting set by hand reads "set by
  hand" when its stage has no channel.
- Browser: Meetings at 1440×900 (with the lead open beside it) and 390×844;
  Dashboard block; WhatsApp tabs without Booked (a remembered Booked tab opens
  To do). Audit: F-19, F-23, F-24 marked resolved. Suite: 31 files pass.

### Package 6 — done (`574cad8`)

- `mark_other_operators_copies(business_id)`, called at the end of
  `file_website_enquiry` (so both an enquiry and a booking that files one
  trigger it). Uses `find_existing_business(..., owner_id=other)` for each
  other user — the same matcher as the import's overlap notice.
- Stage `booked_elsewhere` in `_LATER_PIPELINE_STAGES` (terminal, seeded by
  its own marker). `set_business_pipeline` stops follow-ups for it as for
  `booked`.
- Choices: leaves a copy alone if it's already `booked` (their own meeting)
  or on any terminal stage; clears `pipeline_at` too so not even the marking
  time shows; a booking from a copied link (package 5's reference) doesn't
  trigger it — it isn't a website lead being filed. **Question for the
  operator:** the decided label is "Booked via the website", and the plan
  applies it on an enquiry too, before any booking. It's built that way; say
  if an enquiry-only match should read differently.
- Handover §3 updated. Suite: 32 files pass.

### Package 7 — done (`62eb404`)

- `team_notifications` table; `get_notify_email` / `save_notify_email`
  (`notify_email:<uid>`), `queue_team_notification` (called on an enquiry's
  first filing in `file_website_enquiry`, and on a booking in both paths of
  `_cal_booking_created`), `run_team_notifications(send=None)`;
  `sender.send_notification`; scheduler `run_team_notifications()` in
  `_one_pass`. Routes `GET/PUT /api/notify-email`; field in My settings'
  "Website enquiries that don't book" card.
- Choices: sent by the scheduler, not the routes (contract B: never call out
  from the inbound route); first active sending account; not counted against
  any cap; five attempts.
- Tests: tests/test_notifications.py. Suite: 33 files pass.

### Package 8 — done (`fc6a8ed`)

- **Removed:** `zoho_calendar.py`; `/api/zoho/{status,calendars,prefs,disconnect}`,
  `/zoho/connect`, `/zoho/callback`; `/api/businesses/<id>/meeting/{sync,invite,cancel}`
  and `/api/meetings/address-check`; `_book_meeting` and its use in
  `api_set_business_pipeline` and `api_log_call`; Team setup "Zoho Calendar" and
  My settings "Meetings & calendar"; the stage dialog's calendar fields
  (`meetingFieldsHtml`, `collectMeetingFields`, `_meetingEmailChanged`,
  `toastMeeting`, `loadZohoStatus`/`loadZohoCalendars` and the option helpers) and
  the panel's `retryMeetingSync`, `sendMeetingInvite`, `cancelMeeting`; in db
  `get/save_meeting_prefs`, `get/save/delete_zoho_connection` and the
  `MEETING_*` constants; Calling's Zoho hint, toasts and `timezone` field;
  `tests/test_zoho_calendar.py`.
- **Kept:** the `meetings` and `zoho_connections` tables (comments now say
  retired). `_migrate_retire_zoho` (in `init_db`, marker
  `_retired_zoho_calendar`) empties `zoho_connections` once.
- **Booking by hand:** the pipeline route saves the stage and time through
  `set_business_pipeline` and nothing else; a `meeting` key in the body (what
  the old dialog sent) is ignored. A "Meeting booked" call outcome likewise.
  The stage dialog shows one line for Meeting booked: the time is kept here and
  shows on Meetings, nothing goes on a calendar, use Copy booking link to book
  on Cal.com. Old Zoho rows (`source` '') show in the reduced box
  (`meetingBoxHtml`): the stage's time, where it came from, Join if the row
  has a link.
- **Departures and choices:**
  - The clean-up also deletes `zoho_region`, `zoho_client_id`,
    `zoho_client_secret` and every `meeting_prefs:<id>` from settings. The plan
    names only `zoho_connections`, but the client secret is a secret for the
    same reason, and the meeting defaults are the B-7 half that goes with Zoho.
  - `/api/meetings/address-check` is removed with the dialog's email field it
    served. That also closes the meeting half of B-8's "small tells".
  - Booking by hand no longer writes a `meetings` row. The Zoho code wrote one
    even with no connection. Nothing needs it: the Meetings page and the panel
    take the time from the stage.
  - `get_pipeline_board` no longer selects the Zoho columns (`event_uid`,
    `sync_error`, invite fields); nothing read them after the Booked tab went.
  - Kept, though no route calls them now: `db.add_business_email`
    (`tests/test_suppression.py` checks it honours suppression) and
    `db.save_meeting` (`tools/audit/serve.py` uses it to plant old-style rows).
  - Three tests from packages 4, 5 and 7 (`test_website_followups.py`,
    `test_meetings.py`, `test_notifications.py`) read source files without
    `encoding="utf-8"`, so they failed on Windows (cp1252) though they pass on
    Linux. Fixed in this commit. The baseline on this machine before package 8
    was 30 of 33 passing, the three being exactly these.
- **Audit:** the Zoho parts of B-7, B-8, B-13 and B-17 marked resolved in
  place, and the status block updated (it also now counts package 5's F-19,
  F-23, F-24, which weren't in it). Handover: §5f marked retired with what was
  removed and kept, its Zoho to-do list removed from §9, §0/§1/§8/§10 and the
  B-13 line updated, a Plan 2 status paragraph added to §9. README's booking
  text rewritten for Cal.com.
- **Tests:** `tests/test_meetings.py` §5 (every removed route is a 404; no
  module; a backslash business name booked by hand → 200, stage and typed time
  saved, no meeting row, no saved invite address, listed on Meetings; a
  "Meeting booked" call outcome → stage and time, no row; an old Zoho row
  reaches the panel and Meetings with its Join link, and moving the stage's
  time leaves it alone; no Zoho code or wording left in `static/js`,
  `templates` or `app.py`; the clean-up empties the table and the settings,
  keeps other per-person keys and both tables, and runs once).
- **Browser** (audit harness, `tools/audit/serve.py`, Chromium 1440×900):
  Meetings lists the harness's three old Zoho rows, with Join on the one that
  has a link; that lead's panel shows the reduced box ("Today 3:30 pm · from
  WhatsApp · Join ↗ · Arranged outside Cal.com…"); the stage dialog for
  Meeting booked shows only the stage, the time and the new line, and saving
  stores the typed time; Settings has neither Zoho card. No console errors
  from this change (see "Noticed" for one from the harness).
- Suite: 32 files pass.

## Hand-back (Plan 2, 2026-11-22)

The plan's "Hand-back" section asks for: per package the commit, tests added,
what was checked by hand and what was done differently; the captured Cal.com
payloads and how they differed from the handoff; plus, from this log, what
each website needs, the operator's steps and the open questions. A reviewer
checks the branch against the plan separately.

**State of the branch:** all eight packages are built on `plan-2-cal-com`.
Packages 1 and 2 (and `30b17d9`, the payload download) are merged and
deployed (PR #2). Packages 3 to 8 are only on the branch: **merging it
deploys them** and runs package 8's one-time clean-up on the live database
(below). Suite: 32 files, all pass on Windows (Python 3.13) and were written
for the server's Python 3.11.

### Per package

| # | Commit(s) | Tests added | Checked by hand | Done differently from the plan, and why |
|---|---|---|---|---|
| 1 Settings and the two doors | `e9d6607`; `30b17d9` (download of received deliveries) | `tests/test_inbound.py` (no key, wrong key, worker key → 401; bad signature → 401; unknown site → 400; oversize → 413; repeat post doesn't duplicate; 56 checks) | Browser: both Settings cards, the show-once key dialog | The Cal.com route stored every verified delivery and answered 200 without parsing, so the deployed route could capture real payloads before the parser existed. Idempotency was per identical body until package 3 added Cal.com's booking id. `30b17d9` (an admin download of what Cal.com sent) isn't in the plan; it was how the payloads were captured. A site with no owner set still stores the enquiry, filed for no one until set |
| 2 An enquiry becomes a lead | `9a9625d` | `tests/test_inbound.py` §9 (owner from the site setting; the other operator can't see it; suppressed address recorded, its email lead suppressed; a local UK number matches the international one) | Browser: an enquiry posted the way a site would; Contacts → its panel shows the stage pill and the "Website enquiry" fold, answers in form order | Business name falls back to the person's name when the form leaves it empty. An enquiry sets the stage only when the business has none, so a deal never moves backwards. Answers stored as an ordered list, because Flask sorts JSON object keys |
| 3 A booking updates the lead | `e046fc6` (created, rescheduled), `48f13ab` (cancelled) | `tests/test_cal_bookings.py`, built on the captured fixtures (created → stage, time, meeting row, email enrolment ended, unsent WhatsApp paused; repeat delivery changes nothing; reschedule; cancel; no `wa_log` row ever) | Browser: the four scrubbed fixtures replayed, signed, into a local server; the lead read "Today 6:30 pm · Booked on Cal.com · from hexiv.co.uk · Join", then "Cancelled on Cal.com" with no Join | Two commits, because the cancellation payload was captured later. A booking with no prior enquiry takes its answers from the site's "Label: answer" notes. A booking whose metadata names no known site is recorded on the event as "not filed". A reschedule whose original never arrived is filed as a booking marked moved. A site with no owner keeps the event waiting (`CalWaiting`), retried on the next settings save |
| 4 Following up people who didn't book | `5904f68` | `tests/test_website_followups.py` (injected clock: nothing before the wait; both after; only email with no number; neither when booked in between; no email to a suppressed address; run twice, queued once) | Nothing beyond the tests | "Booked in between" also counts stage `booked`/`booked_elsewhere`, not only a Cal.com booking. A do-not-contact business gets neither follow-up. Every enquiry past the wait is marked queued even when nothing was queued, so choosing campaigns later doesn't reach back to older enquiries |
| 5 One Meetings page | `2cffc4e` | `tests/test_meetings.py` §1–4 (every channel listed; count; Copy booking link signed and matched; Dashboard block; Booked tab gone) | Browser: Meetings at 1440×900 with a lead open beside it and at 390×844; the Dashboard block; WhatsApp tabs without Booked (a remembered Booked tab opens To do) | The pipeline route's call into Zoho stayed until package 8, because removing it earlier would have broken `test_zoho_calendar.py` before package 8 deleted it. "Needs an outcome" = the time has passed and the stage is still booked. A cancelled Cal.com meeting stays listed while the stage is booked, and drops out of the count |
| 6 The other operator's copy | `574cad8` | `tests/test_booked_elsewhere.py` (the other operator sees the stage and nothing more, through every route; their follow-ups stop; the enquiry's owner sees nothing about the other copy) | Nothing beyond the tests | Leaves a copy alone if its owner already booked it or it's on a terminal stage; clears `pipeline_at` so not even the time of marking shows; a booking from a copied link doesn't trigger it. Handover §3 records the crossing |
| 7 Telling the team | `62eb404` | `tests/test_notifications.py` (own address per operator; routes queue, never send; retried then left; no address, no email; the scheduler runs it) | Nothing beyond the tests | Sent by the scheduler, not the routes (contract B: never call out from the inbound route), from the first active sending account, not counted against any cap, five attempts |
| 8 Remove ShoutReach's Zoho booking | `fc6a8ed` | `tests/test_meetings.py` §5; `tests/test_zoho_calendar.py` deleted | Browser, on the audit harness: old Zoho rows on Meetings and in the reduced box with Join; the stage dialog without calendar fields saves the typed time; Settings without either Zoho card | The clean-up also deletes the Zoho settings and `meeting_prefs:<id>` (secrets, and B-7's meeting half). `/api/meetings/address-check` removed with the invite field it served. Booking by hand writes no `meetings` row. Three tests from packages 4, 5, 7 fixed to read files as UTF-8 (they failed on Windows). Details in the package 8 entry above |

### The Cal.com payloads (`tests/fixtures/`)

Captured on the live route on 2026-11-22: the webhook page's test ping
(01:28 UTC), a real booking made through hexiv.co.uk's form (01:32), its
reschedule (01:37) and its cancellation from Cal.com's emailed cancel link
(01:42). Scrubbed copies: `cal_ping.json`, `cal_booking_created.json`,
`cal_booking_rescheduled.json`, `cal_booking_cancelled.json`.
`tests/fixtures/README.md` lists what was replaced (attendee name and email,
organizer email, business/website/phone, the CalDAV calendar id). Everything
else is exactly as sent. The signature check (`X-Cal-Signature-256`,
lower-case hex HMAC-SHA256 of the raw body) passed on all four real
deliveries.

How they differ from the handoff document:

- **Metadata matches, and has more.** `payload.metadata` carries `site`,
  `phone` and `website` as the handoff says, plus the UK form's answers
  under their own keys (`businessType`, `challenge`, `source`, `volume`,
  `ads`, `businessName`) and Cal.com's own `videoCallUrl`.
- **The phone is only in metadata.** The attendee's `phoneNumber` is null.
- **A reschedule is a new booking.** New `uid`, new `bookingId`, new video
  link; `rescheduleUid`, `rescheduleId` and `rescheduleStartTime` name the
  old one. The handoff assumed the same booking moved.
- **A cancellation carries the uid of the booking it ends** (the rescheduled
  one), `status` "CANCELLED", `cancellationReason` "" and
  `requestReschedule` false.
- **Declining the calendar invite doesn't cancel.** The operator's "No" on
  the invite went to the calendar, not to Cal.com, so no webhook came. Only
  Cal.com's own cancel link (or its dashboard) sends one.
- **The test phone had no `+`** ("17097648718"), so phones are read with
  the site's default country.
- **The ping is a different shape**: no `uid`, no metadata, organizer time
  zone `Europe/London`. It is answered 200 and ignored.
- **The host time zone in the real bookings is `America/St_Johns`**
  (Newfoundland), for the organizer and the attendee. See open question 4.

### The websites (`HexivUK`, `hexivsite`: separate sessions)

Each site gets the same small change: a **server route** that the booking
form calls at the moment it reaches the calendar (where it calls EmailJS
today). The route checks the shape and forwards to ShoutReach. The form must
not wait on it: if ShoutReach is down or slow, the calendar still appears
(fire and forget, short timeout, errors logged on the server only).

**Contract A, as built:**

```
POST https://shoutreach.hexiv.co/api/inbound/website-lead
X-API-Key: <the website key>          (server-only env var, never NEXT_PUBLIC_)
Content-Type: application/json

{
  "site":     "hexiv.co.uk" | "hexiv.co",        required; anything else → 400
  "name":     "...",                             required
  "email":    "...",                             required
  "business": "...",                             optional (≤200)
  "phone":    "...",                             optional, as typed (≤50)
  "website":  "...",                             optional (≤500)
  "country":  "GB",                              UK site always "GB"; .co only if it knows
  "locale":   "en" | "ar",                       .co only
  "answers":  { "Question label": "Answer label", ... },   English labels, in the order asked, ≤40
  "summary":  "the plain-text summary the site already builds"   (≤5000)
}
```

- Replies `{"ok": true}` and never echoes data. 401 for a missing or wrong
  key (a scraper worker key doesn't work here), 400 for a bad shape or
  unknown site, 413 over 32 KB, 429 over 60 posts a minute per key.
- Unknown fields are ignored; every string is cut to its limit; a list
  answer is joined with ", ".
- Posting the same email from the same site again updates the open enquiry
  rather than adding a second; an empty field keeps what the first post
  said.
- **hexiv.co.uk:** the form already has English labels; send `site:
  "hexiv.co.uk"` and `country: "GB"`.
- **hexiv.co:** today it sends option *ids*; map each to its English label
  with the English content file (`content.en.ts`) before sending, whatever
  the visitor's language. Send `site: "hexiv.co"`, `locale`, and `country`
  only if the form knows it.
- **Keep EmailJS** until package 7's team emails have been seen working for
  a week; removing it is a separate change.
- **Copy check:** the line under the details step should say the visitor
  may be followed up by email or WhatsApp. If it doesn't, propose wording;
  the operator approves copy changes.
- The Cal.com embed already sends `metadata[site]`, `metadata[phone]`,
  `metadata[website]` and the answers; keep it as it is (package 3 reads
  them from the captured shape).

### For the operator

The webhook is already created in Cal.com and verified working (it delivered
the four test events). Still to do:

1. **Merge `plan-2-cal-com`** when the review passes (it deploys packages 3
   to 8). Take a backup first, as always. Package 8's clean-up runs once on
   the live database: it empties `zoho_connections` and deletes
   `zoho_region`, `zoho_client_id`, `zoho_client_secret` and every
   `meeting_prefs:<id>` setting. Nothing was ever connected, so nothing a
   person uses is lost.
2. **Team setup → Website and bookings:** set who receives each site's
   leads and the .co site's default country (the UK site defaults to GB);
   paste the Cal.com booking link (e.g.
   `https://cal.com/hexiv/free-strategy-call`) for Copy booking link; check
   the two waits (30 minutes for WhatsApp To do, 24 hours for the email).
   Saving this also files any enquiries or bookings that arrived while a
   site had no owner.
3. **Generate the website key** on that card (shown once), and put it on
   each site's hosting as a server-only environment variable.
4. **Each person, My settings → Website enquiries that don't book:** pick
   the email campaign and the WhatsApp campaign for people who don't book,
   and a notification address. Until a campaign is chosen, enquiries are
   recorded but not followed up; without an address, no team email is sent.
5. Once the sites post contract A, watch one real enquiry end to end:
   it appears as a lead, its owner gets the email, it's followed up after
   the waits if it doesn't book.

### Open questions

1. **ANSWERED 2026-11-22, built in `0997911`:** an enquiry alone now reads
   "Filled in the website form — not booked"; see "Follow-ups after the
   hand-back" below. *(Original question:)* **"Booked via the website" on an
   enquiry alone (package 6).** The plan
   applies the other operator's marking when an enquiry is filed, before any
   booking, so their copy reads "Booked via the website" even if the visitor
   never books. Built as the plan says. Should an enquiry-only match read
   differently?
2. **Copy booking link, unproven against Cal.com (package 5).** The copied
   link carries `metadata[shoutreach_ref]` using Cal.com's documented URL
   prefill. Not yet seen in a real booking. Make one booking from a copied
   link and check that it lands on that lead (the Meetings page shows it
   "from a call" or "from WhatsApp", not as a new website lead).
3. **The three real test deliveries on the live server** are still unread
   by the deployed code (packages 1–2 only store them). The first delivery
   or Website-settings save after the merge reads them, so the test booking
   appears as a lead (the operator's own test business, Hevixxx), booked then
   cancelled. Delete it once seen.
4. **The Cal.com host's time zone.** The real bookings say the organizer is
   on `America/St_Johns` (Newfoundland, UTC−3:30). ShoutReach converts each
   booking into the host's time zone once, on arrival, and shows that wall
   clock everywhere (Handover §5c). If the Cal.com profile's time zone is
   wrong, every website meeting will show at Newfoundland time. Check
   Cal.com → Settings → General → Timezone before the merge.

### Noticed, not fixed

1. **FIXED in `dd3715e`** (see below). **A Cal.com booking that was
   cancelled, then booked again by hand**
   (packages 3 and 5). The `meetings` row stays `source='cal'`,
   `state='cancelled'`. So the panel keeps showing the Cal.com box,
   "Cancelled on Cal.com" with the old time, instead of the stage's new
   time. Meetings marks the row cancelled and leaves it out of the sidebar
   count, though it lists the new time. Found by reading
   `meetingBoxHtml` and `count_meetings`; not exercised. Fixing it needs a
   decision: should a hand booking after a Cal.com cancellation hide the old
   box, or clear the old row? Left for the review.
2. **FIXED in `8374b4f`** (see below). **A saved lead audit from before
   `engagement` existed breaks the lead panel.** `auditResultsHtml` reads `site.engagement.length` with no guard,
   so a business whose audit was saved before `cf49426` (2026-09) throws, and
   its panel doesn't render. Seen on the audit harness, whose canned audit
   lacks the field. Worth checking whether the live database has such audits.
   Not related to this plan.
3. `db.add_business_email` and `db.save_meeting` have no caller in a route
   any more (kept for a test and the harness, above). `/api/businesses/<id>/ics`
   is no longer linked from any page since the Booked tab went (package 5);
   Calling's `/api/calls/<id>/ics` still is.
4. **FIXED in `c207d9f`** (with three more in sender.py). `app.py` imports
   `sys` and `url_for` without using them, and `db.py` imports `Path` without
   using it. This was already so before this branch.

### For the reviewer

```bash
git fetch origin plan-2-cal-com && git switch plan-2-cal-com
pip install --ignore-installed blinker -r requirements.txt Pillow beautifulsoup4 playwright playwright-stealth
for f in tests/test_*.py; do python "$f" > /dev/null 2>&1 || echo "FAIL $f"; done   # 32 files, no output = all pass
python tests/test_meetings.py          # §5 is package 8
python tests/test_cal_bookings.py      # the parser, on the real fixtures
git diff bea9be1..plan-2-cal-com --stat
git grep -n -i "zoho" -- '*.py' '*.js' '*.html'
#   expected: Zoho *Mail* (SMTP/IMAP help, provider detection), the retired-table
#   comments and _migrate_retire_zoho in db.py, one docstring line in app.py,
#   tests/test_meetings.py §5, and tools/audit/serve.py's fake old meeting rows
```

Replaying the fixtures by hand: start the app on a throwaway database
(`DB_PATH`, `SHOUTREACH_NO_SCHEDULER=1`), set a Cal.com secret in Team setup,
then post each `tests/fixtures/cal_*.json` to `/api/inbound/cal` with
`X-Cal-Signature-256` = hex HMAC-SHA256 of the exact bytes with that secret
(`deliver()` in `tests/test_cal_bookings.py` does exactly this).


## Follow-ups after the hand-back (2026-11-22, second pass)

The operator asked for these fixes in one batch, for one review; testing
items left for later. Each is its own commit on `plan-2-cal-com`; the
suite passes after each.

1. `913048e` — **date-dependent test.** `test_website_followups.py` fixed
   the enquiry's arrival at 22 Nov 10:00 UTC; once the real clock passed
   12:00 UTC the expected "hold until 12:00" was in the past and the test
   failed with nothing wrong in the app. Arrival is now two days ahead of
   the real clock and every expected time is computed from it.
2. `0997911` — **open question 1 answered:** an enquiry alone no longer
   reads "Booked". New built-in terminal stage `enquired_elsewhere`
   ("Filled in the website form — not booked"); it moves up to
   `booked_elsewhere` when a booking follows, never back down. Follow-ups
   stop either way. Handover §3 updated. Tests in test_booked_elsewhere.py.
3. `dd3715e` — **"Noticed" 1 fixed:** booking by hand (pipeline route, or a
   "Meeting booked" call outcome) after a Cal.com cancellation deletes the
   cancelled Cal.com meeting row (`clear_cancelled_cal_meeting`), so the
   lead and Meetings show the new time. A live Cal.com meeting is never
   touched. Tests: test_cal_bookings.py §5. Known edge left alone: booking
   by hand while a Cal.com meeting is still on changes the stage's time,
   but the panel's Cal.com box still shows Cal.com's time (Cal.com owns it).
4. `8374b4f` — **"Noticed" 2 fixed:** the lead panel no longer crashes
   on an audit saved before `engagement` existed; four list fields default
   to empty. Verified on the harness (lead 41 threw before, renders after).
5. `c207d9f` — **"Noticed" 4 fixed:** unused imports removed (`sys`,
   `url_for` in app.py; `Path` in db.py; and pyflakes found `time`, `random`,
   `Optional` in sender.py). Nothing referred to them in the app, tests or
   tools/audit.

**Where this leaves Plan 2 (for the next session):** all eight packages and
these follow-ups are on `plan-2-cal-com`, pushed, suite 32/32 passing (also
compiled under Python 3.11). Nothing left to build. Next steps belong to
the operator: one batch review, then merge (deploys packages 3–8 and the
follow-ups; take a backup first — package 8's clean-up runs once). The
three testing items below are left for the operator. Plan 3 (redesign) is
to be done later in its own session and branch, not on this one.

Still for the operator to test later (unchanged): a booking made from
"Copy booking link" (question 2); Cal.com's time zone (question 4);
deleting the "Hevixxx" test booking after the merge (question 3).
