# AutoDub

Automated multilingual audio dubbing. Upload text/Excel, translate, run QC, and generate audio files.

## Quick Start (Docker + GHCR)

```bash
git clone https://github.com/JugaadChhabra/MultiLingual-Dub.git
cd MultiLingual-Dub
touch .env
```

Fill all required keys in `.env`:

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
- `ENGLISH_VOICE`

Then run:

```bash
docker pull ghcr.io/jugaadchhabra/autodub:latest
docker compose up -d
```

Open `http://localhost:8080`.

## Translation Providers

- Existing Indian language targets (`bn-IN`, `en-IN`, `gu-IN`, `hi-IN`, `kn-IN`, `ml-IN`, `mr-IN`, `od-IN`, `pa-IN`, `ta-IN`, `te-IN`) continue to use Sarvam.
- Additional foreign language targets (`fr`, `de`, `es`, `ru`, `pt`) use in-process translation inside the same AutoDub service.
- QC (Gemini), batching, and audio generation flow are unchanged for all targets.

## Runtime `.env` in Browser

The web UI supports pasting full `.env` text into a runtime config box.  
This config is stored in memory per browser session (HTTP-only cookie), not written to files.

## Common Commands

```bash
docker compose logs -f
docker compose restart
docker compose down
```

## API

```bash
curl http://localhost:8080/health
curl http://localhost:8080/batch/excel-jobs/JOB_ID
```

## Development (Local)

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Run API:

```bash
uvicorn api.routes:app --reload --port 8080
```
