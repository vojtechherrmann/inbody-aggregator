# UI design: Report and History

The decisions behind the viewer UI, and the chart module the app should adopt. The working prototype
lives in `docs/prototype/`:

| File | What | In the real app |
|---|---|---|
| `charts.js` | the History module: unit conversion, time axis with pauses, line chart, stacked bar chart, the History blocks, info popovers, hover/keyboard, table views, Notes | **adopt as-is** as a static file |
| `charts.css` | chart styles; consumes the theme tokens listed at its top | adopt as-is |
| `i18n.js` | **one dictionary per language** (`en`, `cs`) for everything the UI says: shell, settings, sharing, report, charts, metric names, units, date and number formats | port the keys to the app's i18n catalog; `charts.js` reads `window.InbodyI18n[lang]` or takes `strings` |
| `ui.css` | theme tokens (light + dark) + shell, panels, report styles | reference for the Jinja templates |
| `app.js` | prototype shell (Settings, Sharing, Upload), Report tab rendered client-side, delete with confirm + undo | reference only: the app renders these on the server |
| `index.html` | page template (no inline script/style; data as `application/json`) | reference for the base template |
| `build_example.py` | generates **synthetic** example data, inlines everything into `ui.html` | prototype only |
| `ui.html` | GENERATED single-file page, published as the artifact | prototype only |
| `serve_local.py` | serves the template with the **real** data from `samples/`, in memory, on 127.0.0.1 | prototype only |

## 1. Page layout

- **App bar** (wraps on phones): the brand (text, below) · (spacer) · **Upload** (primary) ·
  **Sharing** · **Settings** (gear) · Log out.
  - **Brand:** the whole brand is TEXT in one typeface and one colour: "InBody" in weight 800 and
    "Aggregator" in weight 500, tight (a normal word gap), same baseline and size, so it reads as
    one name. Typeface **Saira** (SIL OFL 1.1, variable weight, vendored in `static/fonts/` with its
    licence, `font-src 'self'`); colour token `--brand-red` = the InBody wordmark's own red
    `#90001f`, brightened to `#ff2a55` in dark mode so both runs stay legible and identical. The
    link keeps `aria-label="InBody Aggregator"`.
  - **Upload** is the primary action, because an upload always goes to the viewer's own account.
    Viewing someone else: visible but `aria-disabled="true"` with a tooltip (not `disabled`, so the
    tooltip shows and it stays focusable).
  - **Sharing** (person-plus icon + label): same enabled/disabled rule as Upload. Opens the sharing
    panel (section 5).
  - **Settings** (gear icon + label): opens the settings panel (section 6).
  - Both panels are dropdowns under their button (on phones they span the app bar); they close on
    Esc (focus returns to the button) or an outside click; only one is open at a time.
- **Tab row** (Report and History pages): the **"whose data" selector first, then the tabs**.
  - The selector has no visible label (it carries an `aria-label`), is deliberately prominent
    (15 px, weight 600, 1.5 px dark border) and lists only people the viewer may see: their own
    entry reads "(me)" / „(já)", every other person is just the name (no "shared with you"
    suffix); an admin sees everyone. With nobody else to pick (the guest, a user nothing is shared
    with) it is a plain name instead of a one-item dropdown.
  - On phones the selector takes its own full-width row above the tabs.
- **Frozen top block** (like frozen panes): on a roomy screen (viewport **> 760 px wide AND >= 640 px
  tall**) the app bar, the tab row and, on History, the filter row (Range, summary, "Shrink pauses
  over [N] mo") stay pinned at the top while the page scrolls. Phones and short screens: normal flow,
  nothing pinned. Pure CSS `position: sticky`, no JS. The three rows have FIXED heights
  (`--bar-h: 56px`, `--tab-h: 47px`; the filter row's `top` is `--ibc-pin-top` = their sum) so the
  offsets stack, and each lower row tucks 2 px UNDER the one above it (its own padding fills the
  overlap; z-index app bar 30 > tab row 29 > filter row 28) so no sub-pixel gap can show content
  scrolling underneath at any zoom / device pixel ratio (verified at 0.9, 1, 1.1, 1.25 and 2x,
  light and dark). Each row has an opaque background (`--card` for the bar, `--page` for the others)
  and a plain bottom border (the filter row's is a lighter hairline, always on). The guest banner
  and the prototype banner stay in the page flow and scroll away. `scroll-padding-top` equals the
  pinned height (+ a margin), so anchors, `scrollIntoView` and focus never land under the block. The
  Settings panel (z 30 inside the app bar) and the viewing `<select>` open above it. The quotes in
  the margins are `position: fixed` and unaffected.
- **Vertical-view notice** (every page of base.html, incl. landing, login and the guest): a slim calm strip
  right under the app bar, shown by CSS **only at viewport width <= 760 px** (`@media (max-width: 760px)`, the same
  breakpoint as the frozen block and the 1-column cards; no UA sniffing; the wording says "vertical
  view", never "mobile"): "The app is not optimised for a vertical (portrait) view yet. You can use it, but landscape
  will be more comfortable." / „Aplikace zatím není optimalizovaná pro vertikální pohled. Použít ji
  můžeš, ale na šířku bude pohodlnější." Not pinned (it scrolls away),
  non-blocking, stacks above the guest banner; a small ✕ (aria-label Close / Zavřít) hides it and
  `shell.js` remembers that in localStorage (`inbody-mobile-notice-dismissed`, class `notice-off` on
  `<html>`; without JS the strip simply stays). The prototype has the same strip (`inbody-proto:settings`
  store key `mobileNoticeOff`).
- **Contact Vojta** (one shared link): every "write to / ask Vojta" in the UI is the same mailto link
  `<a class="contact-link">Contact Vojta</a>` (cs „Kontaktovat Vojtu“), rendered by the Jinja globals
  `contact_link(context)` / `contact_text(key, context)` in `app.py`. The address comes ONLY from
  `config.CONTACT_EMAIL` (server-side render, never in JS or a cookie; empty = the label as plain
  text). Each context has a short pre-filled subject (`contact.subject.<context>`: general, forgot,
  account, delete, extra, upload (+ upload id), error (+ code)); the signed-in user id is appended,
  never a measured value. Offered on: login (forgotten password, no account), landing (account on
  request), the privacy page (data deletion), error pages, the upload report (upload problem /
  used-up limit) and refused uploads (type / daily limit). The guest gets it only on the public pages
  (landing, login, privacy), nowhere in the private app. The prototype shows no such text.
- **Tabs:** Report | History. History is the default. Each tab has its own URL (`#report`,
  `#history` in the prototype; real routes in the app).
- **Read-only notice** above the tab content when viewing someone else; every owner-only control
  (Upload, Sharing, "add gym", delete) is hidden or disabled there.
- **Footer:** centred, muted small text "© {current year} VojtaH"; nothing else (no "private site",
  no version).
- Content column max 1120 px; side gutter `max(16px, env(safe-area-inset-*))`; works at 400 px.
- System font stack for everything except the brand (Saira, self-hosted; strict CSP, no font
  host). Light and dark themes from the same tokens.
- **Quotes in the side margins** (pure decoration): 16 short funny quotes `quote.1`…`quote.16`
  (cs + en, **no authors**). Each side margin (the free space beside the 1152 px column) holds one,
  centred both ways (`position: fixed`, `align-items/justify-content: center`), set as a
  typographic piece: font size `clamp(20px, (100vw - 1152px) / 18, 30px)`, line-height 1.25,
  `text-wrap: balance`, a big decorative opening mark (the 66-shaped “ in both languages, never the Czech low „) in the padding above
  the text, set a little in from the bar. ONE look for all 16 (user's choice): Saira 500 caps,
  `0.09em` tracking, a 3 px brand-red bar on the left, left-aligned. Faces are vendored in
  `static/fonts/` with their OFL licences (listed in `vendor/SHA256SUMS`, latin + latin-ext,
  `font-display: swap`): Saira (also the brand) and Playfair Display italic (the quote mark only);
  colours only from tokens (`--ink-2`, `--brand-red`), so contrast follows the other text in both
  themes.
  Shown only at **≥ 1800 px** (margin ≥ 324 px; below, hidden completely, never overlapping
  content, no horizontal scroll), `aria-hidden`, `pointer-events: none`, no inline styles, hidden
  in print.
  **When the pair changes:** only when the user LANDS ON Report or History (tab switch, switching
  the person in the viewing selector, the report stepper: in the app each is a GET of `/report` or
  `/history`). The server draws a NEW pair then (`next_quotes`), never containing either quote of the
  pair shown before (kept in the `inbody_quotes` cookie, `a.b`; garbage is ignored). EVERY other page
  (Upload, Sharing, upload detail, account, /data, login, errors, htmx fragments) keeps the cookie's
  pair; with no cookie yet it draws once. A Settings change keeps the pair too: `POST /settings` sets a
  one-shot `inbody_quotes_keep` cookie that the next full page consumes (it does not draw, then deletes
  the flag). The prototype is one page: `app.js` re-rolls only on the Report/History tab switch, a
  person change and the report stepper/select (same no-repeat rule); Upload / Sharing / Settings and the
  top bar leave the pair alone.

## 2. Data delivered to the page

The server embeds the visible person's measurements as JSON (or serves them from a same-origin
endpoint):

```html
<script type="application/json" id="history-json">{"measurements":[ ... ]}</script>
```

Each measurement is the accepted canonical row **in SI units** (column names from
`extraction/inbody_sheet/measurement.schema.json`) plus:

| key | meaning |
|---|---|
| `id` | measurement id (used for "open this report" and delete) |
| `measured_date` | `YYYY-MM-DD`, the only thing that positions a point on the time axis |
| `measured_time` | as printed, display only (`HH:MM` is shown) |
| `origin` | `csv` · `sheet` · `sheet_history` (partial) |
| `gym` | printed on the sheet, or typed into the optional "gym" field of the upload form; `null` when nobody entered one |

Send only what the charts and the report use (drop `warnings`, `converted_from`, impedance, the
subject fields). Escape `<` as `<` inside the JSON.

**Gym:** shown in the report header, every tooltip and every table. Missing: "Gym not entered" /
„Fitko nezadáno", plus a small **add** link for the owner only. The CSV export never has a gym.

**Source text** (tooltips, tables): "gym · source kind", e.g. "Gym B · result sheet",
„Riverside Gym Brno · výsledkový list", "Gym not entered · CSV export".

## 3. History tab

### 3.1 Filter row (one row, above everything it scopes)

- **Range:** 6 months | 1 year | All | **Custom** (default All). Anchored at the **latest
  measurement**, not at today, so someone who stopped measuring still sees their last period.
- **Custom range** (cs "Vlastní"): a month range, from-month to to-month, chosen in an OWN month-grid
  picker (`<input type="month">` is missing in desktop Safari; no library, no inline styles).
  Clicking **Custom** selects the pill and opens a dialog-like popover (`role="dialog"`, label
  "Choose a month range") under the filter row with two month grids side by side, **From | To**: a
  year header with ‹ › arrows and a 3×4 grid of localized short month names (the module's own
  `fmt.monthYear` names; the aria-label is the long "March 2025"). Months outside the data span
  (first .. last measurement month) are `aria-disabled`; the selected months are `aria-pressed` and
  the months between are tinted. From ≤ To is enforced: a From after To moves To to the same month and
  vice versa. First time, Custom starts with the whole data span, or its last 12 months when the span
  is longer. The pill stays "Custom"; the range is shown next to it as text ("Mar 2025 – Sep 2026",
  the module's month formatting) and the summary line reflects the filtered rows. The charts filter
  from the 1st of the From month to the end of the To month; "Fewer than 2 measurements in this range"
  + Show all works as for the other ranges (a range holding none just says "0 measurements"), and
  Shrink pauses works on the filtered rows. Keyboard: arrows move in a grid (±1 month, ±3 with up
  and down, PageUp/PageDown = a year; roving tabindex), Esc and an outside click close it, Esc and
  Done return the focus to the Custom pill; the popover belongs to the (frozen) filter row, so it
  sits under the pinned block and follows the page; on phones it is full-width and the month
  buttons are ≥ 44 px. State (localStorage chart state): `range: "custom"`, `from: "YYYY-MM"`,
  `to: "YYYY-MM"`; validated on read (malformed or entirely outside the data → "all"; a partly
  outside range is clamped to the data span when the data changed).
- **Shrink pauses** is ONE compact control on the History page, **top right of the filter row, next
  to Range**: `[x] Shrink pauses over [3] mo` / `[x] Zkrátit pauzy nad [3] měs.` = a checkbox
  (default **on**, `breaks: 'auto'`) and a number input (months, default **3**, 1–24). Both apply
  **live** (the charts re-render, no page reload) and persist **per browser** in the chart state in
  localStorage (`breaks`, `pauseMonths`; validated on read, falling back to on / 3). While the box
  is off the number is disabled (greyed, still visible) and the axis is continuous. A tooltip on the
  control explains what a pause is. It is **not** a Settings option and the server knows nothing
  about it (it is client-side only, so the guest needs nothing special). (The pauses are shrunk to
  a narrow break, not skipped; hence the name.)
- Layout: Range on the left with the **summary on the same line, right next to it**, in small
  muted text: "12 measurements · 9 Mar 2024 – 26 Sep 2026" („12 měření · 9. 3. 2024 – 26. 9. 2026";
  „měření" has the same form for every count). On phones it may wrap under Range. The summary
  never mentions pauses.
- The pause control is right-aligned in the filter row (its tooltip and Notes explain the pause
  rule).

### 3.1a Fewer than 2 measurements

| Situation | History tab | Report tab |
|---|---|---|
| **n = 0** | centred empty state: "No measurements yet. Upload your first InBody sheet or CSV export." + **Upload** (owner only; a shared viewer sees just "No measurements yet.") | the same empty state |
| **n = 1** | **no charts, no filter row**: one centred panel with only the sentence "History becomes available once there are at least 2 measurements." / „Historie bude k dispozici, jakmile budou aspoň 2 měření." (no link, no button) | the single measurement as usual; the delta cells stay empty and the meta line says it is the first measurement |
| **n ≥ 2 but the selected range holds < 2** | filter row stays; instead of the charts: "Fewer than 2 measurements in this range." + one-click **Show all** (sets Range = All) | unaffected |

The summary line shows a single date when the range holds one day.

**Prototype-only preview switch** (to see these states): `?only=latest` (keep just the newest
measurement of every person), `?only=first` (just the oldest), `?only=none` (empty) on the local
server URL; the published page has the same switch in its banner ("Preview data: all · only latest
· only first · none", `data-prototype-only`), because the artifact URL can't carry a query string.

### 3.2 Time axis and pauses (shared by every chart on the page)

- Real time axis: positions come from `measured_date` (UTC days). Times are never used.
- **A pause** is a gap between consecutive measurements **longer than N calendar months, default
  3**, a user setting (Settings → Pauses, min 1). For the real data (3 months) the gaps
  Aug 2024 → Jul 2025, Dec 2025 → May 2026 and Jun 2026 → Oct 2026 are pauses.
- **Shrink pauses on:** each pause is shrunk to a narrow break on the axis. The axis becomes segments, each with a
  real time scale inside; segment width ∝ its own time span, **minimum 9 %** of the plot width, so
  an isolated measurement still gets room. Each break takes **2.5 %** of the width (half of the
  first draft) and is drawn as a faint vertical band, a `//` break mark cut into the x-axis, and
  the gap length above it ("11 mo" / „11 měs."; weeks below 61 days). **Lines continue through a
  break as a dashed segment** across the band (all charts, including the layer lines over the bars).
- **Shrink pauses off:** one continuous time scale; a line segment that spans a pause is dashed the
  same way. A dashed segment always means "pause"; nothing else is dashed (3.7).
- Everything is computed as fractions of the plot width, so all charts (wide or small) share the
  same segments and break positions and stay aligned.
- X ticks: month starts, stepped 1/2/3/6/12/24 months so labels sit ≥ 70 px apart inside each
  segment; a segment without a month start gets one label; overlapping labels drop. "Mar 2025" /
  „bře 2025"; years only when stepping by 12+ months.

### 3.3 Chart card anatomy (every card the same)

1. **Title row:** the title, and the **ⓘ info button right-aligned on the same line** (every card).
2. **Control row** (wide cards): metric pickers **left**; unit/mode toggles **right**. On narrow
   screens the toggles wrap under the pickers, still right-aligned.
3. Legend (only when ≥ 2 series), then the plot(s).
4. "Show as table".

No explanatory subtitles and no coverage notes inside cards, so cards in a row have the same
structure and height whatever the data. Explanations live in the **ⓘ popover** (click/tap or
Enter/Space opens it; Esc or an outside click closes it; `aria-expanded` + `aria-controls`), and
coverage facts live in **Notes** (3.8).

**Every y-axis shows its unit in square brackets** above the top tick label, left-aligned with the
tick labels, in the axis-text colour: `[kg]` / `[lb]`, `[%]`, `[kcal]` / `[kJ]`, `[kg/m²]`, `[L]`,
and `[points]` / `[level]` for InBody score / visceral fat level (CS `[body]`, `[úroveň]`). It
follows the unit setting and the card's kg / % toggles.

### 3.4 Body composition (wide card)

| Left: Show | Right: scale / unit |
|---|---|
| **All** (default) · Weight · Skeletal muscle (SMM) · Body fat | with All: **Absolute (kg)** · Relative (%) |
| | with SMM or Body fat: **kg** · % of weight („kg \| % hmotnosti") |
| | with Weight: none (kg only) |

- **All → stacked bars.** Each bar is that day's weight split, bottom to top, into skeletal muscle
  (blue), **other lean mass** = weight − SMM − body fat (aqua; „Ostatní netuková hmota") and body
  fat (orange). Absolute: y from 0, the weight printed above the bar (newest first; colliding labels
  dropped). Relative: 100 % stacked, no labels.
  - **Layer lines:** each layer gets a 2 px line in its colour through the **midpoint of its
    segment** on consecutive bars, in both modes. Midpoints (rather than top edges) keep each line
    inside its own colour band, so a line is never mistaken for the neighbouring layer, and in
    Relative mode the fat line still moves (its top edge would always be 100 %). Lines follow the
    line rules (3.7): dashed across a pause, dotted over measurements without the split.
  - Bars sit at their real date; width = min(20 px, smallest gap between neighbours − 3 px), one
    width per render; 2 px surface gap between segments; 4 px rounded top.
  - Hover per bar (nearest bar; others dim). Tooltip: date + time; body fat / other lean / SMM as
    "16.0 kg · 20.0 %"; "Weight 80.0 kg · 100 %"; source; "click to open".
  - A measurement without SMM or body fat: one **neutral grey bar** of its weight; the tooltip says
    the source has no split; Notes counts them.
- **Weight / SMM / Body fat → one line on one axis**, y rescaled to that series' own range. The
  tooltip always shows both values ("36,0 kg · 45,0 %"). % of weight is computed per measurement
  from that day's weight, so it isn't a rescaled kg line; that's why it's a toggle and not a second
  axis.

### 3.5 Body parts (wide card)

| Left: body part | Right: scale |
|---|---|
| **Arms** (default) · Legs · Trunk; single limb: R arm · L arm · R leg · L leg | **kg** · % of weight · % of ideal · Right − left (only Arms/Legs; disabled with a tooltip otherwise) |

- **Lean mass and body fat are shown together**, as **two stacked mini-plots sharing the x-axis**:
  "Lean mass" on top in the muscle hue (blue), "Body fat" below in the body-fat hue (orange), each
  with its own y-scale and `[unit]`. Arms are ~4 kg lean vs ~0.8 kg fat (legs ~11 vs ~2), so one
  shared scale would flatten fat; a second y-axis is not allowed. The mini-plots are aligned and
  hover-synced like every other chart.
- **Right and left are both solid lines in the same colour; the side is the marker shape:
  right = ▶ triangle pointing right, left = ◀ triangle pointing left** (≈ 14 px, filled, 2 px
  surface ring; trunk keeps the round dot). The legend and the tooltip line keys show the triangles; the legend names "Right
  arm" / "Left arm" and toggles a side in both plots.
- **kg:** `seg_lean_<seg>_kg`, `seg_fat_<seg>_kg`.
- **% of weight** („% hmotnosti"): the segment's lean mass / body fat as % of that measurement's
  body weight, computed per measurement; y-axis `[%]`. Unlike % of ideal, it is calculated here,
  not printed by InBody (the ⓘ says so).
- Tooltips always show **kg · % of weight** together (as in the composition chart), plus the
  printed % of ideal when the source has it.
- **% of ideal:** only the InBody-printed `seg_*_pct`. Missing → a gap. **Never computed.** A
  reference line at 100 % = ideal.
- **Right − left:** per mini-plot one series R − L around a 0 midline, y symmetric, "▲ right has
  more" / "▼ left has more" in the corners; tooltip shows both sides.
- „All limbs" was removed (arms and legs differ ~3×).
- The ⓘ box explains the mode and includes: "Lean mass of a body part = muscle + water + other
  non-fat tissue in that part; it is not the same as skeletal muscle (SMM)."

### 3.6 Other indicators (small multiples, one metric each, own y-scale)

BMI · InBody score · Visceral fat level · Basal metabolic rate · Total body water · Protein. Single
blue line, no legend, compact height, ⓘ with a one-line definition.

### 3.6a Value labels (all charts)

Numbers are printed in the charts wherever they fit, never overlapping and never outside the plot:
- **line charts:** a label above (else below) each point, placed by priority: the latest point
  first (near the right edge it may end at the point instead of being centred), then the maximum
  and minimum, then the rest from newest to oldest; a label that would collide with another label,
  a point, a zone or reference label, or the plot edge is skipped. Number only; the y-axis tag
  carries the unit. There is **no right-gutter end label** and no leader line: the latest value is
  labelled inline like every other point, and the plot uses the full width.
  Ink: secondary text token with a surface-coloured halo.
- **stacked bars:** one value per segment (up to 3 per column), inside the segment: horizontal when
  the bar is wide enough, rotated 90° when only the height allows, otherwise skipped; plus the
  weight on top. Inside a categorical fill the text is white in both themes (never the series
  colour).

### 3.7 Gaps and partial series (all charts)

| Situation | Drawing |
|---|---|
| consecutive measurements both have the value, no pause between | **solid** 2 px line |
| a pause between them (Shrink pauses on: across the break band; off: across the long gap) | **dashed** 1.75 px (6/4 dash, 85 % opacity) |
| measurements in between lack the value (no pause) | **dotted** 1.75 px (round dots, 70 % opacity) |

Dashed and dotted stay secondary to the 2 px fully opaque solid lines (thinner, slightly
transparent, broken) but are clearly visible on both themes.
| an isolated value | a dot only |
| no value in the selected range | the plot area keeps its height and says "No measurement in this range has this value." |
| person has no measurements | "No measurements yet." |

Tooltips and tables show "—" for a missing value. **No dual axes anywhere.**

### 3.8 Notes section (bottom of the History tab)

One "Notes" / „Poznámky" list, one short line per chart or metric group, recomputed for the
current range, controls and settings, for example:

- "Body parts (kg): Lean mass in 12 of 12 measurements, Body fat in 11 of 12 measurements. The Gym C
  app screenshot has no segmental body fat."
- "Body parts: Right: ▶ triangle pointing right. Left: ◀ triangle pointing left. Both sides are solid lines in the same colour."
- "InBody score: in 11 of 12 measurements."
- "Visceral fat level, Protein: only on InBody result sheets (2 of 12 measurements); the CSV export
  leaves them empty."
- "A pause is a gap longer than 3 months between two measurements. Change the length in Settings."
- "Lines: solid between measurements; dashed across a pause (gap longer than 3 months); dotted where
  the measurements in between don't have the value."
- "“—” in tooltips and tables: that measurement's source didn't have the value."

The module builds the list itself (each card calls `ctx.setNote(id, text)`; strings under
`chart.notes` in `i18n.js`).

### 3.9 Table view ("Show as table") — same rule for every chart

- Columns, in this order: **Date | value(s) | Source**.
- Date in the locale format, **newest first** (a row header).
- Value columns = the series **currently shown** in the chart (legend toggles respected), **in
  legend order**, in the current unit setting, with the unit in the header in square brackets:
  "Body fat [kg]", „Netuková hmota – pravá ruka [kg]", "Weight [lb]". Values right-aligned,
  without units in the cells.
- Source last: "gym · source kind"; missing gym → "Gym not entered" / „Fitko nezadáno".
- "—" for missing values.

### 3.10 Interactions and accessibility

- **Crosshair** snaps to the nearest measurement; hovered points get a larger dot with a surface
  ring. **All charts are hover-synced** (crosshair / highlighted bar at that date in every chart,
  the tooltip only in the hovered one).
- **Tooltip:** values lead (bold), labels follow; line keys for lines (with ▶ / ◀ for right / left), swatches for
  bar parts; footer with "gym · source kind". No "click to open" hint text.
- **Open a report:** click (mouse) or tap a second time (touch) → the Report tab at that measurement.
- **Keyboard:** each plot is focusable; ← → move between measurements, Home/End jump, Enter opens
  the report, Esc hides. Moves are announced through a polite live region.
- Identity is never colour alone: legends (names + ▶ / ◀ marker shapes), ▶ / ◀ markers on the
  points for right / left, stacked-bar legend in stack order, table views.
- Control state (range, pauses on/off, picks, modes) persists per viewer (`onChange`).

### 3.11 Colour

- Categorical slots 1–3 of the reference palette: blue `#2a78d6` / `#3987e5`, orange `#eb6834` /
  `#d95926`, aqua `#1baf7a` / `#199e70` (light / dark). Validated with the dataviz validator, **all
  pairs**: light vs `#ffffff` CVD ΔE ≥ 9.2, normal-vision ≥ 24.0; dark vs `#1a1a19` CVD ΔE ≥ 9.4,
  normal-vision ≥ 20.9. Light aqua is 2.82:1 on white → relief via table view + legend.
- Colour follows the entity everywhere: **skeletal muscle and lean mass = blue, body fat = orange**
  (composition and body parts alike), other lean = aqua, weight line = neutral ink, weight-only bar
  = neutral grey. Filters never repaint.
- Status colours appear only in the Report's evaluation pills (always with an icon), the
  directional change colours (green up / red down, always with ▲ ▼ and a sign) and destructive
  buttons.

## 4. Report tab

Latest measurement by default; any measurement via ‹ › and a date select (newest first, latest
marked), or by clicking a point in History.

1. **Header:** "Sat 26 Sep 2026 14:05" („so 26. 9. 2026"), stepper, and for the owner or an admin
   (never a shared viewer) a **red trash-icon button** "Delete measurement" (4.3).
2. **Meta line:** *latest* chip, source chip, gym (or "Gym not entered" + owner-only *add*),
   "Changes compare with 5 Mar 2026, the previous measurement."
3. **Tiles:** Weight · Skeletal muscle (SMM) · Percent body fat · InBody score, each with its delta.
4. **Muscle–fat analysis:** weight, SMM, body fat.
5. **Obesity analysis:** BMI, PBF, visceral fat level, waist–hip ratio, obesity degree.
6. **Body composition analysis:** total body water, protein, minerals, fat-free mass (FFM); dry lean
   mass and soft lean mass only when present.
7. **Metabolism and score:** BMR, InBody score, SMI, recommended intake.
8. **Segmental analysis:** laid out like the body figure (R arm · trunk · L arm / R leg · L leg;
   2 columns on phones): per segment two rows, lean mass and body fat, each with the value (+ "110 %
   of ideal" when printed), its delta and its evaluation pill.
9. **Weight control** only when the source has any of it.

**Row anatomy** (value tables): left = the **category name in bold**, and **under it, on its own
line, the evaluation pill** (if any); right = the value and under it the delta. Rows with a printed
normal range add a **range bar** under them.

**"—"** for every core field the source didn't have; a footnote explains "—", the comparison rule,
the symbols and percentage points. All values follow the unit setting.

### 4.1 Evaluation pills (one component everywhere)

Every evaluation in the report is the same **pill**: fully rounded (`border-radius: 999px`), compact
(≈ 2 px 10 px padding, 12 px text), tinted background + matching border in the status colour family,
**icon + text** inside (✓-in-circle for good, !-in-triangle for warning/bad), never colour alone, never
plain inline text. Used for:

| Source | Values → tone |
|---|---|
| printed BMI / PBF evaluation (`bmi_eval`, `pbf_eval`, the sheet's checkboxes) | Normal → good; Under, Slightly over → warning; Over → bad |
| printed segmental evaluation (`seg_lean_*_eval`, `seg_fat_*_eval`) | lean: Under → warning, Normal / Over → good; fat: Over → warning, Normal / Under → good |
| a printed normal range (`<field>_low/_high`) | Within normal → good; Below / Above normal → warning |

CS: „Normální", „Pod", „Lehce nad", „Nad", „V normě", „Pod normou", „Nad normou".

### 4.2 Range bars

When the sheet printed a normal range: a track with the grey normal band and a dot at the value;
the line beside it reads only "normal 60.0–80.0 kg" / „norma 60,0–80,0 kg" (the evaluation is the
pill under the name). Under the track a **tick scale**: a step from 1 / 2 / 2.5 / 5 × 10^k chosen so
about 5 ticks fall inside the bar's domain, 3 px hairlines, muted axis-text labels, tabular numbers,
current unit and locale (CS decimal comma). A tick label that would overlap its neighbour or stick
out of the bar is hidden (its hairline stays).

### 4.3 Deltas (changes)

- Compared with the **most recent earlier measurement that has the value**; when that isn't the
  previous measurement, the delta says so ("−3 vs 9 Mar 2024").
- **No earlier value → the delta cell stays empty** (blank, same line height and width, so the
  layout doesn't jump); no "nothing to compare" text.
- **Arrow + signed value + unit.** A metric whose unit is % changes in **percentage points**:
  "▼ −2.5 pp" / „▼ −2,5 p.p." (PBF, obesity degree, % of ideal, % of weight, …).
- **Colour follows the health polarity of the metric** (user decision 2026-11-16): for fat
  (fat mass, PBF, visceral level, WHR, segmental fat) **down → green, up → red**; for muscle / lean
  mass (SMM, FFM, soft lean, SMI, segmental lean, InBody score) **up → green, down → red**; where
  no verdict is possible (weight, BMI, water, protein, minerals, BMR, intake, obesity degree) the
  delta stays **neutral ink**; no change → neutral, "=". Arrow + sign + unit remain the non-colour
  cue; no ✓ / ! icons on changes. (`views.delta_tone`; `InbodyCharts.compare()` returns only the
  direction `verdict: "up" | "down" | "same" | "none"`.)
- Whether a value is healthy is said by the **evaluation pills** (4.1), which describe the value,
  not the change.

### 4.4 Delete: icon button, in-page confirmation, undo

- **Red trash-bin icon button** (inline SVG, status "critical" red `#d03b3b`, `aria-label` +
  tooltip "Delete measurement" / „Smazat měření") next to the measurement picker. Shown only to the
  owner or an admin, never to a shared viewer. Never a text link.
- Click → an **in-page confirmation** (`role="alertdialog"`, not `confirm()`) under the header:
  "Delete the measurement of {date}? You can upload that day again afterwards." / „Smazat měření
  z {date}? Ten den pak můžeš nahrát znovu." (one text for every case; the archive isn't mentioned
  to the user), with **Cancel** and a red **Delete**. Focus goes to Cancel; Esc cancels.
- After deleting: a toast "Measurement of 26 Sep 2026 deleted. **Undo**" for 8 s; Undo restores it.
  In the app the delete is a POST (CSRF); the day becomes free (upload-pipeline section 3).
- **The same icon button + confirm pattern is used wherever something can be deleted or revoked**:
  measurements, uploads on the upload page, and sharing grants (section 5).

## 5. Sharing panel

Opened from the **Sharing** button (person-plus icon). Only for the viewer's own data.

- **"Who can see my data" / „Kdo vidí moje data":** the viewer's own grants, each "jakub ·
  read-only" with a red trash-icon button to revoke (same in-page confirm: "Stop sharing your data
  with jakub? They will no longer see your measurements."). Below: "Username" input + **Share**
  button; "They get read-only access to your full history. Usernames can be checked at most 10× per
  hour." Errors in place (`role="status"`): user not found, already shared, can't share with
  yourself, too many checks.
- **"Shared with me" / „Sdíleno se mnou":** read-only list of owners who shared with the viewer;
  clicking a name switches the Viewing selector to them.
- **Never list all users.** The panel shows only the viewer's own grants and the owners who shared
  with them; a username is checked one at a time, rate-limited server-side (10 per hour per user).
- Prototype: simulated in memory; the example owner (the guest owner) shares with „jakub", and Jana and Tomáš
  share with the guest owner.

## 6. Settings panel

Opened from the **Settings** gear button; a small form with grouped options:

| Group | Options |
|---|---|
| Language | Čeština · English |
| Appearance (cs "Vzhled") | **Dark (default)** · Light · System (cs: Tmavý · Světlý · Podle systému) |
| Units | Metric (kg, cm) · Imperial (lb, ft-in); Energy: kcal · kJ |

**Appearance:** the DEFAULT is dark, whatever the OS prefers. Dark and Light set `<html data-theme>` to
`dark` / `light`; **System** renders NO attribute and the `prefers-color-scheme` media block decides.
App: the choice is the 4th part of the `inbody_units` cookie (always written, `dark|light|system`;
an older cookie without it or an unknown value = dark), rendered server-side, so no flash. Prototype:
`theme.js` (first thing in `<body>`, no inline code) applies the stored choice before first paint,
`app.js` on change; kept in `localStorage`. The dark tokens exist twice with identical bodies (OS
media block + explicit `[data-theme="dark"]`; a test keeps them equal), every dark-specific rule
(tokens, brand red, quotes, charts via tokens, `color-scheme`) follows an explicit choice both ways.

Changes apply immediately: every chart, tooltip, table, axis unit tag, note and the report re-render.
In the app these are **per-user preferences stored server-side** and simply persist (the panel
shows no storage sentence; the prototype's italic "Prototype note: kept in this browser only." is
marked `data-prototype-only`, and **every element with `data-prototype-only` is a dev note the app
drops**). They are rendered into the page (e.g. `data-lang`, `data-mass`, `data-energy`,
`data-energy`); the prototype keeps them in
`localStorage` (wrapped in try/catch).

**Unit conversion lives in one place** (`charts.js`): rows arrive in SI; `toDisplay(row, units)`
converts every `*_kg` (and `*_kg_low/_high`) to lb (÷ 0.45359237) and every `*_kcal` to kJ (× 4.184);
`unitLabel(unit, S)` maps the stored unit to the shown one. Scales, deltas, tables, tooltips and the
report all work on the converted numbers. Lengths (cm → ft-in) only matter if a length is shown;
none is today.

## 7. Language (CS / EN)

Everything switches, including the charts: titles, legends, ⓘ texts, tooltips, axis months („bře
2025"), dates (`d. M. yyyy` → „26. 9. 2026"; EN "26 Sep 2026"), numbers via `Intl.NumberFormat`
(`cs-CZ`: „80,5 kg", „1 800 kcal", „20,0 %"; `en-GB`: "80.5 kg", "20.0%"), break labels („11 měs."),
tables, notes, buttons, the report. Minus signs are U+2212.

**Czech uses the informal form (tykání) everywhere**: „můžeš", „Zadej", „tvoje měření", „Nahraj",
„Přepni"…

**Terminology** (never translate "lean mass" as „svalová hmota"; it reads like SMM):

| English | Czech |
|---|---|
| Skeletal muscle (SMM) | Kosterní svaly (SMM) |
| Lean mass (incl. segmental) | Netuková hmota (e.g. „Netuková hmota – pravá ruka") |
| Fat-free mass (FFM) | Hmota bez tuku (FFM) |
| Other lean mass | Ostatní netuková hmota |
| Body fat | Tělesný tuk |

## 8. Chart module API (`charts.js`)

Global `window.InbodyCharts` (classic script, no build step, no dependencies). Requires `i18n.js`
before it (or pass `strings`).

```js
const view = InbodyCharts.mountHistory(rootElement, {
  measurements,                 // rows as in section 2, SI units
  lang: "cs",                   // or strings: InbodyI18n.cs
  units: { mass: "metric", energy: "kcal" },   // mass metric|imperial, energy kcal|kJ
  pauseMonths: 3,               // initial pause length (a pause = gap longer than N calendar months)
  state: {                      // all optional; defaults shown
    range: "all",               // "6m" | "1y" | "all" | "custom" (then from / to below)
    from: null, to: null,       // "YYYY-MM" months of the custom range (validated, clamped to the data)
    breaks: "auto",             // the page's checkbox: "auto" = Shrink pauses on | "none" = continuous axis
    pauseMonths: 3,             // 1..24, the page's number field (disabled while breaks is "none")
    comp: { pick: "all", mode: "kg", unit: "kg" },   // pick all|weight|smm|fat; mode kg|pct; unit kg|pct
    seg:  { pick: "arms", mode: "kg" }               // pick arms|legs|trunk|right_arm|left_arm|right_leg|left_leg; mode kg|pctw|pct|balance
  },
  onSelect(row) {},             // a point/bar was clicked or Enter pressed: open row.id's report
  onUpload() {},                // owner only: shows the Upload button in the n = 0 empty state
  onChange(state) {}            // persist the controls
});
view.update({ lang: "en" });    // or { units }, { pauseMonths }, { measurements }, { state }
view.destroy();
```

**Zero-JS-glue variant for server-rendered pages:** `charts.js` mounts every
`[data-inbody-history]` element on `DOMContentLoaded` and after each `htmx:afterSettle`:

```html
<div data-inbody-history data-source="history-json" data-lang="cs" data-mass="metric"
     data-energy="kcal" data-report-url="/report/{id}"></div>
<script type="application/json" id="history-json">{"measurements": [...]}</script>
<script src="/static/i18n.js"></script>
<script src="/static/charts.js"></script>
```

Lower-level pieces: `buildAxis(isoDates, {breaks, pauseMonths})` → `{segs, breaks, pauses, frac(t),
segOf(t), ticks(px, strings), isPause(a, b)}`; `lineChart(host, spec)`; `stackChart(host, spec)`;
units `toDisplay`, `withUnits`, `unitLabel`; formatting `fmtNum`, `fmtVal`, `withUnit`;
`compare(key, rows, idx)` (directional change); `METRICS` (unit and decimals per canonical column).

**CSP:** works under `default-src 'none'; script-src 'self'; style-src 'self'` (what
`serve_local.py` sends). No inline scripts, handlers or `style` attributes; dynamic positions use the
CSSOM (`el.style.left`); all text goes in via `textContent`. Colours come from CSS classes and
tokens, so a theme switch needs no re-render. Charts draw synchronously on first layout and redraw
on width change (ResizeObserver), including when a hidden tab becomes visible.

## 9. Running the prototype

- Example page (synthetic data, safe to publish): `python3 docs/prototype/build_example.py` →
  `docs/prototype/ui.html`, published as the artifact.
- Real data, local only: `.venv/bin/python docs/prototype/serve_local.py` → http://127.0.0.1:8765/.
  Builds the 12 measurements in memory from `samples/` (CSV via `validate.parse_csv`, sheets from
  `samples/expected/*.canonical.json`), applies the one-per-day rule in upload order (the Gym C
  screenshot's 8 rows are rejected: those days already exist from the CSV), sets the Gym B
  gym as if typed into the upload form, binds 127.0.0.1 only and writes nothing.
