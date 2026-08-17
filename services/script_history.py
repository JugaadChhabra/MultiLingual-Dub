"""What the script writer has already said, so it stops saying it again.

A model asked for a horoscope twelve times a day, every day, converges on its
own favourite phrasings — the same openings, the same imagery, the same
predictions. The only cheap defence is to show it what it wrote recently and
forbid it. That requires keeping the drafts, which is what this is.

Written at generation time, not at render time: the point is what the model has
already produced, whether or not the operator went on to render it.

Unlike the job stores this is not a mirror of live state — the process holds
nothing in memory between requests, so every read comes off disk. It is also
never pruned, in line with everything else under ``output/``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from services.script_writer import DraftScript
from services.state_mirror import JsonStateMirror

logger = logging.getLogger(__name__)

# Seven days of scripts is enough to catch the sameness a viewer would notice,
# and each is quoted as an excerpt rather than in full: an opening and its first
# turn are what repeat, and the whole set at full length would dominate the
# prompt.
DEFAULT_HISTORY_DAYS = 7
EXCERPT_CHARS = 300


class DraftScriptState(BaseModel):
    title: str
    script: str


class DayScriptsState(BaseModel):
    key: str
    publish_date: str
    category: str
    language: str
    items: list[DraftScriptState] = Field(default_factory=list)
    written_at: datetime | None = None


def _key(*, publish_date: str, category: str, language: str) -> str:
    """One record per day per category per language.

    Regenerating the same day overwrites rather than accumulating, so a run the
    operator threw away and redid does not later read as two days of history.
    """
    safe = lambda part: str(part).strip().replace("/", "_").replace(" ", "_") or "unknown"
    return f"{safe(publish_date)}__{safe(category)}__{safe(language)}"


class ScriptHistoryStore:
    """Generated scripts on disk, one JSON file per day / category / language."""

    def __init__(self, persist_dir: Path | str) -> None:
        self._mirror = JsonStateMirror(persist_dir, DayScriptsState, lambda s: s.key)

    def record(
        self, *, publish_date: str, category: str, language: str, drafts: list[DraftScript]
    ) -> None:
        self._mirror.write(
            DayScriptsState(
                key=_key(publish_date=publish_date, category=category, language=language),
                publish_date=publish_date,
                category=category,
                language=language,
                items=[DraftScriptState(title=d.title, script=d.script) for d in drafts],
                written_at=datetime.now(timezone.utc),
            )
        )

    def recent(
        self,
        *,
        category: str,
        language: str,
        before: str | None = None,
        days: int = DEFAULT_HISTORY_DAYS,
        excerpt_chars: int = EXCERPT_CHARS,
    ) -> list[DraftScript]:
        """The last ``days`` days of drafts for this category and language.

        ``before`` excludes the day being generated, so regenerating a day is not
        told to avoid the draft it is replacing — which would push each attempt
        further from the brief rather than closer to it.
        """
        records = [
            state for state in self._mirror.load().values()
            if state.category == category and state.language == language
            and (before is None or state.publish_date < before)
        ]
        records.sort(key=lambda s: s.publish_date, reverse=True)

        out: list[DraftScript] = []
        for state in records[:days]:
            for item in state.items:
                script = item.script.strip()
                if len(script) > excerpt_chars:
                    script = script[:excerpt_chars].rstrip() + "…"
                out.append(DraftScript(title=f"{state.publish_date} {item.title}", script=script))
        return out
