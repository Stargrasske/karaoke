"""Tests for turning a free-text vibe into a setlist."""
from __future__ import annotations

import pytest

from karaoke import mood_match


def test_descriptive_phrase_has_no_mood_confidence():
    """A genre description must not pull the mood plane around.

    `confidence` weights the plane term, so a phrase with no feeling in it has
    to score zero -- otherwise "electronic" would be dragged toward whatever
    the neutral centre happens to be instead of letting the sound search rank.
    """
    target = mood_match.resolve_target("electronic")
    assert target.confidence == 0.0
    assert target.matched == []
    assert (target.valence, target.arousal) == mood_match._NEUTRAL


def test_felt_phrase_targets_low_energy():
    target = mood_match.resolve_target("I feel wrecked")
    assert target.confidence > 0
    assert "wrecked" in target.matched
    assert target.arousal < 0.35
    assert target.valence < 0.35


def test_lyric_lexicon_covers_words_not_in_the_descriptive_list():
    """`heartbroken` is not in _DESCRIPTIVE; the shared lyric scorer catches it."""
    assert "heartbroken" not in mood_match._DESCRIPTIVE
    target = mood_match.resolve_target("heartbroken and lonely")
    assert "sad" in target.matched
    assert target.arousal < 0.4


def test_lift_flips_the_target_upward():
    down = mood_match.resolve_target("I feel wrecked")
    up = mood_match.resolve_target("I feel wrecked", lift=True)
    assert up.lifted is True
    assert up.arousal > down.arousal
    assert up.valence > down.valence


def test_lift_of_an_angry_phrase_does_not_land_on_sleepy():
    """A plain reflection would invert high arousal into low.

    Lifting someone out of anger means energy with better valence, not a
    lullaby, so arousal is floored rather than mirrored.
    """
    up = mood_match.resolve_target("angry", lift=True)
    assert up.arousal >= 0.55


def test_valence_rescales_the_compressed_brightness_axis():
    """Raw brightness clusters near 0.19 with sd 0.05 across the library.

    Compared directly against a target of 0.8 it adds a near-constant to every
    candidate. Rescaling has to spread the real range over most of 0..1.
    """
    mood_match._BRIGHTNESS_STATS = (0.19, 0.05)
    try:
        assert mood_match._valence(0.19) == pytest.approx(0.5, abs=0.02)
        assert mood_match._valence(0.09) < 0.15
        assert mood_match._valence(0.29) > 0.85
        # Never escapes 0..1, however far out the input sits.
        assert 0.0 <= mood_match._valence(0.0) <= 1.0
        assert 0.0 <= mood_match._valence(0.55) <= 1.0
    finally:
        mood_match._BRIGHTNESS_STATS = None


def test_valence_handles_missing_brightness():
    assert mood_match._valence(None) == 0.5


def test_plane_score_weights_arousal_over_valence():
    """Energy is measured and spreads widely; valence is inferred and barely
    moves, so a track wrong on energy must score worse than one wrong on
    valence by the same amount."""
    mood_match._BRIGHTNESS_STATS = (0.19, 0.05)
    try:
        target = mood_match.MoodTarget(valence=0.5, arousal=0.5, confidence=1.0, matched=[])
        # brightness 0.19 rescales to valence 0.5, so only energy differs here.
        wrong_arousal = mood_match._plane_score(0.9, 0.19, target)
        # energy on target, valence off by the same 0.4 (0.5 -> 0.9).
        wrong_valence = mood_match._plane_score(0.5, 0.19 + 0.4 * 4 * 0.05, target)
        assert wrong_arousal < wrong_valence
    finally:
        mood_match._BRIGHTNESS_STATS = None


def test_plane_score_is_neutral_for_unanalysed_tracks():
    target = mood_match.MoodTarget(valence=0.8, arousal=0.8, confidence=1.0, matched=[])
    assert mood_match._plane_score(None, None, target) == 0.5


def test_match_excludes_queued_tracks(monkeypatch):
    """Exclusion happens before scoring, so a queued song never reaches output."""
    class FakeHit:
        def __init__(self, tid):
            self.track_id = tid
            self.similarity = 0.5

    monkeypatch.setattr(
        "karaoke.search.sounds_like_text",
        lambda q, k=10, os_client=None: [FakeHit(1), FakeHit(2)],
    )
    # Every candidate is excluded, so the blended path has nothing to rank and
    # falls through to the plane-only query against the (empty) test library.
    target, rows = mood_match.match("upbeat", limit=5, exclude={1, 2})
    assert all(r["track_id"] not in {1, 2} for r in rows)
