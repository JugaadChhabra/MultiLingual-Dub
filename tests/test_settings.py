"""Tests for configuration resolution.

Several of these moved here from tests/test_batch_service.py: the validations
they cover used to run inside a started job, and now run at the edge before one
is accepted. The behaviour is still checked — just where it now lives.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from batch.service import english_voice_is_required
from services.elevenlabs import ElevenLabsSettings
from services.email import EmailSettings
from services.nas import NasConfig
from services.qc import QCSettings
from services.runtime_config import MissingSettingError, read_setting, require
from services.s3 import S3Config, S3ConfigError
from services.sarvam import SarvamSettings
from services.settings import BatchSettings, Settings, missing_keys, required_keys
from services.video_pipeline.heygen_client import HeyGenSettings

REPO_ROOT = Path(__file__).resolve().parent.parent

FULL_ENV = {
    "ELEVEN_LABS": "eleven",
    "SARVAM_API": "sarvam",
    "GEMINI_API_KEY": "gemini",
    "HEYGEN_ISHWARI": "heygen",
    "AWS_ACCESS_KEY": "ak",
    "AWS_SECRET_KEY": "sk",
    "AWS_BUCKET": "bucket",
    "AWS_REGION": "region",
    "BATCH_ENABLE_QC": "true",
}


@pytest.fixture
def clean_env(monkeypatch):
    """No ambient configuration.

    api/routes.py calls load_dotenv() at import time, so any test that imports
    the app pulls the developer's real .env into os.environ for the whole
    session. Without scrubbing, these tests would pass or fail depending on
    what happens to be in that file.
    """
    for key in set(FULL_ENV) | {
        "DESI_VOCAL_VOICE", "ENGLISH_VOICE", "US_VOICE_ID", "ISHWARI_VOICE_ID",
        "AWS_ENDPOINT_URL", "GEMINI_QC_MODELS", "BATCH_ENABLE_S3_UPLOAD",
        "BATCH_MAX_LANGUAGE_PARALLELISM", "HEYGEN_BATCH_CONCURRENCY",
        "NAS_MODE", "NAS_ROOT_PATH", "NAS_SERVER", "NAS_SHARE", "NAS_PORT",
        "NAS_USERNAME", "NAS_PASSWORD", "NAS_DOMAIN", "US_CHARACTER_NAS_ROOT_PATH",
        "RESEND_API_KEY", "RESEND_FROM_ADDRESS", "NOTIFY_EMAILS",
    }:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


# --- precedence ------------------------------------------------------------


def test_session_value_wins_over_env(clean_env) -> None:
    clean_env.setenv("ELEVEN_LABS", "from-env")

    assert read_setting("ELEVEN_LABS", {"ELEVEN_LABS": "from-session"}) == "from-session"


def test_env_fills_in_what_the_session_omits(clean_env) -> None:
    """Per-key fallback: a partial paste inherits the rest from the process."""
    clean_env.setenv("SARVAM_API", "from-env")

    assert read_setting("SARVAM_API", {"ELEVEN_LABS": "x"}) == "from-env"


def test_an_empty_session_value_falls_back_rather_than_blanking(clean_env) -> None:
    clean_env.setenv("SARVAM_API", "from-env")

    assert read_setting("SARVAM_API", {"SARVAM_API": "   "}) == "from-env"


def test_missing_everywhere_is_empty(clean_env) -> None:
    assert read_setting("SARVAM_API", None) == ""


# --- failing early, and all at once ---------------------------------------


def test_all_missing_keys_are_reported_together(clean_env) -> None:
    with pytest.raises(MissingSettingError) as exc:
        require(("AWS_BUCKET", "AWS_REGION", "SARVAM_API"), None)

    assert exc.value.keys == ["AWS_BUCKET", "AWS_REGION", "SARVAM_API"]


def test_s3_reports_its_own_error_type(clean_env) -> None:
    with pytest.raises(S3ConfigError):
        S3Config.resolve(None)


def test_resolving_everything_fails_when_a_key_is_absent(clean_env) -> None:
    session = dict(FULL_ENV)
    del session["GEMINI_API_KEY"]

    with pytest.raises(MissingSettingError, match="GEMINI_API_KEY"):
        Settings.resolve(session)


def test_a_complete_session_resolves(clean_env) -> None:
    settings = Settings.resolve(dict(FULL_ENV))

    assert settings.s3.bucket == "bucket"
    assert settings.eleven.api_key == "eleven"
    assert settings.qc.api_key == "gemini"
    assert settings.sarvam.api_key == "sarvam"


# --- QC toggle (moved from test_batch_service) ----------------------------


def test_qc_disabled_is_a_configuration_error(clean_env) -> None:
    """Audio is only ever generated after QC, so this is not a feature toggle.
    Used to fail a started job; now rejects it."""
    session = dict(FULL_ENV, BATCH_ENABLE_QC="false")

    with pytest.raises(ValueError, match="BATCH_ENABLE_QC must be true"):
        QCSettings.resolve(session)


def test_an_unreadable_qc_toggle_is_treated_as_off(clean_env) -> None:
    with pytest.raises(ValueError, match="BATCH_ENABLE_QC must be true"):
        QCSettings.resolve(dict(FULL_ENV, BATCH_ENABLE_QC="banana"))


def test_qc_models_fall_back_to_defaults(clean_env) -> None:
    settings = QCSettings.resolve(dict(FULL_ENV))

    assert settings.models


# --- English voice (moved from test_batch_service) ------------------------


def test_english_targets_require_an_english_voice(clean_env) -> None:
    settings = ElevenLabsSettings(api_key="k", desi_voice_id="d", english_voice_id="")

    with pytest.raises(ValueError, match="ENGLISH_VOICE is required"):
        english_voice_is_required(["en-IN", "hi-IN"], settings)


def test_non_english_targets_do_not_require_an_english_voice(clean_env) -> None:
    settings = ElevenLabsSettings(api_key="k", desi_voice_id="d", english_voice_id="")

    english_voice_is_required(["hi-IN", "ta-IN"], settings)


# --- derived required keys ------------------------------------------------


def test_required_keys_include_the_heygen_key() -> None:
    """The hand-maintained list this replaced omitted it, so a session with no
    HeyGen key reported itself fully configured."""
    assert "HEYGEN_ISHWARI" in required_keys()


def test_optional_keys_are_not_required() -> None:
    keys = required_keys()

    # Has a hardcoded fallback voice.
    assert "DESI_VOCAL_VOICE" not in keys
    # Only needed when a request targets English.
    assert "ENGLISH_VOICE" not in keys
    # NAS works in local mode with nothing set.
    assert "NAS_SERVER" not in keys
    # Notifications are skipped when unconfigured, never fatal.
    assert "RESEND_API_KEY" not in keys


@pytest.mark.parametrize(
    "cls",
    [S3Config, ElevenLabsSettings, HeyGenSettings, QCSettings, SarvamSettings],
    ids=lambda c: c.__name__,
)
def test_every_required_key_really_is_required(clean_env, cls) -> None:
    """A key is only allowed on REQUIRED if its absence actually fails that
    subsystem's resolution — otherwise the status endpoint blocks people for
    no reason."""
    assert cls.REQUIRED, f"{cls.__name__} declares no required keys"

    for key in cls.REQUIRED:
        session = dict(FULL_ENV)
        del session[key]
        with pytest.raises((MissingSettingError, ValueError)):
            cls.resolve(session)


def test_the_audio_bundle_does_not_demand_video_keys(clean_env) -> None:
    """An Excel batch must not be blocked by an unset HeyGen key: the video
    pipeline resolves its own settings, separately."""
    session = dict(FULL_ENV)
    del session["HEYGEN_ISHWARI"]

    Settings.resolve(session)  # must not raise


def test_missing_keys_lists_what_is_absent(clean_env) -> None:
    assert set(missing_keys(None)) == set(required_keys())
    assert missing_keys(dict(FULL_ENV)) == []


# --- per-subsystem behaviour ----------------------------------------------


def test_us_character_gets_its_own_nas_root(clean_env) -> None:
    config = NasConfig.resolve({"NAS_ROOT_PATH": "/main", "US_CHARACTER_NAS_ROOT_PATH": "/us"})

    assert config.for_character("us").root_path == "/us"
    assert config.for_character("US").root_path == "/us"
    assert config.for_character("indian").root_path == "/main"


def test_us_character_uses_the_default_root_when_none_is_configured(clean_env) -> None:
    config = NasConfig.resolve({"NAS_ROOT_PATH": "/main"})

    assert config.for_character("us").root_path == "/main"


def test_an_invalid_nas_mode_falls_back_to_local(clean_env) -> None:
    assert NasConfig.resolve({"NAS_MODE": "ftp"}).mode == "local"


def test_a_bad_nas_port_falls_back_to_445(clean_env) -> None:
    assert NasConfig.resolve({"NAS_PORT": "not-a-number"}).port == 445


def test_character_voices_resolve_per_character(clean_env) -> None:
    settings = HeyGenSettings.resolve(
        dict(FULL_ENV, ISHWARI_VOICE_ID="voice-in", US_VOICE_ID="voice-us")
    )

    assert settings.voice_for_character("indian") == "voice-in"
    assert settings.voice_for_character("us") == "voice-us"
    assert settings.voice_for_character(None) == "voice-in"
    assert settings.voice_for_character("martian") == "voice-in"


def test_an_unconfigured_character_voice_is_none(clean_env) -> None:
    settings = HeyGenSettings.resolve(dict(FULL_ENV))

    assert settings.voice_for_character("us") is None


def test_a_bad_batch_concurrency_falls_back(clean_env) -> None:
    assert HeyGenSettings.resolve(dict(FULL_ENV, HEYGEN_BATCH_CONCURRENCY="lots")).batch_concurrency == 4
    assert HeyGenSettings.resolve(dict(FULL_ENV, HEYGEN_BATCH_CONCURRENCY="0")).batch_concurrency == 1


@pytest.mark.parametrize("raw,expected", [("true", True), ("YES", True), ("1", True),
                                          ("false", False), ("", False), ("maybe", False)])
def test_s3_upload_toggle_parsing(clean_env, raw, expected) -> None:
    assert BatchSettings.resolve({"BATCH_ENABLE_S3_UPLOAD": raw}).upload_to_s3 is expected


def test_email_is_only_configured_when_every_part_is_present(clean_env) -> None:
    assert not EmailSettings.resolve({"RESEND_API_KEY": "k"}).configured
    assert EmailSettings.resolve(
        {"RESEND_API_KEY": "k", "RESEND_FROM_ADDRESS": "a@b.c", "NOTIFY_EMAILS": "x@y.z"}
    ).configured


def test_notify_emails_splits_and_trims(clean_env) -> None:
    settings = EmailSettings.resolve({"NOTIFY_EMAILS": " a@b.c , , d@e.f "})

    assert settings.to_addresses == ("a@b.c", "d@e.f")


# --- the constraint that keeps this from unravelling ----------------------


def test_only_settings_resolvers_read_raw_keys() -> None:
    """read_setting is the one door to raw configuration, and only settings
    resolvers may open it. Anywhere else and we are back to configuration being
    read six frames deep, where a missing key fails a running job.
    """
    allowed = {
        Path("services/runtime_config.py"),
        Path("services/settings.py"),
        Path("services/nas.py"),
        Path("services/s3.py"),
        Path("services/elevenlabs.py"),
        Path("services/qc.py"),
        Path("services/sarvam.py"),
        Path("services/email.py"),
        Path("services/video_pipeline/heygen_client.py"),
    }
    pattern = re.compile(r"\bread_setting\s*\(")
    offenders = []
    for package in ("api", "services", "batch"):
        for path in (REPO_ROOT / package).glob("**/*.py"):
            rel = path.relative_to(REPO_ROOT)
            if rel in allowed:
                continue
            if pattern.search(path.read_text()):
                offenders.append(str(rel))

    assert offenders == [], f"read_setting called outside a settings resolver: {offenders}"


def test_config_is_not_read_from_the_environment_outside_the_allowed_modules() -> None:
    """os.getenv is likewise confined. retry.py and audio_compress.py are the
    two deliberate exceptions — process tuning, not session config."""
    allowed = {
        Path("services/runtime_config.py"),
        Path("services/retry.py"),
        Path("services/audio_compress.py"),
    }
    pattern = re.compile(r"os\.(getenv|environ)")
    offenders = []
    for package in ("api", "services", "batch"):
        for path in (REPO_ROOT / package).glob("**/*.py"):
            rel = path.relative_to(REPO_ROOT)
            if rel in allowed:
                continue
            if pattern.search(path.read_text()):
                offenders.append(str(rel))

    assert offenders == [], f"environment read outside the allowed modules: {offenders}"
