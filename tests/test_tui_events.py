"""Unit tests for TUI platform event polling and reactive refresh."""
from __future__ import annotations

from unittest.mock import MagicMock
from karaoke.tui import KaraokeTui


def test_tui_initializes_event_timestamp():
    app = KaraokeTui.__new__(KaraokeTui)
    app._last_event_ts = 12345.0
    assert app._last_event_ts == 12345.0


def test_tui_handles_postprocess_event(monkeypatch):
    app = KaraokeTui.__new__(KaraokeTui)
    app._sync_key = ("some", "song")
    app._det = MagicMock(is_active=False)

    poll_detection_called = []
    load_songs_called = []
    notifications = []

    monkeypatch.setattr(app, "_poll_detection", lambda: poll_detection_called.append(True))
    monkeypatch.setattr(app, "load_songs", lambda: load_songs_called.append(True))
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg))

    events = [
        {"task_name": "karaoke.tasks.postprocess_track", "state": "SUCCESS", "ts": 100.0}
    ]
    app._handle_platform_events(events)

    assert app._sync_key is None
    assert len(poll_detection_called) == 1
    assert len(load_songs_called) == 1
    assert any("Track data updated" in n for n in notifications)


def test_tui_handles_queue_and_metadata_events(monkeypatch):
    app = KaraokeTui.__new__(KaraokeTui)
    app._sync_key = ("some", "song")
    app._det = MagicMock(is_active=True)

    queue_rendered = []
    poll_detection_called = []
    monkeypatch.setattr(app, "_render_queue", lambda: queue_rendered.append(True))
    monkeypatch.setattr(app, "_poll_detection", lambda: poll_detection_called.append(True))
    monkeypatch.setattr(app, "notify", lambda msg, **k: None)

    events = [
        {"task_name": "karaoke.playback.queue_advance", "state": "SUCCESS", "ts": 101.0},
        {"task_name": "karaoke.metadata.wikibase_link", "state": "SUCCESS", "ts": 102.0},
    ]
    app._handle_platform_events(events)

    assert len(queue_rendered) == 1
    assert len(poll_detection_called) == 1


def test_ytmusic_queue_follow_moves_highlight_by_video_id(monkeypatch):
    app = KaraokeTui.__new__(KaraokeTui)
    app._ytmusic_queue_follow = True
    app._queue_at = -1
    app._queue = [
        {"track_id": 1, "url": "https://music.youtube.com/watch?v=AAAAAAAAAAA"},
        {"track_id": 2, "url": "https://music.youtube.com/watch?v=BBBBBBBBBBB"},
    ]
    app._ytmusic_video_to_queue_index = app._queue_video_map()
    rendered = []
    monkeypatch.setattr(app, "_render_queue", lambda: rendered.append(app._queue_at))

    det = MagicMock(url="https://music.youtube.com/watch?v=BBBBBBBBBBB&list=PL_TEMP")
    app._sync_queue_to_detection(det, None)

    assert app._queue_at == 1
    assert rendered == [1]


def test_apply_restored_playlist_syncs_with_playing_song(monkeypatch):
    app = KaraokeTui.__new__(KaraokeTui)
    app.notify = MagicMock()
    app._render_queue = MagicMock()
    app._det = MagicMock(is_active=True, url="https://music.youtube.com/watch?v=BBBBBBBBBBB", title="Song Two")

    rows = [
        {"track_id": 1, "artist": "A", "title": "Song One", "url": "https://music.youtube.com/watch?v=AAAAAAAAAAA"},
        {"track_id": 2, "artist": "B", "title": "Song Two", "url": "https://music.youtube.com/watch?v=BBBBBBBBBBB"},
    ]

    from karaoke import playerctl
    monkeypatch.setattr(playerctl, "playing_player", lambda: "chromium")
    monkeypatch.setattr(playerctl, "status", lambda p: "Playing")

    app._apply_restored_playlist(rows, "PL_TEST", "Test PL")

    assert app._queue_at == 1
    assert app._ytmusic_queue_follow is True
    app._render_queue.assert_called_once()


def test_apply_restored_playlist_resumes_at_last_played_song(monkeypatch):
    app = KaraokeTui.__new__(KaraokeTui)
    app.notify = MagicMock()
    app._render_queue = MagicMock()
    app._det = MagicMock(is_active=False)

    rows = [
        {"track_id": 1, "artist": "A", "title": "Song One", "url": "https://music.youtube.com/watch?v=AAAAAAAAAAA"},
        {"track_id": 2, "artist": "B", "title": "Song Two", "url": "https://music.youtube.com/watch?v=BBBBBBBBBBB"},
    ]

    from karaoke import playerctl, localcache
    monkeypatch.setattr(playerctl, "playing_player", lambda: "")
    monkeypatch.setattr(localcache, "get_last_played_track", lambda: {"artist": "B", "title": "Song Two"})

    app._apply_restored_playlist(rows, "PL_TEST", "Test PL")

    assert app._queue_at == 1
    assert app._ytmusic_queue_follow is True
    app._render_queue.assert_called_once()


def test_action_queue_rollback_steps_back(monkeypatch):
    app = KaraokeTui.__new__(KaraokeTui)
    app.notify = MagicMock()
    app._render_queue = MagicMock()
    app._queue = [
        {"track_id": 1, "artist": "A", "title": "Song One"},
        {"track_id": 2, "artist": "B", "title": "Song Two"},
        {"track_id": 3, "artist": "C", "title": "Song Three"},
    ]
    app._queue_at = 2
    app._play_once = True
    played_indices = []
    app.play_queue_index = lambda idx: played_indices.append(idx)

    from karaoke import localcache
    monkeypatch.setattr(localcache, "get_recent_queue_events", lambda limit=15: [
        {"queue_index": 2},
        {"queue_index": 0},
    ])
    monkeypatch.setattr(localcache, "save_active_queue", lambda *a, **k: None)
    monkeypatch.setattr(localcache, "record_queue_event", lambda *a, **k: 1)

    app.action_queue_rollback()

    assert app._queue_at == 0
    assert played_indices == [0]

