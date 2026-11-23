# Content Hub Docker Deployment (GHCR)

## Pull and Run

```bash
git clone https://github.com/JugaadChhabra/MultiLingual-Dub.git
cd MultiLingual-Dub
touch .env
```

Set all required keys in `.env`:

- `SARVAM_API`
- `OPENROUTER_API_KEY` (script writer, translation QC, story mode)
- `AWS_ACCESS_KEY`
- `AWS_SECRET_KEY`
- `AWS_BUCKET`
- `AWS_REGION`
- `BATCH_ENABLE_S3_UPLOAD`
- `BATCH_ENABLE_QC`
- `ELEVEN_LABS`
- `DESI_VOCAL_VOICE`
- `ENGLISH_VOICE`

Start:

```bash
docker pull ghcr.io/jugaadchhabra/autodub:latest
docker compose up -d
```

`docker compose up -d` no longer pulls a newer image by itself. To update, see
"Redeploying safely".

App URL: `http://localhost:8080`

## Translation Routing

- Sarvam handles existing Indian language targets.
- In-process translation handles `fr`, `de`, `es`, `ru`, and `pt` inside Content Hub.
- QC and TTS flow stays the same after translation.

## Configuration

Every key is read from the `.env` file beside `docker-compose.yml`; restart the
container after changing it. The header's config dot shows how many required
keys were found, and hovering it lists any that are missing.

Optional tuning (defaults shown, set in `.env`):

- `ELEVENLABS_MAX_CONCURRENCY` (10): simultaneous ElevenLabs requests.
- `AUDIO_MAX_CONCURRENT_JOBS` (3): audio jobs running at once; the rest wait in
  a queue with a visible position.
- `APP_THREAD_POOL_SIZE` (32): size of the app's worker thread pool.
- `MAX_UPLOAD_MB` (10): largest Excel upload accepted; bigger ones get 413.
- `MAX_SHEET_ROWS` (2000): most rows one Excel job may have; longer sheets get
  400 naming the count and the limit.
- `MAX_QUEUED_JOBS` (20): once this many audio jobs are waiting, new Excel and
  story jobs get 503 with `Retry-After` until the line shortens.
- `MIN_FREE_DISK_GB` (5): with less free space than this on the output volume,
  new Excel and story jobs get 507 asking the maintainer to clear space.
- `OUTPUT_RETENTION_DAYS` (14): audio jobs that ended (completed, failed or
  cancelled) longer ago than this have their local fallback zips
  (`output/batch_archives/<job_id>/`) and job record deleted. Swept at startup
  and every 6 hours; queued/running jobs and everything under `output/heygen/`
  are never touched. A local fallback zip exists because its S3 upload failed,
  so it may be the only copy: download it before it expires.
- `VIDEO_OUTPUT_RETENTION_HOURS` (48, fractions allowed): video jobs that ended
  (completed, failed or cancelled) longer ago than this have their job folder
  (`output/heygen/<job_id>/`: TTS audio, raw and carded renders) deleted, as do
  job-id-named folders with no job record whose modification time is older than
  this. Swept at startup and every 6 hours. Job records, queued/running/recovering
  jobs and the `_`-prefixed folders (`_jobs`, `_batches`, `_library`, `_scripts`,
  `_audio_cache`) are never touched. The NAS holds the filed copy.
- `TTS_CACHE_RETENTION_DAYS` (7, fractions allowed): cached TTS audio
  (`output/heygen/_audio_cache/*.mp3`) not reused for longer than this is
  deleted by the same sweep; a cache hit refreshes the file's age. Other files
  in that folder are left alone.

## Redeploying safely

A restart fails every running or queued audio job: their work lives in memory.
So redeploy deliberately.

1. Check nothing is in flight. This must print `[]`:

   ```bash
   curl http://<host>:8080/batch/active
   ```

   Otherwise it lists each running or queued job (`job_id`, `kind`, `status`,
   `queue_position`, `rows_processed`, `total_rows`, `started_at`). Wait for them
   to finish, or cancel them.
2. Pull and restart:

   ```bash
   docker compose pull && docker compose up -d
   ```

Keep one uvicorn worker: the job stores are in memory, so a second worker would
not see the first one's jobs. The container is given 30 seconds (`stop_grace_period`)
to stop.

## Compilations (Comp engine)

The Compilations page needs the Comp engine, which runs on the Mac mini outside
Docker. Setting it up is covered step by step in [MAC_MINI_SETUP.md](MAC_MINI_SETUP.md).

## Health and Logs

```bash
curl http://localhost:8080/health
docker compose logs -f autodub
docker compose ps
```

## Stop

```bash
docker compose down
```
