"""Tests for the stage / TV prompter view and SSE streaming endpoint."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from karaoke.stage_view import get_stage_state, render_stage_html, stage_event_stream


@pytest.fixture()
def client():
    from karaoke.ctrl_api import app
    return TestClient(app)


def test_render_stage_html():
    html_text = render_stage_html()
    assert "<!DOCTYPE html>" in html_text
    assert "Karaoke Stage View" in html_text
    assert 'id="rhythm-bar"' in html_text
    assert 'id="lyrics-container"' in html_text
    assert 'id="line-active"' in html_text
    assert 'id="line-next"' in html_text
    assert 'id="up-next-track"' in html_text
    assert 'id="cast-badge"' in html_text
    assert "/api/stage/stream" in html_text
    assert "EventSource" in html_text


def test_get_stage_state_idle():
    with patch("karaoke.player_open.browser_playback", return_value=None), \
         patch("karaoke.playerctl.playing_player", return_value=None), \
         patch("karaoke.localcache.load_active_queue", return_value=None):
        state = get_stage_state()
        assert state["artist"] == ""
        assert state["title"] == ""
        assert state["status"] == "Stopped"
        assert state["casting"] is False
        assert state["lines"] == []
        assert state["upcoming_queue"] == []
        assert state["active_line_index"] == -1
        assert state["episode"]["kind"] == "unknown"


def test_get_stage_state_with_playback_and_lyrics():
    fake_playback = {
        "present": True,
        "position": 15.5,
        "duration": 200.0,
        "casting": True,
        "paused": False,
        "url": "https://music.youtube.com/watch?v=xyz",
    }
    fake_meta = MagicMock(artist="Queen", title="Bohemian Rhapsody", album="A Night at the Opera", url=None, duration=None)

    raw_lrc = (
        "[00:10.00]Is this the real life?\n"
        "[00:15.00]Is this just fantasy?\n"
        "[00:20.00]Caught in a landslide\n"
    )

    fake_ly = MagicMock()
    fake_ly.synced_raw = raw_lrc
    fake_ly.lines = [(10.0, "Is this the real life?"), (15.0, "Is this just fantasy?"), (20.0, "Caught in a landslide")]

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {
        "bpm": 72.0,
        "detected_key": "Bb Major",
        "energy": 0.65,
    }

    fake_queue = {
        "current_index": 0,
        "rows": [
            {"artist": "Queen", "title": "Bohemian Rhapsody"},
            {"artist": "Queen", "title": "Don't Stop Me Now"},
        ],
    }

    with patch("karaoke.player_open.browser_playback", return_value=fake_playback), \
         patch("karaoke.playerctl.playing_player", return_value="chromium"), \
         patch("karaoke.playerctl.current_metadata", return_value=fake_meta), \
         patch("karaoke.playerctl.status", return_value="Playing"), \
         patch("karaoke.playerctl.art_url", return_value="https://art.example/cover.jpg"), \
         patch("karaoke.localcache.connect") as mock_conn, \
         patch("karaoke.localcache.find_track_id", return_value=123), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=fake_ly), \
         patch("karaoke.track_analysis.ensure_schema"), \
         patch("karaoke.localcache.load_active_queue", return_value=fake_queue):

        mock_conn.return_value.__enter__.return_value = fake_conn

        state = get_stage_state()
        assert state["artist"] == "Queen"
        assert state["title"] == "Bohemian Rhapsody"
        assert state["album"] == "A Night at the Opera"
        assert state["position_s"] == 15.5
        assert state["duration"] == 200.0
        assert state["casting"] is True
        assert state["status"] == "Playing"
        assert state["bpm"] == 72.0
        assert state["key"] == "Bb Major"
        assert len(state["lines"]) >= 3
        # At 15.5s, line 1 ([00:15.00] "Is this just fantasy?") is active
        assert state["active_line_index"] == 1
        assert len(state["upcoming_queue"]) == 1
        assert state["upcoming_queue"][0]["title"] == "Don't Stop Me Now"


def test_get_stage_state_uses_browser_metadata_without_mpris():
    fake_playback = {
        "present": True,
        "position": 15.5,
        "duration": 200.0,
        "casting": False,
        "paused": False,
        "url": "https://music.youtube.com/watch?v=xyz",
        "artist": "Queen",
        "title": "Bohemian Rhapsody",
        "album": "A Night at the Opera",
        "artUrl": "https://art.example/cover.jpg",
    }
    fake_ly = MagicMock(
        synced_raw="[00:10.00]First line\n[00:15.00]Active line\n[00:20.00]Next line",
        lines=[(10.0, "First line"), (15.0, "Active line"), (20.0, "Next line")],
    )
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None

    with patch("karaoke.player_open.browser_playback", return_value=fake_playback), \
         patch("karaoke.playerctl.playing_player", return_value=""), \
         patch("karaoke.localcache.connect") as mock_conn, \
         patch("karaoke.localcache.find_track_id", return_value=123), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=fake_ly), \
         patch("karaoke.track_analysis.ensure_schema"), \
         patch("karaoke.localcache.load_active_queue", return_value=None):
        mock_conn.return_value.__enter__.return_value = fake_conn
        state = get_stage_state()

    assert state["artist"] == "Queen"
    assert state["title"] == "Bohemian Rhapsody"
    assert state["position_s"] == 15.5
    assert state["active_line_index"] == 1


def test_get_stage_state_uses_recorder_detection_without_player():
    mark = MagicMock(
        ok=True,
        artist="The Offspring",
        title="Self Esteem",
        start_estimate=100.0,
    )
    fake_ly = MagicMock(
        synced_raw="[00:10.00]First line\n[00:15.00]Active line\n[00:20.00]Next line",
        lines=[(10.0, "First line"), (15.0, "Active line"), (20.0, "Next line")],
    )
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None

    with patch("karaoke.player_open.browser_playback", return_value=None), \
         patch("karaoke.playerctl.playing_player", return_value=""), \
         patch("karaoke.recorder.active_sessions", return_value=[4]), \
         patch("karaoke.recorder.load_marks", return_value=[mark]), \
         patch("karaoke.stage_view.time.time", return_value=115.5), \
         patch("karaoke.localcache.connect") as mock_conn, \
         patch("karaoke.localcache.find_track_id", return_value=1), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=fake_ly), \
         patch("karaoke.track_analysis.ensure_schema"), \
         patch("karaoke.localcache.load_active_queue", return_value=None):
        mock_conn.return_value.__enter__.return_value = fake_conn
        state = get_stage_state()

    assert state["artist"] == "The Offspring"
    assert state["title"] == "Self Esteem"
    assert state["position_s"] == 15.5
    assert state["active_line_index"] == 1


def test_lyric_episode_marks_long_gap_as_instrumental():
    from karaoke.stage_view import _lyric_episode

    lines = [
        {"index": 0, "time": 10.0, "end": None, "text": "A short line", "words": []},
        {"index": 1, "time": 40.0, "end": None, "text": "Vocals return", "words": []},
    ]
    assert _lyric_episode(lines, 5.0)["kind"] == "intro"
    assert _lyric_episode(lines, 11.0)["kind"] == "vocals"
    episode = _lyric_episode(lines, 25.0)
    assert episode["kind"] == "instrumental"
    assert episode["label"] == "Instrumental break"
    assert _lyric_episode(lines, 41.0)["kind"] == "vocals"


def test_stage_event_stream_generator():
    async def go():
        gen = stage_event_stream(interval=0.001)
        chunk = await anext(gen)
        assert chunk.startswith("data: ")
        assert chunk.endswith("\n\n")
        data = json.loads(chunk[6:-2])
        assert "timestamp" in data
        assert "lines" in data

    asyncio.run(go())


def test_ctrl_api_stage_endpoints(client):
    r_stage = client.get("/stage")
    assert r_stage.status_code == 200
    assert "text/html" in r_stage.headers["content-type"]
    assert "Karaoke Stage View" in r_stage.text

    r_tv = client.get("/tv")
    assert r_tv.status_code == 200
    assert "text/html" in r_tv.headers["content-type"]
    assert "Karaoke Stage View" in r_tv.text

    r_state = client.get("/api/stage/state")
    assert r_state.status_code == 200
    data = r_state.json()
    assert "lines" in data
    assert "casting" in data
    assert "status" in data
