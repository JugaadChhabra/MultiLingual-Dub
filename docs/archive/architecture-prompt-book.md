# Prompt book: architecture deepening (Oct 2026)

These come from the 28 Oct architecture review. Each ticket is one GitHub issue
and one unattended session. Run them with:

    scripts/run-tickets.sh docs/archive/architecture-prompt-book.md          # all, in book order
    PUSH=end scripts/run-tickets.sh docs/archive/architecture-prompt-book.md # one push (one Docker build) at the end

Order: quick independent tickets first, then the Script writer chain (#3 → #4),
then the Video job intake chain (#5 → #6), then the speculative #7. A ticket
whose blocker failed is skipped, and a re-run picks it up.

Conventions every session follows:
- The vocabulary comes from CONTEXT.md (video job, render, Renderer, talking photo,
  Script writer, Script history, draft, state mirror, Interrupted, Recovery).
- Commit messages follow the repo's style: one imperative sentence in sentence
  case that says what changes for the operator or the code, with no
  `feat:`/`fix:` prefix (e.g. "Throttle free translation and cap its retries
  independently").
- Tests go through the module's interface. Use the existing fakes in
  `tests/fakes.py` and `tests/conftest.py`, and never make a real HeyGen,
  Gemini, ElevenLabs, S3 or NAS call.

## 1 — #8 Batch progress shows the live state *(model: sonnet; effort: low)*
```
/mattpocock-skills:implement #8

Read the issue first: `gh issue view 8`.

The mismatch:
- static/pane-video.js:677 sets the bar live only when `p.status === "rendering"`, and :689 gives the amber badge only when a row status is "rendering". The backend never emits "rendering".
- services/video_pipeline/batch_store.py:21 (row status: pending|running|completed|failed) and :29 (batch status: queued|running|completed|partial|failed) are the real vocabulary.
- static/pane-video.js:655 and :839 list "cancelled" as terminal for the video batch poll. Video batches have no cancel, so drop it there. Check first that neither line serves the audio pane, and leave it alone if it does.

Change the pane to use "running". Add a small backend test (e.g. in tests/test_video_batch.py, or a new tests/test_batch_status_vocabulary.py) asserting the exact Literal values of the batch and row status fields via typing.get_args. Its docstring should say the Video pane matches on these strings, so a rename is a cross-file change.

There's no JS test harness. Keep the JS change minimal and run `node --check static/pane-video.js`.
```

## 2 — #2 Talking-photo listing goes through the Renderer seam *(model: sonnet; effort: low)*
```
/mattpocock-skills:implement #2

Read the issue first: `gh issue view 2`.

Where things are:
- The route: api/routes.py:196-205 (`list_heygen_talking_photos`). It calls `list_talking_photos` from services/video_pipeline/heygen_client.py directly, with `HeyGenSettings.resolve(session)`. It returns {"items": [...]}, with 500 on MissingSettingError and 502 on other errors.
- The seam: services/video_pipeline/renderer.py (`VideoRenderer` ABC, :23). The production adapter is services/video_pipeline/heygen_renderer.py, and the fake is tests/fakes.py:17 (`FakeRenderer`). `TalkingPhotoSlots` (services/video_pipeline/slots.py:21) owns slot rules; don't move listing there unless it clearly fits better. Default to the Renderer.
- Dependency wiring: `VideoDeps` / `_build_video_deps` in api/routes.py (~:124-153). The recover routes show how a route gets deps and maps MissingSettingError.
- Seam parity tests: tests/test_seam_parity.py. Extend them so the fake and the real adapter can't drift on the new method.

Define a small frozen dataclass in renderer.py for a listed photo, with only the fields the pane uses. Read static/pane-video.js to see which fields the picker reads from each item, and keep the JSON response shape byte-compatible. HeyGenRenderer maps the provider JSON into the dataclass. The route serialises it back to the same shape the pane reads today.

Tests: route tests with FakeRenderer for the happy path, the missing key (500) and a provider failure (502). Look at how tests/test_api_script_generation.py and conftest build a TestClient, and override deps the same way.
```

## 3 — #3 Script writer writes a day for a category, owning Script history *(review)*
```
/mattpocock-skills:implement #3

Read the issue first: `gh issue view 3`. Then read CONTEXT.md's Script writer / Script history / Draft / Script brief entries, and docs/SCRIPT_UNIQUENESS_SPEC.md (including its addenda).

What the route does today (api/routes.py, `generate_video_scripts` ~:405-500):
- validates item_set and an empty brief (:427-433; the writer re-checks the brief at services/script_writer.py:469)
- picks ZODIAC_SIGNS, or a one-item set from `safe_stem(title)` (:435-441)
- reads history twice: `script_history.recent(...)` and `script_history.facts(...)`, both with before=publish_date (:452-457)
- calls `write_daily_scripts(...)` (services/script_writer.py:425), then `script_history.record(...)`
- on `ScriptRepairError` (:475-491), records `exc.drafts` and turns `exc.violations` into `warnings`. This branch has NO test today.

The module-level store is at api/routes.py:114. ScriptHistoryStore is in services/script_history.py (record :99, recent :137, facts :167). Dependency smell to fix while you're here: script_history imports DraftScript from script_writer (history:25).

Target shape (adjust names if the code argues otherwise, and say why in the hand-off):
- A `ScriptWriter` class (or an equivalently deep function set) constructed with a `ScriptHistoryStore` and `ScriptWriterSettings`. Its main operation is `write_day(*, category, language, publish_date, brief, item_set: "zodiac" | "single", title: str | None) -> DaySet`, where `DaySet` holds drafts plus warnings (an empty list when clean).
- `write_day` reads history in whatever shape the writer needs. You may collapse recent()/facts() into one view if that simplifies things; keep record() semantics (replace by day/category/language). It generates, records, and on a partial repair records the partial drafts and returns them with warnings instead of raising.
- Keep `write_daily_scripts` as an internal step if that's the smallest change, but the route must not call it.
- The route builds a ScriptWriter (module-level, like the history store, so tests can swap it) and maps the result to the existing JSON response. The response shape must not change; static/pane-video.js reads it.

Tests:
- New tests at the ScriptWriter interface, using the fake genai client pattern from tests/test_script_writer.py (`_install`, ~:43-61) and a ScriptHistoryStore on tmp_path. Cover: a clean day recorded to history; a second day sees the first day's facts; a partial repair records partial drafts and returns warnings.
- tests/test_api_script_generation.py: update it to swap the ScriptWriter instead of stubbing `write_daily_scripts` and `script_history` separately. Keep every existing assertion about the response.
- Don't rewrite the parse/validate tests. Those modules are already deep.

Update CONTEXT.md's Script writer / Script history entries in the same commit.
```

## 4 — #4 Script writer records submitted rows *(blocked by #3; review)*
```
/mattpocock-skills:implement #4

Read the issue first: `gh issue view 4`. #3 has merged, so the Script writer now owns Script history. Read its current interface before starting.

What the batch route does today (api/routes.py, `create_heygen_batch_job` ~:503-625; line numbers will have shifted after #3):
- `_TITLE_TO_ITEM_KEY` / `_item_keys_for` (~:370-402) rebuild the item-key ↔ title mapping in reverse, with a one-off rule (title == key) re-derived separately from the generate path's `safe_stem(title)`. It raises HTTPException from inside domain logic.
- It records history from the submitted rows only when category, language and publish_date are all present, building DraftScript objects itself (~:559-588).
- tests/test_api_script_generation.py tests `_item_keys_for` directly (~:318-336), and has a batch-path test showing facts survive (~:282-312).

Target:
- Add `record_submitted(*, category, language, publish_date, rows: list[(title, script)])` (or similar) to the Script writer. It maps titles to item keys using the same item sets it generates from (`SetItem`, `ZODIAC_SIGNS` in services/script_writer.py), applies the one-off rule in one place shared with write_day, and replaces the day's history entry.
- An unrecognised title raises a domain error (e.g. `UnknownItemTitle` naming the titles). The route maps it to the same 400 as today.
- The "record only when category, language and date are all present" rule moves into this method, or into a clearly named guard next to it. Test it at the writer's interface.
- Delete `_TITLE_TO_ITEM_KEY` and `_item_keys_for` from the route, and move their tests to the writer's tests.
- Add an end-to-end writer-level test: write_day for day D (fake model), edit one draft's text, record_submitted, then write_day for D+1. Assert the next prompt or facts reflect the EDITED text, not the generated one.

Keep the batch response and behaviour identical otherwise. Update CONTEXT.md's Script history paragraph about the second write if its wording no longer matches where the write lives.
```

## 5 — #5 Video job intake for single submissions *(review)*
```
/mattpocock-skills:implement #5

Read the issue first: `gh issue view 5`. Then read CONTEXT.md's Video pipeline section.

What happens today:
- api/routes.py `create_heygen_video_job` (~:208-293) reads form fields and the upload, builds `VideoJobSpec` field by field (~:250-266: `x or None`, `character or "indian"`, overlay/music checkbox parsing ~:231-232), resolves the voice (~:288), and spawns run_video_job. There are no tests (tests/test_video_single_naming.py:17 only inspects the signature).
- `VideoJobSpec`: services/video_pipeline/types.py (include_overlay/include_music ~:27-28).
- Browser-only rules in static/pane-video.js: `MAX_SCRIPT = 1000` (:48) mirrors `MAX_SCRIPT_CHARS` (services/script_writer.py:72); `safeTitle` (:308) and `scrubTitle` (:491) strip \ / : * ? " < > | and a trailing .mp4. The NAS path (services/nas.py `build_video_path` ~:178) only replaces / and \.

Target:
- New module services/video_pipeline/intake.py ("Video job intake"). Its pure function is `spec_from_single(fields: SingleSubmission) -> VideoJobSpec`, which raises `IntakeError(message)` on invalid input. `SingleSubmission` is a small dataclass of the raw form values, with no FastAPI types. It owns: blank → None, the character default, toggle parsing, title scrubbing (same character set as the pane, plus .mp4 stripping), and the script length cap (import MAX_SCRIPT_CHARS; don't duplicate the number).
- The route parses HTTP into SingleSubmission, calls intake, maps IntakeError to 400, and otherwise behaves as before.
- Leave the pane's checks in place for fast feedback, but add a comment at MAX_SCRIPT and scrubTitle saying the server enforces the same rules in intake.py.

Tests:
- tests/test_video_intake.py: table-driven tests of spec_from_single (scrubbing, length cap at and over the limit, defaults, toggles).
- A route-level test for POST /video/heygen with the fakes: one accepted submission (assert the spec handed to the job, by swapping the job launcher the way other API tests swap runners), and one rejected for length.

Add a "Video job intake" entry to CONTEXT.md.
```

## 6 — #6 Batch submissions go through Video job intake *(blocked by #5; review)*
```
/mattpocock-skills:implement #6

Read the issue first: `gh issue view 6`. #5 has merged, so services/video_pipeline/intake.py exists. Read it first and extend it; don't start a second module.

What happens today:
- api/routes.py `create_heygen_batch_job` parses either an Excel upload (temp-file dance, via services/video_pipeline/batch_excel.py `read_heygen_batch_rows`) or JSON rows (`read_heygen_batch_rows_json`), builds BatchRowState, and spawns `run_video_batch_job` with ~17 kwargs.
- services/video_pipeline/batch_runner.py:40-59 is the runner signature. :93-100 builds a VideoJobSpec per row and never sets include_overlay/include_music, so batches always burn both. The finalize step that reads those flags is services/video_pipeline/pipeline.py `_finalize_video` (~:222-236).
- Duplicate titles are rejected only in static/pane-video.js (~:562-563).
- The overlay card text is hardcoded as "{sign} राशिफल | date" (services/video_pipeline/overlay.py:75). Product decision for this ticket: do NOT change the card text. Respecting the overlay toggle for batch and Auto is enough; the operator turns cards off for non-horoscope one-offs.

Target:
- intake gains `specs_from_batch(rows, shared: BatchSettings) -> list[VideoJobSpec]` (names flexible). It applies the same per-row rules as spec_from_single, rejects duplicate titles (after scrubbing) with an IntakeError naming them, and carries include_overlay/include_music from the shared settings.
- `run_video_batch_job` takes the prepared specs (plus its real collaborators) instead of the raw shared fields it used to build specs from. Shrink its signature, and update tests/test_video_batch.py to match.
- The batch route sends the toggles into intake. Keep the Script history recording added by #4 exactly as it is.
- static/pane-video.js sends the existing overlay/music checkbox values with batch and Auto submissions, using the same field names as the single path.

Tests:
- intake tests for batch: duplicates rejected; toggles carried; per-row length cap.
- A batch-runner test, with burn_cards spied as tests/test_video_job.py already does: overlay off means no cards burned for any row.
- Existing batch tests stay green with both toggles on: shared talking photo, history, summary email.

Run `node --check static/pane-video.js` after the JS change.
```

## 7 — #7 State mirror owns load-and-settle and finalize *(model: sonnet)*
```
/mattpocock-skills:implement #7

Read the issue first: `gh issue view 7`. Then read CONTEXT.md's "State mirror" and "Interrupted" entries. They explain why there is NO common store base class. Respect that.

Where things are:
- services/state_mirror.py: `JsonStateMirror` (:26) with write (:48) and load (:62).
- batch/store.py: `_finalize_summary` (:15), IN_FLIGHT_STATUSES (:32), `_load` (:47, settles at :52).
- services/video_pipeline/store.py: `_finalize` (:16), which is identical to batch/store.py's, IN_FLIGHT_STATUSES (:37), `_load` (:52, settles at :64), `fail` (:119).
- services/video_pipeline/batch_store.py: IN_FLIGHT_STATUSES (:47), `_load` (:61), which also settles in-flight ROWS (:73), not just the batch.

Target:
- `JsonStateMirror.load_settled(in_flight: Iterable[str], settle: Callable[[TState], None]) -> dict[str, TState]` (or similar). It loads, calls `settle` on each state whose status is in flight, writes the settled state back, and returns the result. Each store passes its own settle callback, because what "failed" means (and the video batch's row settling) differs per store.
- Pick a sensible home for the single finalize helper. If the two summary types don't share a shape, a small Protocol is fine, but don't force it if they really differ. Say what you chose in the hand-off.
- Each store's `_load` shrinks to one call. create/start/complete/fail stay where they are, unchanged.

Tests: tests/test_job_persistence.py must pass unchanged. Add one mirror-level test with an arbitrary status set and a recording settle callback.

Update CONTEXT.md's State mirror entry to say the mirror now owns load-and-settle as well as serialization.
```
