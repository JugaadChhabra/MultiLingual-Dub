"""Tests for turning one row into audio.

The unit both the main pass and the retry pass now run through, so a behaviour
proven here is proven for both — which was not true when they were two separate
copies of the chain.
"""
from __future__ import annotations

import asyncio

import pytest

from batch.models import ExcelRow
from batch.voiceover import RowOutcome, VoiceoverDeps, voice_row
from services.elevenlabs import ElevenLabsSettings
from services.qc import QCError, QCSettings
from services.sarvam import SarvamSettings

LANGS = ["hi-IN", "ta-IN"]


def _row(*, emotion: str = "", text: str = "hello") -> ExcelRow:
    return ExcelRow(row_index=2, text=text, emotion=emotion, activity_name="Act", audio_type="promo")


def _deps(*, parallelism: int = 2, teaching_mode: bool = False) -> VoiceoverDeps:
    return VoiceoverDeps(
        sarvam=SarvamSettings(api_key="sarvam"),
        qc=QCSettings(api_key="gemini", models=["model-a"], enabled=True),
        eleven=ElevenLabsSettings(api_key="key", desi_voice_id="desi", english_voice_id="english"),
        teaching_mode=teaching_mode,
        translation_parallelism=parallelism,
    )


@pytest.fixture
def happy(monkeypatch):
    """Every provider works. Records what each was asked for."""
    calls: dict[str, list] = {"translate": [], "qc": [], "tts": []}

    async def translate(text, language, _sarvam=None):
        calls["translate"].append((text, language))
        return language, f"t:{text}:{language}", None

    def qc(original, translations, languages, *, settings=None, teaching_mode=False):
        calls["qc"].append((original, dict(translations), list(languages), teaching_mode))
        return {lang: f"qc:{text}" for lang, text in translations.items()}

    def tts(text, language, settings=None):
        calls["tts"].append((text, language))
        return b"audio:" + text.encode()

    monkeypatch.setattr("batch.voiceover._translate_language_async", translate)
    monkeypatch.setattr("batch.voiceover.qc_translations_batch", qc)
    monkeypatch.setattr("batch.voiceover._generate_elevenlabs_audio_bytes", tts)
    monkeypatch.setattr("batch.voiceover.compress_mp3_bytes", lambda b: b + b":compressed")
    return calls


def _run(row, languages, deps=None) -> RowOutcome:
    return asyncio.run(voice_row(row, languages, deps or _deps()))


# --- the happy path --------------------------------------------------------


def test_every_language_gets_audio(happy) -> None:
    outcome = _run(_row(), LANGS)

    assert set(outcome.audio) == {"hi-IN", "ta-IN"}
    assert outcome.failures == []
    assert outcome.languages_attempted == 2


def test_qc_is_called_once_for_the_whole_row(happy) -> None:
    """Gemini prices per request and the system instruction is identical across
    a row's languages, so one call per row is the point, not an accident."""
    _run(_row(), ["hi-IN", "ta-IN", "te-IN", "kn-IN"])

    assert len(happy["qc"]) == 1
    _, translations, languages, _ = happy["qc"][0]
    assert set(languages) == {"hi-IN", "ta-IN", "te-IN", "kn-IN"}
    assert len(translations) == 4


def test_the_chain_runs_in_order(happy) -> None:
    _run(_row(), ["hi-IN"])

    assert happy["translate"] == [("hello", "hi-IN")]
    assert happy["qc"][0][1] == {"hi-IN": "t:hello:hi-IN"}
    assert happy["tts"] == [("qc:t:hello:hi-IN", "hi-IN")]


def test_audio_is_compressed(happy) -> None:
    outcome = _run(_row(), ["hi-IN"])

    assert outcome.audio["hi-IN"].endswith(b":compressed")


def test_emotion_prefixes_the_spoken_text(happy) -> None:
    _run(_row(emotion="excited"), ["hi-IN"])

    assert happy["tts"][0][0].startswith("[excited] ")


def test_no_emotion_means_no_prefix(happy) -> None:
    _run(_row(emotion=""), ["hi-IN"])

    assert not happy["tts"][0][0].startswith("[")


def test_teaching_mode_reaches_qc(happy) -> None:
    _run(_row(), ["hi-IN"], _deps(teaching_mode=True))

    assert happy["qc"][0][3] is True


def test_no_languages_does_no_work(happy) -> None:
    outcome = _run(_row(), [])

    assert outcome.audio == {}
    assert outcome.failures == []
    assert happy["translate"] == []
    assert happy["qc"] == []


# --- translation failures --------------------------------------------------


def test_a_failed_translation_is_reported_with_its_stage(monkeypatch, happy) -> None:
    async def translate(text, language, _sarvam=None):
        if language == "ta-IN":
            return language, None, "429 rate limit"
        return language, f"t:{text}:{language}", None

    monkeypatch.setattr("batch.voiceover._translate_language_async", translate)

    outcome = _run(_row(), LANGS)

    assert set(outcome.audio) == {"hi-IN"}
    assert len(outcome.failures) == 1
    assert outcome.failures[0].language == "ta-IN"
    assert outcome.failures[0].stage == "translation"
    assert "429" in outcome.failures[0].reason


def test_an_empty_translation_counts_as_a_failure(monkeypatch, happy) -> None:
    async def translate(text, language, _sarvam=None):
        return language, "   ", None

    monkeypatch.setattr("batch.voiceover._translate_language_async", translate)

    outcome = _run(_row(), ["hi-IN"])

    assert outcome.audio == {}
    assert outcome.failures[0].stage == "translation"


def test_a_crashing_translation_task_does_not_sink_the_row(monkeypatch, happy) -> None:
    async def translate(text, language, _sarvam=None):
        if language == "ta-IN":
            raise RuntimeError("task exploded")
        return language, f"t:{text}:{language}", None

    monkeypatch.setattr("batch.voiceover._translate_language_async", translate)

    outcome = _run(_row(), LANGS)

    assert set(outcome.audio) == {"hi-IN"}
    assert outcome.failures[0].language == "ta-IN"
    assert "task exploded" in outcome.failures[0].reason


def test_a_language_that_failed_translation_is_not_sent_to_qc(monkeypatch, happy) -> None:
    async def translate(text, language, _sarvam=None):
        if language == "ta-IN":
            return language, None, "nope"
        return language, f"t:{text}:{language}", None

    monkeypatch.setattr("batch.voiceover._translate_language_async", translate)

    _run(_row(), LANGS)

    assert list(happy["qc"][0][1]) == ["hi-IN"]


def test_qc_is_skipped_entirely_when_every_translation_fails(monkeypatch, happy) -> None:
    async def translate(text, language, _sarvam=None):
        return language, None, "nope"

    monkeypatch.setattr("batch.voiceover._translate_language_async", translate)

    outcome = _run(_row(), LANGS)

    assert happy["qc"] == []
    assert happy["tts"] == []
    assert len(outcome.failures) == 2


# --- QC failures -----------------------------------------------------------


def test_a_qc_error_fails_every_language_it_covered(monkeypatch, happy) -> None:
    def qc(*args, **kwargs):
        raise QCError("gemini exhausted")

    monkeypatch.setattr("batch.voiceover.qc_translations_batch", qc)

    outcome = _run(_row(), LANGS)

    assert outcome.audio == {}
    assert {f.language for f in outcome.failures} == {"hi-IN", "ta-IN"}
    assert all(f.stage == "qc" for f in outcome.failures)
    assert happy["tts"] == []


def test_an_unexpected_qc_crash_is_also_a_qc_failure(monkeypatch, happy) -> None:
    def qc(*args, **kwargs):
        raise ValueError("something else entirely")

    monkeypatch.setattr("batch.voiceover.qc_translations_batch", qc)

    outcome = _run(_row(), ["hi-IN"])

    assert outcome.failures[0].stage == "qc"
    assert "something else entirely" in outcome.failures[0].reason


def test_an_empty_qc_result_fails_only_that_language(monkeypatch, happy) -> None:
    def qc(original, translations, languages, *, settings=None, teaching_mode=False):
        return {lang: ("" if lang == "ta-IN" else f"qc:{t}") for lang, t in translations.items()}

    monkeypatch.setattr("batch.voiceover.qc_translations_batch", qc)

    outcome = _run(_row(), LANGS)

    assert set(outcome.audio) == {"hi-IN"}
    assert outcome.failures[0].language == "ta-IN"
    assert outcome.failures[0].stage == "qc"


# --- TTS failures ----------------------------------------------------------


def test_a_failed_tts_is_reported_with_its_stage(monkeypatch, happy) -> None:
    def tts(text, language, settings=None):
        if language == "ta-IN":
            raise RuntimeError("11labs timeout")
        return b"audio"

    monkeypatch.setattr("batch.voiceover._generate_elevenlabs_audio_bytes", tts)

    outcome = _run(_row(), LANGS)

    assert set(outcome.audio) == {"hi-IN"}
    assert outcome.failures[0].language == "ta-IN"
    assert outcome.failures[0].stage == "tts"
    assert "11labs timeout" in outcome.failures[0].reason


def test_one_language_failing_tts_does_not_stop_the_others(monkeypatch, happy) -> None:
    def tts(text, language, settings=None):
        if language == "hi-IN":
            raise RuntimeError("boom")
        return b"audio"

    monkeypatch.setattr("batch.voiceover._generate_elevenlabs_audio_bytes", tts)

    outcome = _run(_row(), ["hi-IN", "ta-IN", "te-IN"])

    assert set(outcome.audio) == {"ta-IN", "te-IN"}
    assert len(outcome.failures) == 1


# --- retry uses the same code ---------------------------------------------


def test_retrying_a_subset_of_languages_works_the_same(happy) -> None:
    """The retry pass calls this with only the failed languages — the property
    that lets both passes share one implementation."""
    outcome = _run(_row(), ["ta-IN"])

    assert set(outcome.audio) == {"ta-IN"}
    assert len(happy["qc"]) == 1
    assert list(happy["qc"][0][1]) == ["ta-IN"]
