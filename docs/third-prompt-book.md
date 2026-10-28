# Prompt book: Recovery and Script history consistency (Oct 2026)

These are the last two items from the post-run review, left after the
architecture and follow-up books. Run with:

    PUSH=end scripts/run-tickets.sh docs/third-prompt-book.md

The runner now refuses to start if the main checkout has uncommitted tracked
changes, so commit first.

Neither ticket blocks the other. Conventions are as in the earlier books: use
CONTEXT.md vocabulary; commit messages are one imperative sentence in sentence
case with no prefix; test through interfaces with tests/fakes.py; make no real
HeyGen, Gemini, S3 or NAS calls.

## 1 — #13 Recovery owns its rules and is covered by tests *(review)*
```
/mattpocock-skills:implement #13

Read the issue first: `gh issue view 13`. Then read CONTEXT.md's "Recovery", "Interrupted" and "Render" entries; a render is paid for at submission, which is why this path matters.

Where things are:
- Routes in api/routes.py:
  - `list_recoverable_heygen_jobs` (~:329);
  - `recover_heygen_video_job` (~:336-364): checks found, heygen_video_id and spec (404/409/409), builds deps (MissingSettingError → 400), and `spawn`s `recover_video_job`;
  - `recover_all_failed_heygen_jobs` (~:367-387): loops over `video_jobs_store.list_recoverable()` and spawns one recovery each.
- `spawn` is in api/background.py.
- services/video_pipeline/pipeline.py `recover_video_job` (~:255-300) repeats the found, video-id and spec checks as ValueErrors. It sets status "polling", calls `renderer.await_render` for a fresh URL, then `_finalize_video`, and on failure calls `jobs_store.fail`.
- services/video_pipeline/store.py `list_recoverable` (~:129) considers only terminal (failed) jobs.
- The shell's Recover UI (static/shell.js ~:129-140) polls the recoverable list and posts recover-failed.

Gaps to close:
- `/recover` never checks status == "failed", so a completed job with a render id is re-downloaded and filed to the NAS again.
- Two quick requests for the same job both spawn.

Target:
- Recovery gets one interface in the pipeline package: e.g. a `check_recoverable(state) -> None` raising `NotRecoverable(reason, code)`, plus a `start_recovery(job_id, ...)` that claims the job before spawning. Pick the shape; the rules must live in ONE place, which both routes and `recover_video_job` use.
- Claim the job atomically under the store's lock, e.g. a store method that flips a failed job to a "recovering"-type in-flight status and returns False if it isn't failed. That makes a second request a 409, and recover-all skip it.
  - If you add a status, add it to the store's IN_FLIGHT_STATUSES, so a restart mid-recovery settles it back to failed and recoverable.
  - Check that static/pane-video.js and shell.js still handle it (renderSingle treats unknown statuses as in progress).
- Routes map NotRecoverable to 404 or 409 with the same detail texts as today, plus the new not-failed and already-recovering ones.

Tests (new tests/test_api_recovery.py, using the route-test fixtures pattern from tests/test_api_video_single.py; the store is on tmp_path; swap `spawn` or run the recovery inline so it completes in-test; FakeRenderer):
- the list endpoint;
- recover one reaches completed with a NAS path;
- 404; 409 for no render id, no spec, not failed, and already recovering;
- 400 for missing settings;
- recover-all over a mix of jobs recovers exactly the failed ones with render ids;
- a restart mid-recovery settles the job back to failed and recoverable (tests/test_job_persistence.py).
```

## 2 — #14 Script history records the titles that were rendered *(model: sonnet; effort: low)*
```
/mattpocock-skills:implement #14

Read the issue first: `gh issue view 14`. Then read CONTEXT.md's "Script history" entry (the second write, from submitted rows, is the one that matters).

Where things are:
- api/routes.py `create_heygen_batch_job`:
  - parses rows into `batch_rows_parsed` (~:484/:492);
  - runs intake, `items = specs_from_batch(batch_rows_parsed, BatchSettings(...))` (~:502), whose items carry the scrubbed `spec.video_title`;
  - then records history from the RAW rows: `rows=[(r.video_title, r.script) for r in batch_rows_parsed]` (~:531-540).
- services/script_writer.py:
  - `_one_off` (~:673) is the shared one-off item rule.
  - The writing path normalises the name: `Path(title or "").stem.replace(" ", "_") or "Script"` (~:692).
  - `items_for_titles` (~:699-713) maps submitted titles with `_one_off(title)` and does NOT normalise.

Target:
- Record history from the intake items: (item.spec.video_title, item.spec.script). Intake already ran, so a refused batch still records nothing; keep that order.
- Add one `_one_off_name(raw) -> str` normaliser in script_writer.py, used by both the writing path and `items_for_titles` (one-off branch). Consider whether intake's title scrub and this normaliser should agree. If a scrubbed title can still differ from the normalised one, say so in the hand-off rather than coupling the modules.
- Horoscope mapping and UnknownItemTitleError behaviour are unchanged.

Tests:
- tests/test_script_writer.py: write a one-off day, then record_submitted the same title unchanged, and it lands under the same key. A title with spaces normalises the same way in both directions.
- tests/test_api_script_generation.py (the batch-route tests): an Auto batch whose title contains a character intake scrubs (e.g. ':') records the scrubbed title in history.
```
