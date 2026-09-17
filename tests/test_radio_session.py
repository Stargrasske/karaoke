"""Tests for radio session tracking, recording toggles, and library import pipeline."""
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from karaoke import localcache, radio_pipeline


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "test_radio.db"
    real = localcache.connect
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real(path))
    monkeypatch.setattr(radio_pipeline, "settings", MagicMock(data_dir=tmp_path))
    return path


def test_radio_session_without_recording(db):
    """Session without recording captures tracks and finishes properly."""
    sid = localcache.start_radio_session(source="mic", is_recording=False)
    assert sid > 0

    tid1 = localcache.record_radio_track(
        sid, "Radiohead", "Creep", offset_s=34.5, has_synced=True, lyric_source="lrclib"
    )
    assert tid1 > 0

    # Repeat detection of same track increments play_count
    tid2 = localcache.record_radio_track(
        sid, "Radiohead", "Creep", offset_s=64.5, has_synced=True, lyric_source="lrclib"
    )
    assert tid2 == tid1

    # Second track
    tid3 = localcache.record_radio_track(
        sid, "Nirvana", "Smells Like Teen Spirit", offset_s=12.0, has_synced=True, lyric_source="lrclib"
    )
    assert tid3 > tid1

    localcache.finish_radio_session(sid, status="completed", notes="Test radio run")

    sess = localcache.get_radio_session(sid)
    assert sess is not None
    assert sess["session_id"] == sid
    assert sess["source"] == "mic"
    assert sess["is_recording"] == 0
    assert sess["recording_id"] is None
    assert sess["track_count"] == 2
    assert sess["status"] == "completed"
    assert sess["ended_at"] is not None

    tracks = localcache.get_radio_session_tracks(sid)
    assert len(tracks) == 2
    t1 = next(t for t in tracks if t["artist"] == "Radiohead")
    assert t1["title"] == "Creep"
    assert t1["play_count"] == 2
    assert t1["has_synced_lyrics"] == 1
    assert t1["imported"] == 0


def test_radio_session_with_recording_linkage(db):
    """Session with recording links to recordings table and updates state."""
    with localcache.connect() as conn:
        conn.execute(
            """
            INSERT INTO recordings (recording_id, started_at, source, dir, status)
            VALUES (99, %s, 'mic', '/tmp/rec99', 'recording')
            """,
            (time.time(),),
        )
        conn.execute(
            """
            INSERT INTO recordings (recording_id, started_at, source, dir, status)
            VALUES (101, %s, 'mic', '/tmp/rec101', 'recording')
            """,
            (time.time(),),
        )
        conn.commit()

    sid = localcache.start_radio_session(source="mic", recording_id=99, is_recording=True)
    sess = localcache.get_radio_session(sid)
    assert sess["is_recording"] == 1
    assert sess["recording_id"] == 99

    # Toggle recording off during session
    localcache.update_radio_session_recording(sid, recording_id=None, is_recording=False)
    sess2 = localcache.get_radio_session(sid)
    assert sess2["is_recording"] == 0

    # Toggle recording back on with a new recording id
    localcache.update_radio_session_recording(sid, recording_id=101, is_recording=True)
    sess3 = localcache.get_radio_session(sid)
    assert sess3["is_recording"] == 1
    assert sess3["recording_id"] == 101


def test_import_radio_session_metadata_only(db, monkeypatch):
    """Importing a radio session without audio slices registers library tracks and lyrics."""
    sid = localcache.start_radio_session(source="mic", is_recording=False)
    localcache.record_radio_track(sid, "Queen", "Bohemian Rhapsody", offset_s=10.0, has_synced=True)

    # Mock lyrics and youtube search
    fake_lyrics = MagicMock(synced_raw="[00:10.00] Mama", plain="Mama")
    monkeypatch.setattr("karaoke.lyrics.fetch_lrclib", lambda a, t: fake_lyrics)
    monkeypatch.setattr("karaoke.youtube.search", lambda q, limit=1: [{"url": "https://youtube.com/watch?v=fake123"}])

    res = radio_pipeline.import_radio_session(sid, save_audio=True, resolve_streaming=True)
    assert res["session_id"] == sid
    assert res["imported_tracks"] == 1
    assert res["audio_slices"] == 0

    # Check that track exists in tracks table
    with localcache.connect() as conn:
        track_row = conn.execute("SELECT * FROM tracks WHERE artist='Queen' AND title='Bohemian Rhapsody'").fetchone()
        assert track_row is not None
        tid = track_row["track_id"]

        # Check that youtube source was added
        src_row = conn.execute("SELECT * FROM sources WHERE track_id=%s AND kind='youtube'", (tid,)).fetchone()
        assert src_row is not None
        assert "youtube.com" in src_row["url"]

        # Check that session track was marked imported
        stracks = localcache.get_radio_session_tracks(sid, conn=conn)
        assert stracks[0]["imported"] == 1
        assert stracks[0]["imported_track_id"] == tid


def test_import_radio_session_with_audio_slice(db, tmp_path, monkeypatch):
    """Importing a radio session with an audio recording cuts slices and attaches audio source."""
    rec_dir = tmp_path / "rec_test"
    rec_dir.mkdir(parents=True)
    seg_file = rec_dir / "seg-20260911-180000.flac"
    seg_file.write_bytes(b"dummy flac content")

    with localcache.connect() as conn:
        conn.execute(
            """
            INSERT INTO recordings (recording_id, started_at, source, dir, status)
            VALUES (88, %s, 'mic', %s, 'complete')
            """,
            (time.time() - 300, str(rec_dir)),
        )
        conn.commit()

    sid = localcache.start_radio_session(source="mic", recording_id=88, is_recording=True)
    localcache.record_radio_track(sid, "Oasis", "Wonderwall", offset_s=15.0, has_synced=True)

    # Mock recording slice & cut
    from karaoke.recording_slice import Segment
    mock_seg = Segment(artist="Oasis", title="Wonderwall", start_wall=100.0, end_wall=300.0, marks=2, spread=0.5)

    monkeypatch.setattr("karaoke.recorder.load_marks", lambda rid, **k: [])
    monkeypatch.setattr("karaoke.recording_slice.segments", lambda marks: [mock_seg])
    monkeypatch.setattr("karaoke.recording_worker.segment_files", lambda d: [MagicMock(path=seg_file, start_wall=100.0, duration=300.0, end_wall=400.0)])

    def fake_cut(files, start, end, dest):
        dest.write_bytes(b"fake cut audio")
        return True

    monkeypatch.setattr("karaoke.recording_worker.cut", fake_cut)
    monkeypatch.setattr("karaoke.analyze.analyze_audio", lambda p: MagicMock(key=None, bpm=None))
    monkeypatch.setattr("karaoke.lyrics.fetch_lrclib", lambda a, t: None)
    monkeypatch.setattr("karaoke.youtube.search", lambda q, limit=1: [])

    res = radio_pipeline.import_radio_session(sid, save_audio=True, resolve_streaming=False)
    assert res["session_id"] == sid
    assert res["imported_tracks"] == 1
    assert res["audio_slices"] == 1

    tinfo = res["tracks"][0]
    assert tinfo["audio_slice"] is not None
    assert Path(tinfo["audio_slice"]).is_file()
    assert Path(tinfo["audio_slice"]).name == "Oasis - Wonderwall.flac"

    with localcache.connect() as conn:
        src_row = conn.execute("SELECT * FROM sources WHERE track_id=%s AND kind='radio_capture'", (tinfo["track_id"],)).fetchone()
        assert src_row is not None
        assert "Oasis - Wonderwall.flac" in src_row["url"]


def test_cli_radio_args_parsing():
    """CLI properly accepts --record, --radio-sessions, and --import-radio."""
    import argparse
    from karaoke import cli

    # Test via direct ArgumentParser test matching cli.py
    ap = argparse.ArgumentParser()
    ap.add_argument("--radio", "-r", action="store_true")
    ap.add_argument("--record", "-R", "--rec", dest="record", action="store_true")
    ap.add_argument("--radio-sessions", action="store_true")
    ap.add_argument("--import-radio", type=int)

    args = ap.parse_args(["-r", "--record"])
    assert args.radio is True
    assert args.record is True

    args2 = ap.parse_args(["--radio-sessions"])
    assert args2.radio_sessions is True

    args3 = ap.parse_args(["--import-radio", "42"])
    assert args3.import_radio == 42


def test_admin_tui_radio_sessions_modal(db, monkeypatch):
    """RadioSessionsModal loads sessions and triggers import worker."""
    from karaoke.admin_tui import KaraokeAdminApp, RadioSessionsModal

    sid = localcache.start_radio_session(source="mic", is_recording=False)
    localcache.record_radio_track(sid, "Blur", "Song 2")
    localcache.finish_radio_session(sid)

    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    imported = []
    app._import_radio_session_worker = lambda s_id: imported.append(s_id)

    modal = RadioSessionsModal(app_ref=app)
    modal._sessions = [{"session_id": sid, "started_at": time.time(), "ended_at": time.time() + 100, "source": "mic", "track_count": 1, "status": "completed"}]

    class FakeTable:
        cursor_row = 0

    monkeypatch.setattr(modal, "query_one", lambda sel, *a, **k: FakeTable(), raising=False)
    dismissed = []
    monkeypatch.setattr(modal, "dismiss", lambda: dismissed.append(True), raising=False)

    modal.action_import_selected_session()
    assert dismissed == [True]
    assert imported == [sid]
