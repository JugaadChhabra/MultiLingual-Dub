# Claude Project Setup — AutoDub

How to set this repo up as a Claude Project (at claude.ai) so the assistant has the context it needs to help you plan, write copy, design, and iterate without re-explaining the codebase every time.

---

## Step 1 — Create the Project

claude.ai → **Projects** → **Create project**

- **Name:** `AutoDub`
- **Description:** `Self-hosted multilingual dubbing pipeline (Docker). Planning, landing page, roadmap, and product work.`

---

## Step 2 — Custom Instructions

Paste this into the project's **Custom Instructions** field (claude.ai → project → ⚙️ → Custom instructions):

```
You are helping me build and ship AutoDub, a self-hosted multilingual audio
dubbing tool distributed as a Docker image (ghcr.io/jugaadchhabra/autodub).

WHAT AUTODUB IS
- Input: Excel/CSV of source rows + target language codes.
- Pipeline: translate → QC → text-to-speech → upload.
- Languages: 11 Indian (Sarvam) + 5 European (in-process): bn-IN, en-IN,
  gu-IN, hi-IN, kn-IN, ml-IN, mr-IN, od-IN, pa-IN, ta-IN, te-IN, fr, de, es,
  ru, pt.
- Stack: Python · FastAPI · Sarvam · Gemini (QC) · ElevenLabs · AWS S3 ·
  Docker · docker-compose.
- Distribution: GHCR image, BYO API keys via .env, runs on user's infra.
- UI: simple browser UI at :8080 + REST API (POST /batch/excel-jobs, GET
  /batch/excel-jobs/{id}, GET /health).
- License: MIT.

WHO I AM
- Solo builder (JugaadChhabra). I do all git ops myself — never run
  git commit / push / add. Suggest diffs; I'll commit.
- I prefer short, direct responses. No fluff, no trailing summaries.
- I prefer recommendations with one-line tradeoffs over open-ended menus.

CURRENT WORK
- Packaging AutoDub as a paid/self-hosted product.
- Building a landing page (see landing_page.md). Decision: same repo,
  /landing subdirectory on main, excluded from Docker image via
  .dockerignore. Deploy via Vercel/Cloudflare Pages from that subdir.
- Brand name, pricing tiers, and domain are still open questions.

HOW TO HELP
- When I ask for copy/design/marketing work, ground it in the actual
  product (real language list, real env vars, real endpoints).
- When I ask architecture/code questions, check the uploaded source files
  before guessing.
- Flag when a suggestion would bloat the Docker image or leak keys.
- If a decision is already made (see landing_page.md §0), don't re-litigate
  it unless I ask.
- Default to: shortest answer that actually answers the question.
```

---

## Step 3 — Project Knowledge (files to upload)

Upload these from the repo into the project's **Knowledge** section. They give the assistant grounding without being so large they eat the context budget.

### Tier 1 — Always upload (small, high-signal)

- `README.md` — top-level overview, install, env vars
- `DOCKER_README.md` — Docker-specific install notes
- `landing_page.md` — landing page spec + repo-layout decision
- `docker-compose.yml` — services, ports, volumes
- `Dockerfile` — image build steps
- `requirements.txt` — Python deps (signals stack/version)

### Tier 2 — Upload for product/API work

- `api/routes.py` — FastAPI endpoints (the public API surface)
- `api/models.py` — request/response schemas
- `batch/service.py` + `batch/models.py` — batch job lifecycle
- `services/languages.py` — language code mapping (single source of truth)
- `services/runtime_config.py` — how the runtime `.env` UI works

### Tier 3 — Upload only if asked about specific subsystems

- `services/sarvam.py` — Indian-language translation
- `services/translation/` (whole folder) — EU translation path
- `services/qc.py` — Gemini QC
- `services/tts.py`, `services/elevenlabs.py` — voice synthesis
- `services/s3.py` — upload
- `services/video_pipeline/` — only if working on video features
- `static/heygen.html`, `static/app.js` — only if working on the browser UI

### Don't upload

- `venv/`, `__pycache__/`, `output/`, `data/`, `graphify-out/` — noise
- Anything with secrets (`.env`, `*.key`, `credentials.json`)
- Test fixtures (`tests/` unless you're asking about test strategy)

---

## Step 4 — Suggested first prompts in the project

Once set up, kick the tires with:

1. `Read landing_page.md and propose 3 brand name alternatives to "AutoDub" — one literal, one abstract, one playful. One-line tradeoff each.`
2. `Given the endpoints in api/routes.py, draft an OpenAPI-flavored "API" section for the landing page.`
3. `Suggest a Stripe-light pricing implementation that doesn't require a backend — just a "Buy Pro" link.`
4. `What's the smallest .dockerignore change to guarantee /landing never ships in the image?`

---

## Step 5 — Maintenance

- Re-upload `landing_page.md` and `README.md` whenever they change materially. Project Knowledge is a snapshot, not a live link.
- When you add a new service or endpoint, re-upload the relevant file from Tier 2.
- Trim the project knowledge if you hit the size cap — drop Tier 3 first.

---

## Optional — Sync helper

If you want a one-liner to bundle the Tier 1+2 files for re-upload:

```bash
zip autodub-context.zip \
  README.md DOCKER_README.md landing_page.md \
  docker-compose.yml Dockerfile requirements.txt \
  api/routes.py api/models.py \
  batch/service.py batch/models.py \
  services/languages.py services/runtime_config.py
```

Drop the zip into the project and Claude.ai will expand it.
