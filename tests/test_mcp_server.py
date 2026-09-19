"""Tests for the Model Context Protocol (MCP) server for the Karaoke AI DJ."""
from __future__ import annotations

import json
import pytest

from karaoke import mcp_server


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
