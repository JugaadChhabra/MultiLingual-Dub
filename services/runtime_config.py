from __future__ import annotations

import os


class MissingSettingError(ValueError):
    """One or more required keys were absent from the environment."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = sorted(keys)
        super().__init__("Missing required configuration: " + ", ".join(self.keys))


def read_setting(key: str) -> str:
    """Read one key from the process environment; empty string when unset.

    ONLY settings resolvers may call this — the ``resolve`` classmethods in
    nas / s3 / elevenlabs / qc / email / translation / heygen_client. Everything
    else takes an already-resolved settings object. Calling this from anywhere
    else reintroduces exactly what we removed: configuration read six frames
    deep, where a missing key fails a running job instead of a request.
    tests/test_settings.py enforces this.
    """
    return os.getenv(key, "").strip()


def require(keys: tuple[str, ...]) -> dict[str, str]:
    """Read every key, or raise listing ALL the missing ones at once.

    Reporting them together matters: whoever is fixing the .env should be told
    everything that's wrong in one go, not made to discover it one failed
    request at a time.
    """
    values = {key: read_setting(key) for key in keys}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise MissingSettingError(missing)
    return values
