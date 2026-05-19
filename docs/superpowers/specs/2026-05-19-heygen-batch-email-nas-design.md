# HeyGen Pipeline — Batch Excel, Email Notification, NAS Upload

**Date:** 2026-05-19  
**Scope:** Three new features for the HeyGen video pipeline.

---

## 1. Overview

| Feature | Summary |
|---|---|
| Batch Excel | Upload one image + an Excel of scripts → generate N videos sequentially |
| NAS Upload | After each video is downloaded, upload it to NAS for editor access |
| Email Notification | After a batch job fully completes (all NAS uploads done), send one summary email via Resend |

Email is **batch-only**. Single video mode: generate → download → NAS, no email.

---

## 2. Batch Excel

### 2.1 Excel format

| Column | Required | Notes |
|---|---|---|
| `script` | Yes | The line the avatar speaks. Blank rows are skipped. |
| `video_title` | No | Output filename hint. Defaults to `row_{n}` if empty. |

Header must be exactly these two columns in row 1 (case-insensitive, trimmed).

### 2.2 New backend files

**`services/video_pipeline/batch_excel.py`**  
Reads the Excel using `openpyxl`. Validates headers. Returns `list[HeyGenBatchRow]` (dataclass: `row_index`, `script`, `video_title`). Raises `BatchExcelError(ValueError)` on bad format.

**`services/video_pipeline/batch_store.py`**  
`VideoBatchJobsStore` — in-memory store (same async-lock pattern as `VideoJobsStore`).  
State model `VideoBatchJobState`:
- `batch_id: str`
- `status: Literal["queued","running","completed","partial","failed"]`
- `total: int`
- `done: int` (succeeded)
- `failed_count: int`
- `rows: list[BatchRowState]` — each has `{row_index, job_id, script, video_title, status, video_local_url, error}`

**`services/video_pipeline/batch_runner.py`**  
`run_video_batch_job(*, batch_id, rows, image_bytes, image_filename, output_dir, batch_store, video_jobs_store, runtime_config, nas_config)`:
1. Mark batch `running`
2. For each row sequentially:
   a. Call `run_video_job(...)` — reuses existing function unchanged
   b. On success: upload video to NAS via `services/nas.py`; update row state `completed`
   c. On failure: update row state `failed`, continue to next row
3. After all rows: send summary email via `services/email.py`
4. Mark batch `completed` (all succeeded), `partial` (some failed), or `failed` (all failed)

`run_video_job` is **not modified** — no email or NAS logic inside it.

### 2.3 New API routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/video/heygen/batch` | Accepts `image` (UploadFile) + `excel` (UploadFile). Creates batch job, fires `run_video_batch_job` as background task. Returns `{batch_id, status: "queued"}`. |
| `GET` | `/video/heygen/batch/{batch_id}` | Returns full `VideoBatchJobState` for polling. |

### 2.4 Frontend changes (`heygen.html`)

- **Mode toggle** ("Single" / "Batch") above the form. Switches the form between:
  - **Single mode** (current behaviour): image + script textarea
  - **Batch mode**: image upload + Excel upload (no script textarea). Hint text shows expected columns.
- **Batch status section**: replaces the single status box in batch mode. Shows:
  - Overall badge (queued / running / completed / partial / failed) + counts (`done/total`)
  - Per-row table: row #, video title, status badge, download link (once ready)
  - Polls `/video/heygen/batch/{batch_id}` every 4 s (same interval as single mode)

---

## 3. NAS Upload

### 3.1 Module

**`services/nas.py`** — to be implemented once the user shares the reference code.  
Interface that the batch runner will call:

```python
def upload_video_to_nas(
    *,
    local_path: str,        # absolute path to the downloaded .mp4
    video_title: str,       # used as the remote filename hint
    nas_config: NasConfig,  # dataclass populated from env vars
) -> str:                   # returns remote path / URL on NAS
    ...
```

Config sourced from env vars (`NAS_*` — exact keys TBD from reference code).  
Called per-video inside `run_video_batch_job`, wrapped in `try/except` — NAS failure marks the row `failed` but does not abort remaining rows.

---

## 4. Email Notification (Resend)

### 4.1 Module

**`services/email.py`**

```python
def send_batch_summary_email(
    *,
    total: int,
    succeeded: int,
    failed: int,
    failed_rows: list[dict],   # [{row_index, video_title, error}]
    resend_api_key: str,
    from_address: str,
    to_addresses: list[str],
) -> None: ...
```

Single `POST https://api.resend.com/emails` via `httpx`. No SDK.  
Fire-and-forget in batch runner — email failure is logged, never raises.

### 4.2 Email content

- **Subject:** `Bhaktidhaam Batch Complete — {succeeded}/{total} videos ready`
- **Body (plain text + simple HTML):**
  - Total scripts submitted
  - Successfully generated & uploaded to NAS
  - Failed (with row index + title + error reason for each)

### 4.3 Config (env vars)

| Key | Description |
|---|---|
| `RESEND_API_KEY` | Resend API key |
| `RESEND_FROM_ADDRESS` | Verified sender address |
| `NOTIFY_EMAILS` | Comma-separated recipient list (supports 1–N) |

### 4.4 Trigger

Called **once** at the end of `run_video_batch_job`, after all rows and NAS uploads are settled. Never called from single video mode.

---

## 5. Error handling summary

| Failure point | Behaviour |
|---|---|
| Bad Excel format | `POST /video/heygen/batch` returns 400 immediately |
| Individual video job fails | Row marked `failed`, batch continues |
| NAS upload fails | Row marked `failed`, batch continues |
| Email fails | Logged as warning, batch status unaffected |
| All rows fail | Batch marked `failed` |
| Some rows fail | Batch marked `partial` |

---

## 6. Files changed / created

| File | Action |
|---|---|
| `services/video_pipeline/batch_excel.py` | Create |
| `services/video_pipeline/batch_store.py` | Create |
| `services/video_pipeline/batch_runner.py` | Create |
| `services/nas.py` | Create (once reference code shared) |
| `services/email.py` | Create |
| `api/routes.py` | Add 2 new routes |
| `static/heygen.html` | Add mode toggle + batch UI |
| `services/video_pipeline/pipeline.py` | No changes |
