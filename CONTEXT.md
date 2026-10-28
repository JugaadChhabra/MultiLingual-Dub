# Domain language

The words this codebase uses for its own concepts. When a name here appears in
code, it means what it says below and nothing else.

Started 14 Aug 2026, while giving the HeyGen render a seam. Incomplete on
purpose — terms get added when they earn a name, not up front.

## The two pipelines

The repo runs two unrelated pipelines that share a process and little else.

**Audio pipeline** (`batch/`) — an Excel sheet of scripts in, translated and
voiced MP3s out, zipped per language and pushed to S3. Terms: *row*, *activity*,
*language task*.

**Video pipeline** (`services/video_pipeline/`) — a script and a photo in, a
lip-synced video out, pushed to the NAS. The script is now usually written for
the operator rather than by them, so on most days the photo is the only input.
Terms: *video job*, *video job intake*, *render*, *talking photo*, *batch*, *draft*,
*script brief*.

## Video pipeline

**Video job** — one script becoming one video. Has an id, a persisted
`VideoJobSpec`, and a status that walks `queued → tts → uploading → generating →
polling → downloading → nas_upload → completed`. Owned by `run_video_job`.

**Render** — the provider-side act of turning audio plus a talking photo into a
video. Asynchronous and *paid for at submission*, which is why losing track of
one matters: the credit is spent whether or not the file is retrieved.

**Renderer** (`VideoRenderer`) — the seam between a video job and whoever
performs the render. `HeyGenRenderer` in production, `FakeRenderer` in tests.
Nothing crossing it carries a provider's JSON.

**Talking photo** — a still image registered with the provider so it can be
animated. Not a file; an id that the provider issues and holds.

**Talking photo slot** — one of the three talking photos an account may hold at
once. Slots are the scarce resource: `TalkingPhotoSlots` owns the rule for
getting a usable photo out of a capped account (free every slot, then upload
once), and the batch rule that all rows share a single acquired photo.

**Recovery** — re-running the download-and-NAS tail of a video job whose render
finished but which failed afterward. Possible only because the job's render id
and spec are persisted before the download is attempted. Its rules live in
`services/video_pipeline/recovery.py` (`check_recoverable`, raising
`NotRecoverable`): a job is recoverable only if it is `failed`, has a render id
and a spec, and is not already being recovered. A recovery first *claims* the
job (`VideoJobsStore.claim_recovery`, under the store's lock), moving it to the
in-flight status `recovering`, so a second request for the same job is refused
and recover-all passes it by. Recovering a completed job would file it to the
NAS twice. See `start_recovery`, `start_recovery_of_failed` and
`recover_video_job` in the pipeline.

**Interrupted** — a job that was still in flight when the process stopped. On
reload every such job is settled to `failed`, because nothing is running any
more and a job claiming otherwise is lying. For a video job that is also what
makes it *recoverable*: `list_recoverable` only considers terminal jobs, so one
left sitting in `polling` would never be offered for recovery and its finished
render would be stranded.

**Correlation id** — the job id, sent with a render submission so a render can
be found again if the submission's response is lost. Submission is not
idempotent: without this, a retry creates a second paid render. Called
`callback_id` at HeyGen's wire level.

**Character** — which persona a video is voiced and filed as (`indian`, `us`).
Selects the voice and, for `us`, a separate NAS root.

**Video job intake** (`services/video_pipeline/intake.py`) — a submission's
raw form values turned into validated specs, or an `IntakeError` the request
returns as a 400. A single submission (`SingleSubmission`) becomes one
`VideoJobSpec`; a batch (Excel or JSON rows plus the `BatchSettings` every row
shares, overlay and music included) becomes one `BatchItem` per row under the
same rules, and is refused if two rows would be filed under one title. A
batch with any bad row is refused whole, but only after every row is checked:
the one refusal names each bad row and any duplicate titles. The
batch runner receives these specs and builds none of its own. Owns the rules
that used to live only in the Video pane: the title is scrubbed of filename-breaking
characters and a trailing `.mp4` (it becomes the NAS filename) and refused if
nothing is left, and the script is capped at the Script writer's
`MAX_SCRIPT_CHARS`. Also blanks to None, the `indian` character default and the
toggle parsing. The pane still checks the
same rules first, for fast feedback; intake is where they are enforced. The
upload is not intake's business — whether there is a photo is the request's.

**Draft** — a written script the operator has not committed to yet: title plus
text, held in the browser, belonging to no job. Becomes a batch row on Run and
nothing before that. The word marks the gap that exists because a render is paid
for at submission — twelve drafts are read and edited for free, twelve renders
are not.

**Script brief** — what a category's videos are *about*, as opposed to
`video_prompt` and `motion_prompt`, which are how they look and move. The input
to the script writer. A category without one cannot generate.

**Script writer** — Gemini authoring a day's scripts from a brief, a date and an
item list. The author, not a translator or an extractor: nothing is fetched and
no source text exists. Twelve signs come back from one call so they can be varied
against each other.

One operation writes a day for a category: given the category, language, date,
brief and item set (the zodiac, or a one-off of one), it reads its own Script
history, writes, records the result, and returns the drafts with any warnings.
It owns that history — the generate and batch-submit requests only call it. A
set whose repair budget ran out is still recorded and returned, every draft
present, with the unresolved clashes as warnings; only a set with nothing usable
is an error.

**Script history** — every set the writer has produced, on disk, quoted back to
it as what not to repeat. Read and written only by the Script writer. The only
defence against a model converging on its own favourite phrasings across days.
Keyed by day, category and language, so regenerating a day replaces it rather
than adding to the record.

Written twice for a day that gets rendered, both times by the Script writer:
once when the scripts are written, and again from the rows actually submitted,
which replace them. A submitted row carries only its title, so the writer maps
it back to its item through the same sets it writes from; a horoscope batch with
a title that is not a sign is refused rather than filed where nothing matches
it. The second write is the one that matters — the operator's edits are what a
viewer hears, so they are what tomorrow has to avoid sounding like. The first is
kept for the case where a day is generated and never run, because the model
still said it.

## Persistence

**State mirror** — one JSON file per job, rewritten on every mutation, reloaded
at boot. All three job stores hold one. The mirror owns serialization and
load-and-settle: `load_settled` reloads every job and settles the **Interrupted**
ones through a callback each store supplies (what "failed" means differs per
store), and `finalize_summary` is the one timing-stamp helper. What `create` /
`start` / `complete` mean differs enough per pipeline that a common base class
would be mostly overrides, so those stay per store.

What the mirror buys differs by pipeline, and the difference is worth keeping
straight. A *video job* can genuinely be resumed, because its render continues
on HeyGen's side and can be re-fetched. An *audio batch* cannot — its
translations, speech and un-uploaded audio live in memory and die with the
process. Its mirror is a record of how far it got, plus working download links
for activities whose zips already reached disk.

Nothing is pruned. Files accumulate; that is a known growth point, left alone
because automatically deleting records of completed work should be a deliberate
decision rather than a side effect.

## Configuration

**Session config** — a `.env` pasted into the config panel, held per browser
session. Raw input, not yet configuration.

**Settings** — resolved configuration: a frozen object per subsystem
(`NasConfig`, `S3Config`, `HeyGenSettings`, `QCSettings`, …), each owned by the
module it configures and built by its own `resolve(session)`. Resolution happens
**once, at the edge of a request**, and the result is passed down. Nothing
deeper than a route reads a key, so a missing one fails the request that asked
for the work rather than a batch job forty rows in.

Precedence is per key: the session's value if it has a non-empty one, otherwise
the process environment. A partial paste therefore inherits the rest from the
environment.

**Required key** — one whose absence fails its subsystem's `resolve`. The set is
derived from the settings classes rather than listed by hand, which is what
stopped it drifting from what the code reads.

**Process tuning** — `API_RETRY_*` and `AUDIO_COMPRESS_*`, read straight from
the environment and deliberately outside the settings model. They describe the
machine the container runs on, not whose account is in use, and the modules
reading them are leaf utilities called from everywhere.

## Audio pipeline

**Row** — one line of the input Excel: text, emotion, activity name, audio type.

**Activity** — a named group of consecutive rows. The unit of upload: when the
activity name changes, that activity's audio is zipped per language and pushed.

**Language task** — one row voiced into one language. The unit that succeeds or
fails, and the unit the summary counts. Not a unit of *execution*: a whole row
is processed at once so its languages can share one QC call.

**Voiceover** — turning one row into audio in every language asked for:
translate, QC, speak, compress. The audio-side counterpart to *render*, and
deliberately not called that — *render* means the video thing. Both the main
pass and the retry pass go through `voice_row`, which is why they cannot drift.
`voice_row` is two halves that can also be called on their own: `translate_row`
(translate, then optional QC; gives text per language) and `speak_row` (speak
with the row's emotion tag, then compress).

**Activity buffer** — the audio gathered for the activity being processed, held
under non-colliding filenames until the activity ends and it is zipped. Discarded
and recreated at each activity boundary.

**Tally** — the running count of what a job has done, and the only writer of
`JobSummary`. Counters used to be incremented from the loop, the retry pass and
the upload module, with retry decrementing what the loop had already counted.

**Teaching mode** — QC keeps target vocabulary and letters in English while
translating the surrounding explanation, for English-learning content.

**Append mode** — merge newly generated audio into an activity's existing S3
zips instead of creating fresh ones. Requires the folders to already exist.

**Story** — a pasted plain-text story that becomes one activity, instead of a
sheet. `services/story.py` owns it. `split_story` has Gemini break it into
*story lines*, and accepts the split only if joining the lines gives back the
operator's wording (whitespace and quote style aside), every emotion is in
`STORY_EMOTIONS`, and no line is over `MAX_LINE_CHARS` with its emotion tag. A
story over `MAX_STORY_LINES` is refused rather than chunked.

**Story line** — one line of a story: its text and its emotion. Becomes a row
once the story is generated; until then it lives only in the Story pane, where
the operator reviews it.

**Story job** — a reviewed story voiced as one activity (`run_story_job` in
`batch/story_job.py`). Its rows are built in memory, one per story line, each
titled the activity name plus its 1-based line number. Unlike an Excel batch it
works phase by phase: translate every row into drafts with no per-row QC, then
one *story fix* per language, then speak each row with its fixed text. English
targets get the source lines verbatim — no translation, no Gemini. Shares the
Excel batch's tally, activity buffer, retry-then-upload tail (`_close_activity`)
and job endpoints; its retry pass re-speaks a failed voiceover from its fixed
text instead of re-translating it. Being one activity, nothing uploads until
the end, so a cancel while speaking uploads the clips already spoken first.

**Story fix** — one Gemini call per language that sees every line's English,
emotion and draft at once, and rewrites the drafts so the story sounds told, not
translated (`fix_story`). Separate from per-row QC: its own prompt sets the
register (Hindi: everyday spoken, as a parent tells a bedtime story, common
English loanwords kept), keeps names, pronouns and forms of address consistent
across lines, keeps each line's meaning inside that line, and carries over only
QC's script-hygiene rule. Returns fixed text keyed by line id; a line it leaves
out, or a language whose fix fails, fails those language tasks.
