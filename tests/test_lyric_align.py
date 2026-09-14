"""Tests for laying real lyrics onto Whisper's rhythm (offline)."""
import pytest

from karaoke import lyric_align as la
from karaoke.whisper_sync import Word


def _words(pairs):
    return [Word(start=t, end=t + 0.4, text=w) for t, w in pairs]


# Whisper's actual output for Springsteen's "The River": right rhythm, wrong
# words ("up to do" -> "up to doom", "daddy done" -> "daddy John"), plus the
# music-note artifacts it emits over instrumental passages.
RIVER_WORDS = _words([
    (15.86, "🎵I"), (16.4, "come"), (16.9, "from"), (17.3, "down"), (17.8, "in"),
    (18.1, "the"), (18.6, "valley"), (19.5, "🎵"), (20.1, "🎵Where"),
    (21.24, "mister,"), (21.9, "when"), (22.3, "you're"), (22.9, "gone"),
    (24.1, "🎵They"), (24.6, "bring"), (25.1, "you"), (26.32, "up"), (26.8, "to"),
    (27.2, "doom"), (28.6, "Like"), (29.1, "your"), (29.6, "daddy"), (30.2, "John"),
    (32.76, "Me"), (33.2, "and"), (33.6, "Mary"), (34.1, "we"), (34.5, "met"),
    (34.9, "in"), (35.3, "high"), (35.9, "school"), (38.06, "was"), (38.5, "just"),
    (39.1, "17"),
])

RIVER_LYRICS = """I come from down in the valley
Where, mister, when you're young
They bring you up to do
Like your daddy done

Me and Mary, we met in high school
When she was just 17"""


# --- normalization ---------------------------------------------------------

def test_normalize_strips_artifacts_and_punctuation():
    assert la.normalize_word("🎵They") == "they"
    assert la.normalize_word("mister,") == "mister"
    assert la.normalize_word("🎵") == ""
    assert la.normalize_word("// Music //") == ""


def test_normalize_folds_contractions_and_numbers():
    assert la.normalize_word("we'd") == la.normalize_word("wed")
    assert la.normalize_word("17") == la.normalize_word("seventeen")


# --- alignment -------------------------------------------------------------

def test_aligned_text_is_the_real_lyrics_not_whisper_words():
    """The whole point: Whisper gives timing, the source gives words."""
    out = la.align_lines(RIVER_LYRICS.strip().splitlines(), RIVER_WORDS)
    text = [line for _, line in out]
    assert "They bring you up to do" in text        # not "up to doom"
    assert "Like your daddy done" in text           # not "daddy John"
    assert not any("🎵" in line for line in text)


def test_lines_anchor_near_where_whisper_heard_them():
    lrc = la.align_lyrics_to_lrc(RIVER_LYRICS, RIVER_WORDS)
    lines = lrc.splitlines()
    assert lines[0].startswith("[00:15.")          # "I come from down in the valley"
    assert lines[4].startswith("[00:32.")          # "Me and Mary, we met in high school"


def test_blank_lines_do_not_consume_a_timestamp():
    """Stanza separators have nothing to sing."""
    lrc = la.align_lyrics_to_lrc(RIVER_LYRICS, RIVER_WORDS)
    assert len(lrc.splitlines()) == 6              # 6 real lines, blank dropped


def test_timestamps_never_go_backwards():
    out = la.align_lines(RIVER_LYRICS.strip().splitlines(), RIVER_WORDS)
    times = [t for t, _ in out]
    assert times == sorted(times)


def test_line_whisper_never_heard_is_interpolated_between_neighbours():
    """A dropped line still needs a sensible time, not 0.0."""
    lyrics = ["first line here", "totally unheard interlude", "final line here"]
    words = _words([(10.0, "first"), (10.5, "line"), (11.0, "here"),
                    (30.0, "final"), (30.5, "line"), (31.0, "here")])
    out = la.align_lines(lyrics, words)
    assert out[0][0] == 10.0
    assert out[2][0] == 30.0
    assert 10.0 < out[1][0] < 30.0


# --- degenerate inputs -----------------------------------------------------

def test_no_whisper_words_still_returns_every_line():
    lyrics = ["one", "two", "three", "four"]
    out = la.align_lines(lyrics, [], total_duration=120.0)
    assert [line for _, line in out] == lyrics
    assert [t for t, _ in out] == sorted(t for t, _ in out)


def test_no_lyrics_returns_empty():
    assert la.align_lines([], RIVER_WORDS) == []
    assert la.align_lyrics_to_lrc("", RIVER_WORDS) == ""


def test_whisper_heard_only_artifacts():
    """An instrumental transcribed as pure "🎵" anchors nothing."""
    out = la.align_lines(["a line", "another line"],
                         _words([(1.0, "🎵"), (2.0, "🎵")]), total_duration=60.0)
    assert len(out) == 2


def test_whisper_hearing_extra_words_does_not_shift_lines():
    """Invented phrases are absorbed as edits, not treated as anchors."""
    lyrics = ["hello darkness my old friend", "ive come to talk with you again"]
    words = _words([
        (5.0, "hello"), (5.4, "darkness"), (5.9, "my"), (6.2, "old"), (6.6, "friend"),
        (7.0, "yeah"), (7.3, "uh"), (7.6, "hmm"),
        (9.0, "ive"), (9.3, "come"), (9.7, "to"), (10.0, "talk"), (10.4, "with"),
        (10.8, "you"), (11.2, "again"),
    ])
    out = la.align_lines(lyrics, words)
    assert out[0][0] == 5.0
    assert out[1][0] == 9.0


# --- word length and singing rate ------------------------------------------
#
# Timings were derived from word *starts* alone, and untimed lines were spaced
# evenly. Both ignore how long a line takes to sing, which is what made an
# otherwise well-anchored alignment feel off the beat.

class _W:
    def __init__(self, text, start, end=None):
        self.text, self.start = text, start
        self.end = start if end is None else end


def test_a_plausible_word_span_is_accepted():
    assert la.word_span_ok(1.0, 1.4)


def test_an_instantaneous_word_is_rejected():
    """A zero-length word is a decoding glitch, not a syllable."""
    assert not la.word_span_ok(1.0, 1.001)


def test_a_word_spanning_an_instrumental_is_rejected():
    """Whisper attaches one token to a whole break; anchoring on it puts the
    line seconds away from the singing."""
    assert not la.word_span_ok(10.0, 40.0)


def test_implausible_words_are_dropped_from_the_stream():
    toks, starts, last = la._whisper_tokens(
        [_W("real", 1.0, 1.4), _W("stretched", 2.0, 30.0), _W("also", 31.0, 31.3)])
    assert toks == ["real", "also"]
    assert last == pytest.approx(31.3)


def test_a_multi_word_token_is_spread_across_its_span():
    """Stacking them all on the start loses the rhythm inside the token."""
    toks, starts, _ = la._whisper_tokens([_W("two words", 10.0, 11.0)])
    assert toks == ["two", "words"]
    assert starts[0] == pytest.approx(10.0)
    assert starts[1] == pytest.approx(10.5)


def test_a_longer_line_weighs_more():
    short = la.line_weight("what's it for")
    long = la.line_weight(
        "no, don't fake me don't ya know no one's ready for your war")
    assert long > short * 2


def test_even_a_one_word_line_has_weight():
    assert la.line_weight("hey") >= 1.0
    assert la.line_weight("") == 0.0


# -- weighted interpolation ---------------------------------------------

def test_untimed_lines_are_spaced_by_length_not_evenly():
    """A long line and a two-word line should not take the same time."""
    times = [0.0, None, None, 12.0]
    weights = [1.0, 10.0, 2.0, 1.0]
    out = la._interpolate(times, None, weights)
    first_gap = out[1] - out[0]
    second_gap = out[2] - out[1]
    assert second_gap > first_gap * 3     # the long line eats the gap


def test_interpolation_stays_non_decreasing():
    out = la._interpolate([5.0, None, None, 1.0], None, [1.0] * 4)
    assert out == sorted(out)


# -- rejecting impossible anchors ---------------------------------------

def test_an_anchor_needing_impossible_speed_is_dropped():
    """Repeated lyrics let the matcher anchor a late line to an early repeat.
    On a real track that pinned twenty lines into four seconds."""
    weights = [10.0] * 5
    per_line = [0.0, None, None, None, 1.0]      # 4 long lines in 1 second
    kept = la._drop_impossible_anchors(per_line, weights)
    assert kept[4] is None                        # the suspect anchor is gone
    assert kept[0] == 0.0                         # the first is kept


def test_a_comfortable_anchor_is_kept():
    weights = [10.0] * 5
    per_line = [0.0, None, None, None, 40.0]
    kept = la._drop_impossible_anchors(per_line, weights)
    assert kept[4] == 40.0


def test_dropping_anchors_leaves_the_first_one_alone():
    kept = la._drop_impossible_anchors([None, 5.0, 5.1], [10.0] * 3)
    assert kept[1] == 5.0
    assert kept[2] is None


def test_the_result_is_still_one_time_per_line():
    lines = ["a longer line here", "short", "another longish line", "end"]
    words = [_W("longer", 1.0, 1.4), _W("end", 20.0, 20.4)]
    out = la.align_lines(lines, words, total_duration=25.0)
    assert len(out) == len(lines)
    assert [t for t, _ in out] == sorted(t for t, _ in out)


# --- tempo-aware placement -------------------------------------------------
#
# "3 words in 4 bars" is a break, not slow singing; and a beat grid is what
# "too early" is measured against. Both need the tempo the analysis already
# stores.

def test_beat_and_bar_come_from_the_tempo():
    assert la.beat_seconds(120.0) == pytest.approx(0.5)
    assert la.bar_seconds(120.0) == pytest.approx(2.0)


def test_an_absurd_tempo_is_ignored():
    for bpm in (0, None, 5.0, 900.0, "fast"):
        assert la.beat_seconds(bpm) is None


def test_only_the_word_ceiling_scales_with_tempo():
    """A tempo-scaled *floor* threw away real words: a sixteenth note at 76bpm
    is 0.197s, but "ya" measured 0.12s on that very track, and dropping it
    dragged the opening line eight seconds late."""
    slow_lo, slow_hi = la.word_bounds(76.0)
    fast_lo, fast_hi = la.word_bounds(152.0)
    assert slow_lo == fast_lo == la.MIN_WORD_S
    assert slow_hi > fast_hi


def test_a_short_sung_word_survives_a_slow_tempo():
    assert la.word_span_ok(16.36, 16.48, 76.0)      # "ya", 0.12s


def test_a_word_held_across_two_bars_is_rejected():
    assert not la.word_span_ok(0.0, 10.0, 76.0)


# -- breaks -------------------------------------------------------------

def test_a_quiet_stretch_of_bars_is_a_break():
    bar = la.bar_seconds(120.0)                     # 2.0s
    quiet = 2.0 + bar * la.BREAK_BARS + 1           # one gap past the window
    starts = [0.0, 1.0, 2.0, quiet, quiet + 1.0]
    breaks = la.find_breaks(starts, 120.0)
    assert len(breaks) == 1
    assert breaks[0] == (2.0, quiet)


def test_ordinary_spacing_is_not_a_break():
    assert la.find_breaks([0.0, 1.0, 2.0, 3.0], 120.0) == []


def test_no_tempo_means_no_break_detection():
    """Bars are the unit; without a tempo there is nothing to count."""
    assert la.find_breaks([0.0, 100.0], None) == []


def test_lines_are_not_spread_into_a_break():
    """Distributing evenly across a solo puts lyrics where there is no vocal."""
    breaks = [(10.0, 40.0)]
    times = [0.0, None, 50.0]
    out = la._interpolate(times, None, [1.0, 1.0, 1.0], breaks)
    assert out[1] <= 10.0 or out[1] >= 40.0


def test_sung_span_excludes_a_break():
    assert la._sung_span(0.0, 50.0, [(10.0, 40.0)]) == pytest.approx(20.0)


def test_advancing_skips_over_a_break():
    """Ten seconds of singing from zero, with a solo at 5-35, lands at 40."""
    assert la._advance_through_breaks(0.0, 10.0, [(5.0, 35.0)]) == pytest.approx(40.0)


# -- the beat grid ------------------------------------------------------

def test_the_grid_phase_is_inferred_from_the_words():
    """A tempo says how far apart beats are, not where they fall."""
    beat = 0.5
    starts = [0.2, 0.7, 1.2, 1.7]                   # all 0.2 past a beat
    assert la.grid_phase(starts, beat) == pytest.approx(0.2)


def test_a_time_near_the_grid_is_snapped():
    assert la.snap_to_grid(1.02, 0.5, 0.0) == pytest.approx(1.0)


def test_a_time_far_off_the_grid_is_left_alone():
    """A deliberate off-beat entry is not jitter to correct."""
    assert la.snap_to_grid(1.24, 0.5, 0.0) == pytest.approx(1.24)


def test_snapping_without_a_tempo_changes_nothing():
    assert la.snap_to_grid(1.23, None, 0.0) == pytest.approx(1.23)


# -- the opening line ---------------------------------------------------

def test_an_opening_line_matched_to_a_later_repeat_is_pulled_back():
    """Every other line is constrained by its neighbours; the first has
    nothing before it, which is why it drifts. Measured 21.16s on a track
    whose vocal starts at 14.0s."""
    lines = ["dont fake me", "what's it for"]
    toks = ["dont", "fake", "me", "dont", "fake", "me"]
    starts = [14.0, 14.5, 15.0, 21.0, 21.5, 22.0]
    out = la._correct_opening_anchor([21.0, None], lines, toks, starts, 120.0)
    assert out[0] == pytest.approx(14.0)


def test_an_opening_anchor_that_agrees_is_left_alone():
    lines = ["dont fake me"]
    toks = ["dont", "fake", "me"]
    starts = [14.0, 14.5, 15.0]
    out = la._correct_opening_anchor([14.2], lines, toks, starts, 120.0)
    assert out[0] == pytest.approx(14.2)


def test_the_opening_anchor_is_never_moved_later():
    lines = ["dont fake me"]
    out = la._correct_opening_anchor([5.0], lines, ["dont"], [30.0], 120.0)
    assert out[0] == pytest.approx(5.0)


# --- confidence, corroborated by the real lyrics ---------------------------
#
# faster-whisper reports a probability per word and this wrapper discarded it.
# But a plain floor is actively harmful: measured on a track whose vocal onset
# is known, the hallucination "neighal" scored 0.001 while "no,", "don't" and
# "ya" -- all genuinely sung -- scored 0.000, 0.229 and 0.048. Sung words are
# indistinct. What separates them is whether the lyrics contain the word.

_LYRIC_TOKENS = {"no", "dont", "fake", "me", "ya", "know"}


def test_a_confident_word_is_always_trusted():
    assert la.trust_word("anything", 0.9, _LYRIC_TOKENS)
    assert la.trust_word("anything", 0.9, None)


def test_a_hallucination_is_rejected_when_the_lyrics_disagree():
    """"neighal" at p=0.001 appears nowhere in the song."""
    assert not la.trust_word("neighal", 0.001, _LYRIC_TOKENS)


def test_an_indistinct_real_word_is_kept():
    """"no" scored 0.000 and is the first word of the song. A floor alone
    would have cost the whole opening line."""
    assert la.trust_word("no", 0.000, _LYRIC_TOKENS)
    assert la.trust_word("ya", 0.048, _LYRIC_TOKENS)


def test_without_lyrics_only_near_zero_is_rejected():
    """A track no source has words for: the transcription is the only text
    there is, so it is not second-guessed."""
    assert la.trust_word("but", 0.013, None)
    assert not la.trust_word("neighal", 0.001, None)


def test_a_word_with_no_probability_is_trusted():
    """An older backend, or a whole-segment fallback, reports none."""
    assert la.trust_word("whatever", None, _LYRIC_TOKENS)


def test_untrusted_words_are_dropped_from_the_stream():
    class W:
        def __init__(self, text, start, end, p):
            self.text, self.start, self.end, self.probability = text, start, end, p

    words = [W("neighal", 4.0, 4.4, 0.001), W("no", 13.0, 13.3, 0.000)]
    toks, starts, _ = la._whisper_tokens(words, lyric_tokens=_LYRIC_TOKENS)
    assert toks == ["no"]
    assert starts == [13.0]


def test_line_weight_respects_commas_and_breaks():
    w_plain = la.line_weight("where mister when you are gone")
    w_comma = la.line_weight("where, mister, when you are gone")
    w_excl = la.line_weight("where! mister! when you are gone!")
    assert w_comma > w_plain
    assert w_excl > w_plain
