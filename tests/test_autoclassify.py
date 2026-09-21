"""Filling in genre and tone for a track that has neither.

Both labels existed only where someone had done something -- `k`, or a bulk
script -- so a track played for the first time stayed unlabelled indefinitely,
which is exactly the track a listener is looking at.

What it will and will not do is decided by cost: tone is free, genre needs
audio, and the only way to get audio for a Spotify track is to record 45
seconds of it, which is not something a program should start because a song
came on.
"""
from __future__ import annotations

import pytest

from karaoke import autoclassify, localcache


@pytest.fixture()
def conn(tmp_path):
    c = localcache.connect(tmp_path / "auto.db")
    c.execute("INSERT INTO tracks (artist, title) VALUES ('Swans', 'Blind')")
    c.commit()
    yield c
    c.close()


LYRIC = "Forever loving, forever waiting, the universal mind " * 8


def _no_clap(monkeypatch, available=False):
    from karaoke import clap_vector

    monkeypatch.setattr(clap_vector, "available", lambda: available)


def test_a_track_with_words_and_no_tone_wants_one(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    _no_clap(monkeypatch)
    conn.execute("INSERT INTO lyrics (track_id, kind, source, plain_lyrics)"
                 " VALUES (1, 'approved', 'lrclib', %s)", (LYRIC,))
    conn.commit()
    assert "tone" in autoclassify.missing(1, conn)


def test_a_track_that_already_has_a_tone_wants_nothing(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    from karaoke.tone import ToneVerdict

    _no_clap(monkeypatch)
    conn.execute("INSERT INTO lyrics (track_id, kind, source, plain_lyrics)"
                 " VALUES (1, 'approved', 'lrclib', %s)", (LYRIC,))
    conn.commit()
    localcache.record_tone(1, ToneVerdict(tone="sad and mournful", score=0.4),
                           conn)
    assert "tone" not in autoclassify.missing(1, conn)


def test_transcribed_words_are_not_read_for_tone(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    """A Whisper guess has no attitude worth reading -- only the model's."""
    _no_clap(monkeypatch)
    conn.execute("INSERT INTO lyrics (track_id, kind, source, plain_lyrics)"
                 " VALUES (1, 'approved', 'whisper', %s)", (LYRIC,))
    conn.commit()
    assert "tone" not in autoclassify.missing(1, conn)


def test_a_short_lyric_is_not_read(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    _no_clap(monkeypatch)
    conn.execute("INSERT INTO lyrics (track_id, kind, source, plain_lyrics)"
                 " VALUES (1, 'approved', 'lrclib', 'too short')")
    conn.commit()
    assert "tone" not in autoclassify.missing(1, conn)


def test_an_instrumental_wants_no_tone(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    _no_clap(monkeypatch)
    assert "tone" not in autoclassify.missing(1, conn)


def test_genre_is_not_wanted_without_audio(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    """The only way to get audio for a Spotify track is to record it, and
    starting a recording because a song came on is not the program's call."""
    from karaoke import clap_vector, search

    monkeypatch.setattr(clap_vector, "available", lambda: True)
    monkeypatch.setattr(search, "clap_vector_for", lambda tid, os_client=None: None)
    monkeypatch.setattr(autoclassify, "_has_cached_audio", lambda tid, c: None)
    assert "genre" not in autoclassify.missing(1, conn)


def test_genre_is_wanted_when_an_embedding_already_exists(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    from karaoke import clap_vector, search

    monkeypatch.setattr(clap_vector, "available", lambda: True)
    monkeypatch.setattr(search, "clap_vector_for",
                        lambda tid, os_client=None: [0.1] * clap_vector.CLAP_DIM)
    assert "genre" in autoclassify.missing(1, conn)


def test_genre_is_wanted_when_the_audio_is_already_cached(conn, monkeypatch, tmp_path):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    from karaoke import clap_vector, search

    monkeypatch.setattr(clap_vector, "available", lambda: True)
    monkeypatch.setattr(search, "clap_vector_for", lambda tid, os_client=None: None)
    monkeypatch.setattr(autoclassify, "_has_cached_audio",
                        lambda tid, c: tmp_path / "a.webm")
    assert "genre" in autoclassify.missing(1, conn)


def test_nothing_is_wanted_without_the_clap_stack(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    _no_clap(monkeypatch)
    monkeypatch.setattr(autoclassify, "_has_cached_audio",
                        lambda tid, c: "/some/file.webm")
    assert "genre" not in autoclassify.missing(1, conn)


def test_run_reports_only_what_it_added(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    _no_clap(monkeypatch)
    conn.execute("INSERT INTO lyrics (track_id, kind, source, plain_lyrics)"
                 " VALUES (1, 'approved', 'lrclib', %s)", (LYRIC,))
    conn.commit()
    monkeypatch.setattr(autoclassify, "label_tone", lambda tid, c: "sad")
    assert autoclassify.run(1, conn) == {"tone": "sad"}


def test_a_failure_in_one_label_does_not_stop_the_other(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    """This runs behind a track that is playing; nothing here is worth
    interrupting that for."""
    from karaoke import clap_vector, search

    monkeypatch.setattr(clap_vector, "available", lambda: True)
    monkeypatch.setattr(search, "clap_vector_for",
                        lambda tid, os_client=None: [0.1] * clap_vector.CLAP_DIM)
    conn.execute("INSERT INTO lyrics (track_id, kind, source, plain_lyrics)"
                 " VALUES (1, 'approved', 'lrclib', %s)", (LYRIC,))
    conn.commit()

    def _boom(track_id, c):
        raise RuntimeError("no model")

    monkeypatch.setattr(autoclassify, "label_genre", _boom)
    monkeypatch.setattr(autoclassify, "label_tone", lambda tid, c: "sad")
    assert autoclassify.run(1, conn) == {"tone": "sad"}


def test_an_unknown_track_asks_for_nothing(conn, monkeypatch):
    conn.execute("INSERT INTO tracks (track_id, artist, title, duration) VALUES (1, \'a\', \'t\', 123) ON CONFLICT DO NOTHING")
    _no_clap(monkeypatch)
    assert autoclassify.missing(999, conn) == set()
