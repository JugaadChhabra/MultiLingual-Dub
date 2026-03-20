# AutoDub Docker Deployment (GHCR)

## Pull and Run

```bash
git clone https://github.com/JugaadChhabra/MultiLingual-Dub.git
cd MultiLingual-Dub
touch .env
```

Set all required keys in `.env`:

- `SARVAM_API`
- `GEMINI_API_KEY`
- `WASABI_ACCESS_KEY`
- `WASABI_SECRET_KEY`
- `WASABI_BUCKET`
- `WASABI_REGION`
- `WASABI_ENDPOINT_URL`
- `AWS_ACCESS_KEY`
- `AWS_SECRET_KEY`
- `AWS_BUCKET`
- `AWS_REGION`
- `BATCH_ENABLE_WASABI_UPLOAD`
- `BATCH_ENABLE_QC`
- `ELEVEN_LABS`
- `AI_STUDIO_VOICE`
- `DESI_VOCAL_VOICE`

Start:

```bash
docker pull ghcr.io/jugaadchhabra/autodub:latest
docker compose up -d
```

App URL: `http://localhost:8080`

## Runtime Browser Config

The web interface includes a runtime `.env` text area:

- Paste full `.env` content
- Click **Apply Config**
- Config is session-scoped and memory-only
- No secrets are written to repo files

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
