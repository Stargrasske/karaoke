"""Tests for the Karaoke Backend Operations / Admin TUI."""
from __future__ import annotations

from karaoke.admin_tui import KaraokeAdminApp


class _FakeLock:
    def __init__(self, name: str):
        self.name = name
        self.held = False

    def acquire(self, *, blocking: bool = False) -> bool:
        self.held = True
        return True

    def release(self) -> None:
        self.held = False


def _fake_pipeline_lock(monkeypatch):
    monkeypatch.setattr("karaoke.lockfile.ProcessLock", _FakeLock)


def test_admin_app_initialization(monkeypatch):
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._target_workers = 1
    assert app.TITLE == "Karaoke Platform Operations & Worker Control"
    assert app._target_workers == 1


def test_admin_app_scaling_actions(monkeypatch):
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._target_workers = 0
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)

    calls = []
    class FakeApi:
        ctrl_url = "http://127.0.0.1:8765"
        def _http_post(self, url, path, body):
            calls.append((path, body))
            return {"status": "ok", "target": body.get("target")}

    monkeypatch.setattr(app, "api", FakeApi(), raising=False)

    # 1. Start worker
    app.action_scale_up()
    assert app._target_workers == 1
    assert calls[0] == ("/api/workers/scale", {"target": 1})

    # 2. Stop worker
    app.action_scale_down()
    assert app._target_workers == 0
    assert calls[1] == ("/api/workers/scale", {"target": 0})


def test_admin_pipeline_actions_dispatch(monkeypatch):
    """The audio-processing controls launch background workers without raising."""
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._bg_busy = False
    app._bg_lock = None
    _fake_pipeline_lock(monkeypatch)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)

    started = []
    monkeypatch.setattr(app, "run_worker", lambda fn, **k: started.append(fn), raising=False)

    app.action_run_backfill()
    app._release_bg_lock()
    app.action_rebuild_vectors()
    app._release_bg_lock()
    app.action_analyse_recordings()
    app._release_bg_lock()

    # Each control notified the user and queued exactly one background worker.
    assert len(started) == 3
    assert all(callable(fn) for fn in started)
    assert len(notifications) == 3


def test_admin_whisper_align_dispatch(monkeypatch):
    """Whisper alignment control dispatches background worker when inputs are given."""
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._bg_busy = False
    app._bg_lock = None
    _fake_pipeline_lock(monkeypatch)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)

    started = []
    monkeypatch.setattr(app, "run_worker", lambda fn, **k: started.append(fn), raising=False)

    class FakeInput:
        def __init__(self, val):
            self.value = val

    def fake_query(selector, *a, **k):
        if selector == "#align-track-input":
            return FakeInput("62")
        return FakeInput("Some lyrics text")

    monkeypatch.setattr(app, "query_one", fake_query, raising=False)

    try:
        app.action_align_whisper()
        assert len(started) == 1
        assert callable(started[0])
        assert "Aligning lyrics for '62'" in notifications[0]
    finally:
        app._release_bg_lock()


def test_admin_single_operation_guard(monkeypatch):
    """A second audio task is refused while one is already running."""
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._bg_busy = False
    app._bg_lock = None
    _fake_pipeline_lock(monkeypatch)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append((msg, k)), raising=False)

    started = []
    monkeypatch.setattr(app, "run_worker", lambda fn, **k: started.append(fn), raising=False)

    try:
        # First rebuild acquires the lock and dispatches a worker.
        app.action_rebuild_vectors()
        assert len(started) == 1
        assert app._bg_busy is True

        # Second rebuild is refused (lock held) — no new worker, a warning fires.
        app.action_rebuild_vectors()
        assert len(started) == 1
        assert any(k.get("severity") == "warning" for _, k in notifications)
    finally:
        app._release_bg_lock()


def test_admin_task_status_helpers_update_visible_status(monkeypatch):
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    updates = []

    class FakeStatic:
        def update(self, message):
            updates.append(message)

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeStatic(), raising=False)
    app._current_task_label = None
    app._current_task_started_at = None

    app._mark_task_started("Vector rebuild")
    assert app._current_task_label == "Vector rebuild"
    assert "RUNNING" in updates[-1]

    app._mark_task_finished("Vector rebuild", "Vector indices updated")
    assert app._current_task_label is None
    assert "DONE" in updates[-1]


def test_admin_format_event_line_includes_state_task_and_id():
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    line = app._format_event_line({
        "ts": 1788800803.0,
        "state": "SUCCESS",
        "task_name": "karaoke.tasks.postprocess_track",
        "task_id": "abcdef1234567890",
    })
    assert "SUCCESS" in line
    assert "karaoke.tasks.postprocess_track" in line
    assert "abcdef123456" in line


def test_admin_folder_scan_progress_formatter_escapes_paths():
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    line = app._folder_scan_progress_line("item_start", {
        "index": 1,
        "total": 5,
        "name": "Die_Antwoord -2010 - 5 [EP]/01. Enter The Ninja.mp3",
    })
    assert "[1/5]" in line
    assert "Scanning" in line
    assert "\\[EP]" in line


def test_admin_folder_scan_done_formatter_reports_counts():
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    line = app._folder_scan_progress_line("done", {
        "seen": 5,
        "processed": 5,
        "classified": 4,
        "sourced": 3,
        "errors": 0,
    })
    assert "seen=5" in line
    assert "processed=5" in line
    assert "errors=0" in line


def test_admin_recording_status_detects_recording(monkeypatch):
    """refresh_record_status recognizes active recording from 'recording' key."""
    from karaoke.admin_tui import KaraokeAdminApp

    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._record_tick = 0
    app._recording_id = None

    class FakeButton:
        def __init__(self):
            self.label = ""
            self.variant = ""

    class FakePanel:
        def __init__(self):
            self.classes = set()
            self.content = ""
        def set_class(self, val, cls):
            if val: self.classes.add(cls)
            else: self.classes.discard(cls)
        def update(self, c):
            self.content = c

    btn = FakeButton()
    panel = FakePanel()

    def fake_query_one(selector, *a, **k):
        if selector == "#btn-record":
            return btn
        if selector == "#record-panel":
            return panel
        raise ValueError(f"Unknown selector {selector}")

    monkeypatch.setattr(app, "query_one", fake_query_one, raising=False)

    class FakeApi:
        def record_status(self):
            return {
                "recording": [
                    {
                        "recording_id": 42,
                        "status": "recording",
                        "elapsed_s": 125.0,
                        "identified": 3,
                        "marks": 5,
                        "audio_bytes": 1000000,
                        "source": "alsa.monitor",
                    }
                ],
                "count": 1,
            }

    monkeypatch.setattr(app, "api", FakeApi(), raising=False)
    app.refresh_record_status()

    assert app._recording_id == 42
    assert "Stop Record #42" in btn.label
    assert btn.variant == "error"
    assert "-on" in panel.classes
    assert "REC 42" in panel.content


def test_admin_browse_selected_recording_modal_push(monkeypatch):
    """B key or row selection pushes RecordingBrowseModal."""
    from karaoke.admin_tui import KaraokeAdminApp, RecordingBrowseModal

    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._recording_id = 17
    app._recording_rows = {"row-1": 17}

    screens = []
    monkeypatch.setattr(app, "push_screen", lambda scr: screens.append(scr), raising=False)

    app.action_browse_selected_recording()
    assert len(screens) == 1
    assert isinstance(screens[0], RecordingBrowseModal)
    assert screens[0].recording_id == 17

