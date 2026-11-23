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
| 2 | Controls | done | `6033e43` (+ `7012a05` audit) |
| 3 | Icons and the mark | done | `fc2c87e` |
| 4 | Dialogs, dates, wording | done | `8a2833c` |

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

### Package 2 — controls (Windows session, 2026-11-22)

Restyled by class; no class renamed. Two classes **added**: `.btn-icon` (a
quiet one-symbol button) and `.seg` on the three chip rows that switch lists
(the unused `.seg` style was rewritten as their base), plus `.outcome-btn`
for Calling's outcomes.

- **Buttons.** Primary: accent fill, `--on-accent`. Secondary (`.btn-ghost`,
  and `.btn` with no kind): `--raised`, main text, no border. Destructive
  (`.btn-danger`): danger text on the secondary fill; a solid danger button
  only as `#choose-confirm` (the dialog's confirm, text `--bg`, 5.9:1).
  `.btn-amber` (Pause, Resume) became secondary: amber is a state, not a
  button colour. Disabled buttons fade (F-31's lit "Remove selected").
- **Repeated primaries made secondary:** WhatsApp To do's "WhatsApp" on every
  row, Email To do's "Done" on every row, Calling → Campaigns "Work it" per
  campaign, Email → Campaigns "Activate" per campaign (the one inside a
  campaign stays primary), Contacts → Recently deleted "Restore" per group,
  Get leads "Add all to…" / "Download CSV" per list.
- **Per-row ✕ boxes → `.btn-icon`** with `title` and `aria-label`: campaign
  delete, enrolled contact remove, call outcome delete, script line remove,
  email version/variable remove, user delete, account delete. Buttons with a
  word ("Delete", "Take off WhatsApp", "Delete forever") stay `.btn-danger`.
- **Fields.** One element-level rule gives every text box, dropdown and text
  area (classed or not) `--raised`, a `--line-strong` edge, radius 6. Every
  `outline: none` removed. **Focus:** `:focus-visible` 2 px accent ring on
  everything; fields show it on any focus. Checked by pressing Tab through
  WhatsApp To do: dropdown, primary, tabs, chips, row buttons all show
  `2px solid rgb(60,193,208)`. (`plan-desktop-2`'s `focus-ring` probe calls
  `.focus()` from script after mouse clicks, which `:focus-visible` ignores by
  design, so it still prints "none"; not a fault.)
- **Tick boxes and radios:** `accent-color: var(--accent)`; `color-scheme:
  dark` was already on `body`.
- **Tags:** `.badge` and `.pill` share one rule: dot + word, success,
  warning, danger or neutral; `.pill.dashed` is an outline with no dot.
- **Segmented control:** Ready / Follow-up due, the Calling buckets, the
  sent log's Today / 7 days / Everything.
- **Tabs:** accent underline; counts are plain `--text-2` numbers (`.hot`
  ones in warning), no longer little pills.
- **Cards:** `--surface`, radius 10, no border; `.card .card` has no fill.
  Stat tiles and Dashboard to-do tiles lost their borders (hover is a
  background step). Step cards in the step editor lost their border.
- **Tables:** row hover `--raised`, current row `--accent-soft` (already).
- **Windows, menus, toast:** radius 10, `--shadow`, no border; row menus on
  `--raised`.
- **Spacing and radius tokens applied** (the package 1 leftover): every
  padding, margin and gap in `main.css` is a `--sp-*` token (ties rounded
  up; 48 and 64 written as sums of tokens), every radius `--r-control`,
  `--r-card` or `--r-round`. Only 1–2 px hairlines and layout widths (the
  220 px sidebar) are left as numbers. Inline sizes in scripts and templates
  are untouched (later phases).
- **Phone tap targets:** buttons, chips, tabs, dropdowns, the row menu
  button, outcome buttons and the menu button are min 44 px; text links in a
  lead panel or sheet, "N marked not on WhatsApp", "sent today" and
  Calling's script folds too. `measure.py phone`: **0 under 44 px** on
  wa-todo, wa-todo-lead, wa-todo-did-it-send and calling-lead (were 2, 4,
  1, 8 after the first pass). Other screens still list 20 px list tick boxes
  and placeholder buttons (35 px); not in the plan's list.
- **Outcome buttons:** inline style moved to `.outcome-btn` (+ `tone-*`,
  `.on`). Unpicked: secondary fill, main text (F-27's grey). Picked: the tone's
  tint and edge (success, danger, accent, or neutral).
- **Motion:** buttons, tabs, chips, rows, menus 120–160 ms;
  `prefers-reduced-motion` turns all transitions and animations off. The
  pulsing dot and scheduler pill were already gone (package 1). The drawer
  (200 ms) and FAQ open/close are not hover/press motion; left.
- **Tooling:** `keep_shots.py` now takes the newest match per step (stale
  shots from older plan versions had the same step names under other
  numbers and were being picked).
- Walk-throughs: desktop-1, desktop-2, phone-1 all `TOTAL PROBLEMS: 0`.
  Shots in `docs/plans/plan-3-shots/2-controls/`.

**Left, and why (later phases):**
- The sidebar's "Check for email replies & send" is a button styled entirely
  by an inline `style` and `onmouseover` (tokens only, so nothing defeats a
  token); it keeps its own outline look until the sidebar is rebuilt.
- Sidebar items and list rows can't take keyboard focus (no `href` /
  `tabindex`); making them reachable is behaviour (F-14, F-40), not looks.
- Settings' Database tabs mark the open table with `btn-primary` (teal =
  selected); left.

**Package 2 committed** as `6033e43`; the whole suite (32 files) passed
against that exact commit in a separate worktree, then it was pushed. Audit
marks in `7012a05`: F-6, F-35 resolved; F-8, F-11, F-27, F-31, F-39, F-40
partly done.

### Package 3 — icons and the mark (Windows session, 2026-11-22)

- **Source:** `lucide-static` 0.460.0 from the npm registry (tarball unpacked
  in a scratch folder; only its SVG files were read). ISC licence copied to
  `static/js/Lucide-LICENSE.txt`.
- **One place:** `static/js/icons.js` holds the path data for 41 icons as
  JSON between `/* ICONS-JSON-START */` and `/* ICONS-JSON-END */`, and
  `icon(name, {size, cls, label})` returns an inline SVG (aria-hidden unless
  given a label). `app.py` `_load_icons()` parses that block at startup into
  the Jinja global `ICONS`; `templates/macros/icons.html` has the matching
  `icon(name, size, cls, label)` macro. No build step, no network.
- **Replaced:** sidebar items (◉ ⊕ 📅 ✉ ☎ 💬 ⊗ ⚙ ?), the run-now ⟳ and sign
  out ⎋, the ☰ menu button, every window's ✕ close, the 📅 in the lead's
  meeting box, Dashboard card titles, and every symbol in a button label
  (⬆ ⬇ ⏹ ▶ ⏸ ✓ ✕ ⚙ ⊞ ↻ ✎ ⋯ ⟳ ↩ ⤓ 🧹 ⛶ ⚡ ✨ ← → ↗ ↑ ↓), sort arrows (▲ ▼ ⇅),
  the info dot ⓘ, ⚠ on warnings, ✓/✗ in audit results and the outcomes
  table, the empty state's ✉. Icon-only buttons got `aria-label`s.
  Text that used `textContent` to show a symbol now uses `innerHTML` with
  escaped values (run-now, Remove selected, Run checks again, SMTP test,
  scrape heartbeat, the Dashboard sender warning).
- **Help page too:** its section emoji, ▼ chevrons, ⚠ and the symbols it
  used to name buttons ("⚙ Settings", "▶ Activate") are icons. The page is
  one `{% raw %}` block (it documents `{{placeholders}}`), so each icon steps
  out with `{% endraw %}{{ icon(...) }}{% raw %}`.
- **Toasts:** the trailing ✓ / ▶ / ⏸ decorations were dropped from 21
  messages ("Saved ✓" → "Saved").
- **The mark:** `hexiv-mark.svg` beside "ShoutReach" in the sidebar (28 px)
  and on the sign-in page (36 px), and as the tab icon on both pages
  (`<link rel="icon" type="image/svg+xml">`). "Powered by Hexiv" and the
  📣 favicon removed; `.logo-mark` style deleted.
- **Left on purpose:** Activity log symbols (stored by `db.add_log`); ★ in
  ratings ("4.5★", data); → inside prose and text links ("See them in
  Contacts →"); the ← inside a DNS code sample on Help; ≤ in a hint; the
  CSS-drawn ▸ on the lead panel's folds and ↓ between email steps (generated
  content, not in the plan's list).
- **New test:** `tests/test_icons.py` — the set loads from icons.js and
  matches; every name used in a template or script exists (both arms of a
  `?:` too) and none in the set is unused; no icon call printed as text;
  the mark is the tab icon and sits by the name on both pages; the sidebar
  has 11 icons and no symbol; icons.js loads after utils.js; the licence
  ships. Proven able to fail: removing `mail` from the set fails "none
  missing"; un-wrapping Help's icons from the raw block fails "no icon call
  printed as text".
- **Two faults found on the way and fixed before commit:** a template
  substitution left `{{ icon(\'x\', 20) }}` (a backslash) in 14 close
  buttons, which broke the main page (test_icons caught it); a single-quoted
  JS string with `${icon()}` in whatsapp.js stopped the file loading (the
  walk-throughs caught it: 13 problems). Every script is now checked with
  `node --check` after edits.
- Walk-throughs: desktop-1, desktop-2, phone-1 `TOTAL PROBLEMS: 0`. Shots in
  `docs/plans/plan-3-shots/3-icons/`.
- Audit: F-1, F-2 resolved; F-37 partly (confirmations and Title Case are
  package 4); F-5 note: every font size in main.css is now on the scale.
  Handover §8 (test_icons, 33 files) and §10 (where icons live).

**Package 3 committed** as `fc2c87e`; suite run against that commit in a
separate worktree before pushing.

### Package 4 — dialogs, dates, wording (Windows session, 2026-11-22)

- **Dialogs.** `confirmDialog({title, text | html, confirm, danger})` in
  `tables.js` wraps `chooseDialog` (no new dialog): plain `text` is escaped,
  a blank line starts a paragraph. All 15 `confirm()` calls and the 1
  `alert()` replaced: Calling's stop-email outcome, remove outcome; delete
  campaign, remove enrolled (one / selected), the step editor's missing
  variables, delete step; take an address out, bulk delete addresses, delete
  an address; clear logs; delete user, delete account, rotate worker key;
  channel conflicts; and the cross-operator overlap notice (was `alert()`;
  now `async` and awaited in both scraper.js imports, so a dialog after it
  can't replace it). Destructive ones use `danger: true` (solid red confirm,
  text `--bg`); the confirm button says what it does ("Delete campaign",
  "Rotate key"). Conflict and overlap lists are `<ul>`s with every name
  escaped. Wording of three titles tightened (the delete-account one now says
  "sending account"; clear logs is "Clear the whole activity log?"); two got
  a second line saying what stays ("Anything already sent stays in their
  history.", the step's content).
- **Checked on the harness** with a script that opens 11 of the 16 directly,
  reads the dialog and cancels: all 11 open as `#modal-choose`, 9 with the
  red confirm, no native dialog fired, no page error; a business name with
  `<b>` shows literally. The other five need state set up first (remove
  selected enrolled, bulk delete addresses, delete one address, the step
  editor's missing variables, Calling's stop-email outcome): read, and
  parsed by `node --check`, not driven.
- **Dates.** Left outside `fmtWhen` after Plan 1: the Activity log
  (`created_at` chopped to 19 characters, in UTC) — now "Today 6:16 pm";
  the overlap notice's "added 2026-11-20"; the audit's certificate expiry,
  archive first-seen and domain registered/expires dates (plain days,
  `'wall'`). Still raw on purpose: CSV export values and the date picker's
  value (they must be machine-readable), and `utcNow()`'s stored timestamp.
- **Wording.** Button labels and window titles in sentence case: Create
  user, Save password, Change password, Save account, Add an email account,
  Edit account, Save contact, Add contact, Campaign settings, Add variable,
  Enroll contacts / selected / all active, New campaign, Create campaign,
  Email preview, Edit step / Add step N, Add variant, Save step, AI review,
  Remove selected, Assign sending accounts, Add account, Save rules, Add
  user, Apply rewrite. Proper names kept (WhatsApp, Google Maps, CSV,
  Excel). The Help page's prose still names some of them in Title Case
  ("Remove Selected"); left, it's content.
- Walk-throughs: desktop-1, desktop-2, phone-1 `TOTAL PROBLEMS: 0`. Shots in
  `docs/plans/plan-3-shots/4-dialogs/` (the screens look as in package 3;
  the changes are in dialogs and labels).
- Audit: F-37 resolved; F-31's Title Case part done. Handover §10:
  `confirmDialog`.

**Package 4 committed** as `8a2833c`; the suite (33 files) passed against that
commit in a separate worktree before pushing.

## Hand-back (Plan 3, 2026-11-22)

All four packages are built on `plan-3-redesign-phase-1`, one code commit per
package. Nothing is merged or deployed. `db.py`, `scheduler.py` and
`sender.py` are untouched (`git diff 52b0eb9 -- db.py scheduler.py sender.py`
is empty).

Screenshots: `docs/plans/plan-3-shots/<stage>/`, the same twelve screens
each time — sign-in, Dashboard, WhatsApp To do, a lead panel, Calling To do,
Settings, at 1440×900 (`desktop-*`) and 390×844 (`phone-*`). The stages are
`0-before`, `1-tokens`, `2-controls`, `3-icons` and `4-dialogs`. `0-before`
and `1-tokens` were taken in a Linux container; 2 to 4 on Windows, so font
rendering differs slightly between them. The harness's data moves with the
clock, so counts differ between stages.

### Package 1 · Tokens, fonts, sign-in page — `204b017` (+ `8c7c1e3` docs)

- **Before / after:** `0-before/*` → `1-tokens/*`.
- **Checked by hand:** no old colour or old token name in css, scripts or
  templates (grep); no `rgba(` outside a token definition; no Google Fonts
  request in any walk-through (`shoot.py` reports requests leaving the
  machine); contrast computed from the token values (text-2 8.94 / 8.32 /
  7.55 on bg / surface / raised; text-3 4.91 / 4.57 on bg / surface only;
  line-strong 3.30 against raised, 3.64 against surface); signed out,
  `main.css`, the mark and the fonts answer 200 and `app.js` doesn't
  (`tests/test_security.py` section 8, which fails against a prefix-only
  rule).
- **Done differently:** the plan names five layers and lists six; six are
  defined. Its rename counts were 1–5 off the code's (counting, not a
  disagreement).
- **`--green`, where it wasn't obvious:** `.step-num` became accent (a step
  number isn't a success); `.meeting-box` became success (booked). The full
  list of accent and success uses is in the package 1 log entry above.

### Package 2 · Controls — `6033e43` (+ `7012a05` audit)

- **Before / after:** `1-tokens/*` → `2-controls/*`.
- **Checked by hand:** Tab pressed through WhatsApp To do shows a 2 px teal
  ring on every stop; `measure.py phone` shows nothing under 44 px on
  WhatsApp To do, the open lead, "Did it send?" and the Calling lead; the
  outcome buttons, segmented chips and quiet ✕ looked at in the shots.
- **Done differently:** two classes **added**, none renamed: `.btn-icon`
  (the quiet one-symbol button) and `.seg` on the three chip rows that
  switch lists, plus `.outcome-btn` for the call outcomes the plan asked to
  move out of inline styles. `.btn-amber` (Pause, Resume) is secondary,
  because amber is a state, never a button colour (plan §Decided). Tab
  counts became plain numbers. Every padding, margin, gap and radius in
  `main.css` is on a token (48 and 64 are written as sums of tokens).
  `keep_shots.py` now takes the newest shot per step (stale shots with the
  same step name were being picked).

### Package 3 · Icons and the mark — `fc2c87e`

- **Before / after:** `2-controls/*` → `3-icons/*`. The sidebar, tab icon
  and sign-in page are the visible change.
- **Checked by hand:** every page in the three walk-throughs, the Help page
  (its icons sit inside a `{% raw %}` block and first printed as text; fixed
  and pinned), the meeting box, the lead panel's edit and close buttons.
- **Tests:** new `tests/test_icons.py`; shown to fail with an icon missing
  from the set and with Help's icons un-wrapped from the raw block.
- **Done differently:** Lucide 0.460.0 (`lucide-static` from npm, SVGs only;
  ISC licence beside `icons.js`). 41 icons, not "about thirty": the Help
  page's section icons are included. Toasts lost their trailing ✓ / ▶ / ⏸.
  Left as text: Activity log symbols (stored data), ★ in ratings, → in prose
  and text links, the CSS-drawn ▸ on folds and ↓ between email steps.

### Package 4 · Dialogs, dates, wording — `8a2833c`

- **Before / after:** `3-icons/*` → `4-dialogs/*` (the twelve screens barely
  change; this package is in dialogs and labels).
- **Checked by hand:** 11 of the 16 confirmations opened on the harness by
  script: each is the app's dialog, no native dialog fires, 9 destructive
  ones draw the red confirm, a name with `<b>` shows literally. The other
  five need state set up and were read, not driven. Activity log times read
  "Today 6:16 pm".
- **Done differently:** `confirmDialog()` is a small wrapper over
  `chooseDialog` (same dialog, escapes its text) rather than repeating the
  body markup sixteen times. A few dialog titles were tightened while being
  moved (they had been run-on `confirm()` strings).

### Tests

The whole suite passes against each package's exact commit, run in a
separate worktree so later edits couldn't leak in: 32 files after package 2,
33 after packages 3 and 4 (`test_icons.py` added). No test that reads
front-end files needed changing: none of the ids, function names or
placeholders that `test_meetings.py`, `test_cal_bookings.py` or
`test_rendering.py` look for moved.

### For the reviewer

```
git switch plan-3-redesign-phase-1
for f in tests/test_*.py; do python "$f" >/dev/null 2>&1 || echo "FAIL $f"; done
python tests/test_icons.py
python tools/audit/serve.py seed && python tools/audit/serve.py   # leave running
python tools/audit/shoot.py tools/audit/plan-desktop-1.json      # and -desktop-2, -phone-1
python tools/audit/measure.py phone
grep -rnE '\b(confirm|alert)\(' static/js templates               # only chooseConfirm / confirmDialog
for f in static/js/*.js; do node --check "$f"; done
```

### Left for later phases (seen, not done: each would move something)

- The sidebar's "Check for email replies & send" keeps its inline-styled
  outline (tokens only); sidebar items and list rows can't take keyboard
  focus (F-14, F-40).
- "+ Add leads" stays the header's primary button; where it lives is layout
  (§4.7). The per-row "Open" in Calling's queue stays (F-27).
- Tick boxes in lists are 20 × 20 and placeholder buttons 35 px on a phone;
  not on the plan's three screens.
- Inline sizes and spacing in scripts and templates (F-5, F-42) go screen by
  screen with the layouts.
- The Help page's prose still names some buttons in Title Case.
- `infoDot` is still unused; the hover-only tooltips remain (F-8).

## Review fixes (2026-11-23) — `459e364`

The review passed Plan 3 with one fix and tidy-ups. All in one commit;
`db.py`, `scheduler.py` and `sender.py` untouched.

- **R1, the fix.** `[hidden] { display: none !important; }` sits beside the
  reset in `main.css`. Before: at 390×844 as `cofounder` (none marked),
  `#wa-no-wa-link` had `hidden` set and computed `display: flex`, 44 px tall,
  reading "0 marked not on WhatsApp →". After: `display: none`, 0 px; as
  `sheham` (6 marked) it still shows. Every element that uses the attribute
  (run-now button, `wa-crumb`, `wl-filter-note`, the link) is shown with
  `el.hidden = false`, never `style.display`, and none sits under an
  `!important` display rule, so the reset traps nothing; listed on the
  harness, no hidden element is drawn for either user. New
  `tests/test_css.py`: the rule is top-level (not in `@media`) and
  `!important`, and nothing toggled with `.hidden` is also set with
  `style.display`. It fails with `!important` removed.
- **R2, walk-throughs.** `plan-desktop-2.json`: `wa-todo-1280` and
  `wa-leads-1280` now call `showSection('whatsapp')`; `getleads-import` and
  `getleads-history` use `setTab('getleads', …)`, as does `getleads-import`
  in `plan-phone-1.json`. No other `setTab('scraper'` in the plans. Looked
  at the five shots: each shows its screen.
  - **`measure.py` had the same faults:** `getleads-import` used
    `setTab('scraper', …)`, and `wa-booked` opened the WhatsApp Booked tab
    that Plan 2 removed (it measured whatever was on screen). Now
    `setTab('getleads', …)` and a `meetings` step; `wa-messages` opens
    WhatsApp itself. It also read Calling's queue before the queue loaded
    (first row "None" on one run, 555 px on the next); it now waits for the
    first row. Two runs since give the same numbers.
  - **Found on the newly reached screen:** Get leads → Import showed the
    browser's grey "Choose File" (package 2's field rule skips
    `type=file`). Its button is now a secondary button
    (`::file-selector-button`), 44 px on a phone.
- **R3.** `chooseDialog({ cancel: false })` hides Cancel (the button got
  `id="choose-cancel"`, set on every call since the dialog is shared; the
  close button stays). `notifyCrossOwnerOverlap` uses it. Driven on the
  harness: the notice shows OK and the close button only; the next
  `confirmDialog` has Cancel back; no page error.
- **R4.** Overlap notice: `fmtWhen(…, 'wall', { sentence: true })` → "added
  yesterday", "added on Mon 2 Mar". Audit dates: `{ absolute: true }` →
  "certificate until Tue 24 Nov", "site first seen Wed 2 Mar 2016", never
  "Tomorrow". Checked with today, tomorrow, yesterday and a 2016 date.
  Other `'wall'` dates ("next call Today 3:30 pm", "due …") read as labels,
  not mid-sentence; unchanged.
- **R5.** Sentence case: IMAP host; AI provider, Anthropic / OpenAI / Worker
  API key, Company physical address, SMTP host, Email accounts, Global
  sending rules, Domain warmup schedule, AI features (settings.html); City +
  country, Search settings, Live log (scraper.html); Email sequence; A/B
  variants; `<title>Sign in — ShoutReach</title>`. Text naming them:
  "Settings → AI features" (the four messages in campaigns.js that show it;
  the error strings they are matched against are untouched), and the step
  editor's "Campaign variables live under Settings → Campaign variables"
  (was "Edit Settings → Variables"; the campaign's window is "Settings" and
  its field "Campaign variables"). "Settings → Email accounts" already read
  that way. The two "Import a file, paste or scrape →" buttons end in the
  arrow icon. A re-scan of every label, card title, heading, table heading,
  button and option found one more group, the AI model menu's "Input MTok /
  Output MTok", now lower case. What the scan still lists is proper names
  (Gemini, ChatGPT, the "To do" tab, model names) or sentence starts.
  - **Also:** the last two per-row ✕ buttons (remove an audit link in
    Settings, remove a message version in WhatsApp → Messages) were still
    plain ghost buttons, 40 px wide on a phone; they are `.btn-icon` like
    every other row ✕ (package 2).
- **R6.** The review has no R6.
- **R7.** Handover line 3: last updated 2026-11-23; Plans 1 and 2 deployed;
  Plan 3 built, reviewed, fixes in, not merged. §8: 34 test files, and what
  `test_css.py` pins.
- **Not done:** the optional phone-height items (keeping a 44 px tap area on
  the "sent today" pill and the "marked not on WhatsApp" link without
  growing the line, the segmented control's padding, the row menu button's
  row height). The review left them for the operator to ask for.
- **On the way:** the harness's sign-in rate limit (429) tripped after many
  scripted sign-ins and one `measure.py` run failed at sign-in, leaving the
  previous JSON in place. Restarting the harness clears it. The numbers
  below are from runs whose output was checked.

**Output**

- Suite, against `459e364` in a separate worktree (Python 3.13): 34 of 34 files pass, none fail.
- Walk-throughs: `plan-desktop-1` TOTAL PROBLEMS: 0; `plan-desktop-2`
  TOTAL PROBLEMS: 0; `plan-phone-1` TOTAL PROBLEMS: 0.
- `python tools/audit/measure.py phone` (390×844):

  | Screen | Under 44 px | First row / rows in view | Text |
  |---|---|---|---|
  | dashboard | 0 | | Inter |
  | wa-todo | 0 | 396 px / 4 | Inter |
  | wa-todo-lead | 0 | | Inter |
  | wa-todo-did-it-send | 0 | | Inter |
  | wa-leads | 46: list tick boxes, 20 × 20 | 570 px / 3 | Inter |
  | wa-leads-panel | 38: the same tick boxes | | Inter |
  | meetings | 0 | | Inter |
  | wa-messages | 12: placeholder buttons (35 px), campaign fields (42 px) | | Inter |
  | wa-campaigns | 0 | | Inter |
  | calling-todo | 1: "No website only" tick box, 13 × 13 | 555 px / 3 | Inter |
  | calling-lead | 0 | | Inter |
  | contacts | 44: list tick boxes | 518 px / 3 | Inter |
  | email-todo | 1: a 13 × 13 tick box | 345 px / 3 | Inter |
  | email-campaigns | 0 | | Inter |
  | getleads-import | 1: the "copy the format" text link, 35 px | | Inter |
  | settings | 3: fields at 38–42 px | | Inter, and the coding font on the terminal commands |

  No sideways overflow on any screen. The WhatsApp tabs still scroll
  sideways with "Messages" off the edge (F-17, layout). Everything under
  44 px is outside the plan's three screens (WhatsApp To do, the lead
  sheet, the Calling lead), which are all 0.
