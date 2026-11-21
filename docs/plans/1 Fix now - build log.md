# Plan 1 · Fix now — build log

A running log of the Plan 1 build, kept so a later session can pick up where
this one stopped. Newest entry last. Branch: `claude/vibrant-archimedes-i2ilk9`
(the session's branch, used instead of the plan's `plan-1-fix-now` with the
operator's go-ahead). Nothing is pushed to `master`.

## How to resume

1. `git fetch origin claude/vibrant-archimedes-i2ilk9 && git switch claude/vibrant-archimedes-i2ilk9`
2. Read the last entry below: it names the package in progress and what's left.
3. Environment set-up that was needed in a fresh container:
   `pip install --ignore-installed blinker -r requirements.txt Pillow beautifulsoup4 playwright playwright-stealth`
   (Debian's blinker blocks a plain install; `test_extraction.py` and
   `test_resilience.py` import the scraper, which needs playwright.)

## Status

| Package | Finding | State | Commit |
|---|---|---|---|
| 1 Opt-outs permanent | B-1 | not started | |
| 2 Activity log admin-only | B-2 | not started | |
| 3 Sessions | B-3 | not started | |
| 4 Sender resilience | B-6, B-23 | not started | |
| 5 Calling → Reopen | B-22 | not started | |
| 6 Confirm before sending | B-9 | not started | |
| 7 Visible faults | F-20, F-21/33, F-22, F-25, F-30, F-9, B-10, B-14, B-15 | not started | |

## Log

### 2026-11-21 — start

- Baseline: all 25 test files pass (after installing the worker's packages).
- No `./outreach.db` exists in the checkout; tests use their own temp DBs.
