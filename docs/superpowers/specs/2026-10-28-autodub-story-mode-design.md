# AutoDub Story Mode — Paste a Story, Get Contextual Voiceovers

**Date:** 2026-10-28
**Status:** Design — pending review
**Scope:** The AutoDub audio pipeline (`batch/`, `services/qc.py`, the audio pane in
`static/`). The HeyGen video pipeline is untouched.

## Why

AutoDub translates a sheet one row at a time: Sarvam (falling back to Google)
translates each row with no knowledge of its neighbours, then Gemini QC
(`services/qc.py`) tidies that one row, again in isolation. For instructional
sheets that's fine. For stories it produces literal Hindi: the model translating
"Moti barked." has no idea Moti is a dog, that "Aai" is Kiran's mother, who is
speaking, or that the whole thing is a bedtime story told aloud to a child. The
problem has been noticed on Hindi specifically.

A second, separate pain: the operator writes stories into an Excel sheet mostly
so they can assign an emotion to every line (the `emotion` column becomes an
ElevenLabs audio tag — `batch/voiceover.py`, `tts_text = f"[{row.emotion}] {text}"`).
Splitting a story into lines and tagging each one by hand is the tedious part.

## What we're building

A new **Story** section in the AutoDub UI with a three-step flow:

1. **Paste.** Story title, the story as plain English text, target languages.
   Button: "Break into lines".
2. **Review.** Gemini splits the story into voiceover lines and tags each with an
   emotion. The operator sees a table (line #, text, emotion dropdown) and can
   edit text, change an emotion, merge two lines, split a line, and optionally
   download the result as an `.xlsx` in today's sheet format. Nothing has been
   spent on TTS yet. Button: "Generate audio".
3. **Generate.** Every line is translated exactly as today (Sarvam, falling
   back to Google). Then, per target language, **one Gemini "story fix" call sees
   the whole story** — every English line beside its draft translation — and
   rewrites the drafts into a natural retelling for a child listener. It returns
   exactly one output line per input line. Those lines are voiced with their
   emotion tags, zipped per language and uploaded to S3 exactly as an Excel
   batch is today, with the same progress, results and job-status UI.

Lines stay separate end to end because ElevenLabs limits how much text one TTS
request may carry (operator's constraint) and because each line is its own MP3.

Review-before-generate was chosen explicitly over a one-click flow (operator's
decision): a wrong emotion must be catchable before credits are spent.

## Goals

- Translations that read as a story told in the target language — consistent
  pronouns and gender agreement, consistent character names and forms of
  address, idiom rather than word-for-word rendering.
- **Hindi register (operator's decision): everyday spoken Hindi, like a parent
  telling a bedtime story.** Common English loanwords people actually say
  (स्कूल, बस, टीवी) stay as they are; no Sanskritised/formal Hindi.
- No more hand-splitting and hand-tagging stories in Excel.
- Output indistinguishable in shape from today's batches: same file naming, same
  per-language zips, same S3 layout, so downstream consumers notice nothing.

## Non-goals

- Changing the existing Excel batch flow or its translation path. The Excel
  path keeps Sarvam/Google + QC, teaching mode, and append mode as they are.
- Rewriting the English. Gemini splits and tags; it does not edit the wording.
- Multiple stories per paste. One paste = one story = one activity.
- Saving stories/drafts server-side between sessions.

## Anti-goals (would be a failure even if it "works")

- A line count mismatch that silently shifts audio: line 17's Hindi spoken in
  file 16. Alignment must be verified, never assumed.
- Hindi that reads like a translation — formal, literal, or with English
  sentence structure.
- Spending TTS credits before the operator has approved the split.

## Decisions

### Operator's decisions
- Separate UI section for stories, because the flow differs from Excel upload.
- Review step before generation (flow A).
- **Keep the current translation setup** (Sarvam → Google per line) and use
  Gemini only to fix the translations — but with the whole story in view, not
  one line at a time. (An earlier idea of Gemini writing from scratch was
  dropped by the operator. Known risk: a fixer anchors on its draft, so the
  story-fix prompt explicitly licenses free rewording. If the Hindi still sounds
  literal in practice, swapping the fix for a from-scratch write is a contained
  change to one function.)
- ElevenLabs' per-request text limit is why stories are voiced line by line.
- Hindi register: everyday spoken, parent-telling-a-story.

### My calls (flag if wrong)
- **Applies to every selected language, not only Hindi.** The cost is the same,
  and the literal-translation problem is unlikely to be Hindi-only. The prompt
  carries a per-language register note; Hindi's is the one above, other
  languages get "everyday spoken <language>, as a parent would tell it".
- **Naming mirrors today's sheets, and is visible.** The Paste step has an
  editable activity-name field, prefilled from the title lowercased with
  non-alphanumerics removed plus a `story` suffix (`The boy who followed the
  stream` → `theboywhofollowedthestreamstory`), matching existing story sheets.
  Each line's `voiceover_title`/`audio_type` = `activity_name` + 1-based line
  number (`…story1` … `…story32`). No hidden naming rule beyond the prefill.
- **Emotion vocabulary is a fixed list**, shown as a dropdown in Review and
  given to Gemini as the only allowed values. Seed list, taken from existing
  sheets: Cheerful, Narrative, Curious, Clever, Calm, Grateful, Worried,
  Energetic, Joyful, Thoughtful, Magical, Determined, Inspirational. Builder may
  add a handful of obvious ones (Sad, Excited, Surprised, Scared, Gentle) — keep
  it in one constant.
- **Split guidance for Gemini:** keep line lengths similar to existing sheets
  (one to three sentences); a quotation stays on one line with its "said X"
  attribution; questions in a run can be separate lines; the opening greeting
  and the closing moral are their own lines; **no line may exceed a hard
  character cap** (a constant, conservatively below ElevenLabs' per-request
  limit — ~800 characters including the emotion tag is a safe start), and the
  server re-splits or rejects any line over it. Concatenating the lines must
  reproduce the source text (whitespace aside) — the server verifies this and
  rejects a split that changes the words.
- **The story fix replaces per-row QC for story jobs, with its own prompt.**
  Today's QC rule against keeping English words contradicts the everyday-Hindi
  register, so the story-fix prompt is separate. It keeps QC's script-hygiene
  rules (matras, trailing halant, repeated syllables, native script).
- **One story-fix call per language** (not all languages in one call): keeps each
  response small and a failure contained to one language.
- **Fallback = today's behaviour, visibly.** The story-fix response is JSON keyed
  by line id. The call already gets `retry_call` + the model-fallback list. If
  the response is still unusable, or particular keys are missing/empty, those
  lines (or the whole language) go through today's per-row QC
  (`qc_translations_batch`) instead. The job summary gains a counter and the
  job's warning text names the language, e.g. "story fix failed for hi-IN —
  used line-by-line fix". Nothing gets stuck; the degradation is the current
  Excel path.
- **Misplaced-content guard.** The prompt forbids moving content across line
  boundaries. After the fix, any line whose length ratio to its draft is far
  outside the norm for that language (e.g. <0.4× or >2.5×) is flagged in the
  summary. No second verification call — it would double the cost.
- **Retries reuse fixed text.** The end-of-activity retry pass must not
  re-translate or re-fix a story line; a TTS failure retries TTS only, with the
  already-fixed line. A translation failure for one line retries that line's
  translation, and its fixed text is the per-row QC result (story context is
  not re-run for a single line).
- **Model selection** reuses `QCSettings.models` (same API key, same
  model-fallback list) rather than adding new config.

## Shape of the change

New pieces:

- `services/story.py` (name is the builder's call) with two functions (the
  second replaces the "write_story" idea — it *fixes* drafts):
  - `split_story(text, *, settings) -> list[StoryLine]` — Gemini call returning
    `[{text, emotion}]`; validates emotions against the list and that the words
    are preserved.
  - `fix_story(lines, drafts, language, *, settings) -> dict[line_id, str]` —
    one Gemini call per language; input is every line's id, English text,
    emotion and draft translation, plus the title; output is JSON keyed by line
    id. The system prompt carries: the audience (children, listening, not
    reading), "the drafts are literal machine translations — reword freely so it
    sounds told, not translated", the register note, consistency of
    names/pronouns/gender agreement/forms of address across the whole story,
    keep each line's meaning inside that line (separate audio files), keep
    proper nouns, native script, script-hygiene rules.
- Routes (names are the builder's call):
  - `POST /batch/story/split` — `{title, text}` → `{activity_name, lines: [{text, emotion}]}`. Synchronous.
  - `POST /batch/story-jobs` — `{activity_name, lines, target_languages}` →
    `CreateJobResponse`. Validated up front like `create_excel_job` (settings,
    English voice requirement, non-empty lines, emotions in the list). Jobs are
    polled and cancelled through the existing `/batch/excel-jobs/{id}` endpoints
    and `JobsStore`, so the existing progress UI can be reused.
  - `POST /batch/story/xlsx` — lines → `.xlsx` download in the four-column format
    `read_excel_rows` accepts, so a downloaded sheet can be re-run through the
    normal Excel flow.
- `batch/voiceover.py`: split `voice_row` into its two halves — a
  *translate* step (translate + per-row QC → text per language, failures) and a
  *speak* step (text per language + emotion → audio, failures). `voice_row`
  stays as their composition so the Excel path and its tests are unchanged.
- Batch runner: a story job builds `ExcelRow`s in memory (no temp file) and
  reuses the same jobs store, tally, activity buffer, zipping and S3 upload as
  `_run_batch_job_impl`. Its order differs: translate every row (raw drafts, no
  per-row QC) → `fix_story` once per language → speak each row. The story is a
  single activity.
- UI: a third section alongside audio and video in `static/shell.js`, with its own
  `static/pane-story.js`, following the existing pane conventions and the
  `docs/DESIGN_SPEC.md` component styles.

## Constraints

- Gemini output must be JSON only; reuse `_parse_response_json`-style parsing and
  `retry_call`.
- Long stories: a single-call write is fine for the sizes seen (~30 lines,
  ~2.5k words). Put a soft cap at ~120 lines; above that, refuse at the split
  step with a clear message rather than quietly degrading. Chunking can come later.
- The English target (`en-*`), if selected, uses the source lines verbatim — no
  Gemini call.
- Tests follow the existing pattern (`tests/fakes.py`, no real network): split
  validation (words preserved, emotion list enforced), alignment
  checking and fallback, naming, route validation, and that a story job
  produces the same buffer/upload calls as an equivalent Excel job.

## Left to the builder

- Module, function and route names; exact prompt wording within the
  constraints above.
- Whether the story runner shares `_run_batch_job_impl` via a parameter or is a
  sibling that reuses its parts.
- Review-table interaction details (how merge/split are triggered), within the
  existing design system.
