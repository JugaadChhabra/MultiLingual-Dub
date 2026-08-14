"""Configuration, resolved once at the edge of a request.

Every key this app reads comes from one of the settings classes below, each
owned by the module it configures. Nothing deeper than a route resolves
configuration: a handler builds the settings its work needs, and passes them
down. A missing key therefore fails the request that asked for the work,
instead of a batch job forty rows in.

Two knobs deliberately stay outside this model and read the process
environment directly — ``API_RETRY_*`` in services/retry.py and
``AUDIO_COMPRESS_*`` in services/audio_compress.py. They describe the machine
the container runs on, not whose account is being used, and the modules that
read them are leaf utilities called from everywhere; threading settings into
them would recreate the parameter-threading this module exists to remove.
"""
from __future__ import annotations

from dataclasses import dataclass

from services.elevenlabs import ElevenLabsSettings
from services.email import EmailSettings
from services.nas import NasConfig
from services.qc import QCSettings
from services.runtime_config import RuntimeConfig, read_setting
from services.s3 import S3Config
from services.sarvam import SarvamSettings
from services.video_pipeline.heygen_client import HeyGenSettings

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BatchSettings:
    """Policy for the audio batch pipeline — how it uploads and how wide it runs."""

    upload_to_s3: bool
    max_language_parallelism: int | None

    REQUIRED: tuple[str, ...] = ()

    @classmethod
    def resolve(cls, session: RuntimeConfig | None = None) -> BatchSettings:
        raw = read_setting("BATCH_MAX_LANGUAGE_PARALLELISM", session)
        try:
            parallelism = int(raw) if raw else None
        except ValueError:
            parallelism = None
        return cls(
            upload_to_s3=read_setting("BATCH_ENABLE_S3_UPLOAD", session).lower() in _TRUTHY,
            max_language_parallelism=parallelism,
        )


# Every settings class that contributes to "is this session configured?".
# Adding a class here — or a key to one of their REQUIRED tuples — updates the
# status endpoint automatically. The list it replaced was maintained by hand and
# had drifted: it omitted HEYGEN_ISHWARI entirely, so a session with no HeyGen
# key at all reported itself as fully configured.
_SETTINGS_CLASSES = (
    S3Config,
    ElevenLabsSettings,
    HeyGenSettings,
    QCSettings,
    SarvamSettings,
    NasConfig,
    EmailSettings,
    BatchSettings,
)


def required_keys() -> list[str]:
    """Every key that must be present for the app to be fully usable."""
    keys: list[str] = []
    for cls in _SETTINGS_CLASSES:
        for key in cls.REQUIRED:
            if key not in keys:
                keys.append(key)
    return keys


def missing_keys(session: RuntimeConfig | None = None) -> list[str]:
    return [key for key in required_keys() if not read_setting(key, session)]


@dataclass(frozen=True)
class Settings:
    """Everything, for the callers that genuinely need everything.

    Functions deeper in a pipeline take the narrow piece — ``_finalize_video``
    takes a NasConfig, not this — so a signature still says what it touches.
    """

    s3: S3Config
    eleven: ElevenLabsSettings
    qc: QCSettings
    sarvam: SarvamSettings
    nas: NasConfig
    email: EmailSettings
    batch: BatchSettings

    @classmethod
    def resolve(cls, session: RuntimeConfig | None = None) -> Settings:
        """Resolve every subsystem. Raises if any required key is missing.

        Callers that need only part of this should resolve only that part —
        an Excel batch has no business failing because the NAS is unset.
        """
        return cls(
            s3=S3Config.resolve(session),
            eleven=ElevenLabsSettings.resolve(session),
            qc=QCSettings.resolve(session),
            sarvam=SarvamSettings.resolve(session),
            nas=NasConfig.resolve(session),
            email=EmailSettings.resolve(session),
            batch=BatchSettings.resolve(session),
        )
