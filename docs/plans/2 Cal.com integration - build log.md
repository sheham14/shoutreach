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
| 1 | Settings and the two doors | not started | |
| 2 | An enquiry becomes a lead | not started | |
| 3 | A booking updates the lead | **blocked: needs real Cal.com payloads** | |
| 4 | Following up people who didn't book | not started | |
| 5 | One Meetings page | not started | |
| 6 | The other operator's copy | not started | |
| 7 | Telling the team | not started | |
| 8 | Remove ShoutReach's Zoho booking | not started | |

## Log
