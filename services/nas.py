from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from services.runtime_config import RuntimeConfig, get_config_value

logger = logging.getLogger(__name__)

try:
    import smbclient
except Exception:
    smbclient = None

VIDEOS_ROOT = "videos"


@dataclass(frozen=True)
class NasConfig:
    mode: str
    root_path: str
    server: str
    share: str
    username: str
    password: str
    domain: str
    port: int


def get_nas_config(runtime_config: RuntimeConfig | None = None) -> NasConfig:
    mode = get_config_value("NAS_MODE", runtime_config=runtime_config) or "local"
    if mode not in ("local", "smb"):
        logger.warning("Invalid NAS_MODE '%s', falling back to 'local'", mode)
        mode = "local"
    port_raw = get_config_value("NAS_PORT", runtime_config=runtime_config) or "445"
    try:
        port = int(port_raw)
    except ValueError:
        port = 445
    return NasConfig(
        mode=mode,
        root_path=get_config_value("NAS_ROOT_PATH", runtime_config=runtime_config) or "./nas_data",
        server=get_config_value("NAS_SERVER", runtime_config=runtime_config),
        share=get_config_value("NAS_SHARE", runtime_config=runtime_config),
        username=get_config_value("NAS_USERNAME", runtime_config=runtime_config),
        password=get_config_value("NAS_PASSWORD", runtime_config=runtime_config),
        domain=get_config_value("NAS_DOMAIN", runtime_config=runtime_config),
        port=port,
    )


class NasService:
    def __init__(self, config: NasConfig) -> None:
        self.config = config
        self.local_root = Path(config.root_path).resolve()
        remote_base = (
            f"//{config.server}/{config.share}"
            if config.server and config.share
            else ""
        )
        root_sub = config.root_path.strip("/")
        self.remote_root = f"{remote_base}/{root_sub}" if root_sub else remote_base

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if self.config.mode != "smb":
            return
        if smbclient is None:
            raise RuntimeError(
                "smbprotocol is not installed. Run: pip install smbprotocol==1.13.0"
            )
        if not self.remote_root:
            raise RuntimeError("SMB mode requires NAS_SERVER and NAS_SHARE to be configured.")

        kwargs: dict = {
            "username": self.config.username,
            "password": self.config.password,
            "port": self.config.port,
        }
        domain = (self.config.domain or "").strip()
        if domain:
            try:
                smbclient.register_session(self.config.server, domain=domain, **kwargs)
                return
            except TypeError:
                kwargs["username"] = f"{domain}\\{self.config.username}"
        smbclient.register_session(self.config.server, **kwargs)

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _local_path(self, relative_path: str) -> Path:
        return self.local_root / relative_path.lstrip("/")

    def _remote_path(self, relative_path: str) -> str:
        return f"{self.remote_root}/{relative_path.lstrip('/')}"

    def makedirs(self, relative_path: str) -> None:
        if self.config.mode == "local":
            self._local_path(relative_path).mkdir(parents=True, exist_ok=True)
            return
        self.connect()
        smbclient.makedirs(self._remote_path(relative_path), exist_ok=True)

    def write_file(self, relative_path: str, content: bytes) -> None:
        dir_path = str(PurePosixPath(relative_path).parent)
        self.makedirs(dir_path)
        if self.config.mode == "local":
            self._local_path(relative_path).write_bytes(content)
            return
        self.connect()
        with smbclient.open_file(self._remote_path(relative_path), mode="wb") as f:
            f.write(content)

    def read_file(self, relative_path: str) -> bytes:
        if self.config.mode == "local":
            return self._local_path(relative_path).read_bytes()
        self.connect()
        with smbclient.open_file(self._remote_path(relative_path), mode="rb") as f:
            return f.read()

    def list_files(self, relative_path: str) -> list[str]:
        if self.config.mode == "local":
            path = self._local_path(relative_path)
            if not path.exists() or not path.is_dir():
                return []
            return sorted(e.name for e in path.iterdir())
        self.connect()
        base = self._remote_path(relative_path)
        if not smbclient.path.exists(base):
            return []
        return sorted(smbclient.listdir(base))

    # ------------------------------------------------------------------
    # Domain-level helpers
    # ------------------------------------------------------------------

    def ensure_base_folders(self) -> None:
        self.makedirs(VIDEOS_ROOT)

    def build_video_path(self, job_id: str, video_title: str) -> str:
        safe_title = video_title.replace("/", "_").replace("\\", "_")
        if not safe_title.lower().endswith(".mp4"):
            safe_title = f"{safe_title}.mp4"
        return str(PurePosixPath(VIDEOS_ROOT, job_id, safe_title))

    def upload_video(self, job_id: str, video_title: str, local_path: str) -> str:
        """Read local .mp4 and write to NAS. Returns the relative NAS path."""
        content = Path(local_path).read_bytes()
        nas_path = self.build_video_path(job_id, video_title)
        self.write_file(nas_path, content)
        logger.info("NAS upload OK: %s → %s (mode=%s)", local_path, nas_path, self.config.mode)
        return nas_path
