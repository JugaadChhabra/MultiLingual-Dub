"""Tests for word-shingle near-duplicate detection.

The behaviours worth pinning: reworded copies score high, genuinely different
sentences score low, and the degenerate inputs (empty, shorter than a window)
never raise and never report a false match.
"""
from __future__ import annotations

from services.text_similarity import jaccard, similarity, word_shingles


def test_shingles_are_the_sliding_windows() -> None:
    assert word_shingles("a b c d", n=3) == {("a", "b", "c"), ("b", "c", "d")}


def test_a_text_shorter_than_a_window_is_one_window() -> None:
    assert word_shingles("a b", n=3) == {("a", "b")}
    assert word_shingles("", n=3) == set()


def test_identical_text_scores_one() -> None:
    text = "आज पैसा आएगा और रिश्ते मजबूत होंगे"
    assert similarity(text, text) == 1.0


def test_a_reworded_copy_scores_high() -> None:
    # A whole script reworded for another sign: same sentences, a colour and a
    # couple of words changed. This is the sameness a daily channel accretes,
    # and at real script length it stays well clear of the flag threshold.
    a = ("आज तुम्हें अचानक पैसा मिलेगा और मन बहुत खुश रहेगा। "
         "घर में रौनक रहेगी और कोई अपना तुम्हारा साथ देगा। "
         "शाम तक एक अच्छी खबर आएगी जो दिल को सुकून देगी।")
    b = ("आज तुम्हें अचानक धन मिलेगा और मन बहुत खुश रहेगा। "
         "घर में रौनक रहेगी और कोई अपना तुम्हारा साथ देगा। "
         "शाम तक एक अच्छी खबर आएगी जो दिल को खुशी देगी।")
    assert similarity(a, b) >= 0.5


def test_genuinely_different_sentences_score_low() -> None:
    a = ("सुबह एक पुराना दोस्त तुम्हें फोन करेगा और पुरानी यादें ताज़ा होंगी। "
         "काम में एक रुकी हुई बात आगे बढ़ेगी।")
    b = ("शाम की रौशनी में घर पर बैठकर तुम्हें गहरा सुकून मिलेगा। "
         "बचत को लेकर एक समझदारी भरा फैसला होगा।")
    assert similarity(a, b) < 0.2


def test_empty_never_matches() -> None:
    assert similarity("", "") == 0.0
    assert similarity("", "कुछ शब्द यहाँ हैं") == 0.0
    assert jaccard(set(), {("a", "b", "c")}) == 0.0
