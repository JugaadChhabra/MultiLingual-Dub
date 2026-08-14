"""Accumulating one activity's audio under non-colliding filenames.

An activity is a run of consecutive rows sharing an activity name; when the name
changes, whatever has accumulated is zipped per language and uploaded, and a
fresh buffer starts. Holding that state in a named object rather than in three
locals reset together is the point: the reset is one call, so it cannot be
half-done.

Filenames come from the sheet's audio_type column, which is not unique — two
rows in the same activity routinely ask for the same name. The buffer resolves
that by suffixing the row index, and reports having done so.
"""
from __future__ import annotations

import logging
from pathlib import Path

from batch.models import ExcelRow

logger = logging.getLogger(__name__)


def build_output_filename(*, audio_type: str, row_index: int, language: str) -> str:
    filename = audio_type or f"row-{row_index}-{language}"
    if not filename.lower().endswith(".mp3"):
        filename = f"{filename}.mp3"
    return filename


def dedupe_filename(
    filename: str,
    existing_files: dict[str, bytes],
    row_index: int,
) -> tuple[str, bool]:
    """Return a name not already taken, and whether it had to change."""
    if filename not in existing_files:
        return filename, False

    stem = Path(filename).stem or "audio"
    suffix = Path(filename).suffix or ".mp3"
    candidate = f"{stem}-row{row_index}{suffix}"
    counter = 2
    while candidate in existing_files:
        candidate = f"{stem}-row{row_index}-{counter}{suffix}"
        counter += 1
    return candidate, True


class ActivityBuffer:
    """The audio files gathered for the activity currently being processed."""

    def __init__(self, languages: list[str]) -> None:
        self._languages = list(languages)
        self.files: dict[str, dict[str, bytes]] = {lang: {} for lang in self._languages}
        self.collisions_resolved = 0

    def add(self, row: ExcelRow, language: str, audio: bytes, *, log_prefix: str = "") -> str:
        """File one language's audio for ``row``. Returns the name it landed under."""
        wanted = build_output_filename(
            audio_type=row.audio_type, row_index=row.row_index, language=language
        )
        existing = self.files.setdefault(language, {})
        final, collided = dedupe_filename(wanted, existing, row.row_index)
        if collided:
            self.collisions_resolved += 1
            logger.warning(
                "%slang %s: duplicate filename '%s' renamed to '%s'",
                log_prefix, language, wanted, final,
            )
        existing[final] = audio
        return final

    def expected_filename(self, row: ExcelRow, language: str) -> str:
        """The name a row's audio WOULD take, before any dedupe.

        Append mode compares this against what already exists in the remote zip
        to decide whether a language still needs generating.
        """
        return build_output_filename(
            audio_type=row.audio_type, row_index=row.row_index, language=language
        )

    @property
    def is_empty(self) -> bool:
        return not any(self.files.values())

    def total_files(self) -> int:
        return sum(len(files) for files in self.files.values())
