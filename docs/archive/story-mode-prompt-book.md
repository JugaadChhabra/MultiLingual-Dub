# Prompt book: AutoDub Story mode (Oct 2026)

Five tickets that build Story mode, from spec #15 (the design write-up is
docs/superpowers/specs/2026-10-28-autodub-story-mode-design.md). Run with:

    PUSH=end scripts/run-tickets.sh docs/archive/story-mode-prompt-book.md

The runner refuses to start if the main checkout has uncommitted tracked
changes, so commit first.

How the tickets depend on each other (these are also GitHub's native "blocked by" links):
- #16 and #17 can start straight away.
- #18 needs #17.
- #19 needs #16 and #17.
- #20 needs #19.

The book runs them in an order that respects this.

Conventions, as in the earlier books:
- Use CONTEXT.md vocabulary: *row*, *activity*, *language task*. #19 adds *story job* and *story fix*.
- Commit messages are one imperative sentence in sentence case, with no prefix.
- Test through interfaces, with the existing fakes and monkeypatch style.
- Make no real Sarvam, Google, Gemini, ElevenLabs or S3 calls.

Decisions already settled with the operator. Don't reopen them:
- Story mode keeps the current translation setup (Sarvam, falling back to Google, per row). Gemini only *fixes* the drafts, with the whole story in view: one call per language.
- Hindi register is everyday spoken Hindi, the way a parent tells a bedtime story. Common English loanwords (स्कूल, बस) are kept. Story mode applies to every selected language.
- Review comes before generating. No TTS credits are spent until the operator presses Generate.
- Lines stay separate end to end, because of ElevenLabs' per-request text limit and because each line is its own MP3.
- If the story fix fails, the fallback is today's per-row QC, shown as a warning. There is no second verification call.
- The activity name is pre-filled from the title with `story` added, and can be edited. There is no hidden server-side naming rule.

## 1 — #16 Voicing a row splits into translate and speak *(review)*
```
/mattpocock-skills:implement #16

Read the issue first: `gh issue view 16`, then spec #15 for context (`gh issue view 15`). This is a prefactor: the Excel path must behave exactly as it does today.

Where things are:
- batch/voiceover.py:
  - `voice_row` (~:140-240) does translate (`_translate_row_languages`, ~:106), then one batched `qc_translations_batch` call per row (~:188-221), then TTS + `compress_mp3_bytes` per language (~:224-238);
  - `TaskFailure` / `RowOutcome` / `VoiceoverDeps` are at the top (~:38-70);
  - the emotion tag is applied as `f"[{row.emotion}] {text}"` (~:226).
- batch/service.py `_retry_failed_tasks` (~:56-100) and the main loop (~:258-300) both call `voice_row`. Leave them calling it.
- tests/test_voiceover.py and tests/test_batch_service.py monkeypatch `batch.voiceover._translate_language_async`, `batch.voiceover.qc_translations_batch` and `batch.voiceover._generate_elevenlabs_audio_bytes`. Keep those names patchable at the same module path, or the existing tests break.

Target:
- Two public halves in batch/voiceover.py. Pick the names; for example:
  - `translate_row(row, languages, deps, *, qc=True, log_prefix="") -> (texts_by_language, failures)`;
  - `speak_row(row, texts_by_language, deps, *, log_prefix="") -> RowOutcome`.
- Translate failures keep stage "translation" and QC failures keep stage "qc"; speak failures are "tts".
- `voice_row` becomes translate followed by speak, with an identical RowOutcome for identical inputs.
- Update the module docstring: the chain is still translate → QC → speak → compress, but now in two callable halves.

Tests:
- All existing tests in tests/test_voiceover.py, tests/test_batch_service.py and tests/test_qc.py pass UNCHANGED. Don't edit them to make them pass.
- New tests in tests/test_voiceover.py:
  - translate with qc=False returns raw drafts and makes no QC call;
  - translate reports translation failures per language;
  - speak prefixes the emotion tag and reports a TTS failure for one language while the others succeed.
```

## 2 — #17 Story section: paste a story and review the split *(review)*
```
/mattpocock-skills:implement #17

Read the issue first: `gh issue view 17`, then spec #15 (`gh issue view 15`).

Where things are:
- Gemini pattern to copy: services/qc.py.
  - `QCSettings` (resolved settings: `api_key`, `models` fallback list) ~:20-45;
  - `_parse_response_json` ~:48 (strips code fences);
  - the model-fallback loop with `retry_call` (services/retry.py) ~:155-180;
  - the system instruction goes in `types.GenerateContentConfig(system_instruction=...)`.
  - Reuse `QCSettings` for the new service. Add no new config keys.
- Routes: api/routes.py.
  - `create_excel_job` (~:672-724) shows how a route resolves `Settings.resolve(session)` via `_session_config_for_request` and maps `MissingSettingError` / `ValueError` to 400.
  - The page routes `/` and `/videogen` (~:188-205) serve index.html with `NO_HTML_CACHE`.
- UI, plain JS with no build step:
  - index.html: sidebar buttons `.sbi[data-section]` ~:32-40; `<main class="pane" id="pane-audio">` ~:50; `#pane-video` ~:121; scripts load ~:386-389 with `?v=` cache busters (bump any you touch).
  - static/shell.js `show(section)` (~:13-27) toggles panes by id and sets the path and title. The startup choice is at ~:174, and the running indicator `running` is at ~:8 / ~:100-110.
  - static/pane-audio.js has the language picker (INDIAN/INTL/GROUPS/SEL, ~:8-75). Reuse its look; extracting the picker into static/ui.js for both panes is fine if it stays small.
  - Styles: static/app.css and docs/DESIGN_SPEC.md / docs/component-sheet.html. Use the existing components and tokens and invent no new visual language.
- The reference story is the sheet the_boy_who_followed_the_stream_voiceoversheet.xlsx in the repo root. Its 32 voiceover_text cells joined with spaces make a good fixture for tests. Its emotions and line lengths show the target split.

Target:
- A new services/story.py (name is your call) holding:
  - `STORY_EMOTIONS`, the list in the issue;
  - `MAX_LINE_CHARS` (~800, counting the "[Emotion] " tag);
  - `MAX_STORY_LINES` (~120);
  - `split_story(text, *, settings) -> list[StoryLine]` (text, emotion).
- Split prompt guidance:
  - never change wording;
  - 1-3 sentences per line, like the reference sheet;
  - a quotation stays on one line with its "said X";
  - the opening greeting and the closing moral get their own lines;
  - emotions only from the list;
  - JSON only.
- Validation:
  - normalise whitespace and curly/straight quotes, then the joined lines must equal the source;
  - every emotion is in the list;
  - every line is within the cap.
  - Retry the Gemini call once on a validation failure, then raise a clear error. The route maps it to 400.
- `POST /batch/story/split`: `{title, text}` → `{activity_name, lines}`. `activity_name` is the title lowercased, non-alphanumerics stripped, plus "story"; the UI shows it in an editable field. Refuse empty text or more than MAX_STORY_LINES lines (400).
- Expose the emotion list to the UI: put it in the split response or a small GET route, so the dropdown and the server share one source.
- Story pane (`#pane-story`, a new static/pane-story.js, a third sidebar button and a linkable `/story` page route served like `/videogen`):
  - Paste step: title, activity name, textarea, languages, "Break into lines".
  - Review step: a numbered table with editable text and an emotion `<select>`, plus a way back to edit the paste.
  - No Generate button yet; ticket #19 adds it. Leave a clearly placed, disabled slot or nothing.
  - Add `story` to shell.js `show()` and to the running indicator.

Tests:
- tests/test_story.py. Use a fake genai client, as tests/test_qc.py does with monkeypatch on `genai.Client`. Cover:
  - a happy split;
  - changed wording rejected (then retried, then an error);
  - an unknown emotion rejected;
  - an over-cap line rejected;
  - an over-size story rejected.
- tests/test_api_story.py (TestClient, as in tests/test_api_batch.py), with split_story faked. Cover:
  - 400 for empty text and for an error from split;
  - the happy path returns activity_name with the "story" suffix and the lines.
- tests/test_page_routes.py: `/story` serves the app.
```

## 3 — #18 Story review: merge and split lines, download as Excel *(blocked by #17; model: sonnet; effort: low)*
```
/mattpocock-skills:implement #18

Read the issue first: `gh issue view 18`. Ticket #17 built the Story pane and services/story.py (constants `STORY_EMOTIONS`, `MAX_LINE_CHARS`). Open static/pane-story.js and the story routes in api/routes.py to see what's there.

Where things are:
- batch/excel.py:
  - `REQUIRED_HEADERS = ["voiceover_text", "emotion", "activity_name", "voiceover_title"]` (~:7);
  - `read_excel_rows(path)` (~:22) is the reader any downloaded sheet must satisfy.
- openpyxl is already a dependency. tests/test_api_batch.py builds workbooks in memory with `Workbook()` / `BytesIO`; copy that.

Target:
- Review table:
  - merge with next: join the texts with a space and keep the first line's emotion; refuse with a message if the result exceeds MAX_LINE_CHARS;
  - split at the cursor: both halves keep the emotion;
  - renumber after each change.
  - Keep the controls small and consistent with the existing components (docs/DESIGN_SPEC.md).
- `POST /batch/story/xlsx`: `{activity_name, lines: [{text, emotion}]}` → an .xlsx download (Content-Disposition with `<activity_name>.xlsx`).
  - One row per line, in the four REQUIRED_HEADERS columns; `voiceover_title` = activity_name + 1-based index.
  - Return 400 for no lines, an empty line text, or an emotion not in STORY_EMOTIONS.
- A "Download as Excel" button on the review step.

Tests:
- tests/test_api_story.py:
  - xlsx happy path: write the response bytes to tmp_path, run them through `read_excel_rows`, and assert the texts, emotions, activity names and titles round-trip;
  - the 400 cases.
```

## 4 — #19 Story job: translate, fix the whole story, then voice *(blocked by #16, #17; review)*
```
/mattpocock-skills:implement #19

Read the issue first: `gh issue view 19`, then spec #15. Read CONTEXT.md's "Audio pipeline" terms.

What earlier tickets built:
- #16 split row voicing in batch/voiceover.py into a translate half (with a way to skip per-row QC) and a speak half; `voice_row` composes them.
- #17 built services/story.py (`split_story`, `STORY_EMOTIONS`, `MAX_LINE_CHARS`), the split route, and static/pane-story.js with the review step.
- Open those files to see the exact names.

Where things are:
- batch/service.py:
  - `run_excel_batch_job` (~:106) wraps `_run_batch_job_impl` (~:146-300) and has to be mirrored:
    - jobs_store start/fail/complete/cancel;
    - `BatchTally`;
    - S3Client or the local-archive fallback (`tally.warn_about_uploads`);
    - `VoiceoverDeps` built with `_resolve_language_parallelism`;
    - `ActivityBuffer`;
    - `_close_activity` → `_retry_failed_tasks` + `_upload_activity_archives`;
    - a cancellation check per row;
    - `update_summary` per row.
  - Reuse these. Don't copy them. If sharing needs a small extraction (e.g. the close-activity/upload tail), do it.
- batch/models.py `ExcelRow` (row_index, text, emotion, activity_name, audio_type). A story builds these in memory: audio_type = activity_name + index, row_index from 1.
- api/routes.py `create_excel_job` (~:672-724) shows the up-front validation:
  - `Settings.resolve(session)`;
  - `english_voice_is_required(parsed_languages, settings.eleven)`;
  - `parse_target_languages`;
  - `jobs_store.create`;
  - `spawn(...)`.
  - Polling and cancelling stay on `/batch/excel-jobs/{id}` and `/batch/excel-jobs/{id}/cancel`.
- static/pane-audio.js has `start()` / `poll()` / `renderProgress()` / `finish()` (~:138-260). Reuse the same progress, results and cancel UI in the Story pane. Extract shared bits into static/ui.js if that's cleaner than duplicating them.
- Gemini pattern: services/qc.py (QCSettings, `_parse_response_json`, `retry_call`, the model-fallback loop, LANGUAGE_NAMES / LANGUAGE_SCRIPT_HINTS from services/languages.py). QC's script-hygiene rule 3 (~:125) is the text to carry into the story-fix prompt. Its "no unnecessary English" rule must NOT carry over.

Target:
- services/story.py `fix_story(title, lines, drafts, language, *, settings) -> dict[str, str]`, keyed by line id. The system prompt is exactly as the issue lists it:
  - audience;
  - reword freely;
  - the per-language register, with Hindi's spelled out;
  - consistency of names, pronouns and forms of address;
  - keep each line's meaning within the line;
  - proper nouns;
  - script hygiene;
  - JSON only.
  - Stable instructions go in the system prompt; per-story data goes in contents.
- A story job runner (e.g. `run_story_job` in batch/service.py, or a sibling module). For one activity:
  - translate every row with QC skipped;
  - run `fix_story` once per non-English language (in parallel, bounded like translations);
  - speak each row with the fixed text;
  - then the shared close-activity tail.
  - English targets get the source text verbatim, with no translate and no Gemini call.
  - In this ticket, a language whose fix fails gets TaskFailure(stage="qc") for its rows. #20 adds the fallback.
- `POST /batch/story-jobs`: `{activity_name, lines, target_languages}`, validated as the issue lists, → CreateJobResponse.
- Story pane: a "Generate audio" button on the review step that posts the reviewed lines and then polls and shows progress, results and cancel exactly like Audio.
- CONTEXT.md: add *story job* and *story fix* under the Audio pipeline.

Tests:
- tests/test_story_job.py, in the style of tests/test_batch_service.py: settings via the same `_settings()` shape, FakeS3Client, and monkeypatched translate, Gemini and TTS. Cover:
  - one zip per language with `<activity>N` names;
  - TTS receives "[Emotion] <fixed text>", not the draft;
  - exactly one fix call per non-English language;
  - cancellation stops the job.
- tests/test_story.py: the fix prompt contains the Hindi register instruction and every line id; the reply parses into a mapping keyed by line id.
- tests/test_api_story.py:
  - job creation 400s: empty lines, bad emotion, over-cap line, missing English voice when en-* is selected;
  - the happy path returns a job id that `GET /batch/excel-jobs/{id}` finds. Swap the runner with a fake, as test_api_batch.py does with `run_excel_batch_job`.
```

## 5 — #20 Story jobs fall back visibly and keep fixed text on retry *(blocked by #19; review)*
```
/mattpocock-skills:implement #20

Read the issue first: `gh issue view 20`. #19 built the story job runner and `fix_story`. Open them to see the exact names and where a failed fix currently becomes TaskFailure(stage="qc").

Where things are:
- batch/models.py `JobSummary` (~:25-50). Add the counters here (e.g. `story_fix_fallback_lines`, `story_fix_flagged_lines`) and a warning field, or reuse a general warning if one fits. `upload_warning` is upload-specific, so don't overload it.
- batch/tally.py `BatchTally` is the only writer of the summary (see its docstring ~:44-50). Add methods; don't poke at the summary directly.
- batch/service.py `_retry_failed_tasks` (~:56-100) re-runs `voice_row`, which would re-translate. For story jobs, a TTS failure must re-speak with the fixed text it already has. A translation failure retries translate (with per-row QC) for that row only.
- services/qc.py `qc_translations_batch` is the per-row fallback.
- The Story pane's results view (static/pane-story.js, or the shared finish code in static/ui.js) is where the counts and the warning appear.

Target:
- Whole-language fallback: if the fix raises, or returns nothing usable, every row in that language goes through `qc_translations_batch` with its draft, the fallback counter increases per line, and the warning names the language.
- Partial fallback: missing or empty ids → only those rows fall back.
- Length-ratio guard: if fixed length / draft length < 0.4 or > 2.5, increase the flagged counter (put the bounds in named constants). The fixed text is still used.
- Retry: a story job's retry pass re-speaks TTS failures with the stored fixed text and re-translates only translation failures.
- English verbatim (if #19 didn't already cover it): no translate and no Gemini call.
- The UI shows the counters and the warning when they are non-zero.

Tests (tests/test_story_job.py):
- fix raises → audio is still produced for that language, plus the warning and counter;
- the reply misses ids → only those rows fall back, and the per-row QC fake sees exactly those rows;
- a ratio outlier is counted;
- a TTS failure once → the retry succeeds, the TTS fake gets the fixed text, and the translate and Gemini fakes are not called again;
- English → the source text reaches TTS and there are no translate or Gemini calls.
- The existing tests/test_batch_service.py tests pass unchanged.
```
