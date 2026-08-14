from __future__ import annotations

import os
from io import StringIO

from dotenv import dotenv_values

# A parsed session .env, as pasted into the config panel. This is raw input, not
# resolved configuration — it becomes configuration by being handed to
# Settings.resolve() at the edge of a request. See services/settings.py.
RuntimeConfig = dict[str, str]


class MissingSettingError(ValueError):
    """One or more required keys were absent from both session config and env."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = sorted(keys)
        super().__init__("Missing required configuration: " + ", ".join(self.keys))


def parse_env_text(env_text: str) -> RuntimeConfig:
    parsed = dotenv_values(stream=StringIO(env_text))
    result: RuntimeConfig = {}
    for key, value in parsed.items():
        if key is None:
            continue
        if value is None:
            continue
        result[str(key).strip()] = str(value).strip()
    return result


def read_setting(key: str, session: RuntimeConfig | None = None) -> str:
    """Read one key: the session's value if it has a non-empty one, else the
    process environment. Empty string when neither has it.

    ONLY settings resolvers may call this — the ``resolve`` classmethods in
    nas / s3 / elevenlabs / qc / email / translation / heygen_client. Everything
    else takes an already-resolved settings object. Calling this from anywhere
    else reintroduces exactly what we removed: configuration read six frames
    deep, where a missing key fails a running job instead of a request.
    tests/test_settings.py enforces this.
    """
    if session is not None:
        value = session.get(key, "").strip()
        if value:
            return value
    return os.getenv(key, "").strip()


def require(keys: tuple[str, ...], session: RuntimeConfig | None) -> dict[str, str]:
    """Read every key, or raise listing ALL the missing ones at once.

    Reporting them together matters: someone pasting a .env should be told
    everything that's wrong in one go, not made to discover it one failed
    request at a time.
    """
    values = {key: read_setting(key, session) for key in keys}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise MissingSettingError(missing)
    return values
