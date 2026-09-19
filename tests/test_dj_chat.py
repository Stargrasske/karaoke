"""Tests for the Karaoke AI DJ Chat client and Textual screen."""
from __future__ import annotations

import json
import pytest

from karaoke.dj_chat import DJChatSession, DJChatScreen, StandaloneDJChatApp
from karaoke.tui import KaraokeTui


def test_dj_chat_session_help():
    session = DJChatSession()
    reply = session.ask("/help")
    assert "Karaoke AI DJ Commands" in reply
    assert "/now" in reply
    assert "/suggest" in reply
    assert "/vibe" in reply
    assert "/stats" in reply


def test_dj_chat_session_stats():
    session = DJChatSession()
    reply = session.ask("/stats")
    assert "Karaoke Crowd & DJ Stats" in reply
    assert "Total Library Events" in reply


def test_dj_chat_session_search():
    session = DJChatSession()
    reply = session.ask("/search queen")
    assert "Found" in reply or "No tracks found" in reply


def test_dj_chat_session_model_switch():
    session = DJChatSession()
    reply = session.ask("/model qwen3:0.6b")
    assert "qwen3:0.6b" in reply
    assert session.model == "qwen3:0.6b"


def test_karaoke_tui_has_dj_binding():
    """Verify that 'D' is registered in KaraokeTui bindings."""
    keys = [b[0] if isinstance(b, tuple) else b.key for b in KaraokeTui.BINDINGS]
    assert "D" in keys
    actions = [b[1] if isinstance(b, tuple) else b.action for b in KaraokeTui.BINDINGS]
    assert "ai_dj_chat" in actions


@pytest.mark.anyio
async def test_dj_chat_screen_mount():
    """Verify that the DJChatScreen composes and mounts cleanly in Textual."""
    app = StandaloneDJChatApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Check that chat log, input, and buttons exist on the active modal screen
        assert pilot.app.screen.query_one("#chat-log") is not None
        assert pilot.app.screen.query_one("#chat-input") is not None
        assert pilot.app.screen.query_one("#btn-now") is not None
        assert pilot.app.screen.query_one("#btn-suggest") is not None
        assert pilot.app.screen.query_one("#btn-add-1") is not None
        assert pilot.app.screen.query_one("#btn-add-all") is not None
        assert pilot.app.screen.query_one("#btn-list") is not None
        assert pilot.app.screen.query_one("#btn-follow") is not None
        assert pilot.app.screen.query_one("#btn-clear-dj") is not None
        # Press escape to dismiss
        await pilot.press("escape")


@pytest.fixture
def sample_tracks():
    """Populate test database with mock songs for harmonic suggestion testing."""
    import time
    from karaoke import localcache
    now = time.time()
    with localcache.connect() as conn:
        cur = conn.cursor()
        tracks_data = [
            (101, "Queen", "Bohemian Rhapsody", "C minor", 143.6),
            (102, "The Killers", "Mr. Brightside", "C minor", 148.0),
            (103, "Bon Jovi", "Livin' on a Prayer", "E minor", 123.0),
            (104, "Modjo", "Lady", "A# minor", 128.0),
        ]
        for tid, artist, title, key, bpm in tracks_data:
            cur.execute(
                "INSERT INTO tracks (track_id, artist, title, album, duration, play_count) VALUES (%s, %s, %s, '', 200, 10)",
                (tid, artist, title),
            )
            cur.execute(
                "INSERT INTO track_analysis (track_id, detected_key, bpm, energy, updated_at) VALUES (%s, %s, %s, 0.8, %s)",
                (tid, key, bpm, now),
            )
            cur.execute(
                "INSERT INTO sources (source_id, track_id, kind, url) VALUES (%s, %s, 'youtube_music', %s)",
                (tid, tid, f"https://music.youtube.com/watch?v=mock_{tid}"),
            )


def test_dj_chat_session_playlist_workflow(sample_tracks):
    """Test full cycle: suggest -> numeric 1 queue -> view dj-list -> clear dj-list."""
    session = DJChatSession()
    # 1. Clear any prior playlist state
    session.ask("/clear-dj")

    # 2. Get suggestions
    suggest_reply = session.ask("/suggest")
    assert "DJ Follow-Up Suggestions" in suggest_reply
    assert len(session.last_candidates) > 0

    # 3. Queue suggestion #1 by just typing '1'
    add_reply = session.ask("1")
    assert "Added" in add_reply
    assert "Karaoke: DJ List" in add_reply or "dj-list" in add_reply

    # 4. View playlist contents
    list_reply = session.ask("/dj-list")
    assert "Karaoke: DJ List" in list_reply or "dj-list" in list_reply
    assert "1 track" in list_reply

    # 5. Clear playlist
    clear_reply = session.ask("/clear-dj")
    assert "cleared" in clear_reply


def test_dj_chat_session_queue_all(sample_tracks):
    """Test queuing all suggestions at once via 'all'."""
    session = DJChatSession()
    session.ask("/clear-dj")
    session.ask("/suggest")
    count = len(session.last_candidates)

    reply = session.ask("all")
    assert f"Added {count} song(s)" in reply

    list_reply = session.ask("/dj-list")
    assert f"{count} track" in list_reply

    session.ask("/clear-dj")


def test_dj_chat_vibe_for_current_playing_song(sample_tracks):
    """Verify that pressing '✨ Lyric Vibe' or running /vibe automatically analyzes the currently playing song."""
    # 1. Test with current_track_provider (as passed by TUI live player)
    session1 = DJChatSession(current_track_provider=lambda: 101)
    reply1 = session1.ask("/vibe")
    assert "Vibe & Lyric Analysis" in reply1
    assert "Dominant Mood" in reply1
    assert "Singing Delivery Tips" in reply1
    assert "Sentiment Spectrum" in reply1
    assert "Queen" in reply1 or "Bohemian Rhapsody" in reply1

    # 2. Test natural 'lyric vibe' command
    session2 = DJChatSession(current_track_id=102)
    reply2 = session2.ask("lyric vibe")
    assert "Vibe & Lyric Analysis" in reply2
    assert "The Killers" in reply2 or "Mr. Brightside" in reply2

    # 3. Test screen initialization with current_track_provider
    screen = DJChatScreen(current_track_provider=lambda: 101, initial_prompt="/vibe")
    assert screen.session.current_track_provider is not None
    assert screen.initial_prompt == "/vibe"

