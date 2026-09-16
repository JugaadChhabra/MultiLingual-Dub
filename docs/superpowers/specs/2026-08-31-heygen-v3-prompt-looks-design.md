# HeyGen v3 Migration — Prompt-Generated Outfit Looks

**Date:** 2026-08-31
**Status:** Design — pending review
**Scope:** The `services/video_pipeline/` (HeyGen "videogen") pipeline only. AutoDub is untouched.

## Goal

Let one existing synthetic character (e.g. ईश्वरी) wear many outfits/settings
across videos, by generating **prompt-driven look variations** of that character
and rotating through them automatically at render time — while **preserving the
ElevenLabs voice** the pipeline already produces.

Concretely: migrate creation to `POST /v3/avatars` (`type: "prompt"`) and the
render to `POST /v3/videos`, replacing the current v1/v2 talking-photo path.

## Context (what exists today)

The pipeline renders a talking-photo avatar over pre-rendered ElevenLabs audio:

- Per run it uploads an image → `POST /v1/talking_photo` → `talking_photo_id`
  (`heygen_client.upload_talking_photo`), working around HeyGen's 3-photo-avatar
  cap by deleting groups (`slots.py`, `clear_talking_photos`).
- Renders via `POST /v2/video/generate`, `type: "talking_photo"`,
  `use_avatar_iv_model: true`, voice `{type: "audio", audio_asset_id}` — HeyGen
  only lip-syncs (`heygen_client.create_avatar_iv_video`).
- Polls `GET /v1/video_status.get`, downloads `video_url`, burns branded cards +
  music (`overlay.burn_cards`), uploads to NAS (`pipeline._finalize_video`).

The render engine is abstracted behind `VideoRenderer` (`renderer.py`), with
`HeyGenRenderer` (`heygen_renderer.py`) as the only implementation. That seam is
the primary substitution point for this migration.

## Key decisions

1. **Looks come from prompts, not footage.** The character already exists as a
   prompt/synthetic (photo-type) avatar. New outfit looks are created with
   `POST /v3/avatars` `type: "prompt"` + `avatar_id` = the character's base look,
   which keeps identity consistent and saves the new look into the same group.
   No video footage, **no consent flow** (photo/prompt avatars have
   `consent_status: null`).

2. **Render engine is Avatar IV (v3 default).** Prompt/photo looks are
   `photo_avatar` type; **Avatar V renders only `digital_twin` looks and is
   therefore out of scope.** `POST /v3/videos` with `engine` omitted uses
   Avatar IV, which accepts photo avatars.

3. **ElevenLabs voice is preserved.** `POST /v3/videos` accepts `audio_asset_id`
   ("HeyGen asset ID of an uploaded audio file to lip-sync"), mutually exclusive
   with `script`/`voice_id`. So TTS + content-hash cache + `upload_asset`
   (`POST /v1/asset`) all stay; only the render endpoint changes.

4. **Hard cutover.** The v2 talking-photo render path and the 3-avatar-cap
   rotation are removed, not kept behind a flag.

5. **Auto-rotation across the ready wardrobe**, with a per-render override.

6. **Wardrobe management gets a new UI panel** backed by new routes.

### Superseded (recorded so the reasoning isn't re-litigated)

An earlier turn explored **Avatar V + digital twins + multiple filmed
recordings + consent**. That is abandoned: the character is prompt-generated, and
the priority is easy prompt outfit-variations over Avatar V fidelity. Avatar V,
digital twins, and consent are **not** part of this build.

## Non-goals (YAGNI)

- Avatar V, `digital_twin` avatars, `reference_look_id`, consent flow.
- HeyGen TTS voices (`script`/`voice_id`) — we feed our own audio.
- Burned-in captions (`caption` object) — we burn our own branded cards; download
  the clean `video_url`.
- Seeding a brand-new base character from scratch in-app — the base character
  already exists; we only add looks to it. (Base look id is configuration.)

## Architecture

### New/changed HeyGen client functions (`heygen_client.py`)

**Wardrobe (avatar looks):**

- `create_prompt_look(*, api_key, name, prompt, avatar_id) -> str`
  `POST /v3/avatars` `{type:"prompt", name, prompt, avatar_id}` → returns
  `data.avatar_item.id` (the new look id). `avatar_id` is the base character look,
  used as visual reference; the new look lands in the same group automatically.
- `get_look(*, api_key, look_id) -> dict`
  `GET /v3/avatars/looks/{look_id}` → `{id, name, avatar_type, status,
  supported_api_engines, preview_image_url, group_id}`.
- `list_looks(*, api_key, group_id) -> list[dict]`
  `GET /v3/avatars/looks?group_id=...` → all looks in the character's group;
  callers filter to `status == "completed"`.

**Render (v3 videos):**

- `create_v3_video(*, api_key, avatar_id, audio_asset_id, motion_prompt,
  aspect_ratio, resolution, video_title, callback_id) -> str`
  `POST /v3/videos` `{type:"avatar", avatar_id, audio_asset_id, motion_prompt?,
  aspect_ratio, resolution, title, callback_id?}` (engine omitted = Avatar IV) →
  `data.video_id`. Keeps the existing non-idempotent-submit discipline:
  `retries=1` on the POST, and on a transport error, adopt an orphaned render via
  `callback_id` before resubmitting (see Recovery risk below).
- `get_v3_video_status(*, api_key, video_id) -> dict`
  `GET /v3/videos/{video_id}` → `{status, video_url, captioned_video_url,
  duration, failure_code, failure_message}`. Status values: `pending`,
  `processing`, `completed`, `failed`.
- `poll_v3_until_done(...)` — same structure as `poll_until_done` but keyed on the
  v3 status vocabulary and reads `failure_message` on failure.

**Reused unchanged:** `upload_asset` (audio → `audio_asset_id`), `download_video`
(Range-resumed download of the clean `video_url`), `_send` retry wrapper.

**Removed:** `create_avatar_iv_video`, `get_video_status`, `poll_until_done`
(v1 variant), `upload_talking_photo` / `_post_talking_photo`,
`list_talking_photos` / `_fetch_first_look_id`, `delete_avatar_group`,
`clear_talking_photos` / `_list_avatar_group_ids`, `QUOTA_EXCEEDED_CODE`, and the
entire `slots.py` module (`TalkingPhotoSlots`).

### Renderer seam (`renderer.py` / `heygen_renderer.py`)

- `submit(...)`: `photo_id` param → `avatar_id` (the look id); replace
  `width`/`height` with `aspect_ratio`/`resolution`; body maps to
  `create_v3_video`.
- `await_render(...)`: calls `poll_v3_until_done`; `RenderedVideo.video_url` reads
  the clean `video_url` (never `captioned_video_url`).
- Remove `upload_photo` / `clear_photos` from the seam (talking-photo concept is
  gone).

### Wardrobe service (`wardrobe.py`, new)

Thin orchestration over the client, per character:

- `add_look(name, prompt)`: `create_prompt_look` (avatar_id = base look) → poll
  `get_look` until `status == "completed"` → return the look summary.
- `ready_looks()`: `list_looks(group_id)` filtered to `completed` (and
  `avatar_type == "photo_avatar"`).
- Resolves `group_id` + base look id from config (below).

### Selection / rotation

- **Batch:** resolve `ready_looks()` once, **shuffle and deal** one look per row
  (cycling if rows > looks). No shared mutable cursor — safe under the concurrent
  batch workers. A row's explicit `look_id` always overrides.
- **Single render:** pick a random ready look, unless `spec.look_id` is set.
- Selection happens in the batch runner / route edge and is written onto each
  `VideoJobSpec.look_id`, so `run_video_job` just consumes a resolved look id.

### Pipeline changes (`pipeline.py`)

- `run_video_job` drops `image_bytes`, `image_filename`, `slots`; the render no
  longer uploads a per-run photo. It resolves `spec.look_id` (required by this
  point) and submits.
- Replace the image-geometry clamp (`dimensions` / `clamp_for_render`) with a
  fixed `aspect_ratio="9:16"`, `resolution="1080p"` (matching the 1080×1920 frame
  `overlay.burn_cards` expects). `image_geometry.py` usage retires here.
- `_finalize_video` and the card-burn + music + NAS tail are **unchanged**.

### Data model (`types.py`)

- `VideoJobSpec.talking_photo_id` → `look_id: str | None`.
- Remove `width` / `height` (aspect is fixed 9:16); keep `motion_prompt`,
  `video_title`, `publish_date`, `character`, and all TTS fields.

### Config (`.env` / `runtime_config`)

Per character, resolved through the existing session-env/`read_setting` system:

- `ISHWARI_AVATAR_GROUP_ID` — the character's HeyGen group (for `list_looks`).
- `ISHWARI_BASE_LOOK_ID` — the base look passed as `avatar_id` when creating new
  looks.
- (`US_AVATAR_GROUP_ID` / `US_BASE_LOOK_ID` if/when the `us` character is wired.)

`HEYGEN_ISHWARI` (API key) and the ElevenLabs voice ids are unchanged.

### Routes + UI (`api/routes.py`, `static/`)

- `POST /video/heygen/looks` — create a prompt look `{name, prompt}` (returns
  look id, kicks off training).
- `GET /video/heygen/looks` — list looks with `status` + `preview_image_url`.
- `GET /video/heygen/looks/{look_id}` — poll one look's training status.
- Retire/redirect the old talking-photo routes.
- New "Wardrobe" pane: enter a prompt to add a look, see each look's thumbnail +
  training status, browse the ready wardrobe. Follows existing pane conventions
  (`static/pane-video.js`, `shell.js`).

### Recovery (`recover_video_job`)

Shape unchanged: re-fetch a fresh `video_url` via `get_v3_video_status`
(presigned URLs expire), then re-run the download → card → NAS tail. The
`callback_id` adoption for a lost submit response depends on a v3 "list recent
videos" endpoint — see risks.

## Verification plan (spike — first execution step)

Run against a real `HEYGEN_ISHWARI` key **before** building, since the whole
premise rests on #1:

1. **Bridge:** create a prompt look off the base character, poll to `completed`,
   then `POST /v3/videos` `type:"avatar"` + that look as `avatar_id` +
   `audio_asset_id` (a real ElevenLabs asset) + engine omitted → confirm a
   lip-synced video renders and `video_url` downloads.
2. **Look shape:** confirm the created look's `avatar_type` and
   `supported_api_engines`, and that `list_looks?group_id=` returns it once
   `completed`.
3. **Frame:** confirm `aspect_ratio:"9:16"` + `resolution:"1080p"` yields a
   1080×1920 render the overlay burns cleanly onto.
4. **Recovery:** confirm whether a v3 endpoint lists recent videos by/with
   `callback_id`. If not, document that submit-response-loss recovery degrades to
   "persist `video_id` from a successful submit" (which still covers the common
   post-submit failure path).

If #1 fails (Avatar IV over `audio_asset_id` for a prompt look doesn't lip-sync
as expected), stop and re-open the voice decision before writing any code.

## Testing

Mirror the existing suite (`tests/test_heygen_client.py`, `test_video_job.py`,
`test_video_batch.py`) using the `_transport` `MockTransport` seam:

- Client: prompt-look create parses `avatar_item.id`; v3 status polling maps
  `processing`/`completed`/`failed`; submit stays single-shot with callback
  adoption.
- Rotation: shuffled deal covers the wardrobe without repeats until it must cycle;
  per-row `look_id` overrides.
- Pipeline: `run_video_job` submits with the resolved look id and reaches the
  unchanged finalize tail; recovery re-fetches a fresh `video_url`.

## Risks / open questions

- **[Blocking] Avatar IV + `audio_asset_id` on a prompt look** — the core bridge
  (spike #1). Everything downstream assumes it works.
- **Recovery by `callback_id`** — needs a v3 list endpoint; may degrade
  gracefully (spike #4).
- **Look training latency/failure** — prompt-look generation is async and can
  fail; the wardrobe must surface `failed` looks and keep them out of rotation.
- **Empty wardrobe** — if no look is `completed`, a render has nothing to rotate
  to. Define the fallback (fail clearly, or fall back to the base look id).
