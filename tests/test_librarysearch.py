"""Weighted search across the library.

One box, ranked results. The weights exist so that typing a song name finds
the song rather than every track by an artist whose name shares letters with
it -- title first, then album, artist, and lyrics as the last resort.
"""
import pytest

from karaoke import librarysearch as ls


class _Row(dict):
    """Stands in for a sqlite3.Row, which supports keys() and [] access."""

    def keys(self):
        return list(super().keys())


def _row(title="", artist="", album="", words=""):
    return _Row(track_id=1, title=title, artist=artist, album=album, words=words)


# -- how well one field matches -----------------------------------------

def test_an_exact_field_beats_a_prefix_beats_a_word_beats_a_substring():
    assert (ls.field_score("River", "river")
            > ls.field_score("River Deep", "river")
            > ls.field_score("The River Wild", "river")
            > ls.field_score("Riverside", "river"))


def test_matching_ignores_case_and_padding():
    assert ls.field_score("  The   RIVER ", "the river") == ls.EXACT


def test_no_match_scores_nothing():
    assert ls.field_score("Fearless", "banana") == 0.0
    assert ls.field_score("", "river") == 0.0
    assert ls.field_score("River", "") == 0.0


# -- the field priority the user asked for ------------------------------

def test_title_outranks_album_outranks_artist():
    """Typing a name should find the song, not everything by a similarly
    named artist."""
    by_title = ls.score_row(_row(title="Rain"), "rain")[0]
    by_album = ls.score_row(_row(album="Rain"), "rain")[0]
    by_artist = ls.score_row(_row(artist="Rain"), "rain")[0]
    assert by_title > by_album > by_artist


def test_lyrics_rank_last():
    by_artist = ls.score_row(_row(artist="Rain"), "rain")[0]
    by_lyrics = ls.score_row(_row(title="X", words="the rain falls"), "rain")[0]
    assert by_artist > by_lyrics > 0


def test_matching_several_fields_beats_matching_one():
    """A track matching title and artist is a better answer than either alone."""
    both = ls.score_row(_row(title="Rain", artist="Rain"), "rain")[0]
    one = ls.score_row(_row(title="Rain"), "rain")[0]
    assert both > one


def test_the_contributing_fields_are_reported_best_first():
    _, fields = ls.score_row(_row(title="Rain", artist="Rain Dogs"), "rain")
    assert fields[0] == "title"
    assert set(fields) == {"title", "artist"}


# -- lyrics are matched by word, not substring --------------------------

def test_lyrics_match_whole_words_only():
    """Substring matching would make "love" hit "glove" in every song, so the
    lyric field would match nearly everything instead of nothing."""
    assert ls.lyrics_score("I wear a glove", "love") == 0.0
    assert ls.lyrics_score("I need your love", "love") > 0.0


def test_lyrics_can_be_switched_off():
    row = _row(title="X", words="the rain falls")
    assert ls.score_row(row, "rain", search_lyrics=False)[0] == 0.0


# -- the whole search ---------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    from karaoke import localcache

    c = localcache.connect(tmp_path / "t.db")
    c.executescript("""
        INSERT INTO tracks (track_id, artist, title, album) VALUES
            (1, 'Pink Floyd', 'Fearless', 'Meddle'),
            (2, 'Blue Oyster Cult', "(Don't Fear) The Reaper", ''),
            (3, 'Fearless Band', 'Something Else', ''),
            (4, 'Someone', 'Unrelated', '');
        INSERT INTO lyrics (track_id, kind, plain_lyrics) VALUES
            (4, 'approved', 'and you go fearless into the night');
        INSERT INTO sources (track_id, kind, url) VALUES
            (1, 'youtube', 'https://youtu.be/abc'),
            (1, 'spotify', 'https://open.spotify.com/track/x');
    """)
    c.commit()
    yield c
    c.close()


def test_the_exact_title_wins(conn):
    hits = ls.search("fearless", conn)
    assert hits[0].title == "Fearless"


def test_an_artist_match_outranks_a_lyric_match(conn):
    names = [h.artist for h in ls.search("fearless", conn)]
    assert names.index("Fearless Band") < names.index("Someone")


def test_a_partial_word_still_finds_the_title(conn):
    assert any(h.track_id == 2 for h in ls.search("reaper", conn))


def test_an_empty_query_returns_nothing(conn):
    assert ls.search("", conn) == []
    assert ls.search("   ", conn) == []


def test_no_matches_is_empty_not_an_error(conn):
    assert ls.search("zzzznothing", conn) == []


def test_results_are_capped(conn):
    assert len(ls.search("e", conn, limit=2)) <= 2


# -- somewhere to play from ---------------------------------------------

def test_a_browser_url_is_preferred_over_spotify(conn):
    """The queue opens URLs, and a YouTube link plays without an app."""
    assert ls.playable_url(1, conn) == "https://youtu.be/abc"


def test_a_track_with_no_source_has_no_url(conn):
    assert ls.playable_url(4, conn) is None


# --- harvest_albums matching -----------------------------------------------
#
# A featured credit migrates between the artist and title fields depending on
# the source, and a row can have the two exchanged outright. Both cost albums.

import importlib.util as _ilu
from pathlib import Path as _Path

_HA = _ilu.spec_from_file_location(
    "harvest_albums", _Path(__file__).resolve().parent.parent
    / "scripts" / "harvest_albums.py")
harvest_albums = _ilu.module_from_spec(_HA)
_HA.loader.exec_module(harvest_albums)


def test_bare_title_drops_a_featured_credit():
    """"Kungs - I FEEL SO BAD ft. Ephemerals" and "Kungs, Ephemerals - I Feel
    So Bad" are the same song; only the side the credit landed on differs."""
    assert harvest_albums.bare_title("I FEEL SO BAD ft. Ephemerals") == "I FEEL SO BAD"
    assert harvest_albums.bare_title(
        "Instant Crush (feat. Julian Casablancas)") == "Instant Crush"


def test_bare_title_keeps_a_version_marker():
    """A remix is a different recording, not a credit."""
    assert harvest_albums.bare_title(
        "Prayer in C (Nitefreak Remix)") == "Prayer in C (Nitefreak Remix)"


def test_bare_title_never_empties_a_title():
    assert harvest_albums.bare_title("feat. Someone") == "feat. Someone"


def test_a_credit_difference_no_longer_rejects_the_match():
    found = {"artist": "Daft Punk, Julian Casablancas",
             "track": "Instant Crush (feat. Julian Casablancas)"}
    row = {"artist": "Daft Punk", "title": "Instant Crush ft. Julian Casablancas"}
    assert harvest_albums._is_same_song(found, row) is True


def test_a_different_song_is_still_rejected():
    found = {"artist": "Boards of Canada", "track": "Ready Lets Go"}
    row = {"artist": "Boards of Canada", "title": "Music Is Math"}
    assert harvest_albums._is_same_song(found, row) is False


# -- swapped artist/title ------------------------------------------------

def test_a_swap_is_detected_from_the_exchanged_fields():
    """Not guessed from the strings -- "Faint" and "Linkin Park" carry no clue
    about which is which. The evidence is the two fields coming back swapped."""
    found = {"artist": "Linkin Park", "track": "Faint"}
    row = {"artist": "Faint", "title": "Linkin Park"}
    assert harvest_albums.looks_swapped(found, row) is True


def test_an_ordinary_mismatch_is_not_a_swap():
    found = {"artist": "Boards of Canada", "track": "Ready Lets Go"}
    row = {"artist": "Boards of Canada", "title": "Music Is Math"}
    assert harvest_albums.looks_swapped(found, row) is False


def test_a_known_artist_blocks_the_swap(tmp_path):
    """There is a real band called Mighty Ships with a song called "Tom
    Waits", so exchanged fields are not proof. An artist naming nine other
    tracks here is an artist, and that row is not swapped."""
    from karaoke import localcache

    conn = localcache.connect(tmp_path / "t.db")
    try:
        conn.executescript("""
            INSERT INTO tracks (track_id, artist, title) VALUES
                (1, 'Tom Waits', 'Mighty Ships'),
                (2, 'Tom Waits', 'Army Ants'),
                (3, 'Faint', 'Linkin Park');
        """)
        conn.commit()
        assert harvest_albums.artist_used_elsewhere(conn, "Tom Waits", 1) is True
        assert harvest_albums.artist_used_elsewhere(conn, "Faint", 3) is False
    finally:
        conn.close()


def test_search_with_genre_filter(tmp_path):
    from karaoke import localcache
    from karaoke import librarysearch as ls

    conn = localcache.connect(tmp_path / "genre_search.db")
    try:
        conn.executescript("""
            INSERT INTO tracks (track_id, artist, title) VALUES
                (1, 'Artist A', 'Nothing Else Matters'),
                (2, 'Artist B', 'Nothing Compares'),
                (3, 'Artist C', 'Nothing Stays');
            INSERT INTO track_genre (track_id, genre, score, labelled_at) VALUES
                (1, 'heavy metal', 0.9, 1),
                (2, 'pop', 0.8, 1),
                (3, 'jazz', 0.85, 1);
        """)
        conn.commit()

        metal_hits = ls.search("nothing", conn, genre="heavy metal")
        assert len(metal_hits) == 1
        assert metal_hits[0].track_id == 1
        assert metal_hits[0].genre == "heavy metal"

        pop_hits = ls.search("nothing", conn, genre="pop")
        assert len(pop_hits) == 1
        assert pop_hits[0].track_id == 2
        assert pop_hits[0].genre == "pop"

        all_hits = ls.search("nothing", conn, genre="all")
        assert len(all_hits) == 3

        none_hits = ls.search("nothing", conn, genre="reggae")
        assert len(none_hits) == 0
    finally:
        conn.close()

