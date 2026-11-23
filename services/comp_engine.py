"""The Comp engine: where it is, and the one door to talking to it.

Compilations (rhyme videos merged into one YouTube comp) are made by a separate
native process on the Mac mini, comp_gen/source (Go), not in this container:
Docker on macOS cannot reach Apple's hardware video encoder, and the rhymes
library is an SMB share mounted on the host. The engine listens on host
loopback only; the Compilations pane reaches it through api/routers/comps.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from services.runtime_config import read_setting

# Docker Desktop forwards host.docker.internal to the host's loopback, which is
# where the engine's LaunchAgent binds (comp_gen/macos/install.sh).
DEFAULT_URL = "http://host.docker.internal:8765"

# Tests swap in an httpx.MockTransport here, as heygen_client does.
_transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class CompEngineSettings:
    url: str

    # Optional: without it the pane says the engine is unreachable, nothing else breaks.
    REQUIRED: tuple[str, ...] = ()

    @classmethod
    def resolve(cls) -> CompEngineSettings:
        return cls(url=(read_setting("COMP_ENGINE_URL") or DEFAULT_URL).rstrip("/"))


def client(settings: CompEngineSettings, *, read_timeout: float | None = 60.0) -> httpx.AsyncClient:
    """A client for one request. Listing a library over SMB or detecting a
    rhyme's baked-in intro can take a while, hence the long read timeout; video
    streams pass None so a paused player never times out."""
    return httpx.AsyncClient(
        base_url=settings.url,
        transport=_transport,
        timeout=httpx.Timeout(10.0, read=read_timeout),
    )
