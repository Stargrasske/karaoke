"""Storing the silence map, and reading where a capture really ended.

Recording 12 ran 36 minutes and held 21 minutes of music: YouTube Music moved
playback to another device at 15:21 and the rest is dead air. The stored map is
what lets anything else know that -- including after the week's retention has
deleted the audio it was derived from.
"""
from __future__ import annotations

import sqlite3

import pytest

from karaoke import localcache, silence
from karaoke.silence import Silence


@pytest.fixture()
def conn(tmp_path):
    c = localcache.connect(tmp_path / "silence.db")
    c.execute("INSERT INTO recordings (started_at, source, dir, status) "
              "VALUES (%s, %s, %s, %s)", (1000.0, "monitor", "", "complete"))
    c.commit()
    yield c
    c.close()


def test_a_map_round_trips(conn):
    localcache.record_silence(1, "seg-a.flac", [(10.0, 20.0), (100.0, 130.5)],
                              conn, duration_s=600.0)
    rows = localcache.silence_for_recording(1, conn)
    assert [(r["start_s"], r["end_s"]) for r in rows] == [(10.0, 20.0), (100.0, 130.5)]


def test_a_rescan_replaces_rather_than_accumulates(conn):
    """The audio does not change, so a second scan is a correction."""
    localcache.record_silence(1, "seg-a.flac", [(10.0, 20.0)], conn)
    localcache.record_silence(1, "seg-a.flac", [(11.0, 21.0)], conn)
    rows = localcache.silence_for_recording(1, conn)
    assert [(r["start_s"], r["end_s"]) for r in rows] == [(11.0, 21.0)]


def test_a_rescan_of_one_file_leaves_the_others_alone(conn):
    localcache.record_silence(1, "seg-a.flac", [(1.0, 2.0)], conn)
    localcache.record_silence(1, "seg-b.flac", [(3.0, 4.0)], conn)
    localcache.record_silence(1, "seg-a.flac", [(5.0, 6.0)], conn)
    files = {r["file"] for r in localcache.silence_for_recording(1, conn)}
    assert files == {"seg-a.flac", "seg-b.flac"}


def test_a_fully_audible_segment_is_still_recorded_as_scanned(conn):
    """No silence rows is not the same as not looked at."""
    localcache.record_silence(1, "seg-a.flac", [], conn, duration_s=600.0)
    assert localcache.silence_for_recording(1, conn) == []
    assert localcache.silence_scans(1, conn) == {"seg-a.flac": 600.0}


def test_the_measured_duration_is_cached_with_the_scan(conn):
    """It costs a decode to obtain: the segment muxer writes no FLAC header."""
    localcache.record_silence(1, "seg-a.flac", [(0.0, 1.0)], conn,
                              duration_s=597.3)
    assert localcache.silence_scans(1, conn)["seg-a.flac"] == pytest.approx(597.3)


def test_the_tables_are_added_to_a_database_predating_them(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE tracks (track_id INTEGER PRIMARY KEY)")
    old.commit()
    old.close()

    c = localcache.connect(path)
    try:
        names = {r["name"] for r in
                 c.execute("SELECT table_name as name FROM information_schema.tables WHERE table_schema='public'")}
        assert {"recording_silence", "recording_silence_scans"} <= names
    finally:
        c.close()


def test_stored_map_groups_by_file(conn):
    localcache.record_silence(1, "seg-a.flac", [(1.0, 2.0), (5.0, 9.0)], conn)
    localcache.record_silence(1, "seg-b.flac", [(3.0, 4.0)], conn)
    grouped = silence.stored_map(1, conn)
    assert set(grouped) == {"seg-a.flac", "seg-b.flac"}
    assert grouped["seg-a.flac"] == [Silence(1.0, 2.0), Silence(5.0, 9.0)]


# --- where the audio really stopped ----------------------------------------

class _Seg:
    """Stand-in for recording_worker.SegmentFile."""

    def __init__(self, name, start_wall, duration):
        self.path = type("P", (), {"name": name})()
        self.start_wall = start_wall
        self.duration = duration


def _patch_segments(monkeypatch, segs, directory="/nonexistent-but-present"):
    from karaoke import recording_worker

    monkeypatch.setattr(recording_worker, "segment_files", lambda d: segs)
    monkeypatch.setattr(recording_worker, "load_recording",
                        lambda rid, c=None: {"dir": directory})
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)


def test_a_trailing_silence_shortens_the_audible_span(conn, monkeypatch):
    """Recording 12's shape: music, then nothing until the capture is stopped."""
    _patch_segments(monkeypatch, [_Seg("seg-a.flac", 1000.0, 600.0)])
    localcache.record_silence(1, "seg-a.flac", [(98.0, 600.0)], conn)
    # Audio ran to offset 98 of a 600-second segment beginning at t=1000.
    assert silence.audible_until(1, conn) == pytest.approx(1098.0)


def test_a_segment_ending_mid_music_contributes_its_whole_length(conn, monkeypatch):
    _patch_segments(monkeypatch, [_Seg("seg-a.flac", 1000.0, 600.0)])
    localcache.record_silence(1, "seg-a.flac", [(10.0, 20.0)], conn)
    assert silence.audible_until(1, conn) == pytest.approx(1600.0)


def test_the_latest_audible_segment_wins(conn, monkeypatch):
    _patch_segments(monkeypatch, [
        _Seg("seg-a.flac", 1000.0, 600.0),
        _Seg("seg-b.flac", 1600.0, 600.0),
    ])
    localcache.record_silence(1, "seg-a.flac", [], conn)
    localcache.record_silence(1, "seg-b.flac", [(120.0, 600.0)], conn)
    assert silence.audible_until(1, conn) == pytest.approx(1720.0)


def test_a_wholly_silent_recording_reports_nothing_audible(conn, monkeypatch):
    """None, not the start instant.

    "Nothing was ever audible" is a different claim from "the audio ended at
    the moment capture began", and a caller scoring an alignment has to handle
    the first case rather than be handed a span of zero length that looks real.
    """
    _patch_segments(monkeypatch, [_Seg("seg-a.flac", 1000.0, 600.0)])
    localcache.record_silence(1, "seg-a.flac", [(0.0, 600.0)], conn)
    assert silence.audible_until(1, conn) is None


# --- scanning a live recording ---------------------------------------------

def test_the_open_segment_is_left_alone_while_recording(conn, monkeypatch):
    """The newest file is still being written; its map would be a snapshot.

    This is why scanning recording 13 did not require stopping it.
    """
    segs = [_Seg("seg-a.flac", 1000.0, 600.0), _Seg("seg-open.flac", 1600.0, 60.0)]
    _patch_segments(monkeypatch, segs)
    scanned: list[str] = []
    monkeypatch.setattr(silence, "detect",
                        lambda p, **k: scanned.append(p.name) or [])
    monkeypatch.setattr(silence, "measured_duration", lambda p: 600.0)

    silence.scan(1, conn=conn)
    assert scanned == ["seg-a.flac"]


def test_an_already_scanned_file_is_not_scanned_again(conn, monkeypatch):
    segs = [_Seg("seg-a.flac", 1000.0, 600.0), _Seg("seg-open.flac", 1600.0, 60.0)]
    _patch_segments(monkeypatch, segs)
    localcache.record_silence(1, "seg-a.flac", [], conn, duration_s=600.0)
    scanned: list[str] = []
    monkeypatch.setattr(silence, "detect",
                        lambda p, **k: scanned.append(p.name) or [])
    monkeypatch.setattr(silence, "measured_duration", lambda p: 600.0)

    silence.scan(1, conn=conn)
    assert scanned == []
    silence.scan(1, conn=conn, rescan=True)
    assert scanned == ["seg-a.flac"]
