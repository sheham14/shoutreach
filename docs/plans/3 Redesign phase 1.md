# Plan 3 · Redesign, phase 1: foundations

**For:** a coding session. **Branch:** `plan-3-redesign-phase-1`.
**Depends on:** Plan 1 package 7 (the shared date helper and the phone
font-size rule). **Source:** audit §3 and §4. Read [README.md](README.md)
here first.

**Checked against `4f5b303` on 2026-11-22**, after Plans 1 and 2 were
deployed: the counts below are from that commit, and
[what those two plans added](#added-since-this-was-first-written) is part of
this phase.

## What this phase is, and isn't

**It is** the new colours, typeface, icons and controls applied to the screens
as they are laid out today. When it's done the app is recognisably Hexiv on
every page, and **nothing has moved**.

**It isn't** the new layouts. The approved mockup
([canvas](https://claude.ai/artifact/8FU8mQbqpXGhA7bZ1LTesk), source in
`docs/audits/mockup-2026-11-21/`) shows where this is heading: a phone bottom
bar, a bar of actions fixed under the thumb, one-line lists, a Today page.
Those arrive in phases 2 to 4. Use the mockup here only for **how things
look**: colours, type, spacing, the shape of a button, a tag, a field.

If a change would move, add or remove something on a screen, it belongs to a
later phase. Leave it.

## Decided

- **Palette:** hexiv.co's own dark theme, below. Dark only; no light theme.
- **Typeface:** Inter for everything. A coding font only for things that are
  code: keys, placeholders like `{{business_name}}`, terminal commands.
- **One accent:** teal means "the thing to do" and "selected". Green, amber
  and red mean state only. Blue and purple leave the app.
- **The mark:** `static/img/hexiv-mark.svg`.
- **No rewrite.** Plain HTML, CSS and JavaScript stay.

## Tokens

Define these on `:root` in `main.css` and use nothing else for colour, size,
space or radius.

**Colour**

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0b1519` | Page |
| `--surface` | `#101d22` | Cards, sidebar, bars |
| `--raised` | `#15262c` | Fields, hover, menus, secondary buttons |
| `--line` | `#1e3238` | Dividers |
| `--line-strong` | `#5b7880` | The edge of anything you type in or tick (3:1 against its background) |
| `--text` | `#e7eef0` | Main text |
| `--text-2` | `#a3b8be` | Secondary text, labels |
| `--text-3` | `#6d8890` | Faintest text. On `--bg` and `--surface` only; it fails contrast on `--raised` |
| `--accent` | `#3cc1d0` | Primary button, selected, focus, links |
| `--on-accent` | `#06272c` | Text on the accent |
| `--accent-soft` | `#113239` | Selected row, active sidebar item |
| `--success` | `#3FB97A` | Sent, replied, booked, active |
| `--warning` | `#E0A23B` | Waiting on you, due, paused |
| `--danger` | `#F0645A` | Problems; destructive actions |
| `--brand` | `#00A3AD` | The mark only |

Add a soft tint for each of success, warning and danger (the colour at low
strength over the surface) for tag backgrounds and notices.

**Type** — Inter. Sizes: 12, 13, 14 (base), 16, 20, and 28 for the few large
numbers. Weights 400, 500, 600. Numbers in tables and counts use tabular
figures. On phones the base is 15 and fields are 16 (Plan 1).

**Space** — 4, 8, 12, 16, 24, 32. **Radius** — 6 for controls, 10 for cards
and windows, fully round for tags. **Layers** — name the five that exist
(sticky header, sheet, full-screen table, menu, window, toast) instead of the
thirteen numbers in use.

## Added since this was first written

Plans 1 and 2 changed the front end after this plan was drafted. All of it
is restyled here like everything else:

- **A Meetings page exists** (`templates/sections/meetings.html`,
  `static/js/meetings.js`, `.mt-table`, `.mt-group`), with a sidebar item and
  a count. Restyle it as it is laid out today. Its layout in the mockup
  belongs to a later phase.
- **The lead's meeting box** (`meetingBoxHtml` and `calMeetingBoxHtml` in
  `lead_panel.js`, `.meeting-box`), the "Website enquiry" fold, the
  Dashboard's "From the website" card, and the Settings cards "Website and
  bookings" and "Website enquiries that don't book". Several set colours
  inline (`style="color:var(--blue)"` on Join and Copy booking link,
  `var(--red)` and `var(--amber)` on Cal.com states).
- **Helpers to use, not rebuild:** `fmtWhen(value, kind, opts)` for every
  date, `chooseDialog({...})` for every confirmation, `httpsOnly(url)` for a
  join link (all in `static/js/tables.js`).
- **WhatsApp has no Booked tab** and Settings has no Zoho cards any more.

## Packages

### 1 · Tokens, fonts, and a sign-in page that can use them

- Replace the `:root` block with the tokens above, and rename every use of
  the old names. Uses today, in `main.css` and in the scripts and templates:

  | Old | `main.css` | Scripts, templates | Becomes |
  |---|---|---|---|
  | `--bg2` | 12 | 5 | `--surface` |
  | `--bg3` | 21 | 20 | `--raised` |
  | `--border` | 29 | 22 | `--line` |
  | `--border2` | 22 | 25 | `--line-strong` on anything typed in or ticked; `--line` elsewhere |
  | `--muted` | 36 | 36 | `--text-2`; `--text-3` only for the faintest text on `--bg` or `--surface` |
  | `--text2` | 3 | 4 | `--text-2` |
  | `--amber` | 14 | 26 | `--warning` |
  | `--red` | 10 | 23 | `--danger` |
  | `--green` | 27 | 19 | `--accent` or `--success` (next point) |
  | `--blue` | 9 | 24 | `--accent` for links and the thing to do; plain text otherwise |
  | `--purple` | 3 | 0 | plain text |

  **`--accent` already exists and is blue** (`#4da6ff`, 2 uses in `main.css`,
  3 elsewhere). Those become teal with the new token, which is intended;
  look at each once. `--font` and `--mono` keep their names. No old name may
  be left defined or used.
- **Split green.** Today `--green` means the brand, the primary button,
  "selected" *and* "success". Go through every use — 27 in `main.css`, 19 in
  the scripts and templates — and give each either `--accent` (buttons, active
  tab and chip, focus, selection) or `--success` (pills and badges for
  sent/replied/active, positive numbers, the progress bar, filled
  placeholders). Don't leave an alias that makes one stand in for the other.
- **Replace the hand-written tints.** About 46 `rgba(...)` values in
  `main.css` and 23 in the scripts and templates are the old colours at low
  strength. Each becomes a tint token.
- **Remove blue and purple.** The channel colours (`.channel-email`,
  `.channel-calling`, `.channel-whatsapp`) become plain text; links and
  info notices use the accent.
- **Self-host the fonts.** Inter (variable) and a coding font as `woff2`
  under `static/fonts/`, with their licence files, declared with `@font-face`.
  Remove both Google Fonts links. No page may request `fonts.googleapis.com`.
- **Let the sign-in page load them.** Static files sit behind sign-in today,
  which is why `login.html` carries its own 109-line stylesheet. Allow
  `/static/css/`, `/static/img/` and `/static/fonts/` without a session, and
  keep `/static/js/` private. Then `login.html` links `main.css`, drops its
  own stylesheet, and uses the same button and field styles as the app.
  This is the plan's one change to what a signed-out visitor can reach, so
  it gets a test (`tests/test_security.py`): signed out, a file under each of
  the three folders answers 200; `/static/js/app.js`, any path that climbs
  out of them (`/static/css/../js/app.js`) and every `/api/` route still
  answer as they do today.
- **Labels in plain words.** Table headings, field labels, stat labels, the
  sidebar's group names and the lead panel's fold titles lose capitals, the
  coding font and letter-spacing. Sentence case, 12–13 px, `--text-2`.
- **Large numbers in Inter**, tabular. (The coding font's zero has a slash and
  reads as an 8.)

### 2 · Controls

Restyle by class; don't rename classes yet.

| Control | Becomes |
|---|---|
| Primary button | Accent fill, `--on-accent` text. **At most one per screen region** — where a screen shows several today (a "WhatsApp" button on every row, "Done" on every row), make the repeated ones secondary |
| Secondary button | `--raised` fill, main text, no border |
| Destructive | Danger-coloured text on a secondary button. A solid danger button appears only as the confirm in a dialog. Per-row red-outlined ✕ boxes become a quiet icon button |
| Fields, dropdowns, text areas | One style: `--raised` fill, `--line-strong` edge, radius 6. One label style |
| Focus | A 2 px accent outline on **everything** the keyboard can reach. Today dropdowns and the search box have their outline removed and nothing in its place |
| Tick boxes and radios | The app's own colour (`accent-color`), dark scheme |
| Tags | `.pill` and `.badge` share one look: a small dot and a word, in success, warning, danger or neutral |
| Filter chips | Where chips switch between lists (Ready / Follow-up due; the Calling buckets), draw them as one segmented control. The unused `.seg` style can be the base |
| Tabs | Accent underline for the current tab; counts in `--text-2` |
| Cards | `--surface`, radius 10, no border. A card inside a card has no fill of its own |
| Tables | Row hover is `--raised`; the selected row is `--accent-soft` |
| Windows, menus, toast | `--surface` or `--raised`, radius 10, one soft shadow |

Tap targets on phones: buttons, chips, tabs, dropdowns, the row menu button
and the call outcome buttons are at least 44 px tall. The outcome buttons set
their size inline in `calling.js`; move that to a class.

Motion: 120–160 ms on hover and press. Delete the pulsing dot and the unused
scheduler pill. Respect `prefers-reduced-motion`.

### 3 · Icons and the mark

- **One icon set, shipped with the app, no network and no build step.** Lucide
  (ISC licence) is a good fit: outline icons on a 24-unit grid. Keep only the
  ones used — about thirty — as path data in one place, with a JavaScript
  helper and a matching template macro that return an inline SVG. Include the
  licence notice.
- Replace every emoji and symbol used as an icon: the sidebar (◉ ⊕ 📅 ✉ ☎ 💬
  ⊗ ⚙ ?), the browser-tab megaphone, the 📅 in the lead's meeting box, and
  the symbols inside button labels (⬆ ⬇ ⏹ ▶ ✓ ✕ ⚙ ⊞ ↻ ✎ ⋯ ⟳ ⎋ ← → ↗). An
  icon-only button gets an `aria-label`.
- **Leave the symbols inside Activity log lines.** Those are stored text
  written by `db.add_log` (✉ ☎ 🌐 🔑 ⚠), not part of the interface.
- **The mark:** `hexiv-mark.svg` beside "ShoutReach" in the sidebar and on
  the sign-in page, and as the browser-tab icon. Remove "Powered by Hexiv".

### 4 · Dialogs, dates, wording

- The fifteen `confirm()` calls and the one `alert()` left in the scripts and
  templates become the app's own dialog (`chooseDialog`), with the
  destructive ones using its danger form.
- Every date and time goes through Plan 1's helper (`fmtWhen`). None is left
  as a chopped raw string.
- Button labels in sentence case ("Add account", not "Add Account").

## How to check

Use `tools/audit/` (see its README). Before starting, take the desktop and
phone walk-throughs and keep them. After each package, take them again.

- No JavaScript errors or failed requests in any walk-through.
- Every section and tab still shows the same things in the same places.
- `main.css` contains none of the old colours (`#00e87a`, `#4da6ff`,
  `#a78bfa`, `#080c10`) and no `rgba(` that isn't a token definition.
- No element uses Space Grotesk; no request leaves for Google Fonts.
- Signed out, `/static/css/main.css` and the mark load; `/static/js/app.js`
  does not.
- Contrast, computed: `--text-2` on all three backgrounds and `--text-3` on
  the two it's allowed on are at least 4.5:1; `--line-strong` against
  `--raised` and `--surface` at least 3:1.
- `python tools/audit/measure.py phone`: on WhatsApp To do, the lead sheet and
  the Calling lead, nothing a finger taps is under 44 px tall.
- The whole test suite passes. A few tests read front-end files
  (`tests/test_meetings.py`, `tests/test_cal_bookings.py`,
  `tests/test_rendering.py`) for ids, function names and placeholders. If a
  change here moves one of those, update the test in the same commit so it
  checks the same thing, and say so in the hand-back. Don't loosen a check.
- The `tools/audit/` walk-throughs were written before Plan 2 and still open
  WhatsApp → Booked, which is gone (the `wa-booked*` steps in
  `plan-desktop-1.json`, `plan-desktop-2.json` and `plan-phone-1.json`).
  Point those steps at the Meetings page before taking the "before" set.

## Not in this phase

Layout of any screen; the phone bottom bar; bars fixed to the bottom of the
screen; the Today page; the Meetings page's new layout (the page that exists
is restyled in place); removing inline `style` attributes
wholesale (they go screen by screen in later phases — remove one here only
where it sets a colour or font that would otherwise defeat a token).

## Hand-back

Per package: the commit and before/after screenshots of Dashboard, WhatsApp
To do, a lead panel, Calling To do, Settings and the sign-in page, at
computer and phone width. Say which uses of `--green` you made accent and
which success if any were not obvious.
