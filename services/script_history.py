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

from services.script_parse import parse_script
from services.script_validate import HISTORY_DAYS, PROSE_DAYS, HistoryFacts
from services.script_writer import DraftScript
from services.state_mirror import JsonStateMirror

logger = logging.getLogger(__name__)

# Ten days for the facts — colours, numbers, area combinations — because those
# are the rules stated over ten days, and a fact costs a few dozen characters.
DEFAULT_HISTORY_DAYS = HISTORY_DAYS

# Three days for the prose, quoted as excerpts rather than in full. Excerpts are
# what dominate a prompt, and phrasing drifts fast enough that the last three
# days carry nearly all the signal. This window is why the facts above are
# stored separately: an excerpt truncates before the colour and number line,
# which sits at the end of a script, so prose alone told the model to avoid
# colours it could not see.
DEFAULT_PROSE_DAYS = PROSE_DAYS
EXCERPT_CHARS = 300


class DraftScriptState(BaseModel):
    """One stored script, and the facts pulled out of it.

    The parsed fields all default, so records written before they existed load
    unchanged and simply contribute nothing — the window heals as new days are
    written, and no migration is needed.
    """

    title: str
    script: str
    item_key: str = ""
    areas: list[str] = Field(default_factory=list)
    colour: str | None = None
    number: int | None = None
    tags: list[str] = Field(default_factory=list)


class DayScriptsState(BaseModel):
    key: str
    publish_date: str
    category: str
    language: str
    items: list[DraftScriptState] = Field(default_factory=list)
    written_at: datetime | None = None


def _to_state(draft: DraftScript) -> DraftScriptState:
    facts = parse_script(draft.script)
    return DraftScriptState(
        title=draft.title,
        script=draft.script,
        item_key=draft.key,
        areas=list(facts.areas),
        colour=facts.colour,
        number=facts.number,
        tags=list(facts.tags),
    )


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
        """Store a day's drafts, with their facts parsed out.

        Parsing happens here rather than at read time so a change to the
        skeleton cannot retroactively reinterpret what was already written, and
        so the read path stays cheap. It is best-effort: a script that does not
        match the skeleton stores empty facts rather than failing the write.
        """
        self._mirror.write(
            DayScriptsState(
                key=_key(publish_date=publish_date, category=category, language=language),
                publish_date=publish_date,
                category=category,
                language=language,
                items=[_to_state(draft) for draft in drafts],
                written_at=datetime.now(timezone.utc),
            )
        )

    def _window(
        self, *, category: str, language: str, before: str | None, days: int
    ) -> list[DayScriptsState]:
        """The most recent ``days`` days for this category and language.

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
        return records[:days]

    def recent(
        self,
        *,
        category: str,
        language: str,
        before: str | None = None,
        days: int = DEFAULT_PROSE_DAYS,
        excerpt_chars: int = EXCERPT_CHARS,
    ) -> list[DraftScript]:
        """The last ``days`` days of drafts as prose excerpts, for phrasing.

        Deliberately a short window. The colour and number rules are enforced
        from :meth:`facts` instead, which is not truncated and reaches back
        further; this is only what stops the openings sounding the same.
        """
        out: list[DraftScript] = []
        for state in self._window(
            category=category, language=language, before=before, days=days
        ):
            for item in state.items:
                script = item.script.strip()
                if len(script) > excerpt_chars:
                    script = script[:excerpt_chars].rstrip() + "…"
                out.append(DraftScript(
                    title=f"{state.publish_date} {item.title}",
                    script=script,
                    key=item.item_key,
                ))
        return out

    def facts(
        self,
        *,
        category: str,
        language: str,
        before: str | None = None,
        days: int = DEFAULT_HISTORY_DAYS,
    ) -> dict[str, HistoryFacts]:
        """What each item has already used, over the last ``days`` days.

        Grouped by item key, because the rules that matter are per sign: a
        colour repeating for मेष is what a viewer of मेष notices, and the same
        colour on कन्या the same week is fine.

        Records written before facts were stored have no ``item_key`` and are
        skipped rather than guessed at — a wrong grouping would forbid colours
        the wrong sign never used.
        """
        colours: dict[str, list[str]] = {}
        numbers: dict[str, list[int]] = {}
        combinations: dict[str, list[frozenset[str]]] = {}
        previous: dict[str, tuple[str, ...]] = {}

        # Newest first, so the first tag list seen for an item is the one from
        # its most recent day — which is the only one the repeat rule cares about.
        for state in self._window(
            category=category, language=language, before=before, days=days
        ):
            for item in state.items:
                if not item.item_key:
                    continue
                if item.colour:
                    colours.setdefault(item.item_key, []).append(item.colour)
                if item.number is not None:
                    numbers.setdefault(item.item_key, []).append(item.number)
                if item.areas:
                    combinations.setdefault(item.item_key, []).append(frozenset(item.areas))
                if item.tags and item.item_key not in previous:
                    previous[item.item_key] = tuple(item.tags)

        keys = set(colours) | set(numbers) | set(combinations) | set(previous)
        return {
            key: HistoryFacts(
                colours=tuple(colours.get(key, ())),
                numbers=tuple(numbers.get(key, ())),
                combinations=tuple(combinations.get(key, ())),
                previous_tags=previous.get(key, ()),
            )
            for key in keys
        }
