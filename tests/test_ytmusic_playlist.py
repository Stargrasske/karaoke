"""Tests for YouTube Music playlist sync, library import, and client methods."""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch
import pytest

from karaoke import localcache, ytmusic_playlist as yp
from karaoke.lyrics import Lyrics
from karaoke.ytmusic_client import YTMusicAuthError, YTMusicClient, clean_search_term, resolve_auth_file


def _conn(tmp_path):
    return localcache.connect(tmp_path / "karaoke.db")


def _add_track(conn, artist, title, *, synced=True, url=None, kind="youtube"):
    ly = Lyrics(
        plain="line1\nline2",
        synced_raw="[00:01.00] line1\n[00:05.00] line2" if synced else "",
        source="lrclib",
    )
    localcache.add_track_and_lyrics(artist, title, ly, url=url, kind=kind, conn=conn)


# --- Video ID extraction ---------------------------------------------------

def test_extract_video_id_formats():
    assert yp.extract_video_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert yp.extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RD123") == "dQw4w9WgXcQ"
    assert yp.extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert yp.extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert yp.extract_video_id("https://open.spotify.com/track/123") == ""
    assert yp.extract_video_id("") == ""


# --- Candidate selection ---------------------------------------------------

def test_karaoke_tracks_picks_synced_with_yt_sources(tmp_path):
    c = _conn(tmp_path)
    _add_track(c, "Daft Punk", "One More Time", synced=True, url="https://music.youtube.com/watch?v=FGBhQbmPwH8", kind="youtube_music")
    _add_track(c, "Radiohead", "Creep", synced=True, url="https://open.spotify.com/track/xyz", kind="spotify")
    _add_track(c, "Untimed", "Song", synced=False, url="https://youtu.be/123", kind="youtube")

    candidates = yp.karaoke_tracks(c)
    by_artist = {cand.artist: cand for cand in candidates}

    assert "Daft Punk" in by_artist
    assert by_artist["Daft Punk"].video_id == "FGBhQbmPwH8"
    assert by_artist["Daft Punk"].resolved_by == "stored"

    assert "Radiohead" in by_artist
    assert by_artist["Radiohead"].video_id == ""  # Needs resolution

    assert "Untimed" not in by_artist


def test_queue_rows_to_ytmusic_candidates_keeps_youtube_only():
    rows = [
        {"artist": "A", "title": "YT", "url": "https://music.youtube.com/watch?v=ABCDEFGHIJK"},
        {"artist": "B", "title": "Spotify", "url": "https://open.spotify.com/track/123"},
        {"artist": "C", "title": "Duplicate", "url": "https://youtu.be/ABCDEFGHIJK"},
        {"artist": "D", "title": "YT2", "url": "https://www.youtube.com/watch?v=LMNOPQRSTU1&list=RD"},
    ]

    candidates = yp.queue_rows_to_ytmusic_candidates(rows)

    assert [(c.artist, c.title, c.video_id) for c in candidates] == [
        ("A", "YT", "ABCDEFGHIJK"),
        ("D", "YT2", "LMNOPQRSTU1"),
    ]


def test_create_temp_queue_playlist_overwrites_daily_playlist():
    client = MagicMock(spec=YTMusicClient)
    client.is_authenticated = True
    client.get_library_playlists.side_effect = [
        [{"title": "Karaoke Temp Queue 2026-09-10", "playlistId": "PL_TODAY"}],
        [{"title": "Karaoke Temp Queue 2026-09-10", "playlistId": "PL_TODAY"}],
    ]
    client.get_playlist.return_value = {
        "tracks": [{"videoId": "OLDOLDOLD01", "setVideoId": "SET_OLD"}],
    }

    res = yp.create_temp_queue_playlist(
        [{"artist": "A", "title": "Song", "url": "https://music.youtube.com/watch?v=ABCDEFGHIJK"}],
        name="Karaoke Temp Queue 2026-09-10",
        client=client,
    )

    assert res.playlist_id == "PL_TODAY"
    client.create_playlist.assert_not_called()
    client.remove_playlist_items.assert_called_once()
    client.add_playlist_items.assert_called_once_with("PL_TODAY", ["ABCDEFGHIJK"], duplicates=True)


def test_get_latest_temp_queue_playlist_fetches_and_maps():
    client = MagicMock(spec=YTMusicClient)
    client.is_authenticated = True
    client.get_library_playlists.return_value = [
        {"title": "Karaoke Temp Queue 2026-09-11", "playlistId": "PL_LATEST"},
        {"title": "Karaoke Temp Queue 2026-09-10", "playlistId": "PL_OLDER"},
    ]
    client.get_playlist.return_value = {
        "tracks": [
            {"videoId": "VID12345678", "title": "Track One", "artists": [{"name": "Artist One"}]},
        ],
    }

    db_conn = sqlite3.connect(":memory:")
    db_conn.row_factory = sqlite3.Row
    db_conn.executescript(
        """
        CREATE TABLE tracks (track_id INTEGER PRIMARY KEY, artist TEXT, title TEXT);
        CREATE TABLE sources (source_id INTEGER PRIMARY KEY, track_id INTEGER, kind TEXT, url TEXT);
        CREATE TABLE track_analysis (track_id INTEGER PRIMARY KEY, detected_key TEXT, bpm REAL, energy REAL);
        CREATE TABLE track_genre (track_id INTEGER PRIMARY KEY, genre TEXT);
        INSERT INTO tracks VALUES (1, 'Artist One', 'Track One');
        INSERT INTO sources VALUES (1, 1, 'youtube_music', 'https://music.youtube.com/watch?v=VID12345678');
        INSERT INTO track_analysis VALUES (1, 'C major', 120.0, 0.75);
        INSERT INTO track_genre VALUES (1, 'Pop');
        """
    )

    res = yp.get_latest_temp_queue_playlist(client=client, conn=db_conn)
    assert res is not None
    assert res["playlist_id"] == "PL_LATEST"
    assert len(res["rows"]) == 1
    assert res["rows"][0]["track_id"] == 1
    assert res["rows"][0]["artist"] == "Artist One"
    assert res["rows"][0]["genre"] == "Pop"



# --- Playlist Sync & Build -------------------------------------------------

def test_build_or_sync_dry_run():
    client = MagicMock(spec=YTMusicClient)
    client.search_track.return_value = "dQw4w9WgXcQ"

    cands = [
        yp.Candidate("Rick Astley", "Never Gonna Give You Up"),
        yp.Candidate("Daft Punk", "Get Lucky", video_id="5NV6Rdv1a3I", resolved_by="stored"),
    ]

    res = yp.build_or_sync_ytmusic_playlist(
        candidates=cands,
        name="Test Playlist",
        dry_run=True,
        client=client,
    )

    assert res.dry_run is True
    assert res.candidates == 2
    assert res.resolved == 2
    assert res.added == 0
    client.create_playlist.assert_not_called()


def test_build_or_sync_creates_playlist_when_not_existing():
    client = MagicMock(spec=YTMusicClient)
    client.is_authenticated = True
    client.get_library_playlists.return_value = []
    client.create_playlist.return_value = "PL_NEW_123"

    cands = [
        yp.Candidate("Daft Punk", "Get Lucky", video_id="5NV6Rdv1a3I", resolved_by="stored"),
    ]

    res = yp.build_or_sync_ytmusic_playlist(
        candidates=cands,
        name="Karaoke Night",
        dry_run=False,
        client=client,
    )

    assert res.playlist_id == "PL_NEW_123"
    assert res.added == 1
    client.create_playlist.assert_called_once_with(
        title="Karaoke Night",
        description=yp.DEFAULT_DESCRIPTION,
        privacy_status="PRIVATE",
        video_ids=["5NV6Rdv1a3I"],
    )


def test_build_or_sync_appends_only_new_tracks_to_existing():
    client = MagicMock(spec=YTMusicClient)
    client.is_authenticated = True
    client.get_library_playlists.return_value = [
        {"title": "Karaoke Night", "playlistId": "PL_EXISTING_456"}
    ]
    client.get_playlist_tracks.return_value = [
        {"videoId": "VID_ALREADY_THERE", "title": "Old Track", "artist": "Old Artist", "url": ""},
    ]

    cands = [
        yp.Candidate("A", "Song 1", video_id="VID_ALREADY_THERE", resolved_by="stored"),
        yp.Candidate("B", "Song 2", video_id="VID_NEW_1", resolved_by="stored"),
    ]

    res = yp.build_or_sync_ytmusic_playlist(
        candidates=cands,
        name="Karaoke Night",
        dry_run=False,
        client=client,
    )

    assert res.playlist_id == "PL_EXISTING_456"
    assert res.already_present == 1
    assert res.added == 1
    client.add_playlist_items.assert_called_once_with("PL_EXISTING_456", ["VID_NEW_1"])


# --- Import Playlist to Library --------------------------------------------

def test_import_ytmusic_playlist_to_library(tmp_path):
    c = _conn(tmp_path)
    client = MagicMock(spec=YTMusicClient)
    client.get_playlist_tracks.return_value = [
        {
            "videoId": "abc111",
            "title": "Imported Title",
            "artist": "Imported Artist",
            "album": "Imported Album",
            "duration_seconds": 240,
            "url": "https://music.youtube.com/watch?v=abc111",
        }
    ]

    fake_lyrics = Lyrics(plain="sample text", synced_raw="[00:01.00] sample text", source="lrclib")
    with patch("karaoke.lyrics.fetch_lrclib", return_value=fake_lyrics):
        stats = yp.import_ytmusic_playlist_to_library("PL_IMPORT", conn=c, client=client)

    assert stats["total_tracks"] == 1
    assert stats["added_tracks"] == 1
    assert stats["synced_lyrics"] == 1

    # Verify track stored in SQLite
    row = c.execute("SELECT artist, title FROM tracks WHERE artist = 'Imported Artist'").fetchone()
    assert row is not None
    assert row["title"] == "Imported Title"


# --- YTMusicClient & Auth --------------------------------------------------

def test_ytmusic_client_require_auth():
    client = YTMusicClient(auth=None)
    client.is_authenticated = False
    with pytest.raises(YTMusicAuthError):
        client.get_library_playlists()


def test_clean_search_term():
    assert clean_search_term("Song (Official Audio)") == "Song"
    assert clean_search_term("Track [Remastered 2020]") == "Track"
    assert clean_search_term("Hero ft. Someone") == "Hero"


# --- Dialog Dismissal over CDP ---------------------------------------------

def test_dismiss_kiosk_dialogs():
    from karaoke import player_open
    with patch.object(player_open, "_cdp_send", return_value={"result": {"result": {"value": True}}}) as mock_send:
        dismissed = player_open.dismiss_kiosk_dialogs()
        assert dismissed is True
        assert mock_send.call_count >= 1
