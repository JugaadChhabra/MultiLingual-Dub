from __future__ import annotations

import asyncio
import secrets

from services.runtime_config import RuntimeConfig

SESSION_COOKIE_NAME = "autodub_session_id"

class SessionConfigStore:
    def __init__(self) -> None:
        self._configs: dict[str, RuntimeConfig] = {}
        self._lock = asyncio.Lock()

    def generate_session_id(self) -> str:
        return secrets.token_urlsafe(32)

    async def get(self, session_id: str) -> RuntimeConfig | None:
        async with self._lock:
            config = self._configs.get(session_id)
            if config is None:
                return None
            return dict(config)

    async def set(self, session_id: str, config: RuntimeConfig) -> None:
        async with self._lock:
            self._configs[session_id] = dict(config)

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            self._configs.pop(session_id, None)
