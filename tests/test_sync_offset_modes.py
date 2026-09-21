"""Sync offsets belong to the clock they were tuned against.

Two defects motivated these tests, both found with a song left on repeat:

- ``track_sync_offsets`` recorded no mode, so an offset tuned in radio mode
  (where the playhead is dead-reckoned and carries ``DEFAULT_LEAD_S``) was
  reapplied verbatim in an MPRIS mode that has no such lead — wrong by roughly
  the lead. Two real rows in the library had drifted this way.
- ``mic_elapsed`` reckoned forward with no upper bound, so a repeating track
  ran past its own end: the lyrics emptied and the footer counted down to a
  track change that never came.
"""
from types import SimpleNamespace

import pytest

from karaoke import localcache
from karaoke.player import DEFAULT_LEAD_S
from karaoke.tui import KaraokeTui


# -- offset_mode --------------------------------------------------------

def test_radio_is_its_own_clock():
    assert localcache.offset_mode("radio") == "radio"


@pytest.mark.parametrize("mode", ["scan", "spotify", "listen", "browse", ""])
def test_mpris_modes_share_one_clock(mode):
    """They all read playerctl position, so tuning carries between them."""
    assert localcache.offset_mode(mode) == "player"


# -- storage round-trip -------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    c = localcache.connect(tmp_path / "t.db")
    c.execute("INSERT INTO tracks (track_id, artist, title) VALUES (1, 'A', 'B')")
    c.commit()
    yield c
    c.close()


def test_offset_applies_within_its_own_clock(conn):
    localcache.set_sync_offset(1, -12.4, conn, mode="radio")
    assert localcache.get_sync_offset(1, conn, "radio") == -12.4


def test_offset_is_ignored_on_the_other_clock(conn):
    """The actual bug: a radio offset must not leak into MPRIS playback."""
    localcache.set_sync_offset(1, -12.4, conn, mode="radio")
    assert localcache.get_sync_offset(1, conn, "scan") is None


def test_mpris_offset_carries_between_mpris_modes(conn):
    localcache.set_sync_offset(1, 0.5, conn, mode="scan")
    assert localcache.get_sync_offset(1, conn, "spotify") == 0.5


def test_offset_survives_when_no_mode_is_asked_for(conn):
    localcache.set_sync_offset(1, 0.5, conn, mode="radio")
    assert localcache.get_sync_offset(1, conn) == 0.5


def test_legacy_rows_are_honoured_for_any_mode(conn):
    """Pre-migration rows store '' and cannot be attributed to a clock.

    Discarding them would throw away tuning the user did by hand, so they
    still apply — the fix is forward-looking, not retroactive.
    """
    conn.execute("INSERT INTO track_sync_offsets (track_id, offset_s, updated_at, mode)"
                 " VALUES (1, 0.3, 0, '')")
    conn.commit()
    assert localcache.get_sync_offset(1, conn, "radio") == 0.3
    assert localcache.get_sync_offset(1, conn, "scan") == 0.3


def test_missing_track_has_no_offset(conn):
    assert localcache.get_sync_offset(99, conn, "scan") is None
    assert localcache.get_sync_offset(None, conn, "scan") is None


def test_migration_adds_mode_to_an_old_table(tmp_path):
    if getattr(localcache, "_IS_PG", False):
        import pytest
        pytest.skip("Test specifically for SQLite schema migration")
    import sqlite3

    path = tmp_path / "cache.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE track_sync_offsets (track_id INTEGER PRIMARY KEY,"
                " offset_s REAL NOT NULL, updated_at REAL NOT NULL)")
    old.execute("INSERT INTO track_sync_offsets VALUES (1, 0.3, 0)")
    old.commit()
    old.close()

    c = localcache.connect(path)
    try:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(track_sync_offsets)")}
        assert "mode" in cols
        assert localcache.get_sync_offset(1, c, "scan") == 0.3   # data preserved
    finally:
        c.close()


# -- mic_elapsed wrap ---------------------------------------------------

def _tui(duration, offset=0.0, elapsed_since=0.0):
    """A KaraokeTui stub carrying just what mic_elapsed reads."""
    app = KaraokeTui.__new__(KaraokeTui)
    app._mic_ref = SimpleNamespace(offset=offset, offset_mono=0.0,
                                   artist="A", title="B")
    app._track_duration = duration
    return app, elapsed_since


def test_playhead_wraps_when_a_track_repeats():
    """The reported bug: reckoning must not run past the end of the song."""
    app, now = _tui(duration=300.0, offset=290.0, elapsed_since=20.0)
    pos = app.mic_elapsed(now=now)
    assert pos < 300.0
    # 290 + 20 + 12.6 = 322.6 -> 22.6 into the next pass.
    assert pos == pytest.approx(322.6 - 300.0)


def test_playhead_is_untouched_mid_track():
    app, now = _tui(duration=300.0, offset=100.0, elapsed_since=5.0)
    assert app.mic_elapsed(now=now) == pytest.approx(105.0 + DEFAULT_LEAD_S)


def test_unknown_duration_does_not_wrap():
    """No duration is not evidence of a loop; leave the playhead alone."""
    app, now = _tui(duration=None, offset=290.0, elapsed_since=20.0)
    assert app.mic_elapsed(now=now) == pytest.approx(322.6)


def test_implausible_duration_does_not_wrap():
    """A junk duration must not fold a correct playhead back to zero."""
    app, now = _tui(duration=4.0, offset=100.0, elapsed_since=0.0)
    assert app.mic_elapsed(now=now) == pytest.approx(112.6)


def test_no_anchor_means_no_position():
    app = KaraokeTui.__new__(KaraokeTui)
    app._mic_ref = None
    app._track_duration = 300.0
    assert app.mic_elapsed(now=0.0) is None
