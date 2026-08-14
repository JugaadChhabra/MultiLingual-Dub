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
lip-synced video out, pushed to the NAS. Terms: *video job*, *render*, *talking
photo*, *batch*.

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
and spec are persisted before the download is attempted. See
`recover_video_job` and `VideoJobsStore.list_recoverable`.

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

## Persistence

**State mirror** — one JSON file per job, rewritten on every mutation, reloaded
at boot. All three job stores hold one. Only the serialization is shared: what
`create` / `start` / `complete` mean differs enough per pipeline that a common
base class would be mostly overrides.

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
