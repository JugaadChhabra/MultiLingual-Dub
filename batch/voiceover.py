"""Turning one row of the input sheet into audio, in every language asked for.

This is the unit of work the batch pipeline is built from, and the reason it is
its own module: the main pass and the retry pass both need exactly this, and
before it had a name they were two hand-written copies of the same chain that
could — and did — drift apart.

The chain is translate -> QC -> speak -> compress. QC is called ONCE for the
whole row rather than per language: Gemini prices per request and the system
instruction is identical across a row's languages, so batching them is a real
cost difference on a sheet with hundreds of rows.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from batch.models import ExcelRow
from services.audio_compress import compress_mp3_bytes
from services.elevenlabs import (
    ElevenLabsSettings,
    batch_config_for_language,
    synthesize_speech_bytes,
)
from services.qc import QCError, QCSettings, qc_translations_batch
from services.sarvam import SarvamSettings
from services.translation import translate_with_fallback

logger = logging.getLogger(__name__)

# Which step a language task died at. Only 'translation' feeds the
# translation_fallbacks counter, so this cannot be collapsed to a bare string.
Stage = Literal["translation", "qc", "tts"]


@dataclass(frozen=True)
class TaskFailure:
    """One language of one row, and where it went wrong."""

    language: str
    stage: Stage
    reason: str


@dataclass(frozen=True)
class RowOutcome:
    """What a row produced. Languages appear in exactly one of these."""

    audio: dict[str, bytes]
    failures: list[TaskFailure]

    @property
    def languages_attempted(self) -> int:
        return len(self.audio) + len(self.failures)


@dataclass(frozen=True)
class VoiceoverDeps:
    """Everything constant for a whole job: provider settings, the teaching-mode
    flag, and how wide to fan translations out."""

    sarvam: SarvamSettings
    qc: QCSettings
    eleven: ElevenLabsSettings
    teaching_mode: bool = False
    translation_parallelism: int = 1


def _generate_elevenlabs_audio_bytes(
    text: str,
    language: str,
    settings: ElevenLabsSettings,
) -> bytes:
    return synthesize_speech_bytes(
        text,
        api_key=settings.api_key,
        config=batch_config_for_language(language, settings),
    )


async def _translate_language_async(
    text: str, language: str, sarvam: SarvamSettings
) -> tuple[str, str | None, str | None]:
    """Returns (language, translated_text, error). Exactly one of the last two will be None."""
    try:
        translated = await asyncio.to_thread(
            translate_with_fallback,
            text,
            settings=sarvam,
            target_language_code=language,
            source_language_code="auto",
        )
        return language, translated, None
    except Exception as exc:
        return language, None, str(exc)


async def _translate_row_languages(
    *,
    text: str,
    target_languages: list[str],
    max_parallelism: int,
    sarvam: SarvamSettings,
) -> dict[str, tuple[str | None, str | None]]:
    """Translate one text into many languages concurrently.

    Returns {language: (translated_text, error)}. A task that raises outright is
    reported as an error for its language rather than sinking the row.
    """
    semaphore = asyncio.Semaphore(max_parallelism)

    async def _translate(language: str) -> tuple[str, str | None, str | None]:
        async with semaphore:
            return await _translate_language_async(text, language, sarvam)

    tasks = [asyncio.create_task(_translate(language)) for language in target_languages]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: dict[str, tuple[str | None, str | None]] = {}
    for language, result in zip(target_languages, raw):
        if isinstance(result, BaseException):
            results[language] = (None, str(result))
        elif isinstance(result, tuple) and len(result) == 3:
            _, translated, error = result
            results[language] = (translated, error)
        else:
            results[language] = (None, f"Unexpected translation result type: {type(result).__name__}")
    return results


async def voice_row(
    row: ExcelRow,
    languages: list[str],
    deps: VoiceoverDeps,
    *,
    log_prefix: str = "",
) -> RowOutcome:
    """Produce audio for ``row`` in each of ``languages``.

    Never raises for a per-language problem — a language that fails translation,
    QC or TTS comes back as a TaskFailure so the caller can count it and retry
    it later. Only a genuinely unexpected error propagates.
    """
    audio: dict[str, bytes] = {}
    failures: list[TaskFailure] = []

    if not languages:
        return RowOutcome(audio=audio, failures=failures)

    # 1. Translate, fanned out across languages.
    logger.info(
        "%stranslating into %d language(s) (parallelism=%d)",
        log_prefix, len(languages), deps.translation_parallelism,
    )
    translations = await _translate_row_languages(
        text=row.text,
        target_languages=languages,
        max_parallelism=deps.translation_parallelism,
        sarvam=deps.sarvam,
    )

    translated_by_language: dict[str, str] = {}
    for language in languages:
        text, error = translations[language]
        clean = (text or "").strip()
        if error is not None or not clean:
            reason = error or "empty translation result"
            failures.append(TaskFailure(language=language, stage="translation", reason=reason))
            logger.error("%slang %s: translation failed; skipping TTS (%s)", log_prefix, language, reason)
            continue
        translated_by_language[language] = clean

    if not translated_by_language:
        return RowOutcome(audio=audio, failures=failures)

    # 2. QC — one call for the whole row.
    logger.info("%sQC start for %d language(s)", log_prefix, len(translated_by_language))
    qc_by_language: dict[str, str] = {}
    try:
        qc_results = await asyncio.to_thread(
            qc_translations_batch,
            row.text,
            translated_by_language,
            list(translated_by_language.keys()),
            settings=deps.qc,
            teaching_mode=deps.teaching_mode,
        )
    except QCError as exc:
        reason = f"QC failed: {exc}"
        logger.error("%s%s", log_prefix, reason)
        failures.extend(
            TaskFailure(language=lang, stage="qc", reason=reason) for lang in translated_by_language
        )
        return RowOutcome(audio=audio, failures=failures)
    except Exception as exc:
        reason = f"Unexpected QC failure: {exc}"
        logger.error("%s%s", log_prefix, reason)
        failures.extend(
            TaskFailure(language=lang, stage="qc", reason=reason) for lang in translated_by_language
        )
        return RowOutcome(audio=audio, failures=failures)

    for language in translated_by_language:
        qc_text = (qc_results.get(language) or "").strip()
        if not qc_text:
            failures.append(
                TaskFailure(language=language, stage="qc", reason="QC returned empty translation")
            )
            logger.error("%slang %s: QC produced no output", log_prefix, language)
            continue
        qc_by_language[language] = qc_text
    logger.info("%sQC complete", log_prefix)

    # 3. Speak each language, then compress.
    for language, text in qc_by_language.items():
        tts_text = f"[{row.emotion}] {text}" if row.emotion else text
        try:
            raw_audio = await asyncio.to_thread(
                _generate_elevenlabs_audio_bytes, tts_text, language, deps.eleven
            )
        except Exception as exc:
            failures.append(TaskFailure(language=language, stage="tts", reason=str(exc)))
            logger.error("%slang %s: TTS failed (%s)", log_prefix, language, exc)
            continue
        audio[language] = compress_mp3_bytes(raw_audio)
        logger.info("%slang %s: audio ready", log_prefix, language)

    return RowOutcome(audio=audio, failures=failures)
