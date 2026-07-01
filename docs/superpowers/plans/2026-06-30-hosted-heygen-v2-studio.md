# Hosted HeyGen Pipeline — v2 Studio + Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Host the existing HeyGen video pipeline for a single user, adding a new "studio" page that runs on ElevenLabs v2 with speed/levers and an audio-approval (regen → video) step, behind a single shared password.

**Architecture:** Additive changes only. A new `/studio` page and a `/video/heygen/from-audio` endpoint sit alongside the untouched `/heygen` flow. The existing video pipeline already decouples TTS from rendering (audio bytes → `upload_asset` → render with `voice.type=audio`), so an approved audio clip renders without re-running TTS. Auth is a global middleware with an escape hatch (disabled when `APP_PASSWORD` is unset) so existing behavior/tests are unaffected.

**Tech Stack:** FastAPI, Pydantic, `elevenlabs==2.36.1` SDK (`VoiceSettings` already supports `speed`), `itsdangerous` (transitive Starlette dep) for cookie signing, vanilla HTML/JS front-end, pytest + `fastapi.testclient.TestClient`.

## Global Constraints

- ElevenLabs `speed` is valid only in the range **0.7–1.2** (v2). Clamp/validate to this.
- New audio-only model id is **`eleven_multilingual_v2`** (constant `AUDIO_ONLY_MODEL_ID`). The existing `/heygen` flow must keep defaulting to **`eleven_v3`**.
- Auth must be **disabled when `APP_PASSWORD` is unset/empty** (pass-through), read from env **per request** (not at import time).
- No new pip dependencies. Use `itsdangerous` (already available via Starlette) and stdlib `secrets`.
- No changes to the AutoDub pipeline, the `/heygen` page, or batch flows.
- Tests run with: `source venv/bin/activate && pytest <path> -v` from the repo root.

---

### Task 1: ElevenLabs `speed` field + audio-only model constant

**Files:**
- Modify: `services/elevenlabs.py`
- Test: `tests/test_elevenlabs_speed.py`

**Interfaces:**
- Produces: `ElevenLabsTTSConfig(voice_id, model_id, stability, similarity_boost, style, use_speaker_boost, speed=1.0)`; module constant `AUDIO_ONLY_MODEL_ID = "eleven_multilingual_v2"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_elevenlabs_speed.py
from unittest.mock import MagicMock, patch

from services.elevenlabs import (
    AUDIO_ONLY_MODEL_ID,
    ElevenLabsTTSConfig,
    _synthesize_once,
)


def test_audio_only_model_constant() -> None:
    assert AUDIO_ONLY_MODEL_ID == "eleven_multilingual_v2"


def test_config_defaults_speed_to_one() -> None:
    cfg = ElevenLabsTTSConfig(
        voice_id="v", model_id="eleven_v3", stability=0.5,
        similarity_boost=0.75, style=0.0, use_speaker_boost=True,
    )
    assert cfg.speed == 1.0


def test_synthesize_passes_speed_to_voice_settings() -> None:
    cfg = ElevenLabsTTSConfig(
        voice_id="v", model_id=AUDIO_ONLY_MODEL_ID, stability=0.3,
        similarity_boost=0.6, style=0.1, use_speaker_boost=False, speed=0.9,
    )
    fake_client = MagicMock()
    fake_client.text_to_speech.convert.return_value = [b"audio-bytes"]
    with patch("services.elevenlabs.ElevenLabs", return_value=fake_client):
        out = _synthesize_once("hello", api_key="k", config=cfg)
    assert out == b"audio-bytes"
    kwargs = fake_client.text_to_speech.convert.call_args.kwargs
    assert kwargs["model_id"] == "eleven_multilingual_v2"
    assert kwargs["voice_settings"].speed == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && pytest tests/test_elevenlabs_speed.py -v`
Expected: FAIL — `ImportError: cannot import name 'AUDIO_ONLY_MODEL_ID'` (and `ElevenLabsTTSConfig` missing `speed`).

- [ ] **Step 3: Write minimal implementation**

In `services/elevenlabs.py`, add the constant near the existing defaults (after line 15):

```python
AUDIO_ONLY_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_SPEED = 1.0
```

Add `speed` to the dataclass (it is `@dataclass(frozen=True)`), as the last field with a default:

```python
@dataclass(frozen=True)
class ElevenLabsTTSConfig:
    voice_id: str
    model_id: str
    stability: float
    similarity_boost: float
    style: float
    use_speaker_boost: bool
    speed: float = DEFAULT_SPEED
```

In `_synthesize_once`, pass `speed` into `VoiceSettings`:

```python
        voice_settings=VoiceSettings(
            stability=config.stability,
            similarity_boost=config.similarity_boost,
            style=config.style,
            use_speaker_boost=config.use_speaker_boost,
            speed=config.speed,
        ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source venv/bin/activate && pytest tests/test_elevenlabs_speed.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the existing ElevenLabs tests to confirm no regression**

Run: `source venv/bin/activate && pytest tests/test_elevenlabs.py -v`
Expected: PASS (existing 3 tests still green — `get_batch_config_for_language` defaults `speed=1.0`).

- [ ] **Step 6: Commit**

```bash
git add services/elevenlabs.py tests/test_elevenlabs_speed.py
git commit -m "feat(tts): add speed to ElevenLabsTTSConfig + AUDIO_ONLY_MODEL_ID"
```

---

### Task 2: `/tts-elevenlabs` → v2 default, speed lever, returns `audio_id`

**Files:**
- Modify: `api/models.py:16-23` (`ElevenLabsTTSRequest`)
- Modify: `api/routes.py:505-540` (`tts_elevenlabs`)
- Test: `tests/test_tts_endpoint.py`

**Interfaces:**
- Consumes: `ElevenLabsTTSConfig(..., speed=...)` (Task 1), `AUDIO_ONLY_MODEL_ID` (Task 1).
- Produces: `POST /tts-elevenlabs` returns JSON `{ "tts_url": str, "output_file": str, "audio_id": str }` where `audio_id` is the saved file stem (e.g. `elevenlabs-<hex>`). `ElevenLabsTTSRequest` now has `speed: float = 1.0` (validated 0.7–1.2) and `model_id: str = "eleven_multilingual_v2"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tts_endpoint.py
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import routes as api


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ELEVEN_LABS", "test-key")
    return TestClient(api.app)


def test_tts_returns_audio_id_and_uses_speed(monkeypatch) -> None:
    client = _client(monkeypatch)
    with patch("api.routes.synthesize_speech_bytes", return_value=b"xx") as mock:
        resp = client.post(
            "/tts-elevenlabs",
            json={"text": "hello", "voice_id": "v", "speed": 0.9},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["audio_id"].startswith("elevenlabs-")
    assert body["tts_url"].endswith(".mp3")
    cfg = mock.call_args.kwargs["config"]
    assert cfg.model_id == "eleven_multilingual_v2"
    assert cfg.speed == 0.9


def test_tts_rejects_out_of_range_speed(monkeypatch) -> None:
    client = _client(monkeypatch)
    resp = client.post(
        "/tts-elevenlabs",
        json={"text": "hi", "voice_id": "v", "speed": 2.0},
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && pytest tests/test_tts_endpoint.py -v`
Expected: FAIL — response has no `audio_id`; `cfg.model_id` is `eleven_v3`; speed 2.0 is accepted (no 422).

- [ ] **Step 3: Update the request model**

In `api/models.py`, replace `ElevenLabsTTSRequest` (lines 16-23) with:

```python
from pydantic import BaseModel, Field


class ElevenLabsTTSRequest(BaseModel):
    text: str
    voice_id: str
    model_id: str = "eleven_multilingual_v2"
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True
    speed: float = Field(default=1.0, ge=0.7, le=1.2)
```

(Adjust the existing `from pydantic import BaseModel` line at the top to also import `Field`.)

- [ ] **Step 4: Update the route to pass speed and return `audio_id`**

In `api/routes.py`, in `tts_elevenlabs`, add `speed=payload.speed` to the `ElevenLabsTTSConfig(...)` call (inside the `synthesize_speech_bytes` call, after `use_speaker_boost=payload.use_speaker_boost,`):

```python
                use_speaker_boost=payload.use_speaker_boost,
                speed=payload.speed,
```

Then change the final return (lines 537-540) to include `audio_id`:

```python
    return {
        "tts_url": tts_url,
        "output_file": str(out_path),
        "audio_id": out_path.stem,
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `source venv/bin/activate && pytest tests/test_tts_endpoint.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add api/models.py api/routes.py tests/test_tts_endpoint.py
git commit -m "feat(tts): /tts-elevenlabs defaults v2, honors speed, returns audio_id"
```

---

### Task 3: Video pipeline — `speed`, `audio_id`, skip-TTS + safe audio resolution

**Files:**
- Modify: `services/video_pipeline/types.py:7-23` (`VideoJobSpec`)
- Modify: `services/video_pipeline/pipeline.py` (`_tts_cache_key`, add `resolve_audio_path`, `run_video_job`)
- Test: `tests/test_pipeline_audio.py`

**Interfaces:**
- Consumes: `ElevenLabsTTSConfig(..., speed=...)` (Task 1).
- Produces:
  - `VideoJobSpec` gains `speed: float = 1.0` and `audio_id: str | None = None`.
  - `resolve_audio_path(audio_id: str, output_dir: Path) -> Path` (in `pipeline.py`), raising `ValueError` for path-traversal or missing files.
  - `run_video_job` skips TTS when `spec.audio_id` is set, loading bytes from the resolved path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline_audio.py
from pathlib import Path

import pytest

from services.video_pipeline.pipeline import _tts_cache_key, resolve_audio_path


def test_cache_key_differs_by_speed() -> None:
    common = dict(
        script="hi", voice_id="v", model_id="eleven_multilingual_v2",
        stability=0.5, similarity_boost=0.75, style=0.0, use_speaker_boost=True,
    )
    assert _tts_cache_key(**common, speed=1.0) != _tts_cache_key(**common, speed=0.9)


def test_resolve_audio_path_ok(tmp_path: Path) -> None:
    f = tmp_path / "elevenlabs-abc.mp3"
    f.write_bytes(b"x")
    assert resolve_audio_path("elevenlabs-abc", tmp_path) == f.resolve()


def test_resolve_audio_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_audio_path("../../etc/passwd", tmp_path)


def test_resolve_audio_path_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_audio_path("nope", tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && pytest tests/test_pipeline_audio.py -v`
Expected: FAIL — `resolve_audio_path` does not exist; `_tts_cache_key` has no `speed` kwarg.

- [ ] **Step 3: Add `speed` + `audio_id` to the spec**

In `services/video_pipeline/types.py`, add to `VideoJobSpec` (after `use_speaker_boost` on line 15):

```python
    speed: float = 1.0
```

and after `talking_photo_id` (line 19):

```python
    audio_id: str | None = None
```

- [ ] **Step 4: Add speed to the cache key**

In `services/video_pipeline/pipeline.py`, update `_tts_cache_key` (lines 117-132) signature and payload to include `speed`:

```python
def _tts_cache_key(*, script: str, voice_id: str, model_id: str, stability: float,
                   similarity_boost: float, style: float, use_speaker_boost: bool,
                   speed: float) -> str:
    payload = json.dumps(
        {
            "script": script,
            "voice_id": voice_id,
            "model_id": model_id,
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": use_speaker_boost,
            "speed": speed,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

- [ ] **Step 5: Add the safe audio resolver**

In `services/video_pipeline/pipeline.py`, add near the top (after the imports, before `_image_dimensions`):

```python
def resolve_audio_path(audio_id: str, output_dir: Path) -> Path:
    """Resolve a `<audio_id>.mp3` file inside output_dir, rejecting traversal.

    audio_id is the file stem returned by POST /tts-elevenlabs. Raises
    ValueError if the resolved path escapes output_dir or does not exist."""
    root = output_dir.resolve()
    candidate = (root / f"{audio_id}.mp3").resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"audio_id escapes output dir: {audio_id!r}")
    if not candidate.exists():
        raise ValueError(f"audio not found for audio_id: {audio_id!r}")
    return candidate
```

- [ ] **Step 6: Wire speed + skip-TTS into `run_video_job`**

In `run_video_job` (`pipeline.py`), the TTS block starts around line 168. Update the cache key call to pass speed:

```python
        cache_key = _tts_cache_key(
            script=spec.script,
            voice_id=voice_id,
            model_id=spec.model_id,
            stability=spec.stability,
            similarity_boost=spec.similarity_boost,
            style=spec.style,
            use_speaker_boost=spec.use_speaker_boost,
            speed=spec.speed,
        )
```

Add `speed=spec.speed,` to the `ElevenLabsTTSConfig(...)` built for `synthesize_speech_bytes` (after `use_speaker_boost=spec.use_speaker_boost,`).

Then, at the very start of the TTS section (right after `audio_path = job_dir / "audio.mp3"`, before the cache-dir lines ~174), short-circuit when a pre-generated clip was supplied:

```python
        if spec.audio_id:
            await jobs_store.set_status(job_id, "tts", "Using pre-generated audio")
            src = resolve_audio_path(spec.audio_id, output_dir)
            audio_bytes = src.read_bytes()
            audio_path.write_bytes(audio_bytes)
            await jobs_store.patch_summary(
                job_id, audio_bytes=len(audio_bytes), audio_path=str(audio_path)
            )
        else:
            # ... existing cache-check + synthesize block, unchanged ...
```

Indent the existing cache/synthesize block (the `cache_dir = ...` through the `audio_path.write_bytes(audio_bytes)` + `patch_summary` lines, ~174-215) under the new `else:`. Everything from "2. Upload audio (asset)" onward stays exactly as-is.

- [ ] **Step 7: Run tests to verify they pass**

Run: `source venv/bin/activate && pytest tests/test_pipeline_audio.py -v`
Expected: PASS (4 tests).

- [ ] **Step 8: Commit**

```bash
git add services/video_pipeline/types.py services/video_pipeline/pipeline.py tests/test_pipeline_audio.py
git commit -m "feat(video): speed + audio_id skip-TTS with safe audio resolution"
```

---

### Task 4: `/video/heygen` gains model_id+speed; new `/video/heygen/from-audio`

**Files:**
- Modify: `api/routes.py` (`create_heygen_video_job` ~130-195; add `create_heygen_from_audio_job`)
- Test: `tests/test_from_audio_endpoint.py`

**Interfaces:**
- Consumes: `VideoJobSpec(..., speed=..., audio_id=...)` (Task 3), `resolve_audio_path` (Task 3), `run_video_job` (existing).
- Produces:
  - `POST /video/heygen` accepts optional form fields `model_id` (default `eleven_v3`) and `speed` (default 1.0), threaded into the spec.
  - `POST /video/heygen/from-audio` → `{ "job_id": str, "status": "queued" }` (202). Validates `audio_id` (400 on bad/missing), requires image or `talking_photo_id`, dispatches `run_video_job` with `audio_id` set.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_from_audio_endpoint.py
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import routes as api


def test_from_audio_rejects_bad_audio_id() -> None:
    client = TestClient(api.app)
    resp = client.post(
        "/video/heygen/from-audio",
        data={"audio_id": "does-not-exist", "talking_photo_id": "tp_1"},
    )
    assert resp.status_code == 400


def test_from_audio_queues_job(tmp_path, monkeypatch) -> None:
    # audio files resolve under OUTPUT_DIR; point it at tmp and drop a file
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path)
    (tmp_path / "elevenlabs-abc.mp3").write_bytes(b"xx")
    client = TestClient(api.app)
    with patch("api.routes.run_video_job", return_value=None), \
         patch("api.routes.asyncio.create_task") as mock_task:
        resp = client.post(
            "/video/heygen/from-audio",
            data={"audio_id": "elevenlabs-abc", "talking_photo_id": "tp_1"},
        )
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    assert mock_task.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && pytest tests/test_from_audio_endpoint.py -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Add `model_id` + `speed` to `create_heygen_video_job`**

In `api/routes.py`, add two params to the `create_heygen_video_job` signature (after `use_speaker_boost`):

```python
    model_id: str = Form(default="eleven_v3"),
    speed: float = Form(default=1.0),
```

And add them to the `VideoJobSpec(...)` construction (after `use_speaker_boost=use_speaker_boost,`):

```python
        model_id=model_id,
        speed=speed,
```

- [ ] **Step 4: Add the `from-audio` endpoint**

In `api/routes.py`, import the resolver near the other video_pipeline imports:

```python
from services.video_pipeline.pipeline import resolve_audio_path
```

Add the endpoint after `create_heygen_video_job` (before `get_heygen_video_job`):

```python
@app.post("/video/heygen/from-audio", status_code=202)
async def create_heygen_from_audio_job(
    request: Request,
    audio_id: str = Form(...),
    image: UploadFile | None = File(default=None),
    talking_photo_id: str | None = Form(default=None),
    character: str = Form(default="indian"),
    voice_id: str | None = Form(default=None),
    video_prompt: str | None = Form(default=None),
    motion_prompt: str | None = Form(default=None),
    width: int | None = Form(default=None),
    height: int | None = Form(default=None),
    video_title: str = Form(default="HeyGen Avatar IV Job"),
):
    runtime_config = await _runtime_config_for_request(request)

    try:
        resolve_audio_path(audio_id, OUTPUT_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not (talking_photo_id or (image and image.filename)):
        raise HTTPException(status_code=400, detail="provide either an image file or a talking_photo_id")

    image_bytes = b""
    image_filename = "image.jpg"
    if image and image.filename:
        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="image upload was empty")
        image_filename = image.filename

    spec = VideoJobSpec(
        script="",
        character=character or "indian",
        voice_id=voice_id or None,
        video_prompt=video_prompt or None,
        motion_prompt=motion_prompt or None,
        width=width,
        height=height,
        video_title=video_title,
        talking_photo_id=talking_photo_id or None,
        audio_id=audio_id,
    )

    job_id = uuid.uuid4().hex
    await video_jobs_store.create(job_id)

    asyncio.create_task(
        run_video_job(
            job_id=job_id,
            spec=spec,
            image_bytes=image_bytes,
            image_filename=image_filename,
            output_dir=VIDEO_OUTPUT_DIR,
            jobs_store=video_jobs_store,
            runtime_config=runtime_config,
        )
    )

    return {"job_id": job_id, "status": "queued"}
```

> Note: `run_video_job` resolves `audio_id` against its `output_dir` (`VIDEO_OUTPUT_DIR`), but the audio-only endpoint saves clips to `OUTPUT_DIR`. To keep one source of truth, the from-audio job must read from `OUTPUT_DIR`. In `run_video_job`, the skip-TTS branch (Task 3, Step 6) calls `resolve_audio_path(spec.audio_id, output_dir)` — pass `output_dir=OUTPUT_DIR` here for the from-audio dispatch instead of `VIDEO_OUTPUT_DIR`. Change the `output_dir=VIDEO_OUTPUT_DIR` line in THIS endpoint's `run_video_job(...)` call to `output_dir=OUTPUT_DIR`. (The job's own `job_dir`/video output still nest under `OUTPUT_DIR`, matching `/output` mounting.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `source venv/bin/activate && pytest tests/test_from_audio_endpoint.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Regression — existing heygen route still builds a v3 spec**

Run: `source venv/bin/activate && pytest tests/test_api_batch.py tests/test_session_config_api.py -v`
Expected: PASS (no regressions from the new form fields / imports).

- [ ] **Step 7: Commit**

```bash
git add api/routes.py tests/test_from_audio_endpoint.py
git commit -m "feat(video): model_id/speed on /video/heygen + /video/heygen/from-audio"
```

---

### Task 5: New `/studio` page (v2 levers + audio regen → video)

**Files:**
- Create: `static/studio.html` (copy of `static/heygen.html`, adapted)
- Modify: `api/routes.py` (add `GET /studio`)
- Test: manual (no JS test infra in repo)

**Interfaces:**
- Consumes: `POST /tts-elevenlabs` (Task 2), `POST /video/heygen` with `model_id`/`speed` (Task 4), `POST /video/heygen/from-audio` (Task 4), `GET /video/heygen/{job_id}` (existing).
- Produces: `GET /studio` serving the page.

- [ ] **Step 1: Add the route (and a temporary failing check)**

In `api/routes.py`, add after `heygen_page` (line ~116):

```python
@app.get("/studio")
def studio_page() -> FileResponse:
    page = Path("./static/studio.html")
    if not page.exists():
        raise HTTPException(status_code=404, detail="studio.html not found")
    return FileResponse(page)
```

- [ ] **Step 2: Create the page from the HeyGen page**

```bash
cp static/heygen.html static/studio.html
```

- [ ] **Step 3: Retitle and drop batch mode**

In `static/studio.html`:
- Change `<title>` (line ~7) to `AutoDub · Studio`.
- In the mode segment (`#modeSeg`, lines ~250-258), replace the two buttons with the two studio options:

```html
              <button type="button" class="on" data-mode="single">
                <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>audio first
              </button>
              <button type="button" data-mode="direct">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><polygon points="5 3 19 12 5 21 5 3"/></svg>direct
              </button>
```

- Delete the entire `#batchFields` block (lines ~281-293) and the `#pubDate`/excel JS (the `onExcel`, `xlsDrop`, `pubDate` handlers ~552-566, the `pollBatch` function ~810-826, and the batch branch of `submit` ~767-785). The queue tray can stay but is unused; leave the DOM.

- [ ] **Step 4: Add the v2 levers UI**

In `static/studio.html`, inside `#singleFields` (after the script `.script-wrap`, before the closing `</div>` of `#singleFields`, ~line 279), add:

```html
          <div class="lbl">voice id<span class="r"></span><span class="n">ElevenLabs v2</span></div>
          <input type="text" id="voiceId" class="dateinp" placeholder="ElevenLabs voice id" />

          <div class="lbl">levers<span class="r"></span><span class="n">v2 controls</span></div>
          <label class="lever">speed <output id="speedOut">1.00</output>
            <input type="range" id="speed" min="0.7" max="1.2" step="0.01" value="1" /></label>
          <label class="lever">stability <output id="stabOut">0.50</output>
            <input type="range" id="stability" min="0" max="1" step="0.01" value="0.5" /></label>
          <label class="lever">similarity <output id="simOut">0.75</output>
            <input type="range" id="similarity" min="0" max="1" step="0.01" value="0.75" /></label>
          <label class="lever">style <output id="styleOut">0.00</output>
            <input type="range" id="style" min="0" max="1" step="0.01" value="0" /></label>
          <label class="lever chk"><input type="checkbox" id="speakerBoost" checked /> speaker boost</label>

          <div id="audioResult" hidden>
            <div class="lbl">audio<span class="r"></span><span class="n">approve or regenerate</span></div>
            <audio id="audioPlayer" controls style="width:100%"></audio>
          </div>
```

Add minimal lever styling inside the page `<style>` block (near `.dateinp`, ~line 113):

```css
  .lever{ display:flex; align-items:center; gap:10px; font-family:var(--mono); font-size:11px; color:var(--muted); margin-top:10px; }
  .lever input[type=range]{ flex:1; accent-color:var(--accent); }
  .lever output{ min-width:38px; text-align:right; color:var(--ink); }
  .lever.chk{ gap:7px; }
```

- [ ] **Step 5: Replace the run controls (three actions)**

In `static/studio.html`, replace the `.railfoot` block (~296-299) with:

```html
      <div class="railfoot">
        <button class="run-btn" id="genAudio"><span class="recdot"></span><span class="rlabel">Generate audio</span></button>
        <button class="run-btn" id="makeVideo" hidden style="margin-top:8px"><span class="recdot"></span><span class="rlabel">Make video from this audio</span></button>
        <button class="run-btn" id="genVideo" hidden style="margin-top:8px"><span class="recdot"></span><span class="rlabel">Generate video directly</span></button>
      </div>
```

The `#genAudio`/`#makeVideo` pair shows in "audio first" mode; `#genVideo` shows in "direct" mode.

- [ ] **Step 6: Rewire the page JS (levers, mode, three actions)**

In `static/studio.html`, replace the `submit`/run wiring (the `runBtn` block ~722-786) and `setMode` (~457-463) with the studio controller below. Keep all other helpers (`onImage`, `showVideo`, `showDownload`, `pollSingle`, transport, categories) as-is.

```js
  // ── studio mode: 'single' (audio-first) or 'direct' ──
  let mode = "single";
  const genAudio = $("#genAudio"), makeVideo = $("#makeVideo"), genVideo = $("#genVideo");
  function setMode(m) {
    mode = m;
    document.querySelectorAll("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
    updateSegInd();
    genAudio.hidden = m !== "single";
    makeVideo.hidden = true;                 // shown after audio generates
    genVideo.hidden = m !== "direct";
  }
  $("#modeSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setMode(b.dataset.mode); });

  // ── levers ──
  const lever = (id, out, dp) => { const el = $("#" + id), o = $("#" + out); const upd = () => o.textContent = Number(el.value).toFixed(dp); el.addEventListener("input", upd); upd(); return () => Number(el.value); };
  const getSpeed = lever("speed", "speedOut", 2);
  const getStab = lever("stability", "stabOut", 2);
  const getSim = lever("similarity", "simOut", 2);
  const getStyle = lever("style", "styleOut", 2);
  const getBoost = () => $("#speakerBoost").checked;
  const getVoice = () => $("#voiceId").value.trim();

  let approvedAudioId = null;
  const audioResult = $("#audioResult"), audioPlayer = $("#audioPlayer");

  function catPrompts() { const c = activeCat(); return { video: c && c.video_prompt, motion: c && c.motion_prompt }; }

  // ── generate audio (regen loop) ──
  genAudio.addEventListener("click", async () => {
    if (running) return;
    const text = scriptInput.value.trim();
    if (!text) { flash("Write a script first"); return; }
    if (!getVoice()) { flash("Enter a voice id"); return; }
    running = true; refreshRunState && refreshRunState(); setRunLabelFor(genAudio, "Generating…");
    resetLogs(); startClock(); setTransport("submitting", "synthesizing audio…"); addLog("generating audio · v2");
    try {
      const res = await fetch("/tts-elevenlabs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text, voice_id: getVoice(), model_id: "eleven_multilingual_v2",
          speed: getSpeed(), stability: getStab(), similarity_boost: getSim(),
          style: getStyle(), use_speaker_boost: getBoost(),
        }),
      });
      if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
      const data = await res.json();
      approvedAudioId = data.audio_id;
      audioPlayer.src = data.tts_url; audioResult.hidden = false;
      makeVideo.hidden = false;
      setTransport("complete", "audio ready — approve or tweak & regenerate");
      addLog(`audio ready · ${data.audio_id}`, "ok"); chord();
      setRunLabelFor(genAudio, "Regenerate audio");
    } catch (e) { addLog(e.message, "error"); setTransport("failed", e.message); setRunLabelFor(genAudio, "Generate audio"); }
    finally { running = false; stopClock(); }
  });

  // ── make video from approved audio ──
  makeVideo.addEventListener("click", async () => {
    if (running) return;
    if (!approvedAudioId) { flash("Generate audio first"); return; }
    if (!imageFile && !talkingPhotoId()) { flash("Drop an avatar image"); return; }
    const p = catPrompts();
    const fd = new FormData();
    fd.append("audio_id", approvedAudioId);
    if (imageFile) fd.append("image", imageFile);
    fd.append("character", character);
    if (p.video) fd.append("video_prompt", p.video);
    if (p.motion) fd.append("motion_prompt", p.motion);
    await submitVideo("/video/heygen/from-audio", fd, makeVideo, "Make video from this audio");
  });

  // ── direct: script → audio → video (v2) ──
  genVideo.addEventListener("click", async () => {
    if (running) return;
    const text = scriptInput.value.trim();
    if (!text) { flash("Write a script first"); return; }
    if (!getVoice()) { flash("Enter a voice id"); return; }
    if (!imageFile) { flash("Drop an avatar image"); return; }
    const p = catPrompts();
    const fd = new FormData();
    fd.append("image", imageFile);
    fd.append("script", text);
    fd.append("character", character);
    fd.append("voice_id", getVoice());
    fd.append("model_id", "eleven_multilingual_v2");
    fd.append("speed", String(getSpeed()));
    fd.append("stability", String(getStab()));
    fd.append("similarity_boost", String(getSim()));
    fd.append("style", String(getStyle()));
    fd.append("use_speaker_boost", getBoost() ? "true" : "false");
    if (p.video) fd.append("video_prompt", p.video);
    if (p.motion) fd.append("motion_prompt", p.motion);
    await submitVideo("/video/heygen", fd, genVideo, "Generate video directly");
  });

  // talking-photo id is not used on this page (image upload only); stub returns null
  function talkingPhotoId() { return null; }
  function setRunLabelFor(btn, text) { const l = btn.querySelector(".rlabel"); if (l) l.textContent = text; }

  async function submitVideo(url, fd, btn, restLabel) {
    running = true; energyTarget = 1; hideDownload(); resetLogs(); startClock();
    setTransport("submitting", "uploading…"); showRendering(); setRunLabelFor(btn, "Rendering…");
    addLog(`submitting → ${url}`);
    try {
      const res = await fetch(url, { method: "POST", body: fd });
      if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
      const data = await res.json();
      addLog(`queued · job ${data.job_id}`, "ok");
      pollTimer = setInterval(() => pollSingle(data.job_id), 4000); pollSingle(data.job_id);
    } catch (e) {
      addLog(e.message, "error"); setTransport("failed", e.message); showStill("error");
      running = false; energyTarget = 0; stopClock(); setRunLabelFor(btn, restLabel);
    }
  }
```

In `pollSingle`, at the two terminal branches (`endRunUi()` calls), replace `endRunUi()` with the studio reset (there is no `endRunUi` on this page anymore):

```js
        running = false; energyTarget = 0; stopClock();
        genAudio.hidden = mode !== "single"; genVideo.hidden = mode !== "direct";
```

In the `// ── boot ──` block near the end, replace `setMode(false);` with `setMode("single");` and remove the `pubDate`/excel boot lines if present.

- [ ] **Step 7: Manual verification**

```bash
source venv/bin/activate && uvicorn api.routes:app --reload --port 8000
```

Then in a browser (no `APP_PASSWORD` set yet, so auth is off):
1. Open `http://localhost:8000/studio`. Page loads, "audio first" selected, levers visible.
2. Enter a voice id + script, drag an avatar image, click **Generate audio** → audio player appears, "Make video from this audio" button shows. (Requires a real `ELEVEN_LABS` key in `.env`.)
3. Tweak the speed slider, click **Regenerate audio** → new clip plays.
4. Click **Make video from this audio** → transport shows rendering, video plays on completion.
5. Switch to **direct**, click **Generate video directly** → one-shot render on v2.
6. Confirm `http://localhost:8000/heygen` still works unchanged.

- [ ] **Step 8: Commit**

```bash
git add static/studio.html api/routes.py
git commit -m "feat(studio): new /studio page — v2 levers, audio regen → video, direct"
```

---

### Task 6: Single-password auth (middleware + login page)

**Files:**
- Create: `api/auth.py`
- Create: `static/login.html`
- Modify: `api/routes.py` (register middleware + login routes)
- Modify: `.env.example`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: nothing from prior tasks (independent).
- Produces:
  - `api/auth.py`: `AUTH_COOKIE_NAME = "autodub_auth"`; `make_auth_token() -> str`; `is_valid_token(token: str) -> bool`; `async def auth_middleware(request, call_next)`; `register_auth(app)` that adds the middleware and the `/login`, `/auth/login`, `/auth/logout` routes.
  - Env: `APP_PASSWORD`, `APP_SESSION_SECRET`. Auth disabled when `APP_PASSWORD` is empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth.py
from fastapi.testclient import TestClient

from api import routes as api


def test_auth_disabled_when_no_password(monkeypatch) -> None:
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    client = TestClient(api.app)
    assert client.get("/health").status_code == 200
    # a normally-gated route is reachable when auth is off
    assert client.get("/video/heygen/talking-photos").status_code in (200, 500, 502)


def test_gated_route_blocks_without_cookie(monkeypatch) -> None:
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("APP_SESSION_SECRET", "s3cret")
    client = TestClient(api.app)
    r = client.get("/video/heygen/talking-photos", headers={"Accept": "application/json"})
    assert r.status_code == 401


def test_login_then_access(monkeypatch) -> None:
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("APP_SESSION_SECRET", "s3cret")
    client = TestClient(api.app)
    bad = client.post("/auth/login", data={"password": "nope"}, follow_redirects=False)
    assert bad.status_code in (302, 303)
    assert api.auth.AUTH_COOKIE_NAME not in bad.cookies
    ok = client.post("/auth/login", data={"password": "hunter2"}, follow_redirects=False)
    assert ok.status_code in (302, 303)
    assert api.auth.AUTH_COOKIE_NAME in client.cookies
    r = client.get("/video/heygen/talking-photos", headers={"Accept": "application/json"})
    assert r.status_code in (200, 500, 502)


def test_health_and_login_always_open(monkeypatch) -> None:
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    monkeypatch.setenv("APP_SESSION_SECRET", "s3cret")
    client = TestClient(api.app)
    assert client.get("/health").status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && pytest tests/test_auth.py -v`
Expected: FAIL — `api.auth` does not exist; gated route returns 200 (no middleware).

- [ ] **Step 3: Create `api/auth.py`**

```python
# api/auth.py
from __future__ import annotations

import os
import secrets

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

AUTH_COOKIE_NAME = "autodub_auth"
_TOKEN_MAX_AGE = 60 * 60 * 24 * 7  # 7 days
_ALLOWLIST = {"/login", "/auth/login", "/auth/logout", "/health"}
_ALLOW_PREFIXES = ("/static/",)


def _password() -> str:
    return os.getenv("APP_PASSWORD", "").strip()


def _serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("APP_SESSION_SECRET", "").strip() or "dev-insecure-secret"
    return URLSafeTimedSerializer(secret, salt="autodub-auth")


def make_auth_token() -> str:
    return _serializer().dumps("authed")


def is_valid_token(token: str) -> bool:
    try:
        _serializer().loads(token, max_age=_TOKEN_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _is_allowed(path: str) -> bool:
    return path in _ALLOWLIST or any(path.startswith(p) for p in _ALLOW_PREFIXES)


async def auth_middleware(request: Request, call_next):
    if not _password():                       # auth disabled
        return await call_next(request)
    if _is_allowed(request.url.path):
        return await call_next(request)
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if token and is_valid_token(token):
        return await call_next(request)
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return RedirectResponse("/login", status_code=302)
    return JSONResponse({"detail": "Authentication required"}, status_code=401)


def register_auth(app: FastAPI) -> None:
    app.middleware("http")(auth_middleware)

    @app.get("/login")
    def login_page() -> FileResponse:
        return FileResponse("./static/login.html")

    @app.post("/auth/login")
    def do_login(password: str = Form(...)) -> RedirectResponse:
        if _password() and secrets.compare_digest(password, _password()):
            resp = RedirectResponse("/studio", status_code=302)
            resp.set_cookie(
                AUTH_COOKIE_NAME, make_auth_token(),
                httponly=True, samesite="lax", max_age=_TOKEN_MAX_AGE,
            )
            return resp
        return RedirectResponse("/login?e=1", status_code=302)

    @app.post("/auth/logout")
    def do_logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(AUTH_COOKIE_NAME)
        return resp
```

- [ ] **Step 4: Register auth in `api/routes.py`**

Add the import near the other `api.*` imports (top of file):

```python
from api import auth
```

Immediately after `app = FastAPI(lifespan=lifespan)` (line 66), register it:

```python
auth.register_auth(app)
```

- [ ] **Step 5: Create `static/login.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AutoDub · Sign in</title>
<link rel="stylesheet" href="/static/style.css?v=8" />
<style>
  .login{ min-height:100vh; display:grid; place-items:center; }
  .card{ width:min(340px,92vw); border:1px solid var(--line); border-radius:14px; background:rgba(12,12,16,.85); padding:28px 26px; }
  .card h1{ font-family:var(--sans); font-size:18px; margin:0 0 4px; }
  .card p{ font-family:var(--mono); font-size:11px; color:var(--faint); margin:0 0 20px; }
  .card input{ width:100%; font-family:var(--mono); font-size:13px; color:var(--ink); background:rgba(13,13,17,.85); border:1px solid var(--line); border-radius:9px; padding:11px 14px; outline:none; }
  .card input:focus{ border-color:var(--accent); }
  .card button{ width:100%; margin-top:14px; font-family:var(--sans); font-weight:600; font-size:13px; color:#16130d; background:linear-gradient(#f4f1ea,#e3dfd4); border:none; border-radius:9px; padding:11px; cursor:pointer; }
  .err{ color:var(--err); font-family:var(--mono); font-size:11px; margin-top:12px; text-align:center; }
</style>
</head>
<body>
<div class="login">
  <form class="card" method="post" action="/auth/login">
    <h1>AutoDub Studio</h1>
    <p>enter access password</p>
    <input type="password" name="password" placeholder="password" autofocus />
    <button type="submit">Sign in</button>
    <div class="err" id="err" hidden>Wrong password.</div>
  </form>
</div>
<script>
  if (new URLSearchParams(location.search).get("e")) document.getElementById("err").hidden = false;
</script>
</body>
</html>
```

- [ ] **Step 6: Update `.env.example`**

Add a section (near the top):

```
# --- App auth (hosted single-user) ---
# Leave APP_PASSWORD blank to disable auth entirely (local dev).
APP_PASSWORD=
APP_SESSION_SECRET=
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `source venv/bin/activate && pytest tests/test_auth.py -v`
Expected: PASS (4 tests).

- [ ] **Step 8: Full suite regression**

Run: `source venv/bin/activate && pytest -q`
Expected: PASS — existing tests unaffected (they don't set `APP_PASSWORD`, so auth is off).

- [ ] **Step 9: Manual verification**

```bash
APP_PASSWORD=test123 APP_SESSION_SECRET=abc source venv/bin/activate && uvicorn api.routes:app --port 8000
```
1. Open `/studio` → redirected to `/login`.
2. Wrong password → stays on login with error.
3. Correct password → lands on `/studio`, works normally.
4. `/health` reachable without login.

- [ ] **Step 10: Commit**

```bash
git add api/auth.py api/routes.py static/login.html .env.example tests/test_auth.py
git commit -m "feat(auth): single shared-password gate with local-dev escape hatch"
```

---

## Self-Review

**Spec coverage:**
- Auth (single password, cookie, middleware, escape hatch) → Task 6. ✓
- Keys via server `.env` → no code change; documented in `.env.example` (Task 6, Step 6) and existing `get_config_value`. ✓
- v2 + speed on new page (both options) → Tasks 1, 2 (audio), 4 (direct via `/video/heygen` model_id/speed), 3 (pipeline speed). ✓
- Audio regen loop → Task 5 (Generate/Regenerate audio). ✓
- Approved audio rendered exactly (skip TTS) → Task 3 (skip-TTS branch) + Task 4 (`/from-audio`). ✓
- New page, `/heygen` untouched → Task 5 (copy, not modify) + Task 4 defaults preserve v3. ✓
- Single-run only, no batch → Task 5 removes batch UI. ✓
- Safe `audio_id` resolution → Task 3 `resolve_audio_path` + Task 4 400 handling. ✓

**Placeholder scan:** No TBD/TODO; all code steps include full code. Frontend edits reference exact anchors in `static/heygen.html` with complete replacement blocks. ✓

**Type consistency:** `ElevenLabsTTSConfig(..., speed)` used consistently across Tasks 1–4; `_tts_cache_key(..., speed=...)` matches its new signature; `resolve_audio_path(audio_id, output_dir)` signature identical in definition (Task 3) and callers (Tasks 3, 4); `AUTH_COOKIE_NAME` referenced as `api.auth.AUTH_COOKIE_NAME` in tests matches `api/auth.py`. ✓

**Note on output_dir for from-audio:** Task 4 Step 4 explicitly dispatches `run_video_job(..., output_dir=OUTPUT_DIR)` so the skip-TTS resolver (Task 3) finds the clip saved by `/tts-elevenlabs` (which writes to `OUTPUT_DIR`).
