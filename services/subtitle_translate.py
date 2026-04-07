from __future__ import annotations

from dataclasses import dataclass
import logging

from services.runtime_config import RuntimeConfig
from services.translation import translate_with_fallback


@dataclass(frozen=True)
class CueTextBatch:
    indices: list[int]
    text: str


logger = logging.getLogger(__name__)


def _status_code_from_exc(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _is_transient_translate_error(exc: Exception) -> bool:
    status = _status_code_from_exc(exc)
    if status is not None:
        return status == 429 or status >= 500

    message = str(exc).lower()
    transient_markers = (
        "rate limit",
        "429",
        "500",
        "internal server error",
        "timeout",
        "timed out",
        "service unavailable",
        "try again",
    )
    return any(marker in message for marker in transient_markers)


def _build_batches(
    texts: list[str],
    *,
    max_chars_per_request: int,
    separator: str,
) -> list[CueTextBatch]:
    batches: list[CueTextBatch] = []
    current_indices: list[int] = []
    current_parts: list[str] = []
    current_size = 0

    for idx, raw in enumerate(texts):
        text = raw.strip()
        if not text:
            continue

        added_size = len(text)
        if current_parts:
            added_size += len(separator)

        # Always allow at least one cue per batch even if it exceeds limit.
        if current_parts and current_size + added_size > max_chars_per_request:
            batches.append(CueTextBatch(indices=current_indices, text=separator.join(current_parts)))
            current_indices = []
            current_parts = []
            current_size = 0

        if current_parts:
            current_size += len(separator)
        current_parts.append(text)
        current_indices.append(idx)
        current_size += len(text)

    if current_parts:
        batches.append(CueTextBatch(indices=current_indices, text=separator.join(current_parts)))

    return batches


def _split_translated_batch(
    translated_text: str,
    *,
    expected_parts: int,
    separator: str,
) -> list[str] | None:
    parts = [part.strip() for part in translated_text.split(separator)]
    if len(parts) != expected_parts:
        return None
    if any(not part for part in parts):
        return None
    return parts


def translate_subtitle_texts(
    texts: list[str],
    *,
    target_language_code: str,
    runtime_config: RuntimeConfig | None = None,
    source_language_code: str = "auto",
    max_chars_per_request: int = 1800,
) -> list[str]:
    if max_chars_per_request < 200:
        raise ValueError("max_chars_per_request must be >= 200")

    separator = "\n<<<SUB_SPLIT>>>\n"
    translated_texts = list(texts)

    batches = _build_batches(
        texts,
        max_chars_per_request=max_chars_per_request,
        separator=separator,
    )

    for batch in batches:
        try:
            translated_batch_text = translate_with_fallback(
                batch.text,
                runtime_config=runtime_config,
                target_language_code=target_language_code,
                source_language_code=source_language_code,
            )
        except Exception as exc:
            # If the provider is rate-limiting/failing transiently, avoid per-cue fan-out.
            # Keeping source text for this batch prevents amplifying API pressure.
            if _is_transient_translate_error(exc):
                logger.warning(
                    "Subtitle translation batch skipped for %s due to transient provider error: %s",
                    target_language_code,
                    exc,
                )
                continue

            # Fall back to per-cue translation if the batched payload is rejected.
            for idx in batch.indices:
                single_text = texts[idx].strip()
                if not single_text:
                    continue
                try:
                    translated_texts[idx] = translate_with_fallback(
                        single_text,
                        runtime_config=runtime_config,
                        target_language_code=target_language_code,
                        source_language_code=source_language_code,
                    ).strip()
                except Exception as single_exc:
                    logger.warning(
                        "Subtitle translation cue fallback failed for %s; preserving source text. error=%s",
                        target_language_code,
                        single_exc,
                    )
            continue

        split_parts = _split_translated_batch(
            translated_batch_text,
            expected_parts=len(batch.indices),
            separator=separator,
        )

        if split_parts is not None:
            for idx, translated in zip(batch.indices, split_parts):
                translated_texts[idx] = translated
            continue

        # If separator handling changed during translation, retry per cue for correctness.
        for idx in batch.indices:
            single_text = texts[idx].strip()
            if not single_text:
                continue
            try:
                translated_texts[idx] = translate_with_fallback(
                    single_text,
                    runtime_config=runtime_config,
                    target_language_code=target_language_code,
                    source_language_code=source_language_code,
                ).strip()
            except Exception as single_exc:
                logger.warning(
                    "Subtitle translation cue recovery failed for %s; preserving source text. error=%s",
                    target_language_code,
                    single_exc,
                )

    return translated_texts
