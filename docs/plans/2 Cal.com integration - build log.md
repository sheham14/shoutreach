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
| 5 | One Meetings page | not started | |
| 6 | The other operator's copy | not started | |
| 7 | Telling the team | not started | |
| 8 | Remove ShoutReach's Zoho booking | not started | |

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
