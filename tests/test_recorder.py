"""Record mode capture: the session lifecycle and marker storage.

ffmpeg and songrec are faked throughout -- this covers the bookkeeping, not the
audio, which was verified separately against live playback.
"""
import subprocess
import time
from types import SimpleNamespace

import pytest

from karaoke import localcache, recorder


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    real = localcache.connect
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real(path))
    monkeypatch.setattr(recorder, "recordings_dir", lambda: tmp_path / "rec")
    
    # Pre-insert dummy recordings so foreign keys are satisfied
    c = localcache.connect(path)
    localcache.ensure_recording_tables(c)
    c.execute("INSERT INTO recordings (recording_id, started_at, ended_at, status, source, dir) VALUES (1, 0, 0, 'finished', '', '')")
    c.execute("INSERT INTO recordings (recording_id, started_at, ended_at, status, source, dir) VALUES (2, 0, 0, 'finished', '', '')")
    c.execute("ALTER SEQUENCE recordings_recording_id_seq RESTART WITH 3")
    c.commit()
    c.close()
    
    return path


class _FakeProc:
    def __init__(self, alive=True):
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


def _no_audio_loop(monkeypatch):
    """Stop the identification thread from doing anything real."""
    monkeypatch.setattr(recorder, "_identify_loop", lambda session: None)


def test_start_records_the_playing_monitor(db, monkeypatch):
    """Never a microphone: record mode captures the machine's own output."""
    _no_audio_loop(monkeypatch)
    monkeypatch.setattr(recorder.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr("karaoke.sample_audio.monitor_source",
                        lambda sink="": "bluez.monitor")
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(recorder.subprocess, "Popen", fake_popen)
    session = recorder.start()
    try:
        assert session.source == "bluez.monitor"
        assert "bluez.monitor" in seen["cmd"]
        assert "flac" in " ".join(seen["cmd"])
        assert "ebur128=peak=true:framelog=verbose" in seen["cmd"]
        assert session.directory.is_dir()
    finally:
        recorder.stop(session.recording_id)


def test_parse_momentary_audio_level():
    line = "[Parsed_ebur128_0] t: 2.9 M: -24.7 S: -30.2 I: -28.0 LUFS"
    assert recorder._parse_level(line) == pytest.approx(-24.7)
    assert recorder._parse_level("M: -inf S: -inf") is None


def test_start_refuses_when_nothing_is_playing(db, monkeypatch):
    monkeypatch.setattr(recorder.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr("karaoke.sample_audio.monitor_source", lambda sink="": "")
    with pytest.raises(recorder.RecorderError, match="nothing is playing"):
        recorder.start()


def test_start_reports_a_missing_ffmpeg(db, monkeypatch):
    monkeypatch.setattr(recorder.shutil, "which", lambda n: None)
    with pytest.raises(recorder.RecorderError, match="ffmpeg"):
        recorder.start()


def test_stop_closes_the_row_and_finalises_the_segment(db, monkeypatch):
    """SIGTERM, not kill: ffmpeg must finalise the FLAC it is writing."""
    _no_audio_loop(monkeypatch)
    monkeypatch.setattr(recorder.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr("karaoke.sample_audio.monitor_source",
                        lambda sink="": "x.monitor")
    proc = _FakeProc()
    monkeypatch.setattr(recorder.subprocess, "Popen", lambda cmd, **kw: proc)

    session = recorder.start()
    recorder.stop(session.recording_id)

    assert proc.terminated is True
    assert not recorder.is_running(session.recording_id)
    with localcache.connect() as c:
        row = c.execute("SELECT status, ended_at FROM recordings WHERE recording_id=%s",
                        (session.recording_id,)).fetchone()
    assert row["status"] == "complete" and row["ended_at"] is not None


# -- markers ------------------------------------------------------------

def test_a_successful_identification_is_stored(db):
    ref = SimpleNamespace(artist="Portishead", title="Glory Box", offset=30.0)
    recorder.add_mark(1, ref)
    marks = recorder.load_marks(1)
    assert len(marks) == 1
    assert marks[0].ok and marks[0].title == "Glory Box"
    assert marks[0].at_offset == 30.0
    # Less the recognition lead: the offset describes the sampled audio, not
    # the instant the answer was written.
    from karaoke.recording_slice import RECOGNITION_LEAD_S

    assert marks[0].start_estimate == pytest.approx(
        marks[0].at_wall - 30.0 - RECOGNITION_LEAD_S)


def test_a_failed_identification_is_stored_too(db):
    """A gap is evidence about the recording, not something to discard."""
    recorder.add_mark(1, None)
    marks = recorder.load_marks(1)
    assert len(marks) == 1 and marks[0].ok is False


def test_an_empty_title_counts_as_a_failure(db):
    recorder.add_mark(1, SimpleNamespace(artist="", title="", offset=None))
    assert recorder.load_marks(1)[0].ok is False


def test_mark_count_separates_hits_from_attempts(db):
    ref = SimpleNamespace(artist="A", title="B", offset=1.0)
    recorder.add_mark(1, ref)
    recorder.add_mark(1, None)
    recorder.add_mark(1, ref)
    assert recorder.mark_count(1) == (2, 3)


def test_marks_come_back_in_time_order(db):
    ref = SimpleNamespace(artist="A", title="B", offset=1.0)
    for _ in range(3):
        recorder.add_mark(1, ref)
        time.sleep(0.01)
    marks = recorder.load_marks(1)
    assert [m.at_wall for m in marks] == sorted(m.at_wall for m in marks)


def test_marks_of_other_recordings_are_not_returned(db):
    ref = SimpleNamespace(artist="A", title="B", offset=1.0)
    recorder.add_mark(1, ref)
    recorder.add_mark(2, ref)
    assert len(recorder.load_marks(1)) == 1


# -- limits -------------------------------------------------------------

def test_a_session_over_its_time_cap_is_stopped(db, monkeypatch):
    session = recorder.Session(
        recording_id=1, directory=db.parent, source="x",
        process=_FakeProc(), stop=__import__("threading").Event(),
        started_mono=time.monotonic() - (recorder.MAX_HOURS * 3600.0 + 10))
    assert recorder._over_limit(session) is True


def test_a_session_over_its_size_cap_is_stopped(db, monkeypatch):
    monkeypatch.setattr(recorder, "directory_size",
                        lambda d: recorder.MAX_BYTES + 1)
    session = recorder.Session(
        recording_id=1, directory=db.parent, source="x",
        process=_FakeProc(), stop=__import__("threading").Event(),
        started_mono=time.monotonic())
    assert recorder._over_limit(session) is True


def test_a_fresh_session_is_within_limits(db, monkeypatch):
    monkeypatch.setattr(recorder, "directory_size", lambda d: 1024)
    session = recorder.Session(
        recording_id=1, directory=db.parent, source="x",
        process=_FakeProc(), stop=__import__("threading").Event(),
        started_mono=time.monotonic())
    assert recorder._over_limit(session) is False


# -- the TUI key --------------------------------------------------------

def test_record_is_bound_to_O():
    from karaoke.tui import binding_rows
    from karaoke.admin_tui import KaraokeAdminApp
    assert dict(binding_rows(KaraokeAdminApp.BINDINGS))["O"] == "Toggle Live Recording"


# --- the sidebar indicator -------------------------------------------------

def test_short_source_keeps_both_informative_ends():
    """The head says which device, ".monitor" says it is not a microphone."""
    out = recorder_panel_helpers_short(
        "bluez_output.1C_5E_82_70_03_6F.1.monitor", 26)
    assert out.startswith("bluez_output")
    assert out.endswith(".monitor")
    assert len(out) <= 26


def recorder_panel_helpers_short(name, width):
    from karaoke.tui import short_source
    return short_source(name, width)


def test_short_source_leaves_a_short_name_alone():
    from karaoke.tui import short_source
    assert short_source("mic.source", 26) == "mic.source"


def test_short_source_handles_a_non_monitor_name():
    from karaoke.tui import short_source
    out = short_source("alsa_input.pci-0000_c1_00.6.HiFi__Mic2__source", 20)
    assert len(out) <= 20 and "…" in out


def test_record_panel_shows_the_running_state():
    from karaoke.tui import record_panel
    text = record_panel(recording_id=3, elapsed_s=201, marks_ok=4,
                        marks_total=5, size_bytes=22_400_000,
                        source="bluez_output.x.monitor")
    assert "REC 3" in text
    assert "03:21" in text          # elapsed as mm:ss
    assert "4/5" in text
    assert "22 MB" in text
    assert ".monitor" in text


def test_record_panel_shows_hours_when_long():
    from karaoke.tui import record_panel
    assert "2:02:05" in record_panel(recording_id=1, elapsed_s=7325)


def test_record_panel_blink_alternates_the_dot():
    """A still display gives no sign that capture is actually alive."""
    from karaoke.tui import record_panel
    on = record_panel(recording_id=1, blink=True)
    off = record_panel(recording_id=1, blink=False)
    assert on.startswith("●") and off.startswith("○")


def test_record_panel_labels_line_up():
    """ASCII labels in a fixed column, so nothing shifts as numbers change."""
    from karaoke.tui import record_panel
    rows = record_panel(recording_id=1, marks_ok=1, marks_total=2,
                        size_bytes=1_000_000, source="x.monitor").splitlines()[1:]
    starts = {len(r) - len(r.lstrip()) for r in rows}
    values = [r.split()[1] for r in rows]
    assert starts == {0}
    assert all(r.index(v) == rows[0].index(values[0]) for r, v in zip(rows, values))


def test_record_panel_omits_an_unknown_source():
    from karaoke.tui import record_panel
    assert "src" not in record_panel(recording_id=1, source="")


def test_session_source_and_elapsed_are_none_when_not_running():
    assert recorder.session_source(9999) is None
    assert recorder.elapsed(9999) is None
    assert recorder.session_directory(9999) is None


# --- surviving an unclean exit ---------------------------------------------

def test_reconcile_closes_a_row_left_recording(db):
    """The orphan case: ffmpeg outlived the TUI, the row never closed.

    Observed live -- a capture ran unparented for 1h51m while a second TUI
    started another on the same source.
    """
    with localcache.connect() as c:
        c.execute("INSERT INTO recordings (recording_id, started_at, source, dir,"
                  " status) VALUES (7, 1000.0, 'x.monitor', '/tmp/x', 'recording')")
        c.commit()

    assert recorder.reconcile_stale() == [7]

    with localcache.connect() as c:
        row = c.execute("SELECT status, ended_at, note FROM recordings"
                        " WHERE recording_id = 7").fetchone()
    assert row["status"] == "complete"
    assert row["ended_at"] is not None
    assert "not running" in (row["note"] or "")


def test_reconcile_leaves_a_genuinely_running_session_alone(db, monkeypatch):
    """Only rows with no live session are stale."""
    with localcache.connect() as c:
        c.execute("INSERT INTO recordings (recording_id, started_at, source, dir,"
                  " status) VALUES (8, 1000.0, 'x.monitor', '/tmp/x', 'recording')")
        c.commit()
    monkeypatch.setattr(recorder, "active_sessions", lambda: [8])
    assert recorder.reconcile_stale() == []


def test_reconcile_ignores_finished_recordings(db):
    with localcache.connect() as c:
        c.execute("INSERT INTO recordings (recording_id, started_at, source, dir,"
                  " status) VALUES (9, 1000.0, 'x', '/tmp/x', 'analysed')")
        c.commit()
    assert recorder.reconcile_stale() == []


def test_the_tui_stops_recordings_on_exit():
    """Without this the ffmpeg child outlives the app and is never closed out."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    called = []
    import karaoke.recorder as r
    original = r.stop_all
    r.stop_all = lambda: called.append(True)
    try:
        app.on_unmount()
    finally:
        r.stop_all = original
    assert called == [True]


# --- finished recordings do not sit unanalysed -----------------------------
#
# Recording and analysing were separate steps with nothing joining them, so
# sessions accumulated: four of them, nearly a gigabyte, before anyone noticed.

def _record_app(monkeypatch, *, recording_id=7, marks=(5, 6)):
    from karaoke.admin_tui import KaraokeAdminApp
    from karaoke.api_client import ApiClient

    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app.api = ApiClient()
    app._recording_id = recording_id
    notes = []
    app.notify = lambda message, **k: notes.append(message)
    app.refresh_record_status = lambda: None
    monkeypatch.setattr(recorder, "mark_count", lambda rid: marks)
    monkeypatch.setattr(recorder, "stop", lambda rid: None)
    return app, notes


def test_stopping_a_recording_works(monkeypatch):
    app, notes = _record_app(monkeypatch, marks=(5, 6))
    app.action_toggle_record()
    assert app._recording_id is None
    assert any("Recording 7 stopped (5/6 tracks identified)" in n for n in notes)


def test_stopping_a_recording_with_no_marks(monkeypatch):
    app, notes = _record_app(monkeypatch, marks=(0, 4))
    app.action_toggle_record()
    assert app._recording_id is None
    assert any("Recording 7 stopped (0/4 tracks identified)" in n for n in notes)


def test_unanalysed_recordings_are_pointed_out(tmp_path, monkeypatch):
    """Quitting mid-recording closes the row without analysing it; without a
    reminder the audio just sits there and the tracks never gain a key."""
    from karaoke import localcache
    from karaoke.tui import KaraokeTui

    db = tmp_path / "t.db"
    real = localcache.connect
    conn = real(db)
    conn.execute("""
        INSERT INTO recordings (recording_id, started_at, source, dir, status)
        VALUES (1, 1000.0, 'x', '/tmp/x', 'complete'),
               (2, 1000.0, 'x', '/tmp/x', 'analysed'),
               (3, 1000.0, 'x', '/tmp/x', 'complete');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real(db))

    app = KaraokeTui.__new__(KaraokeTui)
    notes = []
    app.notify = lambda msg, **k: notes.append(msg)
    app._warn_unanalysed_recordings()
    assert notes and notes[0].startswith("2 recording(s) awaiting")


def test_no_reminder_when_everything_is_analysed(tmp_path, monkeypatch):
    from karaoke import localcache
    from karaoke.tui import KaraokeTui

    db = tmp_path / "t.db"
    real = localcache.connect
    conn = real(db)
    conn.execute("INSERT INTO recordings (recording_id, started_at, source, dir,"
                 " status) VALUES (1, 1000.0, 'x', '/tmp/x', 'analysed')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real(db))

    app = KaraokeTui.__new__(KaraokeTui)
    notes = []
    app.notify = lambda msg, **k: notes.append(msg)
    app._warn_unanalysed_recordings()
    assert notes == []


def test_the_tui_stops_the_mic_on_exit():
    """A thread worker cannot be cancelled from outside -- Textual can only
    ask -- and `_mic_loop` watches this event and nothing else. Left running,
    it keeps the executor alive and the quit blocks on a join that anyio
    performs with no timeout at all (issue #39)."""
    import threading as t

    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._mic_stop = t.Event()
    import karaoke.recorder as r
    original = r.stop_all
    r.stop_all = lambda: None
    try:
        app.on_unmount()
    finally:
        r.stop_all = original
    assert app._mic_stop.is_set()


def test_stopping_on_exit_survives_a_mic_that_was_never_on():
    """The common case: `R` never pressed, so there is no event to set."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    import karaoke.recorder as r
    original = r.stop_all
    r.stop_all = lambda: None
    try:
        app.on_unmount()          # must not raise
    finally:
        r.stop_all = original
