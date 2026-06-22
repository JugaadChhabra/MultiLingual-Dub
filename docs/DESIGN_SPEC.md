# AutoDub — Web UI Design Specification

> Reverse-engineered from the live front-end. Documents the visual language, tokens,
> components, motion, and interaction patterns currently shipping in the web views so
> future work stays consistent with what exists.

The product ships **two distinct surfaces**, each with its own visual system:

| Surface | File(s) | Audience | Aesthetic |
|---|---|---|---|
| **AutoDub Studio** | `index.html`, `static/style.css`, `static/app.js` | Internal operators running the dub pipeline | Dark "instrument / terminal" — dithered wave, monospace telemetry, single-screen control desk |
| **Bhaktidhaam Content Pipeline** | `static/heygen.html` (self-contained) | HeyGen avatar-video operators | Glassmorphic "devotional tech" — violet/amber glows, ॐ watermark, card form |

They are **not** a shared design system today. This spec describes each as-built and notes where they diverge.

---

## 1. AutoDub Studio (`index.html`)

### 1.1 Design intent
A single, non-scrolling "control desk." Everything — source upload, target picker, run, and live console — fits one viewport. The mood is a calm audio instrument: a generative dithered sound-wave breathes behind the UI, equalizer bars pulse in the wordmark, and selecting a language sends a colored "ping" rippling through the wave. It should read like professional studio software, not a web form.

### 1.2 Color tokens (`:root` in `style.css`)

| Token | Value | Role |
|---|---|---|
| `--bg` | `#0A0A0C` | Page background (near-black) |
| `--bg-2` | `#0D0D11` | Secondary surface |
| `--panel` | `#101015` | Panel fill |
| `--ink` | `#F4F3F1` | Primary text (warm off-white) |
| `--muted` | `#8C8C96` | Secondary text |
| `--faint` | `#54545E` | Tertiary / labels / hints |
| `--line` | `#1E1E24` | Borders |
| `--line-2` | `#161619` | Hairline dividers |
| `--accent` | `#FFB570` | Warm amber — primary accent, focus, active |
| `--accent-dim` | `rgba(255,181,112,.12)` | Accent wash (toggle track) |
| `--ok` | `#7CF5C4` | Success (mint) |
| `--err` | `#FF8A80` | Error (coral) |

**Iridescent ramp (JS, `app.js`):** a 96-step gradient interpolated through
`[teal, sky, violet, butter, salmon, orchid, white]` drives the wave coloring and assigns each
language tile a unique hue (`--tile-hue`) by index. Accent equalizer bars cycle amber → sky
`#7DD3FC` → orchid `#F0ABFC` → mint.

### 1.3 Typography

| Family | Token | Usage |
|---|---|---|
| **Space Grotesk** (400–700) | `--sans` | UI, labels, buttons, metrics |
| **Instrument Serif** (italic) | `--serif` | Accent flourishes — toggle descriptions ("Full अनुवाद") |
| **JetBrains Mono** (400–700) | `--mono` | All telemetry: status bar, labels, codes, logs, hints |
| **Noto Sans (+ Devanagari/Tamil/Bengali)** | `--native` | Native-script language names |

Convention: lowercase labels, mono, letter-spacing `.02–.14em`, often uppercased. A repeated
"label + hairline rule + count" header pattern (`.lbl` → `text · <rule> · .n`) organizes every section.
Mono text carries a `text-shadow` glow against the wave for legibility.

### 1.4 Layout
- `.app`: max-width **1180px**, full-height flex column, `height:100vh`, `overflow:hidden`.
- `main`: CSS grid `1fr 1px 1fr` with a 44px gutter and a gradient `.vrule` divider — **two columns**, vertically centered.
  - **Left** = inputs: source `.xlsx` drop, Teaching-mode / Append-mode toggles, target language mosaic with quick-select.
  - **Right** = action + output: Run button, command echo, tabbed console (summary / feed / logs).
- **Background layers (z-order):** `#wave` canvas (z0) → `.scrim` radial vignette (z0) → `.app` (z1).
- Responsive: at **≤880px** collapses to a single column, `overflow:auto`, `.vrule` hidden, sound-config segment hidden.

### 1.5 Components

**Drop zone (`.drop`)** — rounded (10px), translucent (`rgba(13,13,17,.82)` + 3px blur), 1px border. Mono sub-label. Icon chip `▤` flips to a filled amber `✓` on load; an `×` clear button appears. Hover/drag → border lightens, bg lifts.

**Toggles (`.teach`, `#append`)** — pill rows with a 34×19 iOS-style switch. Off: gray track + faint knob. On: `--accent-dim` track, amber knob slid +15px, status text `off→on`. Description set in italic serif.

**Language mosaic (`.mosaic` / `.tile`)** — `auto-fill minmax(100px,1fr)` grid grouped by `indian · sarvam` and `international · in-process`. Each tile shows a mono `--tile-hue` language code + a **morphing label**: English (sans) cross-fades/blurs into the native script on select (`.en`↔`.nat`, 0.32s blur+translate). Selected: colored inset ring, drifting dot-grid texture, hue-tinted code. `processing` state adds a pulsing glow. Quick-select buttons (`all / indian / intl / clear`) bulk-toggle with staggered wave pings.

**Run button (`.run-btn`)** — the one **light, physical** element: cream gradient, beveled borders, hard `0 3px 0` drop shadow that compresses on `:active` (press-down). Doubles as Run / Running… / Cancel; `⏎` glyph hints ⌘/Ctrl+Enter. Below it a mono command echo (`$ POST /batch/excel-jobs` → live job id).

**Console (`.consolebox`)** — glassy panel (`backdrop-filter: blur(5px)`), tabbed:
- **summary** — empty state (mini-equalizer + "Nothing dubbed yet"); on run, a 4-up metric grid (rows / **targets** amber / **done** mint / **failed** coral, 28px figures) over per-language result rows with animated progress bars.
- **feed** — live per-row activity list.
- **logs** — mono terminal stream (timestamp · status glyph `· ! ✗` · message), error rows in coral, blinking caret. Polls `/logs/important`.

**Header** — wordmark `autodub.` with a live equalizer (`.eq`) of 4 colored bars; right side mono status (`● session ready`, `config 9/12`, `♪ off` sound toggle). The LED dot + config count bind to `/config/session-env/status`.

### 1.6 Motion & feedback
- **Dithered wave** (`#wave` canvas): Bayer-dithered plus-sign cells, summed sine field, `energy` rises to 1 during a run. `ping(x, color)` injects expanding colored ripples on hover/select/idle/per-row. Masked to fade toward the bottom; idle sweep + selected-locale "soft singing" every 1.5s.
- **Optional Web Audio**: pentatonic blips on select; a chord on job completion. Off by default.
- All decorative motion respects `prefers-reduced-motion` (equalizers freeze, wave/pulses stop).
- Transitions are short and functional: 0.08–0.32s on borders, transforms, morphs.

### 1.7 State semantics (color-coded everywhere)
`--accent` amber = active/primary/in-progress · `--ok` mint = ready/done/success · `--err` coral = failed/cancelled/error · `--faint` = queued/idle. The LED dot turns amber when config is incomplete, mint when ready.

---

## 2. Bhaktidhaam Content Pipeline (`heygen.html`)

### 2.1 Design intent
A focused, **single-card form** for generating devotional avatar videos (राशिफल horoscope / जन्मदिन birthday) in Single or Batch mode. Warmer and more consumer-facing than the Studio: a soft purple-and-saffron aura, a faint ॐ watermark, and a saturated gradient CTA. Self-contained — all CSS/JS inline.

### 2.2 Color tokens (`:root`)

| Token | Value | Role |
|---|---|---|
| `--bg` | `#040816` | Deep navy base |
| `--card` / `--card2` | `rgba(255,255,255,.04 / .07)` | Glass fills |
| `--border` / `--border-hi` | `rgba(120,80,255,.18 / .45)` | Violet borders |
| `--violet` | `#7c3aed` | Primary (Single/Batch active, save) |
| `--amber` | `#d97706` | Secondary (content-type active) |
| `--pink / --orange` | `#ec4899 / #f97316` | CTA gradient stops |
| `--green / --red` | `#34d399 / #f87171` | Success / error badges |
| `--t1 / --t2 / --t3` | `#f1f5f9 / #94a3b8 / #4b5563` | Text tiers (slate) |
| `--r / --r-sm` | `14px / 10px` | Radii |

`color-scheme: dark`. Layered fixed background: four radial glows (purple TL, saffron BL, violet center, pink TR) over a navy diagonal gradient, all `filter: blur(80px)`.

### 2.3 Typography
Single family: **Inter** (400–800), `-apple-system` fallback. Devanagari renders via system fonts. Heavy weights (700–800) for headings/CTA; 600 for labels; tight `letter-spacing: -0.4px` on the h1.

### 2.4 Layout
- Centered column, `max-width: 680px`, page padding `44px 20px 80px`.
- **Header**: gradient saffron→red rounded icon tile (`ॐ`, glowing shadow) + title/subtitle, and an outlined "Prompts" settings button (gear icon).
- **Main card**: translucent, 1px violet border, 20px radius, `backdrop-filter: blur(20px)`, layered shadow incl. a violet `0 0 80px` glow.
- **Footer stats**: three icon+label trust badges (AI-Powered / Fast Processing / High Quality).
- A **modal** (`.modal-backdrop`, blur-8 scrim, `modal-pop` scale-in) edits per-content-type video/motion prompts.

### 2.5 Components
- **Mode toggles** — two pill groups. Type (Single/Batch) active = **violet glass gradient** with inset highlight + outer glow (`::before` blur halo, `::after` gloss). Content (राशिफल/जन्मदिन) active = **amber gradient**. Inactive pills are low-contrast white-on-glass.
- **Upload areas** (`.upload-area`) — dashed violet border, circular cloud-upload icon that brightens on hover/drag, decorative corner emoji (🪷 image / 📊 excel), green filename on selection. Drag-drop wired via `DataTransfer`.
- **Inputs** — transparent fill, violet border, focus = `--border-hi` + 3px violet focus ring. Script textarea has an absolutely-positioned footer with `n/1000` char counter and a `✦` sparkle. `Required` pills (violet) flag mandatory fields.
- **Generate button (`.generate-btn`)** — full-width saturated gradient `#c026d3 → #e879f9 → #f97316`, 700 weight, magenta glow shadow, lifts `-2px` on hover. Label swaps "Generate video" ↔ "Generate batch".
- **Status / results** — glass `.status` boxes with uppercase pill **badges**: `completed` (green) / `partial` (amber) / `failed` (red) / neutral. Single mode embeds an HTML5 `<video>` player + download/heygen links + JSON `<pre>`. Batch mode renders a results **table** (#, Title, Status badge, Download).

### 2.6 Interaction model
Vanilla JS, no framework. Mode switch reflows which fields show and toggles `required`. Submit posts `FormData` to `/video/heygen[/batch]`, then polls every **4s** until terminal (`completed`/`partial`/`failed`), re-rendering status. Prompt settings persist per content-type in `localStorage` (`heygen_prompt_settings_<type>`), falling back to built-in defaults.

---

## 3. Cross-surface comparison & guidance

| Dimension | AutoDub Studio | Bhaktidhaam Pipeline |
|---|---|---|
| Base | `#0A0A0C` near-black | `#040816` navy + blurred glows |
| Accent | Single amber `#FFB570` + iridescent ramp | Violet `#7c3aed` + amber `#d97706` + magenta CTA |
| Type | Space Grotesk / Instrument Serif / JetBrains Mono / Noto | Inter only |
| Shape | Sharp, 5–11px radii, hairlines | Soft, 10–20px radii, glass blur |
| Motion | Generative canvas wave, pings, audio | CSS transitions, modal pop, glow |
| Personality | Precision instrument / terminal | Warm devotional product |
| Code org | External CSS + JS modules | Fully inline, single file |

**Shared DNA worth preserving if unifying:** dark base, translucent blurred panels, uppercase letter-spaced micro-labels, semantic state colors (mint=ok / coral-or-red=fail / accent=active), polling-driven live status, generous use of glow for depth, and culturally-aware native-script typography.

**Known inconsistencies (intentional today):** the two surfaces share no tokens, fonts, or radii scale; success colors differ (`#7CF5C4` vs `#34d399`); accent philosophy differs (mono-amber vs multi-hue). Treat any future "design system" work as a reconciliation of these two, not a from-scratch effort.
