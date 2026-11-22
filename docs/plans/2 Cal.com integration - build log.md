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
| 2 | An enquiry becomes a lead | not started | |
| 3 | A booking updates the lead | **blocked: needs real Cal.com payloads** | |
| 4 | Following up people who didn't book | not started | |
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
