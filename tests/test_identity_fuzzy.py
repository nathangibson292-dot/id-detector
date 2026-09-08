"""Fuzzy hint↔audio corroboration: a crowd ID with a one-letter title typo still merges.

Guards the CLUBGRLS/CLUBGIRLS case (same track listed twice, artist/title flipped, one letter off)
without opening the door to merging genuinely different tracks.
"""

from __future__ import annotations

from id_detector.fuse.identity import _within_one_edit, _word_sets_corroborate


def fs(*words: str) -> frozenset[str]:
    return frozenset(words)


def test_within_one_edit() -> None:
    assert _within_one_edit("clubgrls", "clubgrls")  # equal
    assert _within_one_edit("clubgrls", "clubgirls")  # one insertion (missing 'i')
    assert _within_one_edit("colour", "color")  # one deletion
    assert _within_one_edit("grey", "gray")  # one substitution
    assert not _within_one_edit("track", "trench")  # >1 edit
    assert not _within_one_edit("abc", "abcde")  # length gap > 1


def test_crowd_id_with_one_letter_typo_corroborates_the_audio_match() -> None:
    # The real bug: audio "Effy - CLUBGRLS" and comment "CLUBGIRLS - Effy" (flipped + 1 letter).
    assert _word_sets_corroborate(fs("effy", "clubgrls"), fs("clubgirls", "effy"))
    # Order independence and extra collaborators still merge (the exact subset rule).
    assert _word_sets_corroborate(
        fs("breaka", "breaka", "bushbaby"), fs("bushbaby", "eloq", "breaka")
    )


def test_it_does_not_merge_genuinely_different_tracks() -> None:
    # Two different Effy tracks: shared artist but titles are not one edit apart -> no merge.
    assert not _word_sets_corroborate(fs("effy", "pitched"), fs("effy", "clubgrls"))
    # No shared token at all -> no merge even if one title token is close.
    assert not _word_sets_corroborate(fs("skream", "devon"), fs("effy", "devan"))
    # Short differing tokens (< 4 chars) are too risky to fuzzy-merge (e.g. "id"/"up").
    assert not _word_sets_corroborate(fs("effy", "up"), fs("effy", "us"))
    # A single word each is never enough signal.
    assert not _word_sets_corroborate(fs("clubgrls"), fs("clubgirls"))
