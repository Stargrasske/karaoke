"""Tests for YouTube Music playlist detection, auto-load follow, and two-way sync."""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch
import pytest

from karaoke import detect, localcache, ytmusic_playlist as yp
from karaoke.ytmusic_client import YTMusicClient


def _conn(tmp_path):
    return localcache.connect(tmp_path / "karaoke.db")


# --- 1. Playlist ID Extraction ----------------------------------------------

def test_extract_playlist_id():
    # YouTube Music watch URL with list=
    url1 = "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrEnWoR732-DNZk_cZz_8qYw1_zN_1m-e"
    assert localcache.extract_playlist_id(url1) == "PLrEnWoR732-DNZk_cZz_8qYw1_zN_1m-e"

    # YouTube playlist URL
    url2 = "https://www.youtube.com/playlist?list=PL1234567890abcdef"
    assert localcache.extract_playlist_id(url2) == "PL1234567890abcdef"

    # URL with list as first parameter
    url3 = "https://music.youtube.com/watch?list=RDCLAK5uy_1234567890123&v=abc12345678"
    assert localcache.extract_playlist_id(url3) == "RDCLAK5uy_1234567890123"

    # Direct playlist ID string
    assert localcache.extract_playlist_id("PL123456789012345") == "PL123456789012345"
    assert localcache.extract_playlist_id("RDCLAK5uy_1234567890123") == "RDCLAK5uy_1234567890123"
    assert localcache.extract_playlist_id("OLAK5uy_abcdef123456789") == "OLAK5uy_abcdef123456789"

    # Normal video URLs without playlist
    assert localcache.extract_playlist_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ") is None
    assert localcache.extract_playlist_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None
    assert localcache.extract_playlist_id("") is None
    assert localcache.extract_playlist_id(None) is None


# --- 2. Database Lookup & Reverse Video ID Lookup ---------------------------

def test_find_saved_playlist_and_reverse_video_id(tmp_path):
    c = _conn(tmp_path)

    tracks = [
        {
            "position": 1,
            "track_id": 1,
            "artist": "Radiohead",
            "title": "Creep",
            "video_id": "XFkzRNyygfk",
            "url": "https://music.youtube.com/watch?v=XFkzRNyygfk",
        },
        {
            "position": 2,
            "track_id": 2,
            "artist": "Radiohead",
            "title": "Karma Police",
            "video_id": "1uYWYWPc9HU",
            "url": "https://music.youtube.com/watch?v=1uYWYWPc9HU",
        },
    ]

    localcache.save_playlist(
        "PL_RADIOHEAD",
        "Karaoke: Radiohead",
        search_query="radiohead",
        tracks=tracks,
        url="https://music.youtube.com/playlist?list=PL_RADIOHEAD",
        conn=c,
    )

    # 1. Lookup by playlist ID
    pl = localcache.find_saved_playlist_by_id("PL_RADIOHEAD", conn=c)
    assert pl is not None
    assert pl["name"] == "Karaoke: Radiohead"
    assert pl["track_count"] == 2

    # 2. Reverse lookup by video ID
    found_pls = localcache.find_playlist_by_video_id("XFkzRNyygfk", conn=c)
    assert len(found_pls) >= 1
    assert found_pls[0]["playlist_id"] == "PL_RADIOHEAD"

    # Non-existent video ID
    assert localcache.find_playlist_by_video_id("NOT_IN_PLAYLIST", conn=c) == []

    # 3. Append playlist track
    new_track = {
        "track_id": 3,
        "artist": "Radiohead",
        "title": "No Surprises",
        "video_id": "u5CVsCnxyXg",
        "url": "https://music.youtube.com/watch?v=u5CVsCnxyXg",
    }
    new_pos = localcache.append_playlist_track("PL_RADIOHEAD", new_track, conn=c)
    assert new_pos == 3

    updated_pl = localcache.find_saved_playlist_by_id("PL_RADIOHEAD", conn=c)
    assert updated_pl is not None
    assert updated_pl["track_count"] == 3

    all_tracks = localcache.get_saved_playlist_tracks("PL_RADIOHEAD", conn=c)
    assert len(all_tracks) == 3
    assert all_tracks[2]["title"] == "No Surprises"

    # 4. Delete playlist
    localcache.delete_saved_playlist("PL_RADIOHEAD", conn=c)
    assert localcache.find_saved_playlist_by_id("PL_RADIOHEAD", conn=c) is None
    assert localcache.get_saved_playlist_tracks("PL_RADIOHEAD", conn=c) == []


# --- 3. Detection Dataclass Carrying playlist_id ----------------------------

def test_detection_playlist_id_classification():
    # URL with list= in PlayerMetadata
    meta = detect.PlayerMetadata(
        artist="Nirvana",
        title="Smells Like Teen Spirit",
        url="https://music.youtube.com/watch?v=hTWKbfoikeg&list=PL_NIRVANA_HITS",
        player="chromium",
    )
    det = detect.classify(meta)
    assert det.is_active is True
    assert det.playlist_id == "PL_NIRVANA_HITS"

    # URL without list=
    meta_plain = detect.PlayerMetadata(
        artist="Nirvana",
        title="Smells Like Teen Spirit",
        url="https://music.youtube.com/watch?v=hTWKbfoikeg",
        player="chromium",
    )
    det_plain = detect.classify(meta_plain)
    assert det_plain.is_active is True
    assert det_plain.playlist_id == ""


# --- 4. Remote Track Addition (Two-Way Sync) -------------------------------

def test_add_track_to_ytmusic_playlist():
    client = MagicMock(spec=YTMusicClient)
    client.is_authenticated = True
    client.add_playlist_items.return_value = "STATUS_ADD_ITEMS"

    # Addition with known video_id
    ok, res = yp.add_track_to_ytmusic_playlist(
        "PL_TEST",
        "Queen",
        "Bohemian Rhapsody",
        video_id="fJ9rUzIMcZQ",
        client=client,
    )
    assert ok is True
    assert res == "fJ9rUzIMcZQ"
    client.add_playlist_items.assert_called_once_with(
        "PL_TEST", ["fJ9rUzIMcZQ"], duplicates=False
    )

    # Addition without video_id -> resolves via yt.search_track
    client.reset_mock()
    client.is_authenticated = True
    client.search_track.return_value = "RESOLVED_VID"
    ok, res = yp.add_track_to_ytmusic_playlist(
        "PL_TEST",
        "Queen",
        "Don't Stop Me Now",
        client=client,
    )
    assert ok is True
    assert res == "RESOLVED_VID"
    client.add_playlist_items.assert_called_once_with(
        "PL_TEST", ["RESOLVED_VID"], duplicates=False
    )


# --- 5. Remote Playlist Reconciliation -------------------------------------

def test_reconcile_playlist_with_remote(tmp_path):
    c = _conn(tmp_path)
    client = MagicMock(spec=YTMusicClient)

    local_tracks = [
        {"position": 1, "artist": "Oasis", "title": "Wonderwall", "video_id": "6hzrDeceEKc"},
        {"position": 2, "artist": "Oasis", "title": "Don't Look Back In Anger", "video_id": "r8OipmKFDeM"},
    ]
    localcache.save_playlist(
        "PL_OASIS",
        "Karaoke: Oasis",
        search_query="oasis",
        tracks=local_tracks,
        conn=c,
    )

    # Remote has a 3rd track added (e.g. from YouTube Music mobile app)
    client.get_playlist_tracks.return_value = [
        {"artist": "Oasis", "title": "Wonderwall", "videoId": "6hzrDeceEKc"},
        {"artist": "Oasis", "title": "Don't Look Back In Anger", "videoId": "r8OipmKFDeM"},
        {"artist": "Oasis", "title": "Champagne Supernova", "videoId": "tI-5uv4wryI"},
    ]

    reconciled, has_drift = yp.reconcile_playlist_with_remote(
        "PL_OASIS",
        local_tracks,
        client=client,
        conn=c,
    )

    assert has_drift is True
    assert len(reconciled) == 3
    assert reconciled[2]["title"] == "Champagne Supernova"
    assert reconciled[2]["video_id"] == "tI-5uv4wryI"

    # Verify updated in SQLite
    updated_saved = localcache.get_saved_playlist_tracks("PL_OASIS", conn=c)
    assert len(updated_saved) == 3
    assert updated_saved[2]["title"] == "Champagne Supernova"
