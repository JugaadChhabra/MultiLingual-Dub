"""Tests for the batch counters.

These used to be reachable only by running a whole batch job. The arithmetic
they cover — particularly a retry moving a row back from failed to succeeded —
is where the counters could previously disagree with each other.
"""
from __future__ import annotations

from batch.models import ArchiveDownload
from batch.tally import BatchTally
from batch.voiceover import RowOutcome, TaskFailure


def _outcome(*, ok: list[str] = (), failed: list[tuple[str, str]] = ()) -> RowOutcome:
    return RowOutcome(
        audio={lang: b"audio" for lang in ok},
        failures=[TaskFailure(language=lang, stage=stage, reason="boom") for lang, stage in failed],
    )


def _tally(rows: int = 3, languages: int = 2) -> BatchTally:
    tally = BatchTally()
    tally.set_totals(rows=rows, languages=languages)
    return tally


def test_totals_are_rows_times_languages() -> None:
    summary = _tally(rows=40, languages=8).summary

    assert summary.total_rows == 40
    assert summary.language_tasks_total == 320


def test_a_fully_successful_row_counts_as_succeeded() -> None:
    tally = _tally()

    tally.row_finished(2, _outcome(ok=["hi-IN", "ta-IN"]))
    summary = tally.summary

    assert summary.rows_processed == 1
    assert summary.rows_succeeded == 1
    assert summary.rows_failed == 0
    assert summary.language_tasks_succeeded == 2
    assert summary.language_tasks_failed == 0


def test_a_partly_failed_row_counts_as_failed() -> None:
    """Any failed language fails the row — it is not 'half done'."""
    tally = _tally()

    tally.row_finished(2, _outcome(ok=["hi-IN"], failed=[("ta-IN", "tts")]))
    summary = tally.summary

    assert summary.rows_succeeded == 0
    assert summary.rows_failed == 1
    assert summary.language_tasks_succeeded == 1
    assert summary.language_tasks_failed == 1


def test_only_translation_failures_count_as_fallbacks() -> None:
    tally = _tally()

    tally.row_finished(2, _outcome(failed=[("hi-IN", "translation")]))
    tally.row_finished(3, _outcome(failed=[("hi-IN", "qc"), ("ta-IN", "tts")]))
    summary = tally.summary

    assert summary.translation_fallbacks == 1
    assert summary.language_tasks_failed == 3


def test_a_retry_restores_the_row_once_every_language_is_resolved() -> None:
    tally = _tally()
    tally.row_finished(2, _outcome(ok=[], failed=[("hi-IN", "tts"), ("ta-IN", "tts")]))
    assert tally.summary.rows_failed == 1

    tally.retry_succeeded(2)
    mid = tally.summary
    assert mid.rows_failed == 1, "one language still outstanding — row is not rescued yet"
    assert mid.language_tasks_succeeded == 1
    assert mid.language_tasks_failed == 1

    tally.retry_succeeded(2)
    end = tally.summary
    assert end.rows_failed == 0
    assert end.rows_succeeded == 1
    assert end.language_tasks_succeeded == 2
    assert end.language_tasks_failed == 0


def test_a_partial_retry_leaves_the_row_failed() -> None:
    tally = _tally()
    tally.row_finished(2, _outcome(ok=["en-IN"], failed=[("hi-IN", "tts"), ("ta-IN", "qc")]))

    tally.retry_succeeded(2)
    summary = tally.summary

    assert summary.rows_failed == 1
    assert summary.rows_succeeded == 0
    assert summary.language_tasks_succeeded == 2
    assert summary.language_tasks_failed == 1


def test_retries_across_two_rows_are_tracked_separately() -> None:
    tally = _tally()
    tally.row_finished(2, _outcome(failed=[("hi-IN", "tts")]))
    tally.row_finished(3, _outcome(failed=[("hi-IN", "tts")]))
    assert tally.summary.rows_failed == 2

    tally.retry_succeeded(2)
    summary = tally.summary

    assert summary.rows_failed == 1
    assert summary.rows_succeeded == 1


def test_counters_never_go_negative() -> None:
    """Retry bookkeeping is the one place a counter decrements; an unexpected
    extra call must not drive it below zero."""
    tally = _tally()
    tally.row_finished(2, _outcome(ok=["hi-IN"]))

    tally.retry_succeeded(2)
    tally.retry_succeeded(2)
    summary = tally.summary

    assert summary.language_tasks_failed == 0
    assert summary.rows_failed == 0


def test_a_crashed_row_is_not_recoverable() -> None:
    """A row that blew up outside per-language handling cannot be rescued by a
    retry, so a stray retry must not resurrect it."""
    tally = _tally()
    tally.row_crashed(2)
    assert tally.summary.rows_failed == 1

    tally.retry_succeeded(2)
    summary = tally.summary

    assert summary.rows_failed == 1
    assert summary.rows_succeeded == 0
    assert summary.unexpected_row_errors == 1


def test_skipped_languages_accumulate() -> None:
    tally = _tally()

    tally.row_skipped_languages(3)
    tally.row_skipped_languages(2)

    assert tally.summary.language_tasks_skipped == 5


def test_collisions_accumulate_across_activities() -> None:
    """Each activity reports its own buffer's count and is then discarded, so
    the job total is the sum — assigning would keep only the last activity."""
    tally = _tally()

    tally.collisions_resolved(2)
    tally.collisions_resolved(3)

    assert tally.summary.filename_collisions_resolved == 5


def test_upload_counters() -> None:
    tally = _tally()

    tally.upload_succeeded()
    tally.upload_succeeded()
    tally.upload_failed()
    tally.upload_skipped()
    summary = tally.summary

    assert summary.uploads_succeeded == 2
    assert summary.uploads_failed == 1
    assert summary.uploads_skipped == 1


def test_archives_are_recorded_with_their_download() -> None:
    tally = _tally()
    archive = ArchiveDownload(
        activity_name="act", language="Hindi", filename="act-Hindi.zip",
        path="/tmp/act-Hindi.zip", url="/output/act-Hindi.zip", reason="s3_disabled",
    )

    tally.archive_written(archive)
    tally.archive_failed()
    summary = tally.summary

    assert summary.local_archives_succeeded == 1
    assert summary.local_archives_failed == 1
    assert summary.archive_downloads == [archive]


def test_the_first_upload_warning_wins() -> None:
    """The original explanation is the useful one; a later generic failure must
    not overwrite 'uploads were disabled'."""
    tally = _tally()

    tally.warn_about_uploads("uploads are disabled")
    tally.warn_about_uploads("an upload failed")

    assert tally.summary.upload_warning == "uploads are disabled"


def test_the_summary_is_a_snapshot() -> None:
    """Callers get a copy — reaching past the methods to mutate a counter is
    what this class exists to prevent."""
    tally = _tally()
    tally.row_finished(2, _outcome(ok=["hi-IN"]))

    snapshot = tally.summary
    snapshot.rows_succeeded = 999

    assert tally.summary.rows_succeeded == 1
