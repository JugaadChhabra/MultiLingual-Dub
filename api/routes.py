"""The app: logging, start-up, static mounts, and the routers, in order.

Each area's endpoints live in api/routers/; what they share is in api/shared.py.
The container starts ``api.routes:app``.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api import shared
from api.background import spawn
from api.routers import audio, config, library, pages, scripts, story, video, video_batch
from services.nas import NasConfig, NasService
from services.runtime_config import MissingSettingError

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        NasService(NasConfig.resolve()).ensure_base_folders()
    except Exception as exc:
        logging.warning("NAS base folder init failed (non-fatal): %s", exc)
    # The library's first full listing of the HeyGen account is ~30 requests.
    # Make it now, so the first person to open the library doesn't wait on it.
    try:
        spawn(shared.video_library.catalog.renders(shared.build_renderer()), name="library-warmup")
    except MissingSettingError:
        pass
    yield


app = FastAPI(lifespan=lifespan)

app.mount("/output", StaticFiles(directory=str(shared.OUTPUT_DIR)), name="output")
STATIC_DIR = Path("./static")
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Order matters once: library before video, or GET /video/heygen/library is
# taken by GET /video/heygen/{job_id}.
for router in (pages, config, library, video, video_batch, scripts, audio, story):
    app.include_router(router.router)
