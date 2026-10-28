"""Mirroring job state to disk so a restart doesn't erase what happened.

Extracted from the video job store, which needed it first and for the sharpest
reason: a HeyGen render is paid for at submission and keeps running server-side,
so losing the local record of its id strands a finished video nobody can fetch.

Deliberately only the plumbing: serialization, and load-and-settle (the
**Interrupted** rule). The three job stores share this exactly — same atomic
write, same load-everything-on-boot, same "in flight at shutdown means failed" —
while their create/start/complete/fail semantics, and what settling a job
actually changes, differ enough that sharing those would mean a base class full
of overrides. This is composed into a store, not inherited by one.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Generic, Iterable, Protocol, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

TState = TypeVar("TState", bound=BaseModel)


class _TimedSummary(Protocol):
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None


def finalize_summary(summary: _TimedSummary) -> None:
    """Stamp a job summary as finished and derive its duration.

    Shared by the audio and video job summaries, which carry the same three
    timing fields; one copy so a drift can't leave a job looking unfinished.
    """
    summary.finished_at = datetime.now(timezone.utc)
    if summary.started_at:
        delta = summary.finished_at - summary.started_at
        summary.duration_ms = int(delta.total_seconds() * 1000)


class JsonStateMirror(Generic[TState]):
    """One JSON file per job, rewritten on every mutation.

    Best-effort by design: a persistence failure is logged and swallowed. Losing
    the mirror is bad; failing a running job because we could not write a status
    file would be worse.
    """

    def __init__(
        self,
        directory: Path | str,
        model: type[TState],
        key: Callable[[TState], str],
    ) -> None:
        self.directory = Path(directory)
        self._model = model
        self._key = key
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.json"

    def write(self, state: TState) -> None:
        """Atomically replace this job's file.

        Written to a temp file and renamed so a crash mid-write leaves the
        previous good state rather than a truncated one.
        """
        try:
            path = self._path(self._key(state))
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(state.model_dump_json())
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("Failed to persist state for %s: %s", self._key(state), exc)

    def load(self) -> dict[str, TState]:
        """Read every persisted job. An unreadable file is skipped, not fatal —
        one corrupt record must not stop the process from starting."""
        states: dict[str, TState] = {}
        for path in sorted(self.directory.glob("*.json")):
            try:
                state = self._model.model_validate_json(path.read_text())
            except Exception as exc:
                logger.warning("Skipping unreadable persisted job %s: %s", path.name, exc)
                continue
            states[self._key(state)] = state
        if states:
            logger.info("Loaded %d persisted job(s) from %s", len(states), self.directory)
        return states

    def load_settled(
        self,
        in_flight: Iterable[str],
        settle: Callable[[TState], None],
    ) -> dict[str, TState]:
        """Load every job, settling those the restart interrupted.

        A state whose ``status`` is in ``in_flight`` has no task behind it any
        more. ``settle`` mutates it into its terminal form — what "failed" means
        differs per store — and the result is written back so the settlement
        survives the next restart too.
        """
        in_flight = tuple(in_flight)
        states = self.load()
        for state in states.values():
            if state.status in in_flight:  # type: ignore[attr-defined]
                settle(state)
                self.write(state)
        return states
