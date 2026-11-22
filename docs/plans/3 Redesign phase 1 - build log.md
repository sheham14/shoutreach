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
6. **On the operator's Windows machine** (sessions from 2026-11-22 evening):
   Python 3.13, Playwright 1.59.0 with its own Chromium (`chromium-1217`
   under `%LOCALAPPDATA%\ms-playwright`) already work. Don't install 1.63.0
   there; that step is for the container only.

## Branch base

`plan-3-redesign-phase-1` starts at `52b0eb9` (master, "Docs after Plan 2"
merged; `docs/plans/README.md` has the Status column).

## Status

| Package | What | State | Commit |
|---|---|---|---|
| 0 | Walk-throughs point at Meetings; "before" shots | done | `0bae7f7` |
| 1 | Tokens, fonts, sign-in page | done | `204b017` (code), audit marked after |
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

### Package 0 — done (`0bae7f7`)

- `wa-booked*` steps in plan-desktop-1/-2 and plan-phone-1 now open the
  Meetings page and its first lead. The phone plan gained a no-shot step that
  returns to WhatsApp before `wa-campaigns`.
- `shoot.py` now also reports failed requests, any 4xx from the local server
  and any request leaving the machine; it prints problems from sign-in.
- Before: 0 JS errors; 6 problems per walk-through, all Google Fonts requests.
- `keep_shots.py <stage>` saves the 12 hand-back screens as WebP under
  `docs/plans/plan-3-shots/<stage>/` (`0-before`, `1-tokens`, ...).

### Package 1 — code done and committed; paperwork left

Done (suite 32/32, walk-throughs 0 problems at both widths, no request leaves
the machine, none of `#00e87a #4da6ff #a78bfa #080c10` in main.css, no `rgba(`
outside token definitions):

- `:root` replaced with the plan's tokens, plus tints (`--accent-tint/-edge`,
  `--success|warning|danger-soft/-edge`, `--neutral-soft`), `--scrim`,
  `--shadow`, type `--fs-xs..xl` (12,13,14,16,20,28; `--fs-base` is 15 on a
  phone), space `--sp-1..6`, radius `--r-control/card/round`, layers
  `--z-sticky/sheet/fullscreen/menu/window/toast` (the plan names five but
  lists six; six defined; phone drawer + dimmer use the sheet layer). All 13
  z-index numbers replaced. Every font size in main.css is on the scale
  (except the 36px emoji in `.empty-state .icon`, which package 3 removes).
- Renames done everywhere (css, js, templates). Plan counts were within 1-5
  of the code's (e.g. `--bg3` 17 not 20 outside css) — counting differences,
  not a disagreement.
- **Green split.** Accent: primary button, active sidebar item, active tab,
  active chip, `.seg`, `.choice`, focus borders, placeholder hover, send-day
  toggles (a choice), selected call outcome with no state tone. Success:
  badge/pill green, stat/mini-stat green values, progress bars, `.fill`,
  `.meeting-box`, toast ok, sent-today number, scores, "Strengths",
  sender-status ok, test results, worker-connected dot, scrape progress bar,
  Replies headings/cells. Not obvious: `.step-num` became accent-soft/accent
  (a step number isn't success); `.meeting-box` success (booked).
- Blue → accent for links and the bulk bar; `.badge-blue`, `.pill.blue`,
  `.badge-purple`, `.pill.purple`, `.stat-value.blue` restyled neutral (class
  names kept for package 2). `enrollBadge` replied → `badge-green`; the
  lead panel's "Email" channel pill no longer green. Channel classes plain text.
- Kept on purpose: `templates/modals/preview.html` white background (an email
  preview as the recipient sees it).
- Fonts: `static/fonts/InterVariable.woff2` (+`Inter-LICENSE.txt`),
  `JetBrainsMono-Regular.woff2` (+`JetBrainsMono-OFL.txt`); `@font-face` in
  main.css; Google Fonts links removed; Inter preloaded.
- Coding font now only on code: 60 `class="mono"` sites (phones, emails,
  dates, counts) became `class="tnum"` (Inter, tabular). Kept mono:
  `{{placeholders}}`, `{fill-ins}`, keys, URLs to paste, terminal commands,
  JSON paste box, raw database viewer, variable-key inputs.
- Labels: th, form labels, stat labels, nav groups, fold titles, field
  labels, mini-stat/todo labels, audit keys — no capitals, no mono, no
  letter-spacing. 35 Title Case `<th>`/`<label>` texts sentence-cased (no
  test reads them).
- Sign-in page: links main.css, own stylesheet gone; new classes
  `.login-page/.login-box/.login-card/.login-submit/.error-box`; labels tied
  to inputs. ◈ "Powered by Hexiv" and the 📣 favicon stay until package 3.
- `app.py`: `_PUBLIC_STATIC_PREFIXES` + `_is_public_static()` in
  `_require_login` (refuses any `..`/`.` segment, backslash, NUL; then
  normpath prefix check). Test: `tests/test_security.py` section 8 — the
  three folders 200 signed out; `/static/js/app.js` and 9 climbing paths 302;
  all 164 /api route+method pairs answer identically with the rule switched
  off and all refuse a signed-out caller. Proven able to fail: a prefix-only
  rule serves js/app.js via `/static/css/../js/app.js` (7 FAILs).
- Contrast (computed): text-2 8.94/8.32/7.55 on bg/surface/raised; text-3
  4.91/4.57 on bg/surface (used only there: logo-mark, nav-group, quiet
  todo number); line-strong 3.30 vs raised, 3.64 vs surface; on-accent 7.29.

**Left for package 1 before moving on (next session):**
1. Mark in the audit, in place (`✅ RESOLVED`, commit, how checked): F-3,
   F-4, F-7, F-29, F-38; note F-5 partly (type scale and layers; spacing and
   radius with package 2). Update the audit's status block and Handover §8
   (test_security section 8) / §10 if it lists static files.
2. Commit that as part of package 1 (amend not needed: a follow-up commit
   "Package 1: audit marked" is fine).
3. Spacing/radius tokens are defined but not yet applied; apply them as
   controls are restyled in package 2.

Then packages 2, 3, 4 in order, as the plan says. After each: suite,
`shoot.py` for the three plans, `keep_shots.py <n-name>`, log entry, commit,
push. Hand-back at the end per the plan's "Hand-back" section.

### Package 1 — paperwork done (Windows session, 2026-11-22)

- Re-checked before marking: no old colour or old token name anywhere in css,
  scripts or templates; no `rgba(` outside token definitions; no Google Fonts
  or Space Grotesk; contrast recomputed from the token values (same figures
  as above).
- Audit: F-3, F-4, F-7, F-29, F-38 marked `✅ RESOLVED` in place; F-5 marked
  partly done (tokens exist; type sizes and layers applied; space and radius
  with package 2). Status block: 15 of 43 front-end findings.
- Handover §6: the deploy check now says only `/static/js/` needs a login.
  §8: a paragraph on `tests/test_security.py` section 8.
