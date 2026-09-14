"""Tests for Chromecast playback behavior, dual-audio suppression, and queue stall avoidance."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from karaoke import player_open
from karaoke.tui import KaraokeTui


def test_playback_js_checks_cast_and_sets_muted():
    js = player_open._PLAYBACK_JS
    assert "castConnectionState" in js
    assert "isCasting" in js
    assert "v.muted = true" in js
    assert "casting: isCasting" in js
    assert "muted: !!v.muted" in js


def test_dismiss_dialogs_js_guards_against_unpause_when_casting():
    js = player_open._DISMISS_DIALOGS_JS
    lines = js.split("\n")
    keyword_lines = [l for l in lines if "understand" in l or "begrijp" in l or "toch afspelen" in l]
    assert any("toch afspelen" in l for l in keyword_lines)
    for l in keyword_lines:
        assert "'afspelen'" not in l
        assert '"afspelen"' not in l

    assert "isCasting" in js
    assert "!isCasting" in js
    assert "v.play()" in js


def test_browser_playback_skips_dismiss_when_casting():
    with patch("karaoke.player_open._cdp_send") as mock_send, \
         patch("karaoke.player_open.dismiss_kiosk_dialogs") as mock_dismiss:
        mock_send.return_value = {
            "result": {
                "result": {
                    "value": '{"present": true, "paused": true, "readyState": 0, "casting": true}'
                }
            }
        }
        res = player_open.browser_playback(timeout=0.1)
        assert res["casting"] is True
        mock_dismiss.assert_not_called()


def test_browser_playback_allows_dismiss_when_not_casting():
    player_open._last_kiosk_dismiss = 0.0
    with patch("karaoke.player_open._cdp_send") as mock_send, \
         patch("karaoke.player_open.dismiss_kiosk_dialogs") as mock_dismiss:
        mock_send.return_value = {
            "result": {
                "result": {
                    "value": '{"present": true, "paused": true, "readyState": 0, "casting": false}'
                }
            }
        }
        res = player_open.browser_playback(timeout=0.1)
        assert res["casting"] is False
        mock_dismiss.assert_called_once()


def test_tui_watch_queue_respects_casting_state():
    app = KaraokeTui.__new__(KaraokeTui)
    app._play_once = True
    app._queue = [{"artist": "Artist", "title": "Song", "url": "https://music.youtube.com/watch?v=123"}]
    app._queue_at = 0
    app._idle_since = 100.0
    app._last_finished_url = ""
    app._ytmusic_queue_follow = False

    # Case 1: Casting and track not finished -> resets _idle_since and returns (no stall skip)
    casting_state = {
        "present": True,
        "casting": True,
        "ended": False,
        "position": 0.0,
        "duration": 0.0,
        "readyState": 0,
        "url": "https://music.youtube.com/watch?v=123",
    }
    with patch("karaoke.tui.browser_playback", return_value=casting_state), \
         patch.object(app, "action_queue_next", create=True) as mock_next:
        app._watch_queue()
        assert app._idle_since == 0.0
        mock_next.assert_not_called()

    # Case 2: Casting with playlist ("list=") -> sets _ytmusic_queue_follow = True
    playlist_state = {
        "present": True,
        "casting": True,
        "ended": False,
        "position": 10.0,
        "duration": 180.0,
        "url": "https://music.youtube.com/watch?v=123&list=RDAMVM123",
    }
    with patch("karaoke.tui.browser_playback", return_value=playlist_state):
        app._watch_queue()
        assert app._ytmusic_queue_follow is True
