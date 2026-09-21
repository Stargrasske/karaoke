"""Serving one track out of a recording.

The design decision under test is that playback reuses the browser window that
is already open, rather than introducing a player. That window publishes MPRIS,
which is where position for lyric sync is already read from, so a served cut
needs no second clock and no second set of sync offsets.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from karaoke import recording_audio


# --- naming the cut --------------------------------------------------------

def test_a_cut_is_named_after_its_boundary_not_just_its_index():
    """A boundary that moves must miss the cache rather than serve old audio.

    Track 3 of a running recording is not a fixed thing: markers keep arriving
    and a boundary can shift.
    """
    first = recording_audio.cut_name(13, 3, 1000.0)
    moved = recording_audio.cut_name(13, 3, 1014.0)
    assert first != moved
    assert first.endswith(".flac")


def test_cut_names_are_stable_for_the_same_boundary():
    assert (recording_audio.cut_name(13, 3, 1000.0)
            == recording_audio.cut_name(13, 3, 1000.0))


def test_cut_names_separate_recordings_and_tracks():
    names = {recording_audio.cut_name(13, 0, 1.0),
             recording_audio.cut_name(13, 1, 1.0),
             recording_audio.cut_name(14, 0, 1.0)}
    assert len(names) == 3


def test_cuts_live_outside_the_capture_directory(tmp_path, monkeypatch):
    """prune_recordings sizes a session by its directory and deletes by age.

    Derived files kept in there would inflate the first and be destroyed by
    the second.
    """
    from types import SimpleNamespace

    # settings is a frozen dataclass, so the module reference is replaced
    # rather than the field assigned.
    monkeypatch.setattr(recording_audio, "settings",
                        SimpleNamespace(local_db=str(tmp_path / "db" / "k.db")))
    cache = recording_audio.cache_dir()
    assert cache.is_dir()
    assert "recordings" not in cache.parts


# --- the URL ---------------------------------------------------------------

def test_the_track_url_points_at_the_control_api(monkeypatch):
    """Cutting writes files and runs ffmpeg: host-side work, not the read API."""
    monkeypatch.delenv("KARAOKE_CTRL_PORT", raising=False)
    url = recording_audio.track_url(13, 2)
    assert url == "http://localhost:8765/recordings/13/tracks/2"


def test_the_track_url_honours_a_configured_port(monkeypatch):
    monkeypatch.setenv("KARAOKE_CTRL_PORT", "9100")
    assert ":9100/" in recording_audio.track_url(1, 0)


# --- what the browser shows ------------------------------------------------

class _Seg:
    def __init__(self, name, start_wall):
        self.path = type("P", (), {"name": name})()
        self.start_wall = start_wall


class _Span:
    def __init__(self, start_wall, end_wall, duration):
        self.start_wall = start_wall
        self.end_wall = end_wall
        self.duration = duration


def test_a_track_with_no_stored_silence_is_not_called_silent():
    assert recording_audio._mostly_silent(
        _Span(1000.0, 1200.0, 200.0), [], {}) is False


def test_a_track_that_is_mostly_quiet_is_marked_silent():
    """Recording 12's last track: 30s of music, then the player stopped."""
    from karaoke.silence import Silence

    files = [_Seg("seg-a.flac", 1000.0)]
    gaps = {"seg-a.flac": [Silence(30.0, 600.0)]}
    assert recording_audio._mostly_silent(
        _Span(1000.0, 1200.0, 200.0), files, gaps) is True


def test_a_short_gap_does_not_make_a_track_silent():
    from karaoke.silence import Silence

    files = [_Seg("seg-a.flac", 1000.0)]
    gaps = {"seg-a.flac": [Silence(10.0, 20.0)]}
    assert recording_audio._mostly_silent(
        _Span(1000.0, 1200.0, 200.0), files, gaps) is False


def test_silence_outside_the_track_span_does_not_count():
    """A gap after this track belongs to the next one, not to it."""
    from karaoke.silence import Silence

    files = [_Seg("seg-a.flac", 1000.0)]
    gaps = {"seg-a.flac": [Silence(300.0, 590.0)]}
    assert recording_audio._mostly_silent(
        _Span(1000.0, 1200.0, 200.0), files, gaps) is False


# --- the browse list -------------------------------------------------------

def test_an_unknown_recording_browses_to_nothing(tmp_path):
    from karaoke import localcache

    conn = localcache.connect(tmp_path / "browse.db")
    try:
        assert recording_audio.browse_rows(999, conn) == []
    finally:
        conn.close()


def test_rows_carry_what_the_display_needs(tmp_path, monkeypatch):
    """Including the rows that cannot be played: a gap is evidence.

    Hiding unplayable rows is how eight failed identifications looked like an
    unreliable recogniser for a whole afternoon.
    """
    from karaoke import localcache, recorder, recording_worker

    conn = localcache.connect(tmp_path / "browse.db")
    conn.execute("INSERT INTO recordings (started_at, source, dir, status) "
                 "VALUES (%s, %s, %s, %s)", (1000.0, "monitor", str(tmp_path), "complete"))
    for at_wall, offset in ((1030.0, 30.0), (1075.0, 75.0)):
        conn.execute(
            "INSERT INTO recording_marks (recording_id, at_wall, at_offset,"
            " artist, title, ok) VALUES (1, %s, %s, 'Portishead', 'Glory Box', 1)",
            (at_wall, offset))
    conn.commit()

    monkeypatch.setattr(recording_worker, "segment_files", lambda d: [])
    try:
        rows = recording_audio.browse_rows(1, conn)
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["artist"] == "Portishead"
    assert row["index"] == 0
    assert "playable" in row and "silent" in row and "confident" in row
    # No audio on disk, so it cannot be played -- but it is still listed.
    assert row["playable"] is False


def test_the_max_cut_is_bounded():
    """A boundary from bad markers can claim an implausible span."""
    assert 60 <= recording_audio.MAX_CUT_SECONDS <= 3600
