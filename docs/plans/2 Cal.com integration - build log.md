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
| 8 | Remove ShoutReach's Zoho booking | **next — not started** | |

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

## STOPPED HERE (2026-11-22, session usage ran out) — how to carry on

**Next: package 8, remove ShoutReach's Zoho booking (last).** Per the plan:
delete `zoho_calendar.py`, the `/api/zoho/*` and `/zoho/*` routes, the
meeting sync/invite/cancel routes (`/api/businesses/<id>/meeting/...`),
`_book_meeting` and its two callers (`api_set_business_pipeline` with
`meeting` in the body, and `api_log_call`), the Zoho cards in Settings (Team
setup "Zoho Calendar", My settings "Meetings & calendar"), the booking
dialog's calendar fields (`meetingFieldsHtml` in lead_panel.js and whatever
calls it; `_zohoStatus`, `retryMeetingSync`, `sendMeetingInvite`,
`cancelMeeting` and friends), and `tests/test_zoho_calendar.py`. Keep the
`meetings` and `zoho_connections` tables; empty `zoho_connections` with a
guarded one-shot (it can hold tokens). Setting the stage to booked by hand
must still work with a time (that's the package 5 departure noted above).
Then: mark Handover §5f retired and remove its Zoho to-do list from §9; mark
the Zoho parts of audit B-7, B-8, B-13, B-17 resolved (find them by id).
Check meetings rows with source '' (old Zoho ones) still render via the
reduced box (`meetingBoxHtml`).

**Then the hand-back** (plan's "Hand-back" section): per package the commit,
tests, what was checked by hand (each package entry above has it), the
scrubbed payloads (`tests/fixtures/`, README there) and how they differed
from the handoff (package 3 entry). Also still to write up there:

- **Website repos (separate sessions)** — each site's server route posts
  contract A to `https://shoutreach.hexiv.co/api/inbound/website-lead` with
  header `X-API-Key: <website key>` from a server-only env var, at the moment
  it calls EmailJS today, without waiting on it. JSON body: `site`
  ("hexiv.co.uk" | "hexiv.co"), `name`, `email` (required), `business`,
  `phone`, `website`, `country` ("GB" from the UK site), `locale` (.co only),
  `answers` (object, English labels, in the order asked — .co maps its option
  ids via content.en.ts), `summary`. ≤ 32 KB; reply `{"ok": true}`; 60/min.
  Keep EmailJS until package 7 has been seen working for a week. Check the
  line under the details step says they may be followed up by email or
  WhatsApp; propose wording if not.
- **Operator steps:** set each site's owner and country, the booking link,
  and their notification address and follow-up campaigns; put the website
  key on each site's hosting. The webhook already exists and works.
- **Open questions:** (1) "Booked via the website" is also applied when only
  an enquiry matches the other operator's copy (package 6); (2) verify with
  one real booking made from "Copy booking link" that
  `metadata.shoutreach_ref` arrives (package 5); (3) the three real test
  deliveries on the live server get read once this deploys — the test
  booking will show as a lead (Hevixxx), booked then cancelled; delete it.
- **Reviewer commands:** `for f in tests/test_*.py; do python "$f" || echo FAIL $f; done`
  (33 files); replay `tests/fixtures/cal_*.json` signed into a local server
  (see tests/test_cal_bookings.py for how).
