"""Near-duplicate detection for scripts, without a model or a dependency.

Word-shingle Jaccard: two texts that share most of their overlapping n-word
windows are saying the same thing in the same words, even when a colour or a
name differs between them. Deterministic and cheap, unlike an embedding call —
which is the point. The uniqueness the validator already enforces is
structural (a colour, a number, a verbatim sentence); this catches the sameness
that slips past it, where two scripts are reworded copies of one another. It is
meant to feed a soft flag an operator reads, not a hard gate, so a false match
on shared boilerplate costs a glance, not a rewrite.

Three-word windows are specific enough that genuinely different sentences rarely
share one, so distinct scripts score near zero while reworded ones score high —
the gap is what makes a single threshold usable.
"""
from __future__ import annotations

# The window width. Two words is common phrasing ("आज का", "दिन अच्छा"); four is
# so specific that a reworded copy with one word changed stops matching. Three
# is the width where a shared window means a shared thought, not shared grammar.
DEFAULT_SHINGLE_N = 3


def word_shingles(text: str, n: int = DEFAULT_SHINGLE_N) -> set[tuple[str, ...]]:
    """The set of n-word windows in ``text``, whitespace-tokenised.

    A text shorter than one window yields the single window of all its words,
    so a two-word phrase can still match another copy of that same phrase rather
    than silently comparing as empty.
    """
    words = text.split()
    if not words:
        return set()
    if len(words) < n:
        return {tuple(words)}
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def jaccard(left: set, right: set) -> float:
    """Overlap of two sets: shared members over total distinct members.

    Zero when either is empty — nothing can be a near-duplicate of nothing, and
    treating two empty texts as identical would flag every unparseable pair.
    """
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def similarity(left: str, right: str, *, n: int = DEFAULT_SHINGLE_N) -> float:
    """How much two texts reword the same content, in [0, 1]."""
    return jaccard(word_shingles(left, n), word_shingles(right, n))
