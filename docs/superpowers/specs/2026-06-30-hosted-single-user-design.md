# Hosted HeyGen pipeline — auth + v2 audio (regen → video) page

**Date:** 2026-06-30
**Branch:** `feature/tnm`
**Status:** Design approved, pending spec review

## Scope

We are hosting **the HeyGen video pipeline** for a single end user, with a few
changes to fit his requirements. This is **only** the HeyGen flow:

```
script → ElevenLabs audio → HeyGen Avatar IV render → download → NAS upload
```

Implemented today by `POST /video/heygen` → `run_video_job`
(`services/video_pipeline/pipeline.py`), with the `/heygen` page as its UI.

**Out of scope entirely:** the AutoDub pipeline (Excel batch, STT, translation,
Sarvam, S3/email). It lives in the same repo but is unrelated and untouched.

## The three changes

1. **Auth** — gate the app behind a single shared password.
2. **ElevenLabs v2 + levers** — use `eleven_multilingual_v2` with manual control
   of speed, stability, similarity, and style (v2 gives the freedom the user
   wants; the current flow uses `eleven_v3`).
3. **Audio-approval step** — let the user iterate on the audio before rendering:
   - **Option 1 — audio-first (regen loop):** generate audio from the script →
     listen → tweak levers/speed/script and **regenerate** until satisfied →
     "use this audio → generate video." The render uses the **exact approved
     audio clip** (no re-TTS).
   - **Option 2 — direct:** script → audio → video in one shot (like the current
     HeyGen flow, but on v2).

All **single-run** (no batch). Delivered as a **new page**, leaving the existing
`/heygen` page pristine (still `eleven_v3`, one-shot).

## Key facts that make this easy

- `run_video_job` already **decouples TTS from video**: it makes audio bytes,
  uploads them to HeyGen via `upload_asset`, then renders with
  `voice: {type: "audio", audio_asset_id}`. HeyGen already consumes a
  pre-generated audio file — so an approved clip renders as-is, no re-TTS.
- `VideoJobSpec.model_id` already defaults to `eleven_v3`; it just needs a
  `speed` field and an optional `audio_id`.
- Installed `elevenlabs==2.36.1` `VoiceSettings` already supports `speed`.
- `services/runtime_config.get_config_value()` falls back to `os.getenv`, so the
  HeyGen + ElevenLabs API keys are supplied via the deployment `.env` with **no
  code change**.

## Non-goals (YAGNI)

- No multi-user accounts / user DB — single shared password only.
- No batch on the new page — one clip at a time.
- No changes to the existing `/heygen` page or its `eleven_v3` one-shot behavior.
- Nothing in the AutoDub pipeline is touched.
- No auto-cleanup of generated audio files (fine for one user).

## Decisions

| Topic | Decision |
|---|---|
| API keys | Server `.env` / secret store (no code change). |
| Auth | Single shared password, signed HttpOnly cookie + middleware. |
| Page | New page (`/studio`, name TBD); `/heygen` left as-is. |
| Model | New page uses `eleven_multilingual_v2` + speed for both options. |
| Audio input | Raw script text → speak directly (no STT/translate). |
| Audio→video | Option 1 reuses the exact approved audio (skip TTS). |
| Runs | Single-run only, no batch. |
| Audio storage | Files live in `OUTPUT_DIR`, no auto-cleanup. |

## Design

### A. Auth — single shared password

- New env vars: `APP_PASSWORD` (the secret) and `APP_SESSION_SECRET` (cookie
  signing key).
- New module `api/auth.py`:
  - `GET /login` → minimal login page.
  - `POST /auth/login` → constant-time compare against `APP_PASSWORD`; on success
    set a signed, HttpOnly cookie `autodub_auth` and redirect to the new page.
  - `POST /auth/logout` → clear the cookie.
  - Cookie signed with `itsdangerous` (transitive Starlette dep — no new
    package): payload is a token + issue time, verified per request.
- **Middleware** gating every request except an allowlist — `/login`,
  `/auth/login`, `/health`, `/static/*`. Unauthenticated HTML requests → 302 to
  `/login`; other unauthenticated requests → 401 JSON.
- **Escape hatch:** if `APP_PASSWORD` is unset/empty, auth is disabled
  (pass-through) so local dev is unaffected.

### B. Keys

No code change. Document required deploy `.env` keys (HeyGen, ElevenLabs, NAS).
`get_config_value`'s `os.getenv` fallback covers server-supplied keys.

### C. Audio generation (v2 + speed) — used by both options

- `services/elevenlabs.py`:
  - Add `speed: float = 1.0` to `ElevenLabsTTSConfig`, passed into
    `VoiceSettings`. Existing v3 callers default to `1.0`; unchanged.
  - Add `AUDIO_ONLY_MODEL_ID = "eleven_multilingual_v2"`. `DEFAULT_MODEL_ID`
    (`eleven_v3`) stays for the untouched `/heygen` flow.
- `api/models.py::ElevenLabsTTSRequest`: add `speed` (default 1.0, validated
  0.7–1.2); `model_id` defaults to `eleven_multilingual_v2` here.
- `POST /tts-elevenlabs` (extend existing): raw `text` → speak directly on v2;
  returns `{ tts_url, output_file, audio_id }` (`audio_id` = saved file stem).
  This is the endpoint the regen loop calls; each call yields a new `audio_id`.

### D. Video generation + audio hand-off

- `services/video_pipeline/types.py::VideoJobSpec`: add `speed: float = 1.0` and
  optional `audio_id: str | None = None`.
- `services/video_pipeline/pipeline.py::run_video_job`:
  - Pass `spec.speed` into `ElevenLabsTTSConfig` and include it in
    `_tts_cache_key`.
  - **If `spec.audio_id` is present, skip TTS entirely** (and the cache): resolve
    the audio file, load bytes straight into `upload_asset`. Everything
    downstream (talking-photo upload, render, poll, download, NAS) unchanged.
- `POST /video/heygen` (extend existing): add optional `model_id` and `speed`
  form params. **Defaults preserve current behavior** (`/heygen` page sends
  neither → `eleven_v3`). The new page's direct option sends
  `model_id=eleven_multilingual_v2` + `speed`.
- `POST /video/heygen/from-audio` (new): Option 1's hand-off.
  - Inputs: `audio_id` + (image upload OR `talking_photo_id`) + optional
    `motion_prompt`/`video_prompt`, `width`/`height`, `video_title`, `character`.
  - Resolve `audio_id` **safely** to a file under `OUTPUT_DIR` (reject path
    traversal / missing files → 400).
  - Build a `VideoJobSpec` with `audio_id` set and dispatch `run_video_job`.

### E. New page (`static/studio.html` + JS)

- New route `GET /studio` → serves the page (mirrors how `/heygen` serves
  `static/heygen.html`).
- UI: script box, image upload / talking-photo picker, motion prompt, dimensions,
  v2 levers incl. a **speed slider (0.7–1.2)**, and two actions:
  - **Generate audio** → `/tts-elevenlabs`; audio player + download +
    **regenerate** (tweak + call again), and once happy **"Make video from this
    audio"** → `POST /video/heygen/from-audio` with the approved `audio_id`.
  - **Generate video directly** → `POST /video/heygen` with
    `model_id=eleven_multilingual_v2` + speed + levers; polls job status.
- `/login` is a separate minimal HTML page.

## Files touched

- `api/auth.py` (new) — login routes + middleware.
- `api/routes.py` — register auth; extend `/tts-elevenlabs`; add `GET /studio`;
  extend `POST /video/heygen` with `model_id`/`speed`; add `/video/heygen/from-audio`.
- `api/models.py` — `ElevenLabsTTSRequest` gains `speed`; v2 default model.
- `services/elevenlabs.py` — `speed` field + `AUDIO_ONLY_MODEL_ID`.
- `services/video_pipeline/types.py` — `VideoJobSpec.speed`, `.audio_id`.
- `services/video_pipeline/pipeline.py` — speed in TTS + cache key; skip-TTS
  branch when `audio_id` present.
- `static/studio.html` (new) + JS — the two-option page.
- `static/login.html` (or inline) — login page.
- `.env.example` — `APP_PASSWORD`, `APP_SESSION_SECRET`.

**Untouched:** `static/heygen.html`, `/heygen` behavior, and the entire AutoDub
pipeline.

## Testing

- Auth: gated route without cookie → redirect/401; valid login → access;
  `APP_PASSWORD` unset → pass-through.
- Audio (v2): `/tts-elevenlabs` honors speed; out-of-range speed rejected;
  returns a resolvable `audio_id`; regen yields distinct clips.
- Hand-off: `/video/heygen/from-audio` with valid `audio_id` skips TTS and
  renders that exact audio; bad/missing `audio_id` → 400.
- Direct: new page's direct call renders on v2 with speed applied.
- Regression: existing `/heygen` → `/video/heygen` (no model_id/speed) still uses
  `eleven_v3`.

## Open questions

- New page route name — `/studio` is a placeholder; rename freely (e.g. `/tnm`).
