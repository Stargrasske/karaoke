"""Ranking Whisper's guesses below real lyrics, without discarding them.

Einstuerzende Neubauten's "Installation N deg 1" is stored as
"HerausforderDisobeylarationDisobeyedzyIt's the lawczywiscieDisobey not" --
German and Polish fragments fused without spaces, from a track that is mostly
industrial noise. It is fun, and it is not the words.

The distinction that matters: 69 tracks have Whisper's own *text*, while 94 have
``whisper_aligned`` where the words came from a real source and only the
timings are Whisper's. Penalising the second set would be wrong.
"""
from __future__ import annotations

import sqlite3

import pytest

from karaoke import librarysearch
from karaoke.librarysearch import is_transcribed, score_row, search


# --- which sources are a guess --------------------------------------------

def test_plain_whisper_is_a_guess():
    assert is_transcribed("whisper") is True


def test_whisper_aligned_is_not_a_guess():
    """The words are real there; only the timings came from Whisper."""
    assert is_transcribed("whisper_aligned") is False


def test_real_sources_are_not_guesses():
    for source in ("lrclib", "ytmusic_panel_lyricfind",
                   "youtube_caption_manual_en_synced", ""):
        assert is_transcribed(source) is False, source


def test_the_check_is_not_a_substring_match():
    """"does it mention whisper" would wrongly catch 94 good tracks."""
    assert is_transcribed("whisper_aligned") is False
    assert is_transcribed("WHISPER") is True          # case only
    assert is_transcribed("  whisper  ") is True      # whitespace only


# --- scoring ---------------------------------------------------------------

def _row(**over):
    base = {"title": "Installation", "album": "", "artist": "Neubauten",
            "words": "disobey not the law", "lyrics_source": "lrclib"}
    base.update(over)
    return _Row(base)


class _Row:
    """Minimal sqlite3.Row stand-in with .keys()."""

    def __init__(self, data): self._d = data
    def __getitem__(self, k): return self._d[k]
    def keys(self): return list(self._d)


def test_a_guessed_lyric_match_scores_below_a_real_one():
    real = score_row(_row(title="x", artist="y"), "disobey")[0]
    guess = score_row(_row(title="x", artist="y", lyrics_source="whisper"),
                      "disobey")[0]
    assert 0 < guess < real


def test_a_guessed_lyric_match_still_scores_something():
    """69 tracks have no other text; excluding them loses them entirely."""
    score, fields = score_row(_row(title="x", artist="y",
                                   lyrics_source="whisper"), "disobey")
    assert score > 0
    assert "lyrics?" in fields


def test_the_field_name_marks_the_match_as_uncertain():
    """So a caller can say why a result is ranked where it is."""
    _s, fields = score_row(_row(title="x", artist="y",
                                lyrics_source="whisper"), "disobey")
    assert "lyrics?" in fields and "lyrics" not in fields


def test_a_real_lyric_match_is_named_plainly():
    _s, fields = score_row(_row(title="x", artist="y"), "disobey")
    assert "lyrics" in fields


def test_whisper_aligned_lyrics_are_not_penalised():
    plain = score_row(_row(title="x", artist="y"), "disobey")[0]
    aligned = score_row(_row(title="x", artist="y",
                             lyrics_source="whisper_aligned"), "disobey")[0]
    assert aligned == plain


def test_a_title_match_still_beats_a_guessed_lyric_match():
    """What the user actually wanted: real signal first."""
    by_title = score_row(_row(title="Disobey", words="", artist="y"),
                         "disobey")[0]
    by_guess = score_row(_row(title="x", artist="y",
                              lyrics_source="whisper"), "disobey")[0]
    assert by_title > by_guess


def test_a_missing_source_column_does_not_break_scoring():
    """score_row is called with rows from more than one query."""
    row = _Row({"title": "x", "album": "", "artist": "y",
                "words": "disobey not the law"})
    assert score_row(row, "disobey")[0] > 0


# --- end to end through the query -----------------------------------------

@pytest.fixture()
def conn(tmp_path):
    from karaoke import localcache

    c = localcache.connect(tmp_path / "rank.db")
    rows = [("Real Band", "Song One", "lrclib", "disobey the law"),
            ("Neubauten", "Installation", "whisper", "disobey not the law")]
    for i, (artist, title, source, words) in enumerate(rows, start=1):
        c.execute("INSERT INTO tracks (artist, title, duration) VALUES (%s, %s, 200)",
                  (artist, title))
        c.execute("INSERT INTO lyrics (track_id, kind, source, plain_lyrics)"
                  " VALUES (%s, 'approved', %s, %s)", (i, source, words))
    c.commit()
    yield c
    c.close()


def test_the_real_lyric_outranks_the_guess_in_a_real_query(conn):
    hits = search("disobey", conn)
    assert [h.title for h in hits] == ["Song One", "Installation"]
    assert hits[0].score > hits[1].score


def test_the_guess_is_still_findable(conn):
    """Demoted, not filtered: it is the only text that track has."""
    titles = [h.title for h in search("disobey", conn)]
    assert "Installation" in titles


def test_the_penalty_is_a_demotion_not_an_erasure():
    assert 0 < librarysearch.TRANSCRIBED_PENALTY < 1


# --- the flag the user sees ------------------------------------------------

def test_the_track_readout_marks_a_guessed_source():
    from karaoke.tui import track_info

    assert "(guessed)" in track_info(source="whisper")


def test_the_track_readout_does_not_mark_a_real_source():
    from karaoke.tui import track_info

    assert "(guessed)" not in track_info(source="lrclib")
    assert "(guessed)" not in track_info(source="whisper_aligned")


def test_search_hits_carry_the_flag():
    from karaoke.search import _hit

    guessed = _hit({"_source": {"lyrics_source": "whisper"}, "_score": 1.0})
    real = _hit({"_source": {"lyrics_source": "lrclib"}, "_score": 1.0})
    assert guessed.transcribed is True
    assert real.transcribed is False


def test_both_search_paths_share_one_rule():
    """Two implementations would drift into disagreeing about trust."""
    from karaoke import search as os_search

    assert os_search.is_transcribed is is_transcribed
    assert os_search.TRANSCRIBED_SOURCE == librarysearch.TRANSCRIBED_SOURCE


# --- timing a guess must not launder it -----------------------------------

def test_a_timed_transcription_is_still_a_guess():
    """whisper_synced: Whisper's words, since given timings from its own audio.

    Singable, but no better corroborated. Storing it as whisper_aligned -- which
    means *real* words with Whisper timings, and is deliberately not demoted --
    would have hidden that.
    """
    assert is_transcribed("whisper_synced") is True


def test_real_words_with_whisper_timings_stay_undemoted():
    assert is_transcribed("whisper_aligned") is False


def test_the_guessed_sources_are_a_closed_set():
    assert set(librarysearch.TRANSCRIBED_SOURCES) == {"whisper", "whisper_synced"}


def test_a_timed_guess_scores_below_a_real_lyric():
    real = score_row(_row(title="x", artist="y"), "disobey")[0]
    timed_guess = score_row(_row(title="x", artist="y",
                                 lyrics_source="whisper_synced"), "disobey")[0]
    assert 0 < timed_guess < real


def test_the_readout_marks_a_timed_guess_too():
    from karaoke.tui import track_info

    assert "(guessed)" in track_info(source="whisper_synced")
