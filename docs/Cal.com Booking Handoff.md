# Cal.com booking: handoff for Phase 2 (Shoutreach)

**Written:** 2026-11-21, from the session that built the website side.
**Status:** Phase 1 (websites) is live. Phase 2 (getting bookings into
ShoutReach) is not started. Nothing in ShoutReach has changed yet.

## What's live (Phase 1)

Both public sites book the same Cal.com event, **`hexiv/free-strategy-call`**
("Free strategy call", 30 min, location Cal Video, description "Online call").

| Site | Repo | Booking component |
|---|---|---|
| hexiv.co.uk (general SMBs) | `C:\Users\sheha\HexivUK`, seum-hex/HexivUK | `app/components/booking/BookingFlow.tsx` |
| hexiv.co (clinics, EN + AR) | `C:\Users\sheha\hexivsite`, seum-hex/HexivSite | `app/components/BookingWizard.tsx` |

The flow on both: 5 tap questions → name + email (required), business/clinic
name, phone/WhatsApp, website/Instagram (optional) → Cal.com inline calendar,
prefilled.

When a visitor reaches the calendar, **EmailJS** emails the team their answers
(a lead who doesn't book is still caught). Cal.com then sends its own booking
emails. EmailJS is meant to be removed once ShoutReach receives leads.

## Calendar setup (don't redo this)

- **Calendar:** Cal.com is connected to **Zoho Calendar over CalDAV**.
  - URL: `https://calendar.zoho.sa/.well-known/caldav`
  - Username: the full work email.
  - Password: a Zoho app password.
- **Why not Cal.com's Zoho app:** it fails with "invalid client id", because Cal.com's Zoho OAuth client isn't enabled for Zoho's **Saudi** data centre.
- **Shared calendars over CalDAV are read-only.** When website leads move to Anas, connect the shared calendar with **his** login.
- **No personal accounts:** never connect personal Google accounts. Clients must not see personal Gmail addresses.
- **Test booking:** done and passed. Clients see only the work identity.
- **Cal.com plan:** Free.
  - **Webhooks on Free are unconfirmed.** Check Cal.com → Settings → Developer → Webhooks first; that decides the design below.
- **Language:** Cal.com's booker language follows the visitor's browser (Accept-Language). It can't be forced from the embed.

## What the sites send to Cal.com

Every booking carries:

- **`notes`:** a plain-text English summary of every answer. It shows in the
  booking and the Zoho event, and the client can see and edit it.
- **`metadata[...]`:** hidden from Cal.com's UI, but included in webhook
  payloads as `payload.metadata`. Keys:
  - **Both sites:** `site` (`hexiv.co.uk` or `hexiv.co`), `website`, `phone`.
  - **UK:** `businessType`, `challenge`, `source`, `volume`, `ads`, `businessName`.
  - **.co:** `practice`, `bottleneck`, `source`, `volume`, `ads`, `clinic`, `locale` (`en` / `ar`).

Question answers are option **labels** on UK, and option **ids** on .co
(e.g. `under-20`, `in-house`). Labels for the .co ids are in `content.en.ts`
there.

> The `site` key was `source` until fix PRs HexivUK#2 / HexivSite#2 (it was
> overwritten by the `source` question). Check those are merged before relying
> on `metadata.site`.

## Phase 2: what was agreed

1. **Cal.com is the only thing that creates calendar events.**
   - ShoutReach records website bookings but must **not** call Zoho for them, or the client gets a second event and invite.
   - Its own Zoho booking (§5f in Handover.md, never connected) may be dropped or reworked. The operator said that calendar work is recent and may change.
   - Bookings from calls or WhatsApp can go through the Cal.com link too.
2. **A new key-authenticated inbound endpoint**, following the scraper-worker
   `X-API-Key` pattern (`_has_valid_worker_key`, `_is_worker_route`).
   - Login and CSRF don't apply to it.
   - It receives Cal.com webhooks (`BOOKING_CREATED`, `BOOKING_RESCHEDULED`, `BOOKING_CANCELLED`). Verify `X-Cal-Signature-256`, which is HMAC-SHA256 of the body with the webhook secret.
   - It creates or updates the business and contact, sets the pipeline stage to `booked`, and records the meeting time.
3. **Lead capture before booking.**
   - The websites should also post the lead to ShoutReach when it reaches the calendar, so non-bookers can get an email follow-up from ShoutReach's campaigns.
   - That needs a small server route on each site; neither site has one yet. That route holds the ShoutReach key, which must never reach the browser.
   - A matching booking webhook then moves the same lead (matched by email) to booked.
4. **Who owns website leads.**
   - ShoutReach walls leads per operator. Website leads go to **sheham** while testing, then **Anas**.
   - Make it a setting, not code. A shared list was considered and rejected for now, because it would mean changing the ownership model (§3).
5. **If webhooks aren't on the free plan**, step 3 still gets every lead into ShoutReach. Only the "booked" update needs another route, or the Teams plan ($12/user/month).

## Open items

- Confirm webhooks on Cal.com Free (see above).
- Native-speaker review of the new Arabic booking copy on hexiv.co.
- WhatsApp confirmations are deferred until there's a Meta Business account.
  ShoutReach's rule still applies: never automate WhatsApp sending.
