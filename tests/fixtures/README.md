# Test fixtures

`cal_*.json` are real Cal.com webhook bodies, captured on 2026-11-22 from the
live `/api/inbound/cal` route (a test booking made through hexiv.co.uk's
booking form, then rescheduled), with personal details replaced:

| Real | In the fixture |
|---|---|
| attendee name and email | Sam Booker, sam.booker@example.com |
| organizer email | host@example.com |
| business, website, phone (metadata and notes) | Booker Dental, bookerdental.co.uk, 07700900123 |
| the CalDAV calendar id | REDACTED |

Everything else — field names, nesting, uids, ids, times, time zones, the
Cal Video links — is exactly as Cal.com sent it. Build and test the parser
against these files, never against field names from memory.
