# autodub — Landing Page Spec

A spec for the marketing site that sells **autodub** as a self-hosted, Docker-deployed multilingual dubbing instrument. This is **not** the product UI — it is the page that gets developers and content teams to `docker pull`.

The landing inherits the product's visual identity: a quiet, dark, instrument-grade surface with warm amber and a four-color equalizer accent, lowercase monospace labels, and native scripts as first-class design elements.

---

## 0. Repo layout decision

**Decision: same repo, in a `/landing` subdirectory on `main`. Excluded from the Docker image.**

Why not a separate branch:
- Landing is a permanently-diverging artifact, not in-flight work — it never merges back into `main`. That's an orphan branch, not a feature branch.
- `git checkout main` would wipe landing files from disk; can't have both checked out at once.
- CI configs collide (one repo, two unrelated build/deploy pipelines).
- `git log` splits into two unrelated histories.

Why subdirectory wins:
- Both checked out together — edit product + marketing in the same session.
- One `git log`, one issue tracker, one star count.
- README links and changelog references just work.
- Easy to grow into `apps/dub` + `apps/landing` later if a monorepo is warranted.

**Docker exclusion:** add `/landing` to `.dockerignore`. Zero bytes of landing code in the published image.

**Deploy:** Vercel/Cloudflare Pages, pointing at the `/landing` subdirectory as the project root.

---

## 1. Positioning

**One-liner:**
> A self-hosted instrument for multilingual dubbing.

**Elevator pitch (2 sentences):**
> autodub turns a spreadsheet of source text into broadcast-ready dubbed audio across 16 languages — Indian and European — with translation, QC, and voice synthesis built in. Pull the image, paste your keys, ship dubs.

**Who it's for:**
- Content studios dubbing courses, ads, or video into Indian + EU languages
- Localization teams that want to own the pipeline instead of paying per-minute SaaS
- Developers who'd rather run a container than wire Sarvam + Gemini + ElevenLabs by hand

**Why not a SaaS:**
- Your scripts and audio never leave your infra
- BYO API keys → pay vendor cost, not a 5× markup
- Batch thousands of rows without per-minute pricing anxiety

**Tone:** instrument, not app. Studio, not factory. Lowercase, confident, no exclamation points.

---

## 2. Visual identity (inherited from the product)

The landing page is the product's design system extended one tier outward. It should *feel* like the same studio.

### 2.1 Color tokens (copy from `static/style.css`)

```
--bg:        #0A0A0C   /* near-black, warm */
--bg-2:      #0D0D11   /* subtle elevation */
--panel:     #101015   /* surface */
--ink:       #F4F3F1   /* warm off-white text */
--muted:     #8C8C96   /* secondary text */
--faint:     #54545E   /* tertiary / mono labels */
--line:      #1E1E24   /* 1px borders */
--line-2:    #161619   /* hairlines */
--accent:    #FFB570   /* warm amber — primary */
--accent-dim:rgba(255,181,112,.12)
--ok:        #7CF5C4   /* mint */
--err:       #FF8A80   /* coral */
```

**Equalizer palette (4-color accent set — use sparingly for chips, dots, headlines):**
- amber `#FFB570`
- sky `#7DD3FC`
- fuchsia `#F0ABFC`
- mint `#7CF5C4`

These are the bars of the brand mark's equalizer. Reuse them as the four feature-tile accents, language-group dots, and animated highlights. Never use them all in one block — pick one bar per tile.

### 2.2 Type stack (copy from product `<link>` tag)

```
--sans:  "Space Grotesk", system-ui, sans-serif   /* UI + body */
--serif: "Instrument Serif", Georgia, serif       /* italic display moments */
--mono:  "JetBrains Mono", ui-monospace, Menlo    /* labels, code, captions */
--native:"Noto Sans", "Noto Sans Devanagari",
         "Noto Sans Tamil", "Noto Sans Bengali"   /* native scripts */
```

**Type rules:**
- All navigational labels, section labels, captions, metadata: **lowercase, JetBrains Mono, 10–12px, letter-spacing 0.04–0.14em**
- Headlines: Space Grotesk 500–700, **tight tracking (-0.02 to -0.04em)**, **sentence case** (not Title Case)
- Emotional/poetic moments (one per page, max two): **Instrument Serif italic** — never for body
- Native scripts in their own font; never force them into the sans stack

### 2.3 Brand mark

```
autodub<amber-dot><equalizer>
```

- `autodub` — Space Grotesk 700, lowercase, letter-spacing -0.03em
- `.` — amber `#FFB570`, bottom-aligned
- Equalizer — 4 vertical bars, 2.5px wide, animating between 3px and 13px height on a 1.1s loop, each bar a different equalizer-palette color (amber → sky → fuchsia → mint), staggered animation delays (0 / 0.16 / 0.34 / 0.52s)
- When the page hero is "active" (user has scrolled in or button is hovered), the equalizer **speeds up** to 0.46s loop

This mark is the page's heartbeat. It appears in the nav and in the footer.

### 2.4 Background

A full-bleed canvas wave (oscilloscope-style), masked with a `linear-gradient(180deg, #000 0%, #000 15%, rgba(0,0,0,.38) 42%, rgba(0,0,0,.13) 100%)` so it fades into the page. Sits behind a radial scrim that darkens edges. Slow-moving — period in seconds, not milliseconds. The wave amplitude breathes ~6%. This is the product's signature; reuse the canvas implementation.

### 2.5 Components

| Component | Treatment |
|---|---|
| Buttons (primary) | Skeuomorphic beige key — gradient `#f4f1ea → #e3dfd4`, 2px white top-left highlight, 2px tan bottom-right shadow, `0 3px 0 rgba(0,0,0,.45)` underneath, presses down on click. Dark text `#16130d`. **Use only for the one big CTA per section.** |
| Buttons (secondary) | 1px `#1E1E24` border, transparent fill, lowercase mono label, amber on hover |
| Cards / surfaces | `rgba(13,13,17,.82)` fill, `backdrop-filter: blur(3px)`, 1px `#1E1E24` border, 9–11px radius |
| Section labels | Mono 11px, color `--faint`, with a flanking 1px hairline (the `.lbl` pattern: `label <hairline> <meta>`) |
| Dividers | Vertical: `linear-gradient(transparent, #1E1E24 12%, #1E1E24 88%, transparent)` |
| Code blocks | Mono 12px, `rgba(12,12,16,.8)` bg, 1px `#1E1E24` border, copy-on-click |
| Native-script tile | The language-mosaic tile that morphs between English and native script on hover — reuse the exact `.tile` component from the product (English fades up, native script fades in with a 2px blur lift), see §3.6 |

### 2.6 Motion

- Equalizer bars: always running (respect `prefers-reduced-motion`)
- Hero wave: canvas, ~0.5fps perceived
- Section reveals: 200ms opacity + 8px translate-y, IntersectionObserver-triggered
- Tile morphs (English ⇄ native): 320ms ease, with a 2px blur on the outgoing layer
- Run button: skeuo press of `translateY(2px)` on `:active`
- **No parallax. No scroll-jacking. No bouncy springs.** This is an instrument, not a toy.

---

## 3. Page structure

Single long-scroll page. Sections in order:

1. **Nav** (sticky, minimal)
2. **Hero** — headline, sub, primary CTA (copy docker pull), secondary (GitHub)
3. **Powered-by strip**
4. **Live product preview** — the actual two-column UI, scaled down, animated
5. **Pipeline diagram** — 4 stages, equalizer-colored
6. **Language mosaic** — exactly the product's tile component, English ⇄ native morph
7. **Feature grid** — 6 tiles, one equalizer color per tile
8. **Terminal demo** — animated install
9. **Pricing** — community / pro / enterprise
10. **Comparison table** — autodub vs SaaS
11. **FAQ**
12. **Final CTA**
13. **Footer**

---

## 4. Section-by-section copy

### 4.1 Nav

Left: `autodub.<equalizer>` mark (live, animating)

Right (lowercase mono, 11px, spaced):
> `docs` · `github` · `pricing` · **`pull image →`** (amber outline button)

---

### 4.2 Hero

**Eyebrow** (mono, faint, lowercase, letter-spacing 0.14em):
> `open source · self-hosted · v1.x`

**H1** (Space Grotesk 600, -0.03em tracking, 64–80px):
> Dub anything.
> *In any of sixteen voices.*

The second line is **Instrument Serif, italic** — the page's one poetic moment.

**Sub** (Space Grotesk 400, 18px, color `--muted`, max 56ch):
> autodub is a self-hosted dubbing pipeline. Upload a spreadsheet, get translated, QC'd, voice-synthesized audio across Indian and European languages. One docker image. Your keys. Your storage.

**Primary CTA** (skeuomorphic beige key):
> `pull image ⏎`
> Clicking copies `docker pull ghcr.io/jugaadchhabra/autodub:latest` and shows a mint toast: `copied.`

**Secondary CTA** (ghost):
> `star on github →`

**Below CTAs** (mono 11px, color `--faint`, with a leading amber `$`):
```
$ docker pull ghcr.io/jugaadchhabra/autodub:latest
```

**Right of CTAs** (mono 11px, lowercase, hairline-separated):
> `session ready` · `config 12/12` · `♪ off`

Mimics the product header so the page feels like an extension of the app.

---

### 4.3 Powered-by strip

Centered, mono 11px, color `--faint`, dots between:

```
powered by  ·  sarvam  ·  gemini  ·  elevenlabs  ·  aws s3  ·  fastapi
```

No logos. Wordmarks only. This is the product's restraint.

---

### 4.4 Live product preview

**Section label:** `the studio<hairline>1180px wide`

A 1:1 scaled-down render of `index.html` in an animated state — the wave is moving, the equalizer is bouncing, a row in the console is processing, language tiles cycle their English ⇄ native morph at a 4s cadence.

This is **the** hero asset. No screenshot. A real, live, embedded snapshot of the actual UI (iframe or inline-rendered). Caption beneath, Instrument Serif italic:

> *This is the whole app. There is no other UI.*

---

### 4.5 Pipeline diagram

**Section label:** `how it works<hairline>4 stages`

Horizontal flow, four cards, each tagged with one equalizer color:

| | Stage | Description |
|---|---|---|
| 🟧 amber | **upload** | drop an `.xlsx` of source rows + target codes |
| 🟦 sky | **translate** | sarvam for indian targets, in-process for `fr · de · es · ru · pt` |
| 🟪 fuchsia | **qc** | gemini reviews each translation for tone, accuracy, length fit |
| 🟩 mint | **synthesize** | elevenlabs / sarvam voices generate audio. files land in s3 or local disk |

Use the *actual* equalizer colors (not emoji) — a 4px colored bar on the left edge of each card.

Below the diagram, a mono code box:

```
POST   /batch/excel-jobs        →  start a job
GET    /batch/excel-jobs/{id}   →  poll status
GET    /health                  →  liveness
```

---

### 4.6 Language mosaic

**Section label:** `targets<hairline>16 / 16`

Use **the exact `.tile` component from the product** (see `static/style.css` `.tile` + `.morph`). 4-column grid on desktop, 2-column on mobile. Each tile shows:

- Top-left mono: language code (`hi-IN`, `fr`, etc.)
- Center: morphs between English name and native script on hover/scroll-into-view
- On `.on` state: amber inset border + drifting dot pattern

**Group dividers** (mono 9.5px, letter-spacing 0.14em, color `--faint`):

```
indian — via sarvam ─────────────────────────────────
```
`bn-IN` Bengali · `en-IN` English · `gu-IN` Gujarati · `hi-IN` Hindi · `kn-IN` Kannada · `ml-IN` Malayalam · `mr-IN` Marathi · `od-IN` Odia · `pa-IN` Punjabi · `ta-IN` Tamil · `te-IN` Telugu

```
european — in-process ───────────────────────────────
```
`fr` French · `de` German · `es` Spanish · `ru` Russian · `pt` Portuguese

Stagger the morph animation so the mosaic looks alive — every 600ms one tile flips to native, holds 4s, flips back.

Footnote (mono, faint):
> *more languages on the roadmap. open an issue →*

---

### 4.7 Feature grid

**Section label:** `what you get<hairline>out of the box`

3×2 grid, lowercase titles, one equalizer color per tile (4px left bar). Six tiles:

1. 🟧 **batch-first** — process thousands of rows from a single excel upload
2. 🟦 **built-in qc** — gemini scores each translation before audio is generated
3. 🟪 **runtime config** — paste `.env` into the ui; nothing written to disk
4. 🟩 **s3 or local** — toggle `BATCH_ENABLE_S3_UPLOAD` per deployment
5. 🟧 **rest api** — `POST` a job, poll, download. no sdk lock-in
6. 🟦 **your keys, your bill** — bring your own sarvam, gemini, elevenlabs accounts

Cycle through the 4 equalizer colors (amber/sky/fuchsia/mint, repeat).

---

### 4.8 Terminal demo

**Section label:** `install<hairline>one command`

Faux terminal — mac traffic-light dots, amber-on-near-black, JetBrains Mono. Typed-out animation (60–80ms/char), pauses on output lines, equalizer bars in the prompt:

```
$ docker pull ghcr.io/jugaadchhabra/autodub:latest
v1.2.0: pulling from jugaadchhabra/autodub
✓ downloaded 412mb

$ docker compose up -d
✓ autodub-api    started
✓ autodub-worker started

$ open http://localhost:8080
```

Caption (Instrument Serif italic, color `--muted`, centered):
> *That's the whole install.*

---

### 4.9 Pricing

**Section label:** `pricing<hairline>self-host free · pay for support`

Three cards, all `rgba(13,13,17,.82)` glass + `#1E1E24` border. Pro gets an amber 1px inset glow.

**community — $0**
- full image, all features
- github issues for support
- byo api keys
- mit license
- CTA (ghost): `pull the image →`

**pro — $49/mo per deployment**  *(amber `recommended` chip)*
- everything in community
- priority issue triage (48h)
- private slack channel
- migration help
- CTA (skeuo key): `start pro ⏎`

**enterprise — custom**
- sso, audit logs, rbac
- on-prem install support
- sla + dedicated engineer
- custom language onboarding
- CTA (ghost): `talk to us →`

Below cards, mono 11px, color `--faint`:
> *all tiers run on your infrastructure. we never see your audio or scripts.*

---

### 4.10 Comparison table

**Section label:** `vs the saas tools<hairline>honest comparison`

| | **autodub** | typical saas |
|---|---|---|
| hosting | your infra | their cloud |
| cost model | vendor cost only | per-minute markup |
| data residency | yours | theirs |
| batch excel input | ✓ | rare |
| bring your own voices | ✓ | locked catalog |
| 16 langs incl. indian | ✓ | english-first |
| source code | mit on github | closed |

`autodub` column uses amber checkmarks. Other column uses `--faint` text.

---

### 4.11 FAQ

**Section label:** `questions<hairline>asked frequently`

Accordion. Each row: lowercase question (Space Grotesk 500), expands to a `--muted` paragraph. 1px `#1E1E24` divider between rows.

1. **what do i need to run this?** — docker, ~2gb ram, and api keys for sarvam, gemini, and elevenlabs.
2. **where does my data go?** — source rows stay in your container. audio lands in your s3 bucket or local disk — your choice via env flag.
3. **can i add a language?** — yes — the european path is in-process translation; new locales are a small pr.
4. **does pro give me a hosted version?** — no. autodub is self-hosted by design. pro buys you support, not infra.
5. **can i use my own voices?** — yes — set `AI_STUDIO_VOICE`, `DESI_VOCAL_VOICE`, `ENGLISH_VOICE` in `.env`.
6. **is there an api?** — yes — `POST /batch/excel-jobs`, `GET /batch/excel-jobs/{id}`, `GET /health`.
7. **what's the license?** — mit. fork it, ship it.
8. **how do updates work?** — `docker pull ghcr.io/jugaadchhabra/autodub:latest && docker compose up -d`.

---

### 4.12 Final CTA

Centered block, amber 1px inset border, glass surface.

**H2** (Space Grotesk 600, -0.03em):
> ready to dub?

**Sub** (Instrument Serif italic, `--muted`):
> *one pull. one compose. sixteen voices.*

Mono code block (copy-on-click, amber `$` prompt):
```
docker pull ghcr.io/jugaadchhabra/autodub:latest
docker compose up -d
```

Below: `read the docs →` · `star on github →` (ghost, mono)

---

### 4.13 Footer

Three columns, mono 11px, color `--faint`. All lowercase.

- **product:** features · pricing · docs · changelog
- **community:** github · issues · discussions · roadmap
- **legal:** license (mit) · privacy · terms

Bottom row, centered:
```
autodub.<equalizer>  ·  © 2026  ·  built by @jugaadchhabra
```

The equalizer animates here too. It's the heartbeat of the page.

---

## 5. Conversion mechanics

- **Primary metric:** `docker pull` copies (track per button)
- **Secondary:** github star clicks, pro CTA clicks
- **Sticky bottom-right pill** after hero: `$ docker pull autodub` in mono — click to copy, mint toast on success
- **No email gate. No newsletter modal. No cookie popup beyond legal minimum.**

---

## 6. Tech stack recommendation

- **Framework:** Astro (mostly static, fastest paint) or Next.js 15 if you want Stripe/Pro signup wired later
- **Styling:** Tailwind v4 + a tokens CSS file mirroring `static/style.css` variables (single source of truth)
- **Components:** shadcn/ui for accordion + tabs primitives, restyled with the product tokens
- **Motion:** Framer Motion for section reveals; raw canvas for the hero wave (port from product); pure CSS for the equalizer
- **Hosting:** Vercel or Cloudflare Pages
- **Analytics:** Plausible or PostHog (self-hosted matches the ethos)
- **Domain idea:** `autodub.sh` or `autodub.dev`

**Asset reuse:** copy `static/style.css` color + type variables verbatim into a `landing/styles/tokens.css`. Reuse the wave canvas script and the `.tile` morph component. The landing should *be* the product.

---

## 7. Out of scope (for v1)

- Blog / changelog (add later, same design system)
- Customer logos (don't have any yet — don't fake it)
- Video testimonials
- A separate product-screenshot section (the live preview replaces it)

---

## 8. Open questions

1. Brand — keep lowercase `autodub` everywhere, or capitalize in headlines? (Recommend: lowercase always, matches product mark.)
2. Pricing — are pro/enterprise tiers real yet, or community-only for now? (Affects Stripe wiring.)
3. Domain purchased yet? `autodub.sh` vs `autodub.dev`.
4. Append-mode and teaching-mode are product features — call them out in feature grid, or leave them as discoverable depth?
5. Native-script morph on the mosaic — auto-cycle, hover-only, or scroll-triggered once? (Recommend: scroll-triggered once + hover after.)
