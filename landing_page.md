# AutoDub — Landing Page Spec

A spec for the marketing site that sells AutoDub as a self-hosted, Docker-deployed multilingual dubbing tool. This is **not** the product UI — it is the page that gets developers and content teams to `docker pull`.

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

**Docker exclusion:** the image already only copies what the `Dockerfile` declares. Add `/landing` to `.dockerignore` as a belt-and-braces guarantee. Result: zero bytes of landing code in the published image.

**Deploy:** Vercel/Cloudflare Pages, pointing at the `/landing` subdirectory as the project root.

---

## 1. Positioning

**One-liner:**
> Multilingual audio dubbing, self-hosted in one `docker compose up`.

**Elevator pitch (2 sentences):**
> AutoDub turns a spreadsheet of source text into broadcast-ready dubbed audio across 16 languages — Indian and European — with built-in translation, QC, and voice synthesis. Pull the image, paste your API keys, ship dubs.

**Who it's for:**
- Content studios dubbing courses, ads, or video into Indian + EU languages
- Localization teams that want to own the pipeline instead of paying per-minute SaaS
- Developers who'd rather run a container than wire up Sarvam + Gemini + ElevenLabs themselves

**Why not a SaaS:**
- Your scripts and audio never leave your infra
- Bring-your-own API keys → pay vendor cost, not a 5× markup
- Batch thousands of rows without per-minute pricing anxiety

---

## 2. Page Structure

Single long-scroll page. Sections in order:

1. **Nav** (sticky, minimal)
2. **Hero** — headline, sub, primary CTA (copy docker pull), secondary CTA (GitHub)
3. **Social proof strip** — "Built on Sarvam · Gemini · ElevenLabs · AWS S3"
4. **Problem / Solution** — 2-column or alternating
5. **How it works** — 4-step visual flow
6. **Language coverage** — flag/chip grid (16 langs)
7. **Feature grid** — 6 tiles
8. **Live terminal demo** — animated `docker pull` → running output
9. **Pricing** — self-hosted free / pro / enterprise
10. **Comparison table** — AutoDub vs SaaS dubbing tools
11. **FAQ** — 6–8 questions
12. **Final CTA** — big "Run it now" block with copy command
13. **Footer** — links, GitHub, license

---

## 3. Design Direction

**Vibe:** developer-tool serious, not marketing-fluffy. Think Linear / Resend / Railway — confident, monospace accents, restrained motion.

**Color system:**
- Background: near-black `#0A0A0B` with subtle warm tint
- Surface: `#141416`
- Primary accent: signal green `#7AFFB2` (terminal-success feel) OR warm amber `#FFB86B` (audio waveform feel) — **pick amber**, it ties to "audio" and avoids cliché terminal-green
- Text: `#EDEDED` primary, `#9A9A9A` secondary
- Borders: `#222226`, 1px

**Type:**
- Display: Inter Tight or Geist, weight 600, tight tracking
- Body: Inter, weight 400, 16px / 1.6
- Mono: JetBrains Mono or Geist Mono for all code, CLI, and language codes

**Motion (restrained):**
- Hero waveform: slow, looping CSS/SVG audio bars (3–5s)
- Section reveals: 200ms fade + 8px translate-y on intersection
- Terminal demo: typed-out animation (60–80ms/char), pauses on output lines
- No parallax. No scroll-jacking.

**Imagery:**
- No stock photos
- A small animated waveform in hero
- A simulated terminal as the centerpiece visual (real CSS, not a screenshot)
- Language chips with flag emojis OR small SVG flags (prefer SVG for consistency)
- Use of small architecture diagram in "How it works" — simple boxes + arrows, monospace labels

---

## 4. Section-by-Section Copy

### 4.1 Nav

Left: `AutoDub` wordmark (mono, amber dot after the "b")
Right: `Docs` · `GitHub` · `Pricing` · **`Pull image →`** (button, amber)

---

### 4.2 Hero

**Eyebrow:** `OPEN SOURCE · SELF-HOSTED · v1.x`

**H1:**
> Dub anything into 16 languages.
> Without leaving your infra.

**Sub:**
> AutoDub is a self-hosted dubbing pipeline. Upload a spreadsheet, get translated, QC'd, voice-synthesized audio across Indian and European languages. One Docker image. Your keys. Your storage.

**Primary CTA (button, amber):** `Copy docker pull` (clicking copies the command and shows a toast)

**Secondary CTA (ghost):** `Star on GitHub →`

**Below CTAs (mono, small):**
```
$ docker pull ghcr.io/jugaadchhabra/autodub:latest
```

**Right side / below:** soft amber waveform animation

---

### 4.3 Social proof strip

Mono, muted:
> Powered by **Sarvam** · **Gemini** · **ElevenLabs** · **AWS S3** · **FastAPI**

---

### 4.4 Problem / Solution

**H2:** Dubbing pipelines shouldn't be this annoying.

Two columns:

| ❌ The old way | ✅ With AutoDub |
|---|---|
| Wire up Sarvam, Gemini, ElevenLabs, S3 yourself | One container, one compose file |
| Pay per-minute SaaS markups | Pay vendor cost. Nothing more. |
| Send your scripts to a third-party UI | Runs on your laptop, your VPS, your cluster |
| Manual QC across 16 languages | Gemini-powered QC built into the batch flow |
| One-row-at-a-time tools | Batch an Excel file. Walk away. |

---

### 4.5 How it works

**H2:** From spreadsheet to dubbed audio in 4 steps.

Horizontal flow, each step is a card with a number, icon, title, one-sentence description:

1. **Upload** — Drop in an Excel/CSV of source rows + target languages.
2. **Translate** — Sarvam for Indian languages, in-process for `fr · de · es · ru · pt`.
3. **QC** — Gemini reviews each translation for tone, accuracy, and length fit.
4. **Synthesize** — ElevenLabs / Sarvam voices generate audio. Files land in S3 (or local disk).

Below the flow, a small box:
> **Endpoint:** `POST /batch/excel-jobs` → poll `GET /batch/excel-jobs/{id}` → download.

---

### 4.6 Language coverage

**H2:** 16 languages. One pipeline.

Grid of chips (mono code + flag + name), grouped:

**Indian (via Sarvam):**
`bn-IN` Bengali · `en-IN` English · `gu-IN` Gujarati · `hi-IN` Hindi · `kn-IN` Kannada · `ml-IN` Malayalam · `mr-IN` Marathi · `od-IN` Odia · `pa-IN` Punjabi · `ta-IN` Tamil · `te-IN` Telugu

**European:**
`fr` French · `de` German · `es` Spanish · `ru` Russian · `pt` Portuguese

Footnote: *More languages on the roadmap. [Open an issue →]*

---

### 4.7 Feature grid

**H2:** What you get out of the box.

3×2 grid of feature tiles. Each tile = icon (mono line), title, 1-sentence description.

1. **Batch-first** — Process thousands of rows from a single Excel upload.
2. **Built-in QC** — Gemini scores each translation before audio is generated.
3. **Runtime config** — Paste `.env` into the UI; nothing written to disk.
4. **S3 or local** — Toggle `BATCH_ENABLE_S3_UPLOAD` per deployment.
5. **REST API** — `POST` a job, poll, download. No SDK lock-in.
6. **Your keys, your bill** — Bring your own Sarvam, Gemini, ElevenLabs accounts.

---

### 4.8 Live terminal demo

**H2:** It's actually one command.

Faux terminal window (mac-style traffic lights, mono font, amber prompt). Animated typing:

```bash
$ docker pull ghcr.io/jugaadchhabra/autodub:latest
v1.2.0: Pulling from jugaadchhabra/autodub
✓ Downloaded 412MB

$ docker compose up -d
✓ autodub-api    Started
✓ autodub-worker Started

$ open http://localhost:8080
```

Caption beneath: *That's the whole install.*

---

### 4.9 Pricing

**H2:** Self-host free. Pay for support.

Three cards:

**Community — $0**
- Full image, all features
- GitHub issues for support
- BYO API keys
- MIT license
- CTA: `Pull the image →`

**Pro — $49/mo per deployment** (Recommended badge)
- Everything in Community
- Priority issue triage (48h)
- Private Slack channel
- Migration help
- CTA: `Start Pro →`

**Enterprise — Custom**
- SSO, audit logs, RBAC
- On-prem install support
- SLA + dedicated engineer
- Custom language onboarding
- CTA: `Talk to us →`

Below cards, mono note:
> *All tiers run on your infrastructure. We never see your audio or scripts.*

---

### 4.10 Comparison table

**H2:** Why not just use [SaaS dubbing tool]?

| | AutoDub | Typical SaaS |
|---|---|---|
| Hosting | Your infra | Their cloud |
| Cost model | Vendor cost only | Per-minute markup |
| Data residency | Yours | Theirs |
| Batch Excel input | ✅ | Rare |
| Bring your own voices | ✅ | Locked catalog |
| 16 languages incl. Indian | ✅ | Often English-first |
| Source code | MIT on GitHub | Closed |

---

### 4.11 FAQ

Accordion, 6–8 items:

1. **What do I need to run this?** Docker, ~2GB RAM, and API keys for Sarvam, Gemini, and ElevenLabs.
2. **Where does my data go?** Source rows stay in your container. Audio lands in your S3 bucket or local disk — your choice via env flag.
3. **Can I add a language?** Yes — the European path is in-process translation; new locales are a small PR.
4. **Does Pro give me a hosted version?** No. AutoDub is self-hosted by design. Pro buys you support, not infra.
5. **Can I use my own voices?** Yes — set `DESI_VOCAL_VOICE`, `ENGLISH_VOICE` in `.env`.
6. **Is there an API?** Yes — `POST /batch/excel-jobs`, `GET /batch/excel-jobs/{id}`, `GET /health`.
7. **What's the license?** MIT. Fork it, ship it.
8. **How do updates work?** `docker pull ghcr.io/jugaadchhabra/autodub:latest && docker compose up -d`.

---

### 4.12 Final CTA

Large amber-bordered block, centered:

**H2:** Ready to dub?

Sub: *One pull. One compose. Sixteen languages.*

Mono code block (with copy button):
```bash
docker pull ghcr.io/jugaadchhabra/autodub:latest
docker compose up -d
```

Below: `Read the docs →` · `Star on GitHub →`

---

### 4.13 Footer

Three columns, muted text:

- **Product:** Features · Pricing · Docs · Changelog
- **Community:** GitHub · Issues · Discussions · Roadmap
- **Legal:** License (MIT) · Privacy · Terms

Bottom row: `© 2026 AutoDub. Built by @JugaadChhabra.` + small amber waveform mark.

---

## 5. Conversion mechanics

- **Primary metric:** `docker pull` command copies (track via small analytics event on the copy button)
- **Secondary:** GitHub star clicks, Pro CTA clicks
- One sticky bottom-right pill on scroll past hero: `$ docker pull autodub` (click to copy)
- No email gate. No newsletter modal. Devs hate that.

---

## 6. Tech stack recommendation

- **Framework:** Next.js 15 (App Router) or Astro — Astro if you want it dead-fast and mostly static; Next if you want easy Stripe/Pro signup later
- **Styling:** Tailwind v4
- **Components:** shadcn/ui for primitives (button, accordion, tabs)
- **Motion:** Framer Motion for section reveals; pure CSS for the waveform
- **Hosting:** Vercel or Cloudflare Pages
- **Analytics:** Plausible or PostHog (self-hosted matches the ethos)
- **Domain idea:** `autodub.sh` or `autodub.dev`

---

## 7. Out of scope (for v1)

- Blog / changelog (add later)
- Customer logos (don't have any yet — don't fake it)
- Video testimonials
- Interactive demo of the actual product UI (link to a Loom instead until ready)

---

## 8. Open questions for you

1. Brand name — stick with **AutoDub** or rename for marketing?
2. Pricing — are Pro/Enterprise tiers real, or Community-only for now? (Affects whether we need Stripe.)
3. Domain purchased yet?
4. Do you want a hosted-trial option later, or strictly self-host forever?
