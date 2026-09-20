"""Tests for the Model Context Protocol (MCP) server for the Karaoke AI DJ."""
from __future__ import annotations

import inspect
import json
import time
import pytest

from karaoke import mcp_server, player_open


def test_mcp_server_definition():
    """Verify the MCP server is properly constructed with tools."""
    assert mcp_server.dj_mcp.name == "karaoke-dj"
    tool_names = [t.name for t in mcp_server.dj_mcp._tool_manager.list_tools()]
    assert "search_songs" in tool_names
    assert "get_now_playing" in tool_names
    assert "suggest_next_tracks" in tool_names
    assert "analyze_lyric_vibe" in tool_names
    assert "get_dj_stats" in tool_names
    assert "play_track" in tool_names


def test_search_songs():
    """Verify search_songs returns structured JSON."""
    res_str = mcp_server.search_songs(query="", limit=3)
    data = json.loads(res_str)
    assert "count" in data
    assert "tracks" in data
    assert isinstance(data["tracks"], list)
    if data["tracks"]:
        track = data["tracks"][0]
        assert "track_id" in track
        assert "artist" in track
        assert "title" in track
        assert "camelot" in track


def test_search_songs_by_key():
    """Verify search_songs filters by key/Camelot code."""
    res_str = mcp_server.search_songs(key="8A", limit=2)
    data = json.loads(res_str)
    for track in data["tracks"]:
        assert track["camelot"] == "8A"


def test_get_now_playing():
    """Verify get_now_playing handles desktop media probe gracefully."""
    res_str = mcp_server.get_now_playing()
    data = json.loads(res_str)
    assert data["status"] in ("playing", "idle")


def test_suggest_next_tracks():
    """Verify suggest_next_tracks harmonic flow recommendation."""
    # Search for an analyzed track first to act as a seed
    search_res = json.loads(mcp_server.search_songs(limit=10))
    seed = next((t for t in search_res["tracks"] if t.get("camelot") and t["camelot"] != "?"), None)
    if seed:
        res_str = mcp_server.suggest_next_tracks(track_id=seed["track_id"], strategy="harmonic", limit=3)
        data = json.loads(res_str)
        assert "seed" in data
        assert "suggestions" in data
        assert data["seed"]["track_id"] == seed["track_id"]


def test_analyze_lyric_vibe():
    """Verify analyze_lyric_vibe returns mood arc and tips."""
    search_res = json.loads(mcp_server.search_songs(only_synced_lyrics=True, limit=1))
    if search_res["tracks"]:
        track_id = search_res["tracks"][0]["track_id"]
        res_str = mcp_server.analyze_lyric_vibe(track_id)
        data = json.loads(res_str)
        assert "dominant_mood" in data
        assert "dj_singer_tips" in data


def test_get_dj_stats():
    """Verify get_dj_stats returns overall library health and top favorites."""
    res_str = mcp_server.get_dj_stats()
    data = json.loads(res_str)
    assert "plays" in data
    assert "top_tracks" in data
    assert "top_artists" in data


@pytest.fixture
def launcher(monkeypatch):
    """Stub open_song_url, recording calls and validating them for real.

    play_track imports open_song_url inside the function body, so the name
    must be patched on player_open rather than on mcp_server. Every captured
    call is bound against the genuine signature, so an argument list the real
    function would reject still raises TypeError here.
    """
    real_signature = inspect.signature(player_open.open_song_url)
    calls: list[inspect.BoundArguments] = []
    returns = []

    def fake_open_song_url(*args, **kwargs):
        calls.append(real_signature.bind(*args, **kwargs))
        return returns.pop(0) if returns else None

    monkeypatch.setattr(player_open, "open_song_url", fake_open_song_url)
    return calls, returns


@pytest.fixture
def playable_track() -> int:
    """Insert one track with a YouTube source and return its id.

    The autouse clear_db fixture empties every table before each test, so a
    test that needs a row has to create it.
    """
    from karaoke import localcache

    track_id = 4242
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tracks (track_id, artist, title, album, duration, play_count)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (track_id, "The Doors", "Wild Child", "The Soft Parade", 100.0, 0),
        )
        cur.execute(
            "INSERT INTO sources (source_id, track_id, kind, url) VALUES (%s, %s, %s, %s)",
            (1, track_id, "youtube", "https://www.youtube.com/watch?v=USzJmEUzq6M"),
        )
    return track_id


def test_play_track_passes_source_kind(launcher, playable_track):
    """play_track must supply the `kind` argument open_song_url requires."""
    calls, _ = launcher

    mcp_server.play_track(playable_track)

    assert len(calls) == 1
    bound = calls[0]
    bound.apply_defaults()
    # `kind` is positional-or-keyword and has no default: omitting it used to
    # raise TypeError before the call ever reached the player.
    assert "kind" in bound.arguments
    # Artist and title are what let YouTube Music resolve the canonical audio
    # track rather than a video edit with a different intro.
    assert bound.arguments["artist"]
    assert bound.arguments["title"]


def test_play_track_reports_launched_without_pid(launcher, playable_track):
    """A None return is the kiosk/CDP success path, not a failure."""
    calls, returns = launcher

    returns.append(None)
    data = json.loads(mcp_server.play_track(playable_track))

    assert data["status"] == "launched"
    assert data["pid"] is None
    assert len(calls) == 1


def test_play_track_surfaces_pid(launcher, playable_track):
    """The xdg-open path returns a pid, which callers should see."""
    _, returns = launcher

    returns.append(31337)
    data = json.loads(mcp_server.play_track(playable_track))

    assert data["status"] == "launched"
    assert data["pid"] == 31337


def test_play_track_unknown_track(launcher):
    """An unknown id reports an error and never reaches the player."""
    calls, _ = launcher

    data = json.loads(mcp_server.play_track(-1))

    assert "error" in data
    assert calls == []


def test_create_asgi_app_endpoints():
    """Verify create_asgi_app handles HEAD /sse, POST /sse (streamable HTTP), and /health."""
    from starlette.testclient import TestClient

    app = mcp_server.create_asgi_app()
    with TestClient(app) as client:
        # 1. Healthcheck
        r_health = client.get("/health")
        assert r_health.status_code == 200
        assert r_health.json() == {"status": "ok"}

        # 2. HEAD /sse (systemd readiness check)
        r_head = client.head("/sse")
        assert r_head.status_code == 200

        # 3. POST /sse (Streamable HTTP initialize sent by Obot)
        init_payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "obot-test", "version": "1.0"},
            },
        }
        r_post = client.post("/sse", json=init_payload)
        assert r_post.status_code == 200
        assert "capabilities" in r_post.text


def test_dj_playlist_tools(playable_track):
    """Verify add_to_dj_playlist, get_dj_playlist, and clear_dj_playlist tools."""
    # 1. Clear playlist initially
    r_clear = json.loads(mcp_server.clear_dj_playlist())
    assert r_clear["ok"] is True

    # 2. Add track by ID
    r_add = json.loads(mcp_server.add_to_dj_playlist(track_id=playable_track))
    assert r_add["ok"] is True
    assert r_add["playlist_id"] in ("dj-list", "PLeaH9ITPDGQo") or r_add["playlist_id"].startswith("PL")
    assert r_add["position"] == 1
    assert r_add["track"]["track_id"] == playable_track
    assert r_add["track"]["artist"] == "The Doors"

    # 3. Retrieve playlist
    r_get = json.loads(mcp_server.get_dj_playlist())
    assert r_get["playlist_id"] in ("dj-list", "PLeaH9ITPDGQo") or r_get["playlist_id"].startswith("PL")
    assert r_get["track_count"] == 1
    assert len(r_get["tracks"]) == 1
    assert r_get["tracks"][0]["title"] == "Wild Child"

    # 4. Clear playlist and verify empty
    r_clear2 = json.loads(mcp_server.clear_dj_playlist())
    assert r_clear2["ok"] is True
    r_get2 = json.loads(mcp_server.get_dj_playlist())
    assert r_get2["track_count"] == 0
    assert r_get2["tracks"] == []



def test_add_to_dj_playlist_is_idempotent(playable_track):
    """Re-adding a track reports it as present rather than duplicating it."""
    mcp_server.clear_dj_playlist()

    first = json.loads(mcp_server.add_to_dj_playlist(track_id=playable_track))
    assert first["ok"] is True
    assert not first.get("already_present")

    second = json.loads(mcp_server.add_to_dj_playlist(track_id=playable_track))
    assert second["ok"] is True
    assert second["already_present"] is True

    listing = json.loads(mcp_server.get_dj_playlist())
    assert listing["track_count"] == 1


def test_suggest_next_tracks_excludes_playlist_members(playable_track, monkeypatch):
    """A song already in the DJ list is never offered as the next track."""
    # Two analysed tracks that suggest_next_tracks can actually return.
    from karaoke import localcache

    with localcache.connect() as conn:
        cur = conn.cursor()
        for tid, title, bpm, key in (
            (5001, "Seed Song", 120.0, "A minor"),
            (5002, "Queued Song", 121.0, "A minor"),
            (5003, "Fresh Song", 122.0, "A minor"),
        ):
            cur.execute(
                "INSERT INTO tracks (track_id, artist, title, album, duration, play_count)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (tid, "Test Artist", title, "", 180.0, 0),
            )
            cur.execute(
                "INSERT INTO track_analysis (track_id, detected_key, bpm, energy, updated_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (tid, key, bpm, 0.5, time.time()),
            )

    mcp_server.clear_dj_playlist()
    mcp_server.add_to_dj_playlist(track_id=5002)

    data = json.loads(mcp_server.suggest_next_tracks(track_id=5001, limit=10))
    returned = [s["track_id"] for s in data["suggestions"]]
    assert 5002 not in returned, "queued song must not be suggested"
    assert 5003 in returned, "unqueued song should still be offered"

    # Opting out restores the old behaviour.
    allowed = json.loads(
        mcp_server.suggest_next_tracks(track_id=5001, limit=10, exclude_playlist="")
    )
    assert 5002 in [s["track_id"] for s in allowed["suggestions"]]


@pytest.fixture
def paging_library():
    """Ten analysed tracks, enough to page through."""
    from karaoke import localcache

    with localcache.connect() as conn:
        cur = conn.cursor()
        for n in range(10):
            tid = 6000 + n
            cur.execute(
                "INSERT INTO tracks (track_id, artist, title, album, duration, play_count)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (tid, f"Artist {n:02d}", f"Song {n:02d}", "", 180.0, 100 - n),
            )
            cur.execute(
                "INSERT INTO track_analysis (track_id, detected_key, bpm, energy, updated_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (tid, "A minor", 120.0, 0.5, time.time()),
            )
    return [6000 + n for n in range(10)]


def test_search_songs_pages_without_overlap(paging_library):
    first = json.loads(mcp_server.search_songs(limit=4, offset=0))
    second = json.loads(mcp_server.search_songs(limit=4, offset=4))

    ids1 = [t["track_id"] for t in first["tracks"]]
    ids2 = [t["track_id"] for t in second["tracks"]]
    assert len(ids1) == 4 and len(ids2) == 4
    assert not set(ids1) & set(ids2), "pages must not repeat tracks"
    assert second["offset"] == 4


def test_search_songs_reports_more_pages(paging_library):
    """has_more needs a row beyond the window, not just a full page.

    The first implementation stopped collecting exactly at the window, so
    has_more was always False even with results left.
    """
    early = json.loads(mcp_server.search_songs(limit=4, offset=0))
    assert early["has_more"] is True

    last = json.loads(mcp_server.search_songs(limit=4, offset=8))
    assert last["has_more"] is False


def test_search_songs_offset_is_clamped(paging_library):
    data = json.loads(mcp_server.search_songs(limit=3, offset=-5))
    assert data["offset"] == 0
    assert len(data["tracks"]) == 3
