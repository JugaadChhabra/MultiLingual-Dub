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
