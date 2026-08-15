"""The classifier turns a provider failure into something an operator can act on.

The operators are a sound engineer and a video editor. Neither reads logs, and
neither should have to: "ElevenLabs credits exhausted" is actionable, a stack
trace is not. What this must never do is guess confidently — an unrecognised
failure has to say so plainly rather than invent a cause.
"""
from __future__ import annotations

import httpx
import pytest

from services.errors import classify, Cause


def _http_error(status: int, body: str = "", provider_hint: str = "") -> Exception:
    request = httpx.Request("POST", f"https://api.{provider_hint or 'example'}.com/v1/x")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# ── quota: the case the operators hit most ──────────────────────────────────

def test_elevenlabs_quota_is_named_as_credits() -> None:
    exc = _http_error(401, '{"detail":{"status":"quota_exceeded","message":"quota"}}')
    c = classify(exc, provider="elevenlabs", stage="tts")

    assert c.kind == "quota"
    assert "ElevenLabs" in c.title and "credit" in c.title.lower()
    assert c.retryable is False          # retrying a spent quota just burns time
    assert c.action_url                  # somewhere to go and fix it


def test_sarvam_quota_names_the_fallback_that_already_happened() -> None:
    exc = _http_error(403, '{"error":{"message":"insufficient credits"}}')
    c = classify(exc, provider="sarvam", stage="translate")

    assert c.kind == "quota"
    assert "Sarvam" in c.title
    # translation falls back to Google automatically, so this is not fatal
    assert "google" in c.detail.lower()


def test_heygen_quota_is_named() -> None:
    exc = _http_error(400, '{"code":401028,"message":"quota exceeded"}')
    c = classify(exc, provider="heygen", stage="render")

    assert c.kind == "quota"
    assert "HeyGen" in c.title


# ── transient vs permanent ──────────────────────────────────────────────────

def test_rate_limit_is_separated_from_quota() -> None:
    """429 means slow down; quota means stop. Conflating them wastes an operator's
    afternoon waiting for a retry that cannot succeed."""
    c = classify(_http_error(429, "rate limited"), provider="elevenlabs", stage="tts")

    assert c.kind == "rate_limit"
    assert c.retryable is True
    assert "credit" not in c.title.lower()


def test_provider_outage_says_whose_fault_it_is() -> None:
    c = classify(_http_error(503, "upstream down"), provider="sarvam", stage="translate")

    assert c.kind == "provider_down"
    assert c.retryable is True
    assert "their" in c.detail.lower() or "sarvam" in c.detail.lower()


def test_bad_credentials_are_not_reported_as_quota() -> None:
    c = classify(_http_error(401, '{"detail":"invalid api key"}'), provider="elevenlabs", stage="tts")

    assert c.kind == "auth"
    assert c.retryable is False


# ── local failures ──────────────────────────────────────────────────────────

def test_missing_config_names_the_key() -> None:
    from services.runtime_config import MissingSettingError

    c = classify(MissingSettingError(["ELEVEN_LABS"]), provider=None, stage="tts")

    assert c.kind == "config"
    assert "ELEVEN_LABS" in c.title
    assert c.meta["keys"] == ["ELEVEN_LABS"]

    many = classify(MissingSettingError(["ELEVEN_LABS", "SARVAM_API"]), provider=None, stage="tts")
    assert "2 settings" in many.title
    assert "SARVAM_API" in many.detail


def test_nas_local_mode_is_a_warning_not_a_success() -> None:
    c = classify(RuntimeError("NAS upload running in LOCAL mode"), provider="nas", stage="nas_upload")

    assert c.kind == "nas_local"
    assert c.severity == "warn"


# ── the honest fallback ─────────────────────────────────────────────────────

def test_unknown_failure_does_not_invent_a_cause() -> None:
    c = classify(RuntimeError("something nobody predicted"), provider="heygen", stage="render")

    assert c.kind == "unknown"
    # names where it broke, keeps the provider's own words, claims nothing more
    assert "HeyGen" in c.title and "render" in c.title.lower()
    assert "something nobody predicted" in (c.raw or "")
    assert "credit" not in c.title.lower()


def test_classify_never_raises_on_junk() -> None:
    for junk in [None, "", RuntimeError(""), ValueError("\x00"), Exception()]:
        c = classify(junk, provider=None, stage="tts")
        assert isinstance(c, Cause)
        assert c.title


def test_cause_serialises_for_the_api() -> None:
    c = classify(_http_error(429, "slow down"), provider="heygen", stage="render")
    d = c.to_dict()

    assert d["kind"] == "rate_limit"
    assert set(d) >= {"kind", "title", "detail", "severity", "retryable"}


# ── the message is for a human ──────────────────────────────────────────────

@pytest.mark.parametrize("provider,stage", [
    ("elevenlabs", "tts"), ("sarvam", "translate"), ("heygen", "render"),
    ("s3", "upload"), ("nas", "nas_upload"), (None, "tts"),
])
def test_every_title_reads_as_a_sentence_not_a_trace(provider, stage) -> None:
    c = classify(_http_error(500, "Traceback (most recent call last): ..."), provider=provider, stage=stage)

    assert c.title[0].isupper()
    assert "Traceback" not in c.title
    assert len(c.title) < 80
