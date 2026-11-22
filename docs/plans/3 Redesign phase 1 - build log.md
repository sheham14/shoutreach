# Plan 3 · Redesign phase 1 — build log

Newest entry last. If you are a new session picking this up, read "How to
resume", then the **last** log entry: it names what is in progress and what
is left.

## How to resume

1. `git fetch origin plan-3-redesign-phase-1 && git switch plan-3-redesign-phase-1`
2. Read the last entry under "Log" below.
3. Environment set-up in a fresh container:
   - `pip install --ignore-installed blinker -r requirements.txt Pillow beautifulsoup4 playwright-stealth`
   - **Then** `pip install playwright==1.63.0`. `playwright-stealth` pulls in a
     newer Playwright whose browser build isn't in the container; 1.63.0
     matches the pre-installed Chromium (`/opt/pw-browsers/chromium-1243`).
     Never run `playwright install`.
4. Walk-throughs (tools/audit/README.md):
   - `python tools/audit/serve.py seed` then
     `python tools/audit/serve.py` in the background (port 5055; it never
     touches `./outreach.db`).
   - `python tools/audit/shoot.py tools/audit/plan-desktop-1.json` (and
     `plan-phone-1.json`, `plan-desktop-2.json`). Shots land in
     `<tmp>/shoutreach-audit/shots/`; they are copied into
     `docs/plans/plan-3-shots/<stage>/` for the hand-back.
5. Suite: `for f in tests/test_*.py; do python "$f" >/dev/null 2>&1 || echo "FAIL $f"; done`
   (each test points DB_PATH at its own scratch file).

## Branch base

`plan-3-redesign-phase-1` starts at `52b0eb9` (master, "Docs after Plan 2"
merged; `docs/plans/README.md` has the Status column).

## Status

| Package | What | State | Commit |
|---|---|---|---|
| 0 | Walk-throughs point at Meetings; "before" shots | in progress | |
| 1 | Tokens, fonts, sign-in page | not started | |
| 2 | Controls | not started | |
| 3 | Icons and the mark | not started | |
| 4 | Dialogs, dates, wording | not started | |

## Log

### 2026-11-22 — set-up

- Fonts reachable: Inter 4.1 (`web/InterVariable.woff2`, `LICENSE.txt`) from
  github.com/rsms/inter releases, JetBrains Mono 2.304
  (`fonts/webfonts/JetBrainsMono-Regular.woff2`, `OFL.txt`) from
  github.com/JetBrains/JetBrainsMono releases. JetBrains Mono is the coding
  font already named by `--mono`, so it stays.
