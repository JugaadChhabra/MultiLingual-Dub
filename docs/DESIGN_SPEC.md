# AutoDub — Web UI Design Specification

> Describes the visual language, tokens, components, and interaction rules as shipped.
> Both web surfaces now share one token layer (`static/style.css`); keep new work inside it.

| Surface | Files | Audience | Role |
|---|---|---|---|
| **AutoDub Studio** | `index.html` + `static/style.css` + `static/app.js` | Internal operators running the dub pipeline | Excel → multi-language audio batch |
| **Video Studio** | `static/heygen.html` (page-specific CSS/JS inline, tokens shared) | HeyGen avatar-video operators | Image + script → avatar video, single or batch |

Both read as the same product: dark near-black base, generative dithered canvas, mono
telemetry, one amber accent, and a single cream "physical" CTA per screen. They differ in
*layout*, not in language — Studio is a two-column control desk, Video Studio is a
stage-plus-rail monitor.

---

## 1. Tokens (`:root` in `static/style.css`)

### 1.1 Text ramp — four steps, contrast-checked against `--bg`

| Token | Value | Contrast | Use |
|---|---|---|---|
| `--ink` | `#F4F3F1` | 17.9:1 | Primary text, values, active labels |
| `--muted` | `#A8A8B4` | 8.4:1 | Secondary text, guidance, inactive control labels |
| `--faint` | `#7A7A88` | 4.7:1 | Section labels, hints, codes, timestamps — the **floor for anything readable** |
| `--ghost` | `#55555F` | 2.6:1 | Decoration only: frame ticks, inert glyphs. Never a word a user must read. |

`--faint` was `#54545E` (2.6:1) and carried instructions; it now passes AA. If a string
matters, it lives on `--faint` or brighter.

### 1.2 Surfaces, lines, state

| Token | Value | Role |
|---|---|---|
| `--bg` / `--bg-2` / `--panel` | `#0A0A0C` / `#0D0D11` / `#101015` | Page, secondary surface, panel |
| `--line` / `--line-2` / `--line-hi` | `#1E1E24` / `#161619` / `#34343D` | Borders, hairlines, hover borders |
| `--accent` / `--accent-dim` | `#FFB570` / `rgba(255,181,112,.12)` | The only accent: active, selected, focus, in-progress |
| `--ok` / `--err` | `#7CF5C4` / `#FF8A80` | Success / failure |

**Accent discipline.** One accent, roughly 60/30/10. Every *selected* or *active* control
is amber, on both surfaces — language tiles, category chips, mode segment, tabs, toggles.
The iridescent 96-step ramp (`teal → sky → violet → butter → salmon → orchid → white`,
built in JS) encodes **identity, not state**: it colours the background canvas pings and
per-language progress bars. It must never be used to render a selected state, which is
what made the mosaic read as a string of Christmas lights.

### 1.3 Type

| Family | Token | Usage |
|---|---|---|
| Space Grotesk (400–700) | `--sans` | UI, buttons, metrics |
| Instrument Serif (italic) | `--serif` | One flourish: switch descriptions |
| JetBrains Mono (400–700) | `--mono` | All telemetry: labels, codes, logs, hints |
| Noto Sans + Indic | `--native` | Native-script names |

Scale — `--fs-xs 10 · --fs-sm 11 · --fs-md 13 · --fs-lg 15 · --fs-xl 21 · --fs-2xl 28`
(≈1.3 ratio), plus `--fs-native 19px` because Indic scripts need ~1.25× the Latin size to
match apparent x-height. **Use the tokens, not raw px.** The previous ten near-identical
half-pixel sizes (9.5/10/10.5/11/11.5/12/12.5/13/13.5) gave the mono layer no hierarchy.

Convention: lowercase mono labels, letter-spacing `.02–.14em`, often uppercased. The
`label + hairline rule + count` header (`.lbl` → `text · <rule> · .n`) organizes every
section. Live numbers use `tabular-nums`.

---

## 2. Interaction contract (applies to both surfaces)

These are not suggestions — they are what the last audit fixed.

- **Real primitives.** Anything clickable is a `<button>` / `<a>`. Dropzones are buttons
  wrapping the label, with the `<input type="file">` as a sibling. No `<div onclick>`, and
  never a click target nested inside another button.
- **State on the attribute, style off it.** `aria-pressed` drives `.tile` / `.chip`
  selection, `aria-checked` drives `role="switch"` toggles, `aria-selected` drives tabs.
  One source of truth, so the visual and the a11y tree cannot disagree.
- **Status must be earned.** The header LED has three states — mint `session ready`, amber
  `config incomplete`, coral `config unreachable` — all driven by
  `/config/session-env/status`. A surface that does not query it does not get to say
  "ready".
- **The CTA reflects validity.** `run-btn` is `disabled` whenever a blocking reason exists,
  and the reason is written to the `.run-msg` live region beneath it (`role="status"`,
  `aria-live="polite"`). Errors get `.run-msg.err`. The button label is never used as an
  error channel.
- **Overlays.** Drawer and popup carry `role="dialog"` / `role="alertdialog"` +
  `aria-modal`, are `inert` when closed, trap Tab while open, and restore focus to the
  trigger (with a fallback, since re-rendering can destroy it). Collapsed containers that
  are only visually hidden (`max-height:0`) are `inert` too. Destructive actions confirm
  through the popup, focused on the safe choice.
- **Motion.** 0.08–0.32s on borders, transforms, morphs. All decorative motion respects
  `prefers-reduced-motion`.

---

## 3. AutoDub Studio (`index.html`)

**Intent.** A single-screen control desk. A generative dithered sound-wave breathes
behind the UI, equalizer bars pulse in the wordmark, selecting a language sends a coloured
ping through the wave.

**Layout.** `.app` max-width 1180px. `main` is `1fr 1px 1fr` with a 44px gutter and a
gradient `.vrule`. Left = inputs (xlsx drop, Teaching/Append switches, language mosaic +
quick-select). Right = action + output (Run, command echo, tabbed console).

**Single-screen is conditional.** `@media (max-height:860px)` releases `overflow:hidden`
and lets the page scroll. The old rule clipped the bottom of the mosaic into an
unreachable overflow on any laptop-height viewport — never reintroduce a fixed-height
layout without a height escape.

**Components.**
- **Drop (`.dropwrap > button.drop`)** — icon chip `▤` flips to a filled amber `✓`; an
  absolutely-positioned `.x` sibling clears it and returns focus to the drop button.
- **Switches (`.teach`)** — `role="switch"`; 34×19 track, amber when `aria-checked`.
- **Mosaic (`.tile`)** — `auto-fill minmax(100px,1fr)`, grouped by translation provider:
  `indian · sarvam` / `international · google`. Each tile morphs English (sans) → native script (0.32s
  blur+translate) on select, gains an amber inset ring, a drifting dot texture, and an
  amber code.
- **Run (`.run-btn`)** — the one light, physical element: cream gradient, bevelled borders,
  hard `0 3px 0` shadow that compresses on press. Doubles as Cancel during a run; `⏎`
  hints ⌘/Ctrl+Enter. Disabled state is a recessed grey.
- **Console** — glassy tabbed panel. Every tab has a real empty state, centred in the
  panel. `summary` shows a 4-up metric grid (rows / targets / done / failed) over
  per-language rows with hue-coded progress bars; `logs` is a mono stream polling
  `/logs/important`.

---

## 4. Video Studio (`static/heygen.html`)

**Intent.** A studio monitor: a stage on the left (monitor + transport + log/queue tray),
a control rail on the right. The canvas becomes a concentric iris centred behind the
monitor, tightening as a render progresses.

**Layout.** `main.studio` is `minmax(0,1.4fr) minmax(340px,1fr)` with `align-content:center`
— the row is **content-sized and centred, not stretched**. The monitor is
`flex:1; min-height:340px; max-height:min(58vh,540px)` and the rail's `.railscroll` does
not flex, so the CTA sits directly under the form it submits. Stretching the row is what
produced the two voids the audit flagged: a 680px empty monitor and ~300px of dead rail.

**Components.**
- **Monitor** — dropzone for the avatar image, plus an explicit `choose image` button (the
  keyboard route; dragging is the shortcut). Corner ticks, scanlines, and a `mtag` that
  names what the monitor is *showing* (`no input` / `reference` / `rec ● rendering` /
  `playback`) — distinct from the transport, which names what the *job* is doing.
- **Transport** — rec dot, job state (`role="status"`), stage message, timecode, download
  link, logs toggle (`aria-expanded`, controls the tray).
- **Mode vs voice** — deliberately not peers. `mode` (single/batch) reshapes the form, so
  it is wider with the amber indicator; `voice` (Indian/US) sets one parameter, so it is
  narrower with a neutral indicator.
- **Category chips (`.chipwrap`)** — a `.chip` (`aria-pressed`, amber when selected) plus a
  separate `.pen` edit button revealed for the active chip. Two controls, two buttons.
- **Drawer** — edits a category's name, video prompt, motion prompt. The prompt textareas
  are `.fld.grow` and flex to fill the panel; they are the content, so they get the space.
- **Queue rows (`.qrow`)** — index, title, NAS path, status `.badge`, download link.

**Storage.** Code `SEED` is the source of truth for preset categories; `localStorage`
(`autodub_video_cat_store_v1`) holds only the user's *changes* — per-seed field overrides,
deleted seed ids, and user-created categories. Editing a SEED prompt in code therefore
ships immediately unless that user personally edited it.

---

## 5. Shared modules

`static/wave.js` — `AutoDubWave.create(canvas, "linear" | "iris")` plus the exported
`RAMP`. Owns the ramp, Bayer matrix, DPR/resize, pulse bookkeeping, cell stamping, the rAF
loop, and reduced-motion behaviour. The two intensity fields stay separate because they
genuinely differ, including the pulse envelope (linear = blob widening at a fixed x,
iris = ring travelling outward). API: `ping(colour, strength, duration, position)`,
`setEnergy(0..1)`, `setPlayhead(-1..1)`.

`static/audio.js` — `AutoDubAudio.create({enabled})` with `setEnabled` / `enabled` /
`unlockOnFirstGesture` / `note` / `chord`. Studio starts disabled behind the `♪` button;
Video Studio starts enabled and unlocks on the first gesture per autoplay policy.

Both are plain globals loaded before the page script — no build step, and `/static` is a
FastAPI `StaticFiles` mount so they ship as-is.
