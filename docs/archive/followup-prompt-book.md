# Prompt book: follow-ups from the architecture run (Oct 2026)

These come from the review after the architecture prompt book merged. Each one
is a gap between the Video pane and the server that the operator can actually
hit. Run them with:

    PUSH=end scripts/run-tickets.sh docs/archive/followup-prompt-book.md

None blocks another. All four touch static/pane-video.js, and the runner works
serially, cutting each worktree from the latest main, so they don't conflict.

Conventions, as in docs/archive/architecture-prompt-book.md:
- Use CONTEXT.md vocabulary.
- Commit messages are one imperative sentence in sentence case with no
  `feat:`/`fix:` prefix.
- Test through module interfaces with tests/fakes.py and conftest. Make no real
  HeyGen, Gemini, S3 or NAS calls.
- There's no JS test harness. Keep JS changes small, run `node --check` on what
  you touch, and pin any server shape the pane relies on with a backend test.

Pane error handling already exists. Submit failures go through `refusal(r)` in
static/pane-video.js, which turns a non-2xx response into an Error carrying the
server's `detail`. The catch shows "The server refused the render" with that
text. Build on this rather than adding another error path.

## 1 — #12 A refused batch names every bad row at once *(review)*
```
/mattpocock-skills:implement #12

Read the issue first: `gh issue view 12`.

Product decision (made, don't revisit): a batch with any bad row is still refused as a whole. Renders are paid for at submission, and a half-run batch is worse than a refused one. The change is to report EVERY problem in one refusal.

Where it is: services/video_pipeline/intake.py, `specs_from_batch` (~:110-150).
- The per-row loop (~:119) re-raises the first row's IntakeError as `row N ('Title'): ...` (~:130-133).
- The duplicate-title check runs only after all rows pass (~:139-145).
- Tests are in tests/test_video_intake.py.

Target:
- Collect per-row problems through the whole loop. Run the duplicate check on the rows that did scrub to a usable title, and raise ONE IntakeError when anything is wrong.
- Give the message one line per problem, in row order: `row N ('Title'): <what>`, then a duplicate-titles line if any. Cap it at 20 lines plus "…and N more".
- Keep IntakeError's type and how the route maps it (400 with `detail` = the message), so the pane's `refusal()` shows it unchanged. If you add a structured attribute (e.g. `.problems: list[str]`), keep `str(exc)` as the complete message.
- A clean batch behaves exactly as now.

Tests in tests/test_video_intake.py: three bad rows reported together; bad rows plus duplicates in one refusal; the cap with 25 bad rows; an unchanged clean batch. Plus one API test in tests/test_api_script_generation.py (where the batch-route tests live) showing two bad rows both appear in the 400's detail.
```

## 2 — #9 Batch and Auto renders accept a picked talking photo *(review)*
```
/mattpocock-skills:implement #9

Read the issue first: `gh issue view 9`. Then read CONTEXT.md's "Talking photo" and "Talking photo slot" entries.

The failure: in static/pane-video.js, the submit (~:614-617) sends `talking_photo_id` instead of `image` when the operator picked an avatar from the list (state.photoId). The batch route api/routes.py `create_heygen_batch_job` (~:443) declares `image: UploadFile = File(...)`, so FastAPI rejects the request with a 422 before the route runs. The single route (~:227) takes `image: UploadFile | None` plus `talking_photo_id`, and checks for one or the other (~:275, which judges the intake-cleaned `spec.talking_photo_id`).

The batch runner, services/video_pipeline/batch_runner.py `run_video_batch_job` (~:39):
- acquires one shared photo with `slots.acquire(image_bytes=..., ...)` (~:59-71) and copies it onto every item's spec (~:89);
- on acquire failure, falls back to per-row acquire inside run_video_job.

Target:
- Batch route: `image: UploadFile | None = File(default=None)` and `talking_photo_id: str | None = Form(default=None)`. Thread the id through intake's BatchSettings, so the blank-to-None cleaning is the same as single. Refuse with the same 400 text as the single route when there is neither.
- Batch runner: when it gets a talking_photo_id, use it for every row and skip `slots.acquire` entirely (no upload, no clearing slots, because the photo already exists on the account). The image path is unchanged. Say in a docstring line why a picked photo must not go through slots: acquire frees every slot, which would delete the very photo that was picked.
- The pane already sends the right fields. Don't change JS unless the server's field name has to differ, and it shouldn't.

Tests:
- tests/test_video_batch.py: a batch with talking_photo_id and FakeRenderer. Every row's spec carries the id, and the fake records zero photo uploads and zero clears. The image path still acquires exactly once.
- A batch-route test with talking_photo_id and no image is accepted (202). Neither image nor id (or a whitespace id) gives 400.
```

## 3 — #10 Video pane shows the Script writer's unrepaired warnings *(model: sonnet; effort: low)*
```
/mattpocock-skills:implement #10

Read the issue first: `gh issue view 10`.

Server side, already correct: api/routes.py `generate_video_scripts` returns `"warnings": day.warnings` (~:438), a `list[str]`. It is empty for a clean set, and non-empty when the writer's repair budget ran out (services/script_writer.py `DaySet` ~:664-670, built ~:775-795 from `str(v)` of each violation).

Pane side: static/pane-video.js, Generate handler (~:470-482). It reads only `body.items`, then says "N scripts written — read them before you run". The banners host is `els.banners`. `U.clearBanners(els.banners)` already runs at the start of Generate (~:459) and of Run (~:605). `U.banner(host, {kind, title, detail, raw, id})` is in static/ui.js:24; kind "warn" exists (see the showCause mapping in static/shell.js:112-124).

Target:
- After loading drafts, if `body.warnings` is a non-empty array, show ONE banner with kind "warn", id "script-warnings", title "Some drafts still clash", and the warnings as the detail, one per line. Escape them with U.esc if `detail` is HTML; check how banner renders detail in ui.js first. Change the `say(...)` line to mention the clashes.
- With no warnings, behave exactly as now.

Test: in tests/test_api_script_generation.py, pin the response shape. A repair-exhausted day (the existing partial-repair test setup in that file or in tests/test_script_writer.py shows how to force it) returns `warnings` as a non-empty list of strings alongside all drafts, and a clean day returns `warnings == []`. Run `node --check static/pane-video.js`.
```

## 4 — #11 A failed batch shows why it failed
```
/mattpocock-skills:implement #11

Read the issue first: `gh issue view 11`. Then read the header of services/errors.py; a wrong confident cause is worse than none.

What happens:
- static/pane-video.js `finish(status, p)` (~:726-755) reads `p.cause` and passes it to `window.Shell.showCause` (static/shell.js:112, which expects a classified cause dict: kind, severity, title, detail, raw, optional action_url/action_label). Otherwise it shows "No cause was reported by the server."
- Single video jobs have `cause` (services/video_pipeline/types.py:50, set by `classify(...)` in services/video_pipeline/store.py `fail` ~:123).
- Video batches have NO `cause`: `VideoBatchJobState` in services/video_pipeline/batch_store.py (~:28-36) carries only `rows[].error` (row field ~:24). Interrupted rows get "Interrupted by a restart" (~:75).
- `renderQueue(rows)` (~:702-722) renders row index, title, NAS path, status badge and an Open link, never `row.error`.

Target:
- Server: give `VideoBatchJobState` an optional `cause: dict | None`. When a batch ends `failed` or `partial`, set it: if every failed row shares one error, use the classified cause of that row's failure; otherwise use a summary in the same dict shape (kind "batch_rows_failed", severity "error", title like "3 of 12 rows failed", detail = the first error, raw = all row errors one per line). Prefer reusing the video job's stored cause for a row (the runner can read it from the video jobs store, as it already reads state for success, ~:105-110) over re-classifying strings. Don't invent causes.
- Pane: in `renderQueue`, a failed row shows its `row.error` (escaped, truncated to one line with the full text in a title attribute). `finish` keeps using `p.cause`, which now exists for batches.
- Single jobs are unchanged.

Tests: tests/test_video_batch.py for a partial batch with two different row errors (summary cause) and an all-same-error failure (that row's classified cause). tests/test_job_persistence.py: the cause survives a reload. Run `node --check static/pane-video.js`.
```
