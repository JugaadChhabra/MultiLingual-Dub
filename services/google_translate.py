from __future__ import annotations

from dataclasses import dataclass

from services.runtime_config import RuntimeConfig, read_setting


@dataclass(frozen=True)
class GoogleTranslateSettings:
    """Credentials for the official Google Cloud Translation API (v2).

    The key is deliberately optional at resolve time: only a handful of target
    languages route through Google (see GOOGLE_TRANSLATE_LANGUAGES), so a session
    that never touches them should not be forced to configure a key. The
    translator raises a clear MissingSettingError if it is actually asked to
    translate without one — reported per language, like any other translation
    failure, rather than sinking a whole job.

    Unlike the free web endpoint it replaces, this key belongs to a real Google
    Cloud project, so its usage is visible in the Cloud Console under
    APIs & Services -> Cloud Translation API -> Quotas & Metrics.
    """

    api_key: str

    REQUIRED: tuple[str, ...] = ()

    @classmethod
    def resolve(cls, session: RuntimeConfig | None = None) -> "GoogleTranslateSettings":
        return cls(api_key=read_setting("GOOGLE_TRANSLATE_API_KEY", session))
