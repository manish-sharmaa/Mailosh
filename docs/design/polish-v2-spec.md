# Polish v2 — implementation contract

**Status:** approved for implementation · **Date:** 2026-09-02 · **Scope:** Phase 1A visual polish
**Visual reference:** `docs/design/mockups/polish-v2.html` (open it — it is the reference
implementation; every rule below is live in it)
**Amends:** `docs/specs/2026-09-02-phase1-webmail-design.md` §4.1, §4.2, §5.1–§5.3

This is a **polish pass, not a redesign**. The frame, the Graphite & Blue accent, Inter, the list-first
Gmail layout, the three densities and the 52 px Comfortable default are all unchanged. What changes is
depth, contrast and feedback.

Files an implementer touches: `styles/input.css` (all of §1–§7 below) and five templates
(`shell/topbar.html`, `shell/nav.html`, `list/row.html`, `list/toolbar.html`, `layouts/app.html`) for the
handful of markup changes listed in §8. Nothing else.

---

## 0. The six diagnosed problems, and what fixes each

| # | Problem (measured on the shipped build) | Fix |
|---|---|---|
| 1 | `.btn-icon:hover` sets `background: var(--hover)` — **the same colour `.row:hover` already paints**. Rest and hover are pixel-identical: **1.000:1**. | §2 `--ctl-hover` / `--ctl-active` alpha washes + circular target + `--icon`→`--icon-strong` glyph step |
| 2 | No layering. Card/page = **1.083:1** light, **1.089:1** dark. No shadow tokens exist at all. | §1 deeper `--bg`, §3 four elevation tokens |
| 3 | Compose is a flat fill with `box-shadow: 0 1px 2px rgba(15,23,42,.18)` and no hover/active. In dark that shadow colour (`#0F172A`, luminance 0.0088) is **lighter than the page it sits on** (`#111318`, 0.0065) — it renders as a halo, not a shadow. | §4 `--accent-hover` / `--accent-press` / `--shadow-accent` |
| 4 | Nothing moves. `.btn-icon` and `.nav-item` declare `transition-property` with **no `transition-duration`** anywhere in the sheet; CSS's initial `transition-duration` is `0s`, so those transitions never run. `--default-transition-duration` is a Tailwind *theme* variable — it only applies to the `transition` utility class, which no template uses. | §6 explicit `transition` shorthands with `--dur-*` |
| 5 | Weak controls. Checkbox outline is `--line-2` at `opacity: .6` → **1.23:1** light / **1.38:1** dark against the row (AA non-text needs 3:1). `--fg-3` dates/previews are **2.86:1** light / **4.07:1** dark (AA text needs 4.5:1). | §1 `--ctl-line`, raised `--fg-2`/`--fg-3`, §5 control rules |
| 6 | Flat rhythm. Row hover is **1.048:1** light — *less* of a step than the read/unread difference itself (1.073:1). Nav hover is **1.038:1**, i.e. imperceptible. Active nav pill `#E8F0FE` on `#F4F6F8` is **1.014:1** — invisible. | §1 widened `--hover`/`--accent-soft`, §5 full-width nav pills, row hover lift |

All ratios above and below are computed with the WCAG 2.x sRGB relative-luminance formula, compositing
alpha colours onto their real background first. They are measurements, not estimates.

---

## 1. Tokens

Three blocks in `styles/input.css` keep their existing structure and activation conditions: `:root`
(light), `:root[data-theme=dark]`, and `@media (prefers-color-scheme: dark) :root:not([data-theme=light])`.
**Every dark value below must be written into both dark blocks**, exactly as today.

### 1.1 Changed tokens

| Token | Light — was → now | Dark — was → now | Why |
|---|---|---|---|
| `--bg` | `#F4F6F8` → **`#EBEEF3`** | `#111318` → **`#0D1015`** | The page ground has to sit *below* the card. Card/page goes 1.083→**1.163** light, 1.089→**1.169** dark. |
| `--surface` | `#FFFFFF` (unchanged) | `#191C22` → **`#1C2029`** | Lighter = nearer (Carbon). Widens both the card step and the read/unread step. |
| `--read` | `#F5F7FA` → **`#F6F8FB`** | `#14171D` → **`#15191F`** | Small nudge; the read/unread signal is carried by weight, the *hover* gets the big delta. |
| `--hover` | `#EEF2F7` → **`#E4EAF3`** | `#1C2028` → **`#272D39`** | Row hover 1.048→**1.137** light, 1.100→**1.277** dark. This is the single most visible change. |
| `--field` | `#E9EEF4` → **`#E4E9F0`** | `#1F232B` → **`#22262F`** | Search pill stays recessed against the deeper `--bg`. |
| `--line` | `#E3E8EE` → **`#DDE3EB`** | `#282C35` → **`#2C313C`** | Hairlines that actually read: 1.291 vs `--surface` light, 1.251 dark. |
| `--line-2` | `#C9D2DC` → **`#C3CBD6`** | `#3D4350` (unchanged) | Keyboard-cap and floating-layer edges. |
| `--fg-2` | `#5A6472` → **`#4E5867`** | `.65w` → **`rgba(255,255,255,.72)`** | 6.00→**7.20** on white. Read-row senders and toolbar text. |
| `--fg-3` | `#8A94A3` → **`#616A79`** | `.42w` → **`rgba(255,255,255,.56)`** | 12 px dates and Comfortable preview lines are body text: 2.86→**5.13** light, 4.07→**6.25** dark. Carbon's 42 % is a *disabled* value, not a readable one — that role moves to `--fg-disabled`. |
| `--accent-soft` | `#E8F0FE` → **`#D6E4FA`** | `rgba(138,180,248,.16)` → **`#2A3446`** | Selected rows + active nav. Light was invisible on the nav (1.014); now **1.105** there and **1.208** on a read row. The dark value is the opaque resolution of what the alpha already produced over `--surface` (`#2B3444`) — same colour, now identical on the nav and on rows. |
| `--toast` | `#1F2937` → **`#111827`** | `#F3F4F6` (unchanged) | Matches `--fg`; one fewer near-black in the palette. |

Unchanged and deliberately so: `--accent-ink`,
`--on-accent` `#FFFFFF`/`#0D1015` (dark follows `--bg`), `--fg` `#111827`/`rgba(255,255,255,.92)` (light
untouched; dark .90→.92, a rounding nudge), `--star` `#F59E0B`/`#FBBF24`, `--danger`, `--warn`,
`--success`, `--toast-fg`, and all twelve `--label-*` colours.

### 1.2 New tokens

**Interactive glyphs and control washes** — the headline fix.

| Token | Light | Dark | Purpose |
|---|---|---|---|
| `--icon` | `#4E5867` | `rgba(255,255,255,.74)` | Resting colour of every interactive glyph |
| `--icon-strong` | `#111827` | `rgba(255,255,255,.95)` | Same glyph on hover / press / active |
| `--icon-quiet` | `#616A79` | `rgba(255,255,255,.60)` | Star-off, paperclip, `kbd` — informational, still ≥3:1 |
| `--ctl-line` | `#7D8797` | `rgba(255,255,255,.38)` | Checkbox / control outline |
| `--ctl-hover` | `rgba(17,24,39,.07)` | `rgba(255,255,255,.09)` | Icon-button and nav-pill hover background |
| `--ctl-active` | `rgba(17,24,39,.13)` | `rgba(255,255,255,.15)` | …and pressed background |
| `--fg-disabled` | `#9AA2AF` | `rgba(255,255,255,.38)` | Genuinely inactive controls (exempt from contrast minima) |

`--ctl-hover`/`--ctl-active` are **alpha**, not opaque, on purpose: the same token has to lift off the top
bar (`--bg`), the toolbar (`--surface`), a read row and an already-hovered row. An opaque value can only
be correct on one of them — which is exactly how the current bug happened.

**Accent states**

| Token | Light | Dark | Purpose |
|---|---|---|---|
| `--accent-hover` | `#1A5CC8` | `#A8C7FA` | Primary-button hover. Light darkens, dark **lightens** (nearer). |
| `--accent-press` | `#164EA8` | `#6E9CEC` | Primary-button press |
| `--accent-wash` | `rgba(29,106,229,.07)` | `rgba(138,180,248,.10)` | Focused-row tint, layered over the row's own background |

**Status**

| Token | Light | Dark | Purpose |
|---|---|---|---|
| `--star-ink` | `#B45309` | `#FBBF24` | Stroke on a starred star. The amber *fill* is only 2.02:1 on a read row — the identity colour cannot pass 3:1 on its own, so the shape is outlined instead. |
| `--toast-link` | `#7FB0FF` | `#1D4ED8` | The toast is an inverted surface: it takes the *other* theme's accent. |

**Elevation** (new — Task 2 defined none)

| Token | Light | Dark |
|---|---|---|
| `--shadow-1` | `0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.10)` | `0 1px 2px rgba(0,0,0,.55), 0 1px 3px rgba(0,0,0,.40)` |
| `--shadow-2` | `0 2px 4px -2px rgba(16,24,40,.06), 0 6px 14px -4px rgba(16,24,40,.12)` | `0 2px 6px rgba(0,0,0,.55), 0 8px 20px -6px rgba(0,0,0,.55)` |
| `--shadow-3` | `0 4px 10px -4px rgba(16,24,40,.10), 0 18px 44px -12px rgba(16,24,40,.24)` | `0 6px 14px -6px rgba(0,0,0,.50), 0 24px 56px -12px rgba(0,0,0,.72)` |
| `--shadow-accent` | `0 1px 2px rgba(23,78,166,.24), 0 4px 10px -3px rgba(29,106,229,.32)` | `0 1px 2px rgba(0,0,0,.60), 0 4px 12px -4px rgba(0,0,0,.50)` |
| `--shadow-accent-hover` | `0 2px 4px rgba(23,78,166,.26), 0 8px 18px -4px rgba(29,106,229,.42)` | `0 2px 4px rgba(0,0,0,.62), 0 8px 20px -6px rgba(0,0,0,.55)` |

Assignment: **`--shadow-1`** = a hovered row (and nothing else in 1A). **`--shadow-2`** = menus,
popovers, the docked compose. **`--shadow-3`** = genuinely floating layers — toast, command palette,
modal, full-screen compose. **Dark rule:** a shadow on a `#0D1015` ground carries almost no information,
so every dark elevation must be paired with a *lighter surface value* and, for `--shadow-2`/`--shadow-3`,
a `1px solid var(--line-2)` hairline. The edge is what reads, not the blur.

**Surface**

| Token | Light | Dark | Purpose |
|---|---|---|---|
| `--surface-2` | `#FFFFFF` | `#22262F` | Raised surface for menus / dock / palette (the lighter-is-nearer step) |

**Shape and motion**

```
--radius-pill: 999px;          /* nav items, icon buttons, Compose */
--dur-1: 120ms;                /* micro: icon button, nav pill, checkbox, star */
--dur-2: 160ms;                /* row hover, primary button */
--dur-3: 200ms;                /* panels, menus, dock */
--ease:     cubic-bezier(.2, 0, 0, 1);      /* existing default, now named */
--ease-out: cubic-bezier(.16, 1, .3, 1);    /* entrances only */
```

Keep `--radius-kbd/ctl/field/card/palette` and `--row-h` / `--row-lines` **exactly as they are**. The
density system is untouched by this pass.

### 1.3 `@theme inline` additions

Add to the existing `@theme inline` block so Tailwind utilities can reach the new colours:

```
--color-icon: var(--icon);
--color-icon-strong: var(--icon-strong);
--color-icon-quiet: var(--icon-quiet);
--color-ctl-line: var(--ctl-line);
--color-fg-disabled: var(--fg-disabled);
--color-surface-2: var(--surface-2);
--radius-pill: var(--radius-pill);
--shadow-1: var(--shadow-1);
--shadow-2: var(--shadow-2);
--shadow-3: var(--shadow-3);
```

`--ctl-hover`, `--ctl-active`, `--accent-wash`, `--star-ink`, `--toast-link`, `--shadow-accent*` and the
`--dur-*`/`--ease*` tokens stay **plain custom properties** consumed via `var()` by the component rules
below — like `--toast` and the twelve `--label-*` already do. They have no sensible utility form.

---

## 2. Icon buttons — `.btn-icon`

Replace the current rule wholesale.

```css
.btn-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-pill);        /* was --radius-ctl */
  color: var(--icon);                        /* was --fg-2 */
  transition: background-color var(--dur-1) var(--ease),
              color            var(--dur-1) var(--ease),
              box-shadow       var(--dur-1) var(--ease);
}
.btn-icon:hover  { background: var(--ctl-hover);  color: var(--icon-strong); }
.btn-icon:active { background: var(--ctl-active); color: var(--icon-strong); }
.btn-icon:focus-visible { outline-offset: 1px; }   /* 2px would clip inside a 52px row */

.nav-item[aria-disabled=true],
.btn-icon[aria-disabled=true],
[role=search][aria-disabled=true] { cursor: default; }
.btn-icon[aria-disabled=true] { color: var(--fg-disabled); }
.btn-icon[aria-disabled=true]:hover,
.btn-icon[aria-disabled=true]:active { background: transparent; color: var(--fg-disabled); }
.nav-item[aria-disabled=true] { opacity: .55; }
```

Note the disabled treatment changes from `opacity: .55` to a **token colour**. Stacking `.55` opacity on
`--fg-2` produced an effective 36 % white in dark, which is why every top-bar control read as disabled —
including the ones that are not.

Sizes: 32 px in the top bar, 28 px in the toolbar and in row hover actions (templates currently say
`size-7` / `size-[26px]`; move both to 28 px — see §8).

**Measured (light):** glyph `#4E5867` on a hovered row = **5.95:1**, on the toolbar = **7.20:1**, on the
top bar = **6.19:1**; hover glyph `#111827` = **14.7:1**. Hover wash on a hovered row →
`#D5DBE5` = **1.150:1** delta; press → `#C9CFD8` = **1.296:1**. Deltas on the top bar (1.148 / 1.302) and
the toolbar (1.151 / 1.309) are within 0.01 of those — the point of using alpha.

**Measured (dark):** glyph `.74w` on a hovered row = **8.26:1**, toolbar **9.44:1**, top bar **10.57:1**;
hover glyph `.95w` = **12.6:1**. Hover wash → `#3A404B` = **1.325:1**; press → `#474C57` = **1.604:1**.

Every one of these clears WCAG 2.2 AA non-text contrast (3:1) with a wide margin, and hover, press and
focus are each visibly distinct from rest and from each other.

---

## 3. The frame

### 3.1 Main surface (`layouts/app.html`'s `<main>`)

No geometry change. It keeps `rounded-tl-card border-t border-l border-line bg-surface`. The card now
reads because `--bg` dropped, not because a shadow was added — a shadow there would bleed off two
viewport edges and render as a smudge in one corner only. **Deliberately not changed.**

### 3.2 Top bar

Keeps `border-b border-line bg-bg`. The search pill gains a resting hairline so it reads as a field
against the deeper ground:

```css
[role=search] {
  border: 1px solid var(--line);
  transition: background-color var(--dur-1) var(--ease),
              border-color     var(--dur-1) var(--ease),
              box-shadow       var(--dur-1) var(--ease);
}
[role=search]:hover:not([aria-disabled=true]) { border-color: var(--line-2); }
```

(When search ships in 1D, focus adds `border-color: var(--accent)` + `box-shadow: 0 0 0 3px var(--accent-wash)`.)

---

## 4. Primary action — `.compose-btn`

```css
.compose-btn {
  display: flex;
  align-items: center;
  gap: 9px;
  height: 38px;                              /* was 36 */
  margin: 2px 0 12px;                        /* was 4px 8px 10px 0 — full rail width now */
  padding: 0 14px;
  border-radius: var(--radius-pill);         /* was --radius-field */
  background: var(--accent);
  color: var(--on-accent);
  font-weight: 600;
  letter-spacing: -.005em;
  box-shadow: var(--shadow-accent);
  transition: background-color var(--dur-2) var(--ease),
              box-shadow       var(--dur-2) var(--ease);
}
.compose-btn:hover  { background: var(--accent-hover); box-shadow: var(--shadow-accent-hover); }
.compose-btn:active { background: var(--accent-press); box-shadow: var(--shadow-1); }
.compose-btn[aria-disabled=true] { opacity: .6; cursor: default; }
.compose-btn[aria-disabled=true]:hover  { background: var(--accent); box-shadow: var(--shadow-accent); }
.compose-btn .kbd {
  margin-left: auto;
  background: rgba(255, 255, 255, .16);
  border-color: rgba(255, 255, 255, .32);
  border-bottom-width: 1px;
  color: var(--on-accent);
}
:root[data-theme=dark] .compose-btn .kbd { background: rgba(0,0,0,.14); border-color: rgba(0,0,0,.28); }
/* …and the same inside the prefers-color-scheme dark block. */
```

The press state **shrinks** the shadow rather than translating the button — depth, not bounce. No
`transform` anywhere on hover or press in this pass.

**Measured:** label on fill **4.95:1** light / **9.04:1** dark; on hover **6.16 / 11.08**; on press
**7.81 / 6.92**. Fill-vs-fill state deltas: hover **1.245 / 1.226**, press **1.579 / 1.306** — each state
is distinguishable from rest without looking at the shadow.

The same three-state pattern applies to any future filled primary (Send, in the compose dock).

---

## 5. Nav, list and controls

### 5.1 Nav — `.nav-item`

```css
.nav-item {
  display: flex;
  align-items: center;
  gap: 11px;
  height: 32px;
  padding: 0 12px;
  border-radius: var(--radius-pill);         /* was --radius-ctl */
  color: var(--fg-2);
  font-weight: 500;
  transition: background-color var(--dur-1) var(--ease),
              color            var(--dur-1) var(--ease);
}
.nav-item svg { color: var(--icon); }
.nav-item:hover     { background: var(--ctl-hover); color: var(--fg); }
.nav-item:hover svg { color: var(--icon-strong); }
.nav-item[aria-current=page] {
  background: var(--accent-soft);
  color: var(--accent-ink);
  font-weight: 600;
}
.nav-item[aria-current=page] svg { color: var(--accent); }
/* keep the active fill visible under a hover rather than replacing it */
.nav-item[aria-current=page]:hover { box-shadow: inset 0 0 0 999px var(--ctl-hover); }
.nav-item:focus-visible { outline-offset: -2px; }   /* full-bleed pill: inset the ring */
```

The `<nav>` in `layouts/app.html` keeps `px-2`, so a pill is the full 208 px of the rail — Gmail's and
Proton's full-width pill, and the reason the hover finally reads.

**Measured:** active fill vs the nav ground **1.105** light / **1.523** dark (was 1.014 / 1.35); label
`--accent-ink` on it **6.10 / 8.61**; icon `--accent` on it **3.85 / 5.94**. Hover wash on the nav ground
**1.148 / 1.254**.

`.nav-heading` moves to `font-size: 10.5px; font-weight: 700; letter-spacing: .08em; padding: 16px 12px 6px`.

### 5.2 List rows — `.row`

A row carries two shadows that are independent of each other — the focus/open accent bar and the hover
lift — and one quiet-text colour that the tinted states have to raise. All three compose through custom
properties rather than competing for a single declaration, so the result does not depend on rule order.

```css
.row {
  --row-bar:   inset 0 0 0 transparent;       /* focus / open cursor */
  --row-lift:  0 0 0 transparent, 0 0 0 transparent;   /* hover elevation */
  --row-quiet: var(--fg-3);                   /* 12px date + Comfortable preview */
  gap: 12px;                                  /* was 10 */
  background: var(--read);
  border-bottom: 1px solid var(--line);
  box-shadow: var(--row-bar), var(--row-lift);
  position: relative;
  transition: background-color var(--dur-2) var(--ease),
              box-shadow       var(--dur-2) var(--ease),
              border-color     var(--dur-2) var(--ease);
}
.row.is-unread { background: var(--surface); }

/* every tinted row raises its quiet text — see the measurement below */
.row.is-focused,
.row.is-selected,
.row[aria-current=true] { --row-quiet: var(--fg-2); }

.row:hover {
  --row-lift: var(--shadow-1);
  background: var(--hover);
  border-bottom-color: transparent;
  z-index: 1;                                 /* so the shadow paints over its neighbours */
}
/* the row above a hovered row drops its hairline too, so the lift has a clean top edge */
.row:has(+ .row:hover) { border-bottom-color: transparent; }

/* keyboard cursor: the accent bar keeps its identity, the wash makes it legible without
   stealing the selected state's stronger fill. A gradient layer, not a background-color,
   so the read/unread colour underneath survives. */
.row.is-focused {
  --row-bar: inset 3px 0 0 var(--accent);
  background-image: linear-gradient(var(--accent-wash), var(--accent-wash));
}
.row.is-selected      { background: var(--accent-soft); }
.row[aria-current=true] { --row-bar: inset 3px 0 0 var(--accent); background: var(--accent-soft); }

.row-date                              { color: var(--row-quiet); }
[data-density=comfortable] .row-preview { color: var(--row-quiet); }
```

`.row:nth-child(n+101) { content-visibility: auto; … }` is unchanged.

`:has()` is baseline in every browser this app supports; where it is missing the only consequence is that
the hairline above a hovered row stays drawn — harmless, no layout change.

**Why `--row-bar`/`--row-lift` rather than four `box-shadow` declarations.** `.row.is-focused` and
`.row[aria-current=true]` are the same specificity as `.row:hover` and are written after it, so writing
the bar as its own `box-shadow` made it win outright: a hovered row that was also the keyboard cursor
lost its elevation. Since app.js puts `.is-focused` on row 1 at load, that gave the **first row a reader
hovers** the weakest hover feedback in the list. Both rest values keep the layer count and the `inset`
flag of the values that replace them (`--shadow-1` is a two-layer shadow), which is what lets
`transition: box-shadow` interpolate rather than snap.

**Measured row-state ladder (light / dark):** read→hover **1.137 / 1.277** (was 1.048 / 1.100);
read→unread 1.064 / 1.082; read→selected **1.208 / 1.409**; focused wash delta **1.097 / 1.192**.

**Measured quiet text.** `--fg-3` is body text at 12 px and owes 4.5:1, and it does not pay it on a
tinted row. Across the eight grounds a row can present — `--surface` / `--read` / `--hover` /
`--accent-soft`, each with and without the focus wash — `--fg-3` measures 5.46 / 5.13 / 4.51 / **4.25**
and 4.97 / 4.68 / **4.14** / **3.91** in light, and 6.00 / 6.24 / 5.44 / 5.10 and 5.35 / 5.66 / 4.79 /
**4.49** in dark. `--fg-2` on the same eight is 7.20 / 6.77 / 5.95 / 5.60 and 6.55 / 6.17 / 5.46 / **5.15**
light, 9.01 / 9.54 / 7.93 / 7.32 and 7.77 / 8.34 / 6.77 / **6.26** dark — every ground clears, with the
worst case moving 3.91 → 5.15. In Compact and Standard the preview line is already `--fg-2` (it inherits
`.row-text`), so this only aligns Comfortable with them on the rows that are tinted.

### 5.3 Checkbox — `.check-box` and `.row-check`

Both get one rule. Drop `opacity: .6` entirely — it is what destroyed the contrast.

```css
.check-box, .row-check {
  width: 16px; height: 16px;                  /* was 15 */
  border: 1.5px solid var(--ctl-line);        /* was --line-2 */
  border-radius: 4px;
  background: var(--surface);
  color: transparent;
  transition: background-color var(--dur-1) var(--ease),
              border-color     var(--dur-1) var(--ease),
              color            var(--dur-1) var(--ease);
}
.check-box:hover, .row-check:hover,
.row:hover .row-check { border-color: var(--icon-strong); }

.check-box[aria-checked=true], .row-check[aria-pressed=true] {
  background: var(--accent); border-color: var(--accent); color: var(--on-accent);
}
.check-box[aria-checked=mixed] { border-color: var(--accent); color: var(--accent); }

/* on a tinted row an unchecked box needs the accent edge to stay at 3:1 */
.row.is-selected .row-check,
.row[aria-current=true] .row-check { border-color: var(--accent); }
```

The tri-state glyph rules (`.check-all` / `.check-some`) are unchanged.

**Measured:** outline **3.63** on `--surface`, **3.41** on a read row, **3.00** on a hovered row (light);
**3.53 / 3.55 / 3.34** (dark). Was **1.23 / 1.38**. On a selected row the accent edge measures **3.85 /
5.94**.

### 5.4 Star — `.row-star`

```css
.row-star { color: var(--icon-quiet); transition: color var(--dur-1) var(--ease); }
.row-star:hover { color: var(--icon-strong); }
.row-star.is-on { color: var(--star-ink); }
.row-star.is-on svg { fill: var(--star); stroke: var(--star-ink); stroke-width: 1.5; }
```

The amber fill stays the identity colour; the darker stroke is what carries the contrast in light.
**Measured** (over `--surface`, `--read`, `--hover` and a selected row): off-star **4.25–5.46** light,
**5.62–7.19** dark. On-star outline `#B45309` **3.91–5.02** light; `#FBBF24` **7.50–10.56** dark. The
amber fill on its own would have been **1.67–2.15** in light — which is why it is stroked.

### 5.5 Toolbar and row text

```css
.list-toolbar { gap: 2px; padding: 0 8px 0 14px; color: var(--fg-2); }
.toolbar-sep  { height: 18px; margin: 0 7px; }   /* was 20px / 0 4px — real grouping */
.row-end      { width: 88px; }                   /* was 84 — three 28px buttons + gaps */
.chip-more    { background: var(--field); color: var(--fg-2); }
```

Everything else in `.row-from` / `.row-text` / `.row-subject` / `.row-preview` / `.row-date` is unchanged
and simply inherits the new `--fg-2` / `--fg-3`.

### 5.6 Label chips — `.chip`

```css
.chip { background: color-mix(in srgb, var(--chip) 14%, transparent);
        color:      color-mix(in srgb, var(--chip) 58%, var(--fg)); }
:root[data-theme=dark] .chip { background: color-mix(in srgb, var(--chip) 18%, transparent);
                               color: var(--chip); }
/* …and the same inside the prefers-color-scheme dark block. */
```

`row.html` sets `--chip: var(--label-{{color}})` as an inline custom property instead of writing
`background`/`color` directly (§8).

This is a partial fix and is flagged as such. On a read row, light chip text goes from **1.70–3.80:1**
to **4.04–6.81:1**; across all four row states the range is **3.42–7.20:1**. Dark chip text is
**4.49–7.59:1** on a read row and dips to **3.20:1** for indigo on a *selected* row; it is otherwise
untouched. Ten of the twelve light labels now clear AA (4.5:1) on a read row — **amber (4.25) and lime
(4.04) do not**, and on a hovered row emerald, sky, teal and orange join them between 4.09 and 4.47.
Closing that last gap means desaturating the twelve-colour label palette, which is a palette decision,
not a polish pass — see §9.

### 5.7 Toast

```css
.toast { box-shadow: var(--shadow-3); }
.toast a { color: var(--toast-link); font-weight: 600; }
```

---

## 6. Motion

`--dur-1/2/3` and `--ease`/`--ease-out` are declared once in `:root` (they are theme-independent).
The `@media (prefers-reduced-motion: reduce)` block in `@layer base` already zeroes every
`transition-duration` and `animation-duration` with `!important`, and it keeps doing so unchanged —
nothing below needs its own guard.

| Interaction | Properties | Duration | Easing |
|---|---|---|---|
| Icon button, nav pill, checkbox, star — hover / press | `background-color, color, border-color` | `--dur-1` 120 ms | `--ease` |
| List row — hover in/out | `background-color, box-shadow, border-color` | `--dur-2` 160 ms | `--ease` |
| Primary button — hover / press | `background-color, box-shadow` | `--dur-2` 160 ms | `--ease` |
| Search field — hover / focus | `background-color, border-color, box-shadow` | `--dur-1` 120 ms | `--ease` |
| Menus, popovers, quick settings | `opacity, transform` (2 px rise) | `--dur-3` 200 ms | `--ease-out` |
| Toast in / out | `opacity, transform` (6 px rise) | 200 ms in · 150 ms out | `--ease-out` |
| Focus ring | — | 0 ms (instant) | — |
| Skeleton fade-in | `opacity`, 300 ms delay | 120 ms | linear |
| `prefers-reduced-motion: reduce` | all | **0 s** | — |

Rules: never animate `transform` on hover or press; never animate a layout property; one easing family;
no decorative or looping motion beyond the two that already exist (`om-spin`, `om-shimmer`).

Keep `--default-transition-duration` / `--default-transition-timing-function` in `@theme inline` — they
are what Tailwind's `transition` utility uses — but **do not rely on them in `@layer components`**. Every
component rule writes an explicit `transition` shorthand. That is the whole of problem 4.

---

## 7. Focus

`:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }` in `@layer base` stays as the
default. Two documented exceptions, both because 2 px of offset would collide with a neighbour:

* `.btn-icon:focus-visible { outline-offset: 1px; }`
* `.nav-item:focus-visible { outline-offset: -2px; }` (a full-bleed pill has no room outside itself)

`.row.is-focused` is the roving-tabindex **cursor**, not a focus ring, and keeps its inset accent bar;
a row that additionally takes DOM focus still gets the standard outline on top.

**Measured ring contrast:** `--accent` against `--bg` / `--surface` / `--read` / `--hover` /
`--accent-soft` = **4.25 / 4.95 / 4.65 / 4.09 / 3.85** light and **9.04 / 7.74 / 8.37 / 6.55 / 5.94**
dark. All ≥3:1 on every surface a focusable control can sit on.

---

## 8. Template changes (the only ones)

1. **`layouts/app.html`** — `<meta name="theme-color">` currently hardcodes `#F4F6F8` / `#111318`.
   Update all three occurrences to **`#EBEEF3`** / **`#0D1015`** to match the new `--bg`.
2. **`list/row.html`** — row action buttons: `class="btn-icon size-[26px]"` → `class="btn-icon size-7"`
   (28 px). Paperclip: `text-fg-3` → `text-icon-quiet`.
3. **`list/row.html`** — chips: replace the inline `style="background: color-mix(…); color: var(--label-…)"`
   with `style="--chip: var(--label-{{ color }})"` and let §5.6's `.chip` rule do the work.
4. **`list/toolbar.html`** — `size-7` on the icon buttons is already 28 px; only the two disabled pager
   `<span>`s change: drop `opacity-40` and add `aria-disabled="true"` so they take the `--fg-disabled`
   treatment from §2 instead of a compounding opacity.
5. **`shell/nav.html`** — no markup change. `.compose-btn`'s own margin now spans the rail; the
   `<span class="ml-auto opacity-80">` around the `c` cap can drop `opacity-80` (the kbd rule in §4
   already handles its colour).
6. **`shell/topbar.html`** — no markup change.

Nothing in `app.js`, `keys.js`, `actions.js`, `sse.js` or `palette.js` changes. No class names used by JS
(`is-selected`, `is-focused`, `is-unread`, `row-check`, `row-star`, `data-action`, `data-role`) change.

---

## 9. Deliberately not changed

* **Layout metrics.** Row heights 36/44/52, nav 224 px, top bar 52 px, toolbar 44 px, sender column
  170/140 px, the density system. A polish pass must not move furniture.
* **The accent hue was later retuned** (see the amendment at the end of this
  document). Still one accent, as specified — a bluer one.
* **The twelve label colours.** See §5.6 — amber and lime still miss AA on their own light chip wash
  (4.25 and 4.04 on a read row), and four more dip below it on a hovered row. Fixing that properly means
  a desaturated light label ramp (each colour's 700 shade for chip text, the pure hue kept for the nav
  dot). That is a palette decision, tracked separately.
* **A shadow on the main card.** It bleeds off two viewport edges; the deeper `--bg` does the job.
* **Transforms on press.** A 1 px translate is cheap and popular; it is also the thing that makes a UI
  feel like a template. Depth is carried by the shadow instead.
* **`--fg` in light, `--star` in both, `--danger`/`--warn`/`--success`, radii, the icon set, the
  skeleton, `om-spin` / `om-shimmer`.**

---

## 10. Verification checklist

1. Both dark blocks in `styles/input.css` carry identical values (they are two different activation
   conditions, not a duplication slip — the existing file comment explains why).
2. `grep -c "transition:" styles/input.css` ≥ 10, and no `transition-property` is left without a
   matching duration.
3. Hover a row in the running app in both themes: the row background visibly changes *and* an icon
   button inside it visibly changes when the pointer moves onto it. These are two different changes.
4. Tab through the top bar, nav and toolbar: every stop shows a ring; no ring is clipped by a neighbour.
5. Set `prefers-reduced-motion: reduce` in DevTools → every state change is instant.
6. Switch density to Compact and Standard: rows are 36 px and 44 px, one line, nothing else moves.
7. Compare against `docs/design/mockups/polish-v2.html` side by side at 1440 px.



---

## Amendment — the accent ramp

The original ramp (`#1D6AE5` light / `#8AB4F8` dark) read as
under-saturated: the light blue leaned cyan and the dark one was a pale
periwinkle that made every primary control look tentative. The accent is
now **`#1a73e8`** — one brand blue, present in both themes rather than
replaced after dark by a tint standing in for it.

| Token | Light | Dark |
|---|---|---|
| `--accent` | **`#1A73E8`** | **`#1E6FD6`** (the same blue, dimmed so it does not vibrate on a near-black ground) |
| `--accent-hover` | `#1667D2` | `#1A73E8` (dark brightens *toward* the canonical brand blue) |
| `--accent-press` | `#1259B8` | `#195FBB` |
| `--accent-ink` | `#1258C4` | `#A9C9F8` |
| `--accent-soft` | `#DCE9FC` | `#1B2942` |
| `--accent-wash` | `rgba(26,115,232,.08)` | `rgba(42,128,234,.16)` |
| `--on-accent` | `#FFFFFF` | **`#FFFFFF`** (was `#0D1015`) |
| `--toast-link` | `#7FB3F5` | `#1667D2` |

**`--on-accent` in dark changed, and that is the point.** The previous
dark accent was pale because it had to carry *near-black* text; that is
what made it look washed out. `#1E6FD6` is deep enough to carry white at
**4.88:1**, so dark now uses the same white label as light and the two
themes finally show the same brand colour.

Two values were chosen by the arithmetic rather than by taste:

* **Dark hover cannot lighten freely.** Lightening reduces contrast with
  a white label, and the obvious step (`#2A80EA`) measured **3.91:1** and
  failed. Hover is therefore `#1A73E8` — still a brightening, and exactly
  the light theme's accent, which is a happier accident than the rule that
  produced it.
* **`#1a73e8` with white is 4.51:1** in light — it passes AA, but with
  little headroom. It is the value Google ships for the same purpose, and
  hover and press both darken (5.38 and 6.67), so every state a pointer
  reaches is more comfortable than rest. Worth knowing before anyone
  lightens it.

Measured after the change, both themes, every ratio at or above floor:

| | Light | Dark |
|---|---|---|
| Button label on accent / hover / press | 4.51 / 5.38 / 6.67 | 4.88 / 4.51 / 6.18 |
| Accent vs card (ring, links, fill edge) | 4.51 | 3.34 |
| `--accent-ink` on `--accent-soft` | 5.31 | 8.59 |
| `--row-quiet` on a selected row | 5.87 | 8.23 |
| `--row-quiet` on focus wash over hover | 5.40 | 6.85 |
