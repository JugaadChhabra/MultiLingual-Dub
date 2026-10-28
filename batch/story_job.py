"""Driving a reviewed story through the audio pipeline: a story job.

A story is one activity, one row per line, built in memory rather than read
from a sheet. It differs from an Excel batch in its order: every row is
translated into a raw draft first, then one *story fix* per language rewrites
all of that language's drafts at once with the whole story in view, and only
then is anything spoken. Lines a language's fix fails or leaves out fall back
to the Excel batch's per-row QC of their drafts, and are counted. The tally,
activity buffer, retry-then-upload tail and job-status shape are the Excel
batch's own, from batch.service.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from pathlib import Path

from batch.activity import ActivityBuffer
from batch.models import ExcelRow
from batch.naming import _resolve_activity_segment_name
from batch.service import _close_activity, _upload_destination, _voiceover_deps
from batch.store import JobsStore
from batch.upload import _upload_activity_archives
from batch.tally import BatchTally
from batch.voiceover import (
    RowOutcome,
    TaskFailure,
    VoiceoverDeps,
    qc_row,
    speak_row,
    translate_row,
    voice_row,
)
from services.settings import Settings
from services.story import StoryLine, fix_story

logger = logging.getLogger(__name__)


def story_rows(activity_name: str, lines: list[StoryLine]) -> list[ExcelRow]:
    """One row per line; each titled the activity name plus its 1-based number."""
    return [
        ExcelRow(
            row_index=n,
            text=line.text,
            emotion=line.emotion,
            activity_name=activity_name,
            audio_type=f"{activity_name}{n}",
        )
        for n, line in enumerate(lines, start=1)
    ]


def _is_english(language: str) -> bool:
    return language.strip().lower().startswith("en")


async def run_story_job(
    *,
    job_id: str,
    title: str,
    activity_name: str,
    lines: list[StoryLine],
    target_languages: list[str],
    jobs_store: JobsStore,
    settings: Settings,
    output_dir: Path | None = None,
) -> None:
    """Run a story job to completion. Polled and cancelled like an Excel batch."""
    tally = BatchTally()
    try:
        await _run_story_job_impl(
            job_id=job_id,
            title=title,
            activity_name=activity_name,
            lines=lines,
            target_languages=target_languages,
            jobs_store=jobs_store,
            tally=tally,
            settings=settings,
            output_dir=output_dir,
        )
    except Exception as exc:
        logger.exception("Job %s crashed unexpectedly: %s", job_id, exc)
        await jobs_store.fail(job_id, f"Story job crashed: {exc}", tally.summary, exc=exc)


# A fixed line this much shorter or longer than its draft has likely gained or
# lost content to a neighbour. It is still used, but counted as flagged.
STORY_FIX_MIN_LENGTH_RATIO = 0.4
STORY_FIX_MAX_LENGTH_RATIO = 2.5


def _length_is_suspect(fixed: str, draft: str) -> bool:
    if not draft:
        return False
    ratio = len(fixed) / len(draft)
    return not STORY_FIX_MIN_LENGTH_RATIO <= ratio <= STORY_FIX_MAX_LENGTH_RATIO


async def _fix_languages(
    *,
    job_id: str,
    title: str,
    rows: list[ExcelRow],
    languages: list[str],
    drafts: dict[int, dict[str, str]],
    texts: dict[int, dict[str, str]],
    failures: dict[int, list[TaskFailure]],
    deps: VoiceoverDeps,
    tally: BatchTally,
) -> None:
    """One story fix per language, fanned out as wide as translations are.

    Fixed text lands in ``texts``. A language whose fix fails or returns nothing
    usable, and a line its fix left out, fall back to per-row QC of the draft —
    one QC call per row, over every language of it that fell back. Only a line
    that per-row QC also fails becomes a 'qc' failure.
    """
    semaphore = asyncio.Semaphore(deps.translation_parallelism)

    async def _fix(language: str, story: dict[str, StoryLine], story_drafts: dict[str, str]):
        async with semaphore:
            return await asyncio.to_thread(
                fix_story, title, story, story_drafts, language, settings=deps.qc
            )

    # Lines whose translation failed have no draft, and are left out of the fix.
    # Per language: the lines to fix and their drafts, both keyed by line id.
    requests: dict[str, tuple[dict[str, StoryLine], dict[str, str]]] = {}
    for language in languages:
        drafted = [row for row in rows if language in drafts[row.row_index]]
        if drafted:
            requests[language] = (
                {str(row.row_index): StoryLine(text=row.text, emotion=row.emotion) for row in drafted},
                {str(row.row_index): drafts[row.row_index][language] for row in drafted},
            )

    results = await asyncio.gather(
        *(_fix(language, story, story_drafts) for language, (story, story_drafts) in requests.items()),
        return_exceptions=True,
    )

    # Per row, the drafts of the languages whose fix did not cover it.
    fallback: dict[int, dict[str, str]] = defaultdict(dict)
    for (language, (story, story_drafts)), result in zip(requests.items(), results):
        prefix = f"Job {job_id} | story fix {language} | "
        if isinstance(result, BaseException):
            logger.error("%sfailed, falling back to per-row QC: %s", prefix, result)
            result = {}
        missing: list[str] = []
        for line_id in story:
            fixed = result.get(line_id)
            if not fixed:
                missing.append(line_id)
                fallback[int(line_id)][language] = story_drafts[line_id]
                continue
            if _length_is_suspect(fixed, story_drafts[line_id]):
                logger.warning("%sline %s: fixed length is far from the draft's", prefix, line_id)
                tally.story_fix_flagged()
            texts[int(line_id)][language] = fixed
        if missing:
            whole = len(missing) == len(story)
            if not whole:
                logger.error("%slines %s left out, falling back to per-row QC", prefix, missing)
            tally.story_fix_fell_back(language, lines=len(missing), whole_language=whole)

    rows_by_index = {row.row_index: row for row in rows}
    for row_index, row_drafts in fallback.items():
        prefix = f"Job {job_id} | row {row_index} | story fix fallback | "
        qc_texts, qc_failures = await qc_row(rows_by_index[row_index], row_drafts, deps, log_prefix=prefix)
        texts[row_index].update(qc_texts)
        failures[row_index].extend(qc_failures)


async def _run_story_job_impl(
    *,
    job_id: str,
    title: str,
    activity_name: str,
    lines: list[StoryLine],
    target_languages: list[str],
    jobs_store: JobsStore,
    tally: BatchTally,
    settings: Settings,
    output_dir: Path | None,
) -> None:
    tally.started(await jobs_store.start(job_id))
    logger.info("Job %s started | story=%s | languages=%s", job_id, activity_name, target_languages)

    try:
        s3_client = _upload_destination(settings, tally)
    except Exception as exc:
        await jobs_store.fail(job_id, f"Story setup failed: {exc}", tally.summary, exc=exc)
        return

    rows = story_rows(activity_name, lines)
    tally.set_totals(rows=len(rows), languages=len(target_languages))
    await jobs_store.update_summary(job_id, tally.summary)

    deps = _voiceover_deps(settings, target_languages, None)
    folder = _resolve_activity_segment_name(activity_name, None)
    rows_by_index = {row.row_index: row for row in rows}
    english = [language for language in target_languages if _is_english(language)]
    translated = [language for language in target_languages if not _is_english(language)]

    # English is the source itself: no translation and no fix.
    texts: dict[int, dict[str, str]] = {
        row.row_index: {language: row.text for language in english} for row in rows
    }
    drafts: dict[int, dict[str, str]] = {}
    failures: dict[int, list[TaskFailure]] = defaultdict(list)
    crashed: set[int] = set()

    async def _cancelled() -> bool:
        if await jobs_store.is_cancelled(job_id):
            await jobs_store.cancel(job_id, tally.summary)
            return True
        return False

    try:
        # 1. Translate every row into raw drafts, with no per-row QC.
        for row in rows:
            if await _cancelled():
                return
            prefix = f"Job {job_id} | row {row.row_index} | "
            try:
                drafts[row.row_index], row_failures = await translate_row(
                    row, translated, deps, qc=False, log_prefix=prefix
                )
                failures[row.row_index].extend(row_failures)
            except Exception as exc:
                crashed.add(row.row_index)
                logger.exception("%sunexpected translation failure: %s", prefix, exc)

        # 2. One story fix per language, over every line that has a draft.
        if await _cancelled():
            return
        drafted_rows = [row for row in rows if row.row_index not in crashed]
        await _fix_languages(
            job_id=job_id,
            title=title or activity_name,
            rows=drafted_rows,
            languages=translated,
            drafts=drafts,
            texts=texts,
            failures=failures,
            deps=deps,
            tally=tally,
        )

        # 3. Speak each row with its fixed text.
        buffer = ActivityBuffer(target_languages)
        activity_failures: dict[int, list[TaskFailure]] = defaultdict(list)
        for row in rows:
            if await jobs_store.is_cancelled(job_id):
                # The clips already spoken are paid for, and the cancel dialog
                # promises finished rows stay uploaded: upload them, unretried.
                if any(buffer.files.values()):
                    await _upload_activity_archives(
                        job_id=job_id,
                        activity_name=folder,
                        target_languages=target_languages,
                        buffer=buffer,
                        s3_client=s3_client,
                        tally=tally,
                        activity_upload_counts={},
                        output_dir=output_dir,
                    )
                await jobs_store.cancel(job_id, tally.summary)
                return
            prefix = f"Job {job_id} | row {row.row_index} | "
            try:
                if row.row_index in crashed:
                    tally.row_crashed(row.row_index)
                    continue
                spoken = await speak_row(row, texts[row.row_index], deps, log_prefix=prefix)
                outcome = RowOutcome(
                    audio=spoken.audio, failures=failures[row.row_index] + spoken.failures
                )
                for language, audio in outcome.audio.items():
                    buffer.add(row, language, audio, log_prefix=prefix)
                for failure in outcome.failures:
                    activity_failures[row.row_index].append(failure)
                tally.row_finished(row.row_index, outcome)
                logger.info(
                    "%scomplete | languages ok=%d failed=%d",
                    prefix, len(outcome.audio), len(outcome.failures),
                )
            except Exception as exc:
                tally.row_crashed(row.row_index)
                logger.exception("%sunexpected row failure: %s", prefix, exc)
            finally:
                await jobs_store.update_summary(job_id, tally.summary)

        async def _revoice(row: ExcelRow, row_failures: list[TaskFailure], prefix: str) -> RowOutcome:
            """A failed voiceover is re-spoken from its fixed text, and a draft
            whose per-row QC failed is QC'd again; neither is re-translated or
            re-fixed. Only a failed translation is re-voiced from scratch, with
            per-row QC of its own."""
            respeak = {
                f.language: texts[row.row_index][f.language] for f in row_failures if f.stage == "tts"
            }
            requalify = {
                f.language: drafts[row.row_index][f.language] for f in row_failures if f.stage == "qc"
            }
            retranslate = [f.language for f in row_failures if f.stage == "translation"]
            qc_texts, qc_failures = await qc_row(row, requalify, deps, log_prefix=prefix)
            texts[row.row_index].update(qc_texts)
            spoken = await speak_row(row, {**respeak, **qc_texts}, deps, log_prefix=prefix)
            voiced = await voice_row(row, retranslate, deps, log_prefix=prefix)
            return RowOutcome(
                audio={**spoken.audio, **voiced.audio},
                failures=qc_failures + spoken.failures + voiced.failures,
            )

        await _close_activity(
            job_id=job_id,
            activity_name=folder,
            target_languages=target_languages,
            failures=dict(activity_failures),
            rows_by_index=rows_by_index,
            buffer=buffer,
            tally=tally,
            deps=deps,
            s3_client=s3_client,
            activity_upload_counts={},
            jobs_store=jobs_store,
            output_dir=output_dir,
            revoice=_revoice,
        )
        await jobs_store.complete(job_id, tally.summary)
    except Exception as exc:
        await jobs_store.fail(job_id, f"Story job failed: {exc}", tally.summary, exc=exc)
