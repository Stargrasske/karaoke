"""Tests for saved searches and synced YouTube Music playlists in SQLite."""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch
import pytest

from karaoke import localcache, ytmusic_playlist as yp
from karaoke.tasks import sync_ytmusic_playlist
from karaoke.ytmusic_client import YTMusicClient


def _conn(tmp_path):
    return localcache.connect(tmp_path / "karaoke.db")


# --- Clean Name Generation -------------------------------------------------

def test_clean_playlist_name_for_query():
    # Regular searches
    assert yp.clean_playlist_name_for_query("radiohead") == "Karaoke: Radiohead"
    assert yp.clean_playlist_name_for_query("90s rock") == "Karaoke: 90s Rock"
    assert yp.clean_playlist_name_for_query("queen - bohemian rhapsody") == "Karaoke: Queen - Bohemian Rhapsody"

    # Search prefixes
    assert yp.clean_playlist_name_for_query("/oasis") == "Karaoke: Oasis"
    assert yp.clean_playlist_name_for_query("search: coldplay") == "Karaoke: Coldplay"

    # Multiple whitespace
    assert yp.clean_playlist_name_for_query("  the   beatles  ") == "Karaoke: The Beatles"

    # Wildcards and empty fall back to daily temp queue name
    assert yp.clean_playlist_name_for_query("") .startswith("Karaoke Temp Queue ")
    assert yp.clean_playlist_name_for_query("*").startswith("Karaoke Temp Queue ")
    assert yp.clean_playlist_name_for_query("   ").startswith("Karaoke Temp Queue ")

    # Long query gets truncated
    long_q = "a" * 80
    cleaned = yp.clean_playlist_name_for_query(long_q)
    assert len(cleaned) <= 60
    assert cleaned.endswith("…")


# --- Saved Searches Table --------------------------------------------------

def test_record_and_get_saved_searches(tmp_path):
    c = _conn(tmp_path)
    c.execute("DELETE FROM saved_searches WHERE query IN ('radiohead', 'queen')")
    c.commit()

    try:
        localcache.record_search_query("Radiohead", result_count=12, conn=c)
        localcache.record_search_query("Queen", result_count=5, conn=c)
        # Searching Radiohead again should bump use_count
        localcache.record_search_query("radiohead", result_count=12, conn=c)

        searches = [s for s in localcache.get_saved_searches(conn=c) if s["query"] in ("radiohead", "queen")]
        assert len(searches) == 2
        # Most recently used first
        assert searches[0]["query"] == "radiohead"
        assert searches[0]["use_count"] == 2
        assert searches[0]["result_count"] == 12

        assert searches[1]["query"].lower() == "queen"
        assert searches[1]["use_count"] == 1
    finally:
        c.execute("DELETE FROM saved_searches WHERE query IN ('radiohead', 'queen')")
        c.commit()


# --- Saved Playlists & Tracks ----------------------------------------------

def test_save_and_get_playlists_and_tracks(tmp_path):
    c = _conn(tmp_path)

    tracks = [
        {"position": 1, "track_id": 101, "artist": "Daft Punk", "title": "One More Time", "video_id": "FGBhQbmPwH8", "url": "https://music.youtube.com/watch?v=FGBhQbmPwH8"},
        {"position": 2, "track_id": 102, "artist": "Daft Punk", "title": "Harder Better Faster", "video_id": "gAjR4_CbPpQ", "url": "https://music.youtube.com/watch?v=gAjR4_CbPpQ"},
    ]

    localcache.save_playlist(
        "PL_DAFT_PUNK",
        "Karaoke: Daft Punk",
        search_query="daft punk",
        tracks=tracks,
        url="https://music.youtube.com/playlist?list=PL_DAFT_PUNK",
        conn=c,
    )

    playlists = localcache.get_saved_playlists(conn=c)
    assert len(playlists) == 1
    assert playlists[0]["playlist_id"] == "PL_DAFT_PUNK"
    assert playlists[0]["name"] == "Karaoke: Daft Punk"
    assert playlists[0]["search_query"] == "daft punk"
    assert playlists[0]["track_count"] == 2

    saved_tracks = localcache.get_saved_playlist_tracks("PL_DAFT_PUNK", conn=c)
    assert len(saved_tracks) == 2
    assert saved_tracks[0]["position"] == 1
    assert saved_tracks[0]["artist"] == "Daft Punk"
    assert saved_tracks[0]["video_id"] == "FGBhQbmPwH8"
    assert saved_tracks[1]["position"] == 2
    assert saved_tracks[1]["title"] == "Harder Better Faster"


# --- Capping & Database Sync in create_temp_queue_playlist -----------------

def test_create_temp_queue_playlist_caps_and_saves_to_db(tmp_path):
    c = _conn(tmp_path)
    client = MagicMock(spec=YTMusicClient)
    client.is_authenticated = True
    client.get_library_playlists.side_effect = [
        [],
        [{"title": "Karaoke: 90s Hits", "playlistId": "PL_CREATED"}],
    ]
    client.create_playlist.return_value = "PL_CREATED"
    client.get_playlist.return_value = {"tracks": []}

    # Generate 70 dummy rows
    rows = [
        {
            "track_id": i,
            "artist": f"Artist {i}",
            "title": f"Title {i}",
            "url": f"https://music.youtube.com/watch?v=VID{i:08d}",
        }
        for i in range(1, 71)
    ]

    res = yp.create_temp_queue_playlist(
        rows,
        search_query="90s hits",
        client=client,
        max_tracks=50,
        conn=c,
    )

    assert res.playlist_id == "PL_CREATED"
    assert res.name == "Karaoke: 90s Hits"
    # Added tracks capped to 50
    assert res.added == 50
    client.add_playlist_items.assert_called_once()
    added_vids = client.add_playlist_items.call_args[0][1]
    assert len(added_vids) == 50

    # Verify persisted in SQLite
    playlists = localcache.get_saved_playlists(conn=c)
    assert len(playlists) == 1
    assert playlists[0]["playlist_id"] == "PL_CREATED"
    assert playlists[0]["name"] == "Karaoke: 90s Hits"
    assert playlists[0]["track_count"] == 50

    saved_tracks = localcache.get_saved_playlist_tracks("PL_CREATED", conn=c)
    assert len(saved_tracks) == 50
    assert saved_tracks[0]["artist"] == "Artist 1"
    assert saved_tracks[49]["artist"] == "Artist 50"


# --- Celery Task Execution ------------------------------------------------

def test_sync_ytmusic_playlist_task(tmp_path):
    c = _conn(tmp_path)

    mock_result = yp.PlaylistResult(
        playlist_id="PL_CELERY_TEST",
        name="Karaoke: Rock",
        added=10,
        resolved=10,
    )

    with patch("karaoke.ytmusic_playlist.create_temp_queue_playlist", return_value=mock_result) as mock_create, \
         patch("karaoke.localcache.connect", return_value=c):

        payload = {
            "name": "Karaoke: Rock",
            "query": "rock",
            "rows": [{"artist": "Band", "title": "Song", "url": "https://youtu.be/12345678901"}],
            "max_tracks": 50,
        }

        # Execute task directly
        res = sync_ytmusic_playlist.apply(args=[payload]).get()

        assert res["playlist_id"] == "PL_CELERY_TEST"
        assert res["name"] == "Karaoke: Rock"
        assert res["added"] == 10
        assert res["query"] == "rock"

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["name"] == "Karaoke: Rock"
        assert call_kwargs["search_query"] == "rock"
        assert call_kwargs["max_tracks"] == 50
