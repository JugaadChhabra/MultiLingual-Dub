"""Health, and whether the server's .env has every key the app needs."""
from __future__ import annotations

from fastapi import APIRouter

from api.models import ConfigStatusResponse
from services.settings import missing_keys, required_keys

router = APIRouter()


@router.get("/health")
async def health_check() -> dict:
    """Health check endpoint for Docker healthchecks and monitoring."""
    return {"status": "healthy", "service": "autodub"}


# The path keeps its old name: the header's config dot reads it.
@router.get("/config/session-env/status", response_model=ConfigStatusResponse)
async def get_config_status():
    absent = missing_keys()
    return ConfigStatusResponse(
        configured=not absent, missing_keys=absent, required_keys=required_keys()
    )
