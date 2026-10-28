# Content Hub Docker Deployment (GHCR)

## Pull and Run

```bash
git clone https://github.com/JugaadChhabra/MultiLingual-Dub.git
cd MultiLingual-Dub
touch .env
```

Set all required keys in `.env`:

- `SARVAM_API`
- `GEMINI_API_KEY`
- `OPENROUTER_API_KEY` (script writer)
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

App URL: `http://localhost:8080`

## Translation Routing

- Sarvam handles existing Indian language targets.
- In-process translation handles `fr`, `de`, `es`, `ru`, and `pt` inside Content Hub.
- QC and TTS flow stays the same after translation.

## Configuration

Every key is read from the `.env` file beside `docker-compose.yml`; restart the
container after changing it. The header's config dot shows how many required
keys were found, and hovering it lists any that are missing.

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
