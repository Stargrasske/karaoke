"""Tests for the local SQLite lyrics cache + play/discovery stats."""
from __future__ import annotations

from karaoke import localcache
from karaoke.lyrics import Lyrics


import pytest

@pytest.fixture
def c(tmp_path):
    conn = localcache.connect(tmp_path / "karaoke.db")
    yield conn
    conn.close()


def test_put_and_get_cached_lyrics_roundtrip(tmp_path, c):
    ly = Lyrics(
        plain="line one\nline two",
        synced_raw="[00:01.00] line one\n[00:05.00] line two",
        source="lrclib",
        lines=[(1.0, "line one"), (5.0, "line two")],
    )
    localcache.put_cached_lyrics("R.E.M.", "Losing My Religion", ly, conn=c)

    got = localcache.get_cached_lyrics("R.E.M.", "Losing My Religion", conn=c)
    assert got is not None
    assert got.has_synced
    assert got.source == "lrclib"
    assert got.lines[0] == (1.0, "line one")


def test_cache_key_is_case_insensitive(tmp_path, c):
    ly = Lyrics(plain="x", synced_raw="[00:01.00] x", source="lrclib",
                lines=[(1.0, "x")])
    localcache.put_cached_lyrics("R.E.M.", "Losing My Religion", ly, conn=c)
    got = localcache.get_cached_lyrics("r.e.m.", "losing my religion", conn=c)
    assert got is not None and got.has_synced


def test_get_cached_lyrics_miss_returns_none(tmp_path, c):
    assert localcache.get_cached_lyrics("Nobody", "Nothing", conn=c) is None


def test_put_empty_lyrics_is_noop(tmp_path, c):
    localcache.put_cached_lyrics("A", "B", Lyrics(), conn=c)
    assert localcache.get_cached_lyrics("A", "B", conn=c) is None


def test_put_is_upsert(tmp_path, c):
    localcache.put_cached_lyrics(
        "A", "B", Lyrics(plain="old", source="lrclib"), conn=c)
    localcache.put_cached_lyrics(
        "A", "B",
        Lyrics(plain="new", synced_raw="[00:02.00] new", source="whisper",
               lines=[(2.0, "new")]),
        conn=c,
    )
    got = localcache.get_cached_lyrics("A", "B", conn=c)
    assert got is not None
    assert got.plain == "new"
    assert got.source == "whisper"
    assert got.has_synced


def test_summarize_counts_plays_and_discoveries(tmp_path, c):
    localcache.log_event("radio", "discover", artist="A", title="S1", conn=c)
    localcache.log_event("radio", "play", artist="A", title="S1",
                         source="lrclib", has_synced=True, conn=c)
    localcache.log_event("radio", "play", artist="A", title="S1",
                         source="lrclib", has_synced=True, conn=c)
    localcache.log_event("spotify", "play", artist="B", title="S2",
                         source="lrclib", has_synced=True, conn=c)
    localcache.log_event("radio", "cache_hit", artist="A", title="S1", conn=c)
    localcache.log_event("radio", "cache_miss", artist="A", title="S1", conn=c)

    s = localcache.summarize(conn=c)
    assert s.plays == 3
    assert s.discoveries == 1
    assert s.cache_hits == 1
    assert s.cache_misses == 1
    assert 0.49 < s.cache_hit_rate < 0.51
    assert s.distinct_tracks == 2
    assert s.distinct_artists == 2
    # Most-played track first.
    assert s.top_tracks[0] == ("A", "S1", 3)
    assert s.top_artists[0] == ("A", 3)
    assert dict(s.by_mode)["radio"] == 3


def test_summarize_empty(tmp_path, c):
    s = localcache.summarize(conn=c)
    assert s.total_events == 0
    assert s.plays == 0
    assert s.cache_hit_rate == 0.0
    assert s.top_tracks == []


def test_add_track_source_does_not_create_empty_lyrics(tmp_path, c):
    track_id = localcache.add_track_source(
        "Mr. Bungle",
        "Violenza Domestica",
        duration=210.0,
        url="https://www.youtube.com/watch?v=bXWHf2HH8jY",
        kind="youtube",
        conn=c,
    )

    assert track_id > 0
    assert localcache.get_cached_lyrics("Mr. Bungle", "Violenza Domestica", conn=c) is None
    found = localcache.find_track_by_url(
        "https://www.youtube.com/watch?v=bXWHf2HH8jY", c
    )
    assert found == (track_id, "Mr. Bungle", "Violenza Domestica")
    assert c.execute("SELECT count(*) FROM lyrics").fetchone()["count"] == 0


def test_delete_empty_approved_lyrics_keeps_real_lyrics(tmp_path, c):
    localcache.add_track_and_lyrics("Empty", "Placeholder", Lyrics(), conn=c)
    localcache.add_track_and_lyrics("Real", "Song", Lyrics(plain="words", source="manual"), conn=c)
    # Simulate legacy bad rows from the earlier cache indexer.
    empty_id = localcache.find_track_id("Empty", "Placeholder", c)
    assert empty_id is not None
    c.execute(
        "INSERT INTO lyrics (track_id, kind, source, synced_lyrics, plain_lyrics) VALUES (%s, 'approved', 'cache', '', '')",
        (empty_id,),
    )
    c.commit()

    assert localcache.delete_empty_approved_lyrics(conn=c) == 1
    assert c.execute("SELECT count(*) FROM lyrics").fetchone()["count"] == 1
    got = localcache.get_cached_lyrics("Real", "Song", conn=c)
    assert got is not None
    assert got.plain == "words"


def test_log_event_ignores_blank_titles_in_top_lists(tmp_path, c):
    localcache.log_event("query", "play", artist="", title="", conn=c)
    s = localcache.summarize(conn=c)
    assert s.plays == 1
    assert s.distinct_tracks == 0
    assert s.top_tracks == []


def test_add_track_with_url_updates_existing_track_no_duplicate(tmp_path, c):
    """A transcribe-style write whose URL shares a track's video ID must UPDATE,
    not create a duplicate track from divergent parsed artist/title."""
    # Canonical track from LRCLIB, with a YouTube source.
    localcache.add_track_and_lyrics(
        "Ian Asher & Phantogram", "Black Out Days (Stay Away)",
        Lyrics(plain="words", source="lrclib"),
        url="https://www.youtube.com/watch?v=FFxsTiFGWT8", kind="youtube", conn=c,
    )
    canonical_id = localcache.find_track_id(
        "Ian Asher & Phantogram", "Black Out Days (Stay Away)", c)
    assert canonical_id is not None

    # Whisper write-back with filename-derived tags but the SAME video id
    # (short-form youtu.be URL to also exercise video-ID matching).
    localcache.add_track_and_lyrics(
        "", "FFxsTiFGWT8",
        Lyrics(plain="w", synced_raw="[00:01.00] w", source="whisper",
               lines=[(1.0, "w")]),
        url="https://youtu.be/FFxsTiFGWT8", kind="youtube", conn=c,
    )

    # Still exactly one track; its lyrics were updated in place.
    assert c.execute("SELECT count(*) FROM tracks").fetchone()["count"] == 1
    got = localcache.get_lyrics_by_track_id(canonical_id, c)
    assert got is not None and got.source == "whisper"



def test_normalize_gap_metadata_strips_page_decorations():
    n = localcache.normalize_gap_metadata
    assert n("Motorpsycho - Topic", "Vortex Surfer") == ("Motorpsycho", "Vortex Surfer")
    assert n("Luther Vandross", "Never Too Much (Official HD Video) | YouTube Music") == (
        "Luther Vandross", "Never Too Much"
    )


def test_normalize_gap_metadata_rejects_unusable_rows():
    n = localcache.normalize_gap_metadata
    assert n("", "September | YouTube Music") is None          # no artist
    assert n("Artist", "") is None                              # no title
    assert n("RHINO", "Faith No More - Angel Dust (Full Album)") is None  # not a track


def test_normalize_gap_metadata_keeps_real_titles_intact():
    n = localcache.normalize_gap_metadata
    assert n("Gotye", "Somebody That I Used to Know") == (
        "Gotye", "Somebody That I Used to Know"
    )
    # Streaming-edition suffixes are clean_title's job, not the gap normalizer's.
    assert n("Ween", "Voodoo Lady - 2024 Remastered") == (
        "Ween", "Voodoo Lady - 2024 Remastered"
    )


def test_log_lyric_gap_skips_unusable_metadata(tmp_path, c):
    localcache.log_lyric_gap("", "September | YouTube Music", c)
    localcache.log_lyric_gap("islandman - Topic", "Agit", c)
    rows = c.execute("SELECT artist, title FROM lyric_gaps").fetchall()
    assert [(r["artist"], r["title"]) for r in rows] == [("islandman", "Agit")]


def test_normalize_gap_metadata_strips_stranded_topic_prefix():
    """"Artist - Topic - Song" split on its first separator strands "Topic - "."""
    n = localcache.normalize_gap_metadata
    assert n("Mr. Bungle", "Topic - Violenza Domestica") == (
        "Mr. Bungle", "Violenza Domestica"
    )
    assert n("Fugazi", "Topic - Waiting Room") == ("Fugazi", "Waiting Room")


def test_normalize_gap_metadata_rejects_platform_only_title():
    n = localcache.normalize_gap_metadata
    assert n("Violenza Domestica", "YouTube") is None
    assert n("Some Artist", "YouTube Music") is None


def test_normalize_gap_metadata_drops_redundant_artist_prefix():
    """YouTube titles repeat the artist; LRCLIB's exact endpoint can't match that."""
    n = localcache.normalize_gap_metadata
    assert n("Red Hot Chili Peppers", "Red Hot Chili Peppers - Suck My Kiss") == (
        "Red Hot Chili Peppers", "Suck My Kiss"
    )
    # A song genuinely sharing the artist's name survives as itself.
    assert n("Bad Company", "Bad Company - Bad Company") == (
        "Bad Company", "Bad Company"
    )
    # A title that merely starts with a similar word is left alone.
    assert n("Ween", "Weenie Roast") == ("Ween", "Weenie Roast")
