"""Tests for the Model Context Protocol (MCP) server for the Karaoke AI DJ."""
from __future__ import annotations

import inspect
import json
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
