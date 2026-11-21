# Plan 2 · Website enquiries and Cal.com bookings

**For:** a coding session. **Branch:** `plan-2-cal-com`. **Depends on:** Plan 1
packages 1 (suppression list) and 4 (sender resilience) being merged.
**Read first:** [README.md](README.md) here, then
[Cal.com Booking Handoff](../Cal.com%20Booking%20Handoff.md), which says what
the two websites already do and how the calendar is connected. Don't redo the
calendar setup.

## What this builds

1. A visitor who fills in the booking form on hexiv.co.uk or hexiv.co and
   reaches the calendar becomes a lead in ShoutReach, booked or not.
2. A booking, reschedule or cancellation on Cal.com updates that lead.
3. People who reach the calendar and don't book get followed up: one email,
   and a place in WhatsApp To do if they gave a number.
4. Every meeting, from any channel or the website, is listed in one Meetings
   page.
5. ShoutReach's own Zoho booking is removed. Cal.com is the only thing that
   creates calendar events.

## Decided — do not reopen

- **Cal.com is the only calendar writer.** ShoutReach never creates, moves or
  cancels a calendar event, and never emails an invite.
- **Cal.com webhooks are used** (available on the operator's plan, confirmed
  2026-11-21).
- **Who gets a website lead is a setting, per site.** It applies even when the
  other operator already holds that business.
- **When the other operator holds the business,** their copy is marked "Booked
  via the website" and its follow-ups stop. It shows nothing else — no time,
  no answers, no name of who has it. This crosses the wall on purpose
  (decided 2026-11-21) and is isolated in package 6.
- **Follow-up for people who don't book:** after a wait, one email (if the
  address isn't suppressed) and, if they gave a number, a place in WhatsApp
  To do. **Nothing sends a WhatsApp.** The waits are settings: 30 minutes
  before WhatsApp To do, 24 hours before the email.
- **A suppressed address is still recorded as an enquiry** — the person asked
  to be contacted about a booking — but gets no automatic email.
- **ShoutReach's Zoho booking (Handover §5f) is retired.** It was never
  connected to real Zoho.

## The contract between the websites and ShoutReach

Define it once here; both sites and ShoutReach follow it.

### A. A lead reaches the calendar

`POST /api/inbound/website-lead`

- **Auth:** header `X-API-Key: <website key>`. This is a **new kind of key**,
  not the scraper worker's: one key for the websites, held in Team setup,
  shown once, rotatable. A worker key must not work here and this key must
  not work on worker routes.
- **Body (JSON, at most 32 KB):**

  | Field | Notes |
  |---|---|
  | `site` | `hexiv.co.uk` or `hexiv.co`. Anything else is refused |
  | `name`, `email` | Required. The form requires both |
  | `business` | Business or clinic name; may be empty |
  | `phone`, `website` | Optional, as typed |
  | `country` | Two-letter code used to read a local phone number. `GB` from the UK site; the .co site sends nothing unless it knows |
  | `locale` | `en` or `ar`, .co only |
  | `answers` | Object of question → answer, **as readable labels in English**, not option ids |
  | `summary` | The plain-text summary the site already builds |

- **Reply:** `{"ok": true}`. Never echo lead data back.
- **Idempotent:** the same email from the same site again updates the open
  enquiry rather than creating a second one.
- **Limits:** refuse more than a sensible number per minute per key. Unknown
  fields are ignored. Every string is length-capped.

### B. Cal.com reports a booking

`POST /api/inbound/cal`

- **Auth:** Cal.com signs the body. Verify `X-Cal-Signature-256` (HMAC-SHA256
  of the raw body with the webhook secret held in Team setup), in constant
  time, before parsing. No session, no API key.
- **Events:** booking created, rescheduled, cancelled. Anything else: answer
  200 and ignore.
- **Idempotent on Cal.com's booking id.** Cal.com retries.
- **Answer fast.** Do the work, answer 200; never call out to another service
  from this route.

**Before writing the parser, capture real payloads.** Do not write Cal.com's
field names from memory. Use the webhook page's test ping, then one real test
booking, one reschedule and one cancellation against a temporary address that
only records the body. Save scrubbed copies as `tests/fixtures/cal_*.json` and
build the parser and its tests on those. The handoff says each booking carries
`metadata` with `site`, `phone`, `website` and the answers; confirm that in
the captured payload.

Both routes are outside sign-in and the anti-forgery check, the same way the
worker routes are (`_is_worker_route`, `worker_auth_required`). Add them to
that mechanism explicitly; don't widen `_PUBLIC_PATHS` by prefix.

## Packages

### 1 · Settings and the two doors

- Team setup gets a "Website and bookings" card (admins): the website key
  (generate, rotate), the Cal.com webhook secret, the address to paste into
  Cal.com, and per site: who receives its leads and the default country.
  Also the two waits, and the Cal.com booking link.
- Per operator (My settings): which email campaign non-bookers are enrolled
  in, and which WhatsApp campaign they are added to. Either may be left
  unset; then that follow-up doesn't happen.
- Both routes, with auth, limits and idempotency, storing enquiries
  (package 2). Secrets are masked in `GET /api/settings` like the others.

**Tests** (`tests/test_inbound.py`): no key, wrong key, a worker key → 401; a
bad signature → 401; an unknown site → 400; oversize body → 413; a repeat post
doesn't duplicate.

### 2 · An enquiry becomes a lead

- New table `website_enquiries`: the business it belongs to (foreign key with
  `ON DELETE CASCADE`, so Recently deleted covers it automatically), site,
  locale, the answers and summary, when it arrived, the Cal.com booking id
  once there is one, and when its follow-up was queued.
- The business is found or created with the existing matcher
  (`find_or_create_business`) for the site's owner, passing the country so a
  local number matches an international one. The email becomes an email lead
  with the person's first name. Creation goes through Plan 1's suppression
  helper.
- Each site has one named import in `lead_batches` ("Website — hexiv.co"), so
  the existing "From" filter finds website leads on every Leads tab.
- A new built-in pipeline stage, `enquired` ("Enquired on the website"), set
  with a new pipeline channel value `website`. Add `website` to
  `PIPELINE_CHANNELS`. Seed the stage with its own guarded migration; never
  re-seed a built-in an operator has changed.
- The lead panel shows a "Website enquiry" section with the answers.

**Tests:** owner comes from the site setting; the other operator can't see the
enquiry (404 / absent from every list); a suppressed address is recorded but
its email lead is suppressed; a local UK number matches the same number
already held in international form.

### 3 · A booking updates the lead

- On *created*: find the enquiry by attendee email and site (create the lead
  if the booking arrives with no enquiry). Set the stage to `booked`, channel
  `website`, with the meeting time, **through `set_business_pipeline`
  directly** — not through the pipeline route, which calls the Zoho booking
  code.
- **Time.** Cal.com sends an instant. `businesses.next_action_at` holds
  wall-clock time as the operator reads it (Handover §5c). Convert once, on
  arrival, into the host's time zone from the payload, and store the original
  instant as well.
- The `meetings` row records where it came from (`source = 'cal'`), Cal.com's
  booking id, the join link, and its state (booked, moved, cancelled).
- **A booking stops the follow-ups:** any queued email enrolment for that
  business ends, and a WhatsApp lead that hasn't been messaged yet is paused,
  so it leaves To do. (Today a stage change stops WhatsApp follow-ups only
  for leads already messaged, and never stops email.) `log_call` already has
  the "stop email for this business" logic inline; pull it into one function
  and use it from both places. Apply it whenever the stage becomes `booked`,
  from any channel.
- On *rescheduled*: update the time and mark it moved. On *cancelled*: mark
  the meeting cancelled and show that on the lead. **Do not change the stage
  on cancel** — the operator decides what happens next.

**Tests:** built on the captured fixtures. Created → stage, time, meeting row,
email enrolment ended, unsent WhatsApp lead paused. Repeat delivery changes
nothing. Reschedule and cancel. No `wa_log` row is ever written by any of
this.

### 4 · Following up people who didn't book

- A job in the scheduler's pass looks for enquiries older than the WhatsApp
  wait, with no booking and no follow-up queued yet. For each: if there is a
  phone number and the owner has chosen a WhatsApp campaign, add the lead to
  it (ready to send, manual as always); if the address isn't suppressed and
  the owner has chosen an email campaign, enrol it with its first send not
  before arrival plus the email wait. The enrol functions set the first send
  to "now" today; give them an optional "not before".
- Then mark the enquiry's follow-up as queued, so it happens once.
- The job is wrapped like every other in Plan 1 package 4: it can't stop the
  loop.

**Tests:** with an injected clock — nothing before the wait; both follow-ups
after it; only email when there's no number; neither when the lead booked in
between; nothing for a suppressed address's email; run twice, queued once.

### 5 · One Meetings page

- A new sidebar item, **Meetings**, for every operator: all their booked
  meetings from every channel and the website. Columns: when, business,
  where it came from ("from WhatsApp", "from a call", "booked on hexiv.co"),
  state (moved, cancelled), and a Join link. Grouped as Needs an outcome,
  Today, Tomorrow, Later, in **one** table so columns line up.
- On a phone each meeting is two or three lines with the time first, using
  the existing phone-row pattern (`m-card`).
- The sidebar count comes from an unfiltered query, loaded with the page, not
  when the section is first opened.
- **Remove the Booked tab from WhatsApp.** This closes audit findings F-19,
  F-23 and F-24.
- The Dashboard gets a "From the website" block: recent enquiries and where
  each stands.
- The lead's meeting box is reduced to: when, where it came from, Join, and a
  line saying that moving or cancelling happens on Cal.com.
- **Booking from a call or WhatsApp:** the lead panel gets "Copy booking
  link". The link is the Cal.com page prefilled with the lead's name and
  email and carrying a signed reference to the lead in `metadata`, so the
  booking that comes back is matched to exactly that lead. Setting the stage
  to booked by hand, with a time, still works for meetings arranged some
  other way; it no longer touches any calendar.

The mockup shows the intended look
([Meetings](https://claude.ai/artifact/8FU8mQbqpXGhA7bZ1LTesk#artboard-Meetings),
source in `docs/audits/mockup-2026-11-21/`). Build this package in today's
styling; the redesign restyles it later.

### 6 · The other operator's copy *(crosses the wall — decided)*

When an enquiry or booking is filed for one operator and the other holds a
matching business (`find_cross_owner_matches`), set that other copy to a new
built-in terminal stage, `booked_elsewhere`, labelled "Booked via the
website", and stop its follow-ups as in package 3.

It must reveal nothing else: no time, no answers, no contact details, not who
holds it. Nothing is merged. Record this in Handover §3 beside the other
deliberate exceptions.

**Tests:** the other operator sees the stage and nothing more through any
route; their follow-ups stop; the owner of the enquiry sees nothing about the
other copy.

### 7 · Telling the team

Today EmailJS emails the team from the browser. Before that is removed,
ShoutReach has to do it: an optional notification address per user, and one
plain email to the lead's owner when an enquiry arrives and when a booking is
made, sent through an existing sending account. Subject and business name
only in the email; the details are in ShoutReach.

### 8 · Remove ShoutReach's Zoho booking *(last)*

Remove the code that talks to Zoho: `zoho_calendar.py`, the `/api/zoho/*` and
`/zoho/*` routes, the meeting sync/invite/cancel routes, `_book_meeting` and
its two callers, the Zoho cards in Settings, the booking dialog's calendar
fields, and `tests/test_zoho_calendar.py`. Leave the `meetings` and
`zoho_connections` tables in place (dropping tables on the live database
isn't worth the risk); empty `zoho_connections` with a guarded one-shot, since
it can hold tokens. Mark Handover §5f retired and remove its to-do list from
§9.

This also closes the Zoho parts of audit findings B-7, B-8, B-13 and B-17.

## The websites (two other repos, separate sessions)

Each site (`HexivUK`, `hexivsite`) gets the same small change. Neither has a
server route yet; both are ordinary Next.js apps, so they can have one.

- A server route that the booking form posts to at the moment it reaches the
  calendar — the same moment it calls EmailJS today. The route checks the
  shape, then forwards to ShoutReach's contract A with the website key from a
  **server-only** environment variable. The key must never be in a
  `NEXT_PUBLIC_` variable or reach the browser.
- The form must not wait on it: if ShoutReach is down, the calendar still
  appears.
- Send `answers` as English labels. The UK site already has labels; the .co
  site sends option ids today and must map them using its English content
  file. Send `site`, and `country: "GB"` from the UK site.
- Keep EmailJS until package 7 has been seen working for a week. Removing it
  is a separate change.
- Check the line under the details step says the visitor may be followed up
  by email or WhatsApp. If it doesn't, propose wording; the operator approves
  copy changes.

## For the operator

- After package 1 is deployed: create the webhook in Cal.com (Settings →
  Developer → Webhooks) with the address and secret from Team setup, for
  booking created, rescheduled and cancelled.
- Set the website key as an environment variable on each site's hosting.
- Choose, in My settings, the email campaign and WhatsApp campaign for
  non-bookers. Until you do, enquiries are recorded but not followed up.

## Hand-back

Per package: the commit, tests added, and what you checked by hand. Include
the scrubbed Cal.com payloads you captured and anything in them that differed
from the handoff's description.
