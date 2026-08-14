"""The running count of what a batch job has done.

Every counter on JobSummary is written here and nowhere else. That is the whole
point: the numbers used to be incremented from the main loop, the retry pass and
the upload module, and the retry pass had to *decrement* what the main loop had
already counted. Two writers disagreeing about whether a row had failed is
exactly how rows_failed and rows_succeeded could both be wrong at once.

Deliberately synchronous and free of I/O. The caller decides when to persist —
after each row and after each activity upload — which is the granularity the UI
actually renders.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from batch.models import ArchiveDownload, JobSummary
from batch.voiceover import RowOutcome

logger = logging.getLogger(__name__)


class BatchTally:
    def __init__(self) -> None:
        self._summary = JobSummary()

        # Per row, how many of its language tasks are still unresolved. A row is
        # only restored to 'succeeded' once retry has cleared every one of them.
        self._unresolved_by_row: Counter[int] = Counter()
        # Rows counted as failed that a later retry could still rescue.
        self._recoverable_rows: set[int] = set()

    def started(self, at: datetime) -> None:
        self._summary.started_at = at

    def set_totals(self, *, rows: int, languages: int) -> None:
        """Known only after the sheet is read, which is after the job starts."""
        self._summary.total_rows = rows
        self._summary.language_tasks_total = rows * languages

    @property
    def summary(self) -> JobSummary:
        """A snapshot for persisting. Callers must not mutate it — every counter
        has a method here, and reaching past them is what this class exists to
        stop."""
        return self._summary.model_copy(deep=True)

    # --- row lifecycle ----------------------------------------------------

    def row_finished(self, row_index: int, outcome: RowOutcome) -> None:
        """Record a row that ran to completion, however its languages fared."""
        self._summary.rows_processed += 1
        self._summary.language_tasks_succeeded += len(outcome.audio)

        for failure in outcome.failures:
            self._summary.language_tasks_failed += 1
            if failure.stage == "translation":
                self._summary.translation_fallbacks += 1
            self._unresolved_by_row[row_index] += 1

        if outcome.failures:
            self._summary.rows_failed += 1
            # Every failure so far is a per-language one, so a retry can still
            # turn this row around.
            self._recoverable_rows.add(row_index)
        else:
            self._summary.rows_succeeded += 1

    def row_crashed(self, row_index: int) -> None:
        """Record a row that blew up outside the per-language handling. Not
        recoverable: we do not know which languages, if any, are salvageable."""
        self._summary.rows_processed += 1
        self._summary.rows_failed += 1
        self._summary.unexpected_row_errors += 1

    def row_skipped_languages(self, count: int) -> None:
        """Append mode: languages already present in the remote zip."""
        self._summary.language_tasks_skipped += count

    # --- retry ------------------------------------------------------------

    def retry_succeeded(self, row_index: int) -> None:
        """One previously-failed language task now has audio.

        If it was the row's last outstanding failure, the row moves back from
        failed to succeeded — the one place a counter goes down, and the reason
        this bookkeeping is worth having in a single object.
        """
        self._summary.language_tasks_succeeded += 1
        self._summary.language_tasks_failed = max(0, self._summary.language_tasks_failed - 1)

        self._unresolved_by_row[row_index] = max(0, self._unresolved_by_row[row_index] - 1)
        if self._unresolved_by_row[row_index] == 0 and row_index in self._recoverable_rows:
            self._recoverable_rows.discard(row_index)
            self._summary.rows_failed = max(0, self._summary.rows_failed - 1)
            self._summary.rows_succeeded += 1

    def collisions_resolved(self, count: int) -> None:
        """Add one activity's collision count to the job total.

        Accumulates rather than sets: the buffer reporting this is discarded at
        each activity boundary, so assigning would leave only the last
        activity's tally.
        """
        self._summary.filename_collisions_resolved += count

    # --- uploads ----------------------------------------------------------

    def upload_succeeded(self) -> None:
        self._summary.uploads_succeeded += 1

    def upload_failed(self) -> None:
        self._summary.uploads_failed += 1

    def upload_skipped(self) -> None:
        self._summary.uploads_skipped += 1

    def archive_written(self, archive: ArchiveDownload) -> None:
        self._summary.local_archives_succeeded += 1
        self._summary.archive_downloads.append(archive)

    def archive_failed(self) -> None:
        self._summary.local_archives_failed += 1

    def warn_about_uploads(self, message: str) -> None:
        """First warning wins — later upload problems don't overwrite the
        original explanation."""
        if self._summary.upload_warning is None:
            self._summary.upload_warning = message
