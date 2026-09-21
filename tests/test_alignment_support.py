"""Marking how well a stored alignment is supported.

Once timings are written to an LRC, a line placed on a heard word looks exactly
like one guessed between two distant anchors. GAUPA - Febersvan stored lines up
to 183 seconds out and read no differently from a good result.

This is deliberately a flag and not a gate. The measurement across 13 recorded
tracks supports certifying a well-anchored alignment -- all nine at or above
83% anchored scored 0.24-0.89s of jitter -- but not condemning a sparse one:
King Buffalo - Locusts managed 0.77s on 45% anchored while Ministry - Filth Pig
managed only 2.63s on 54%, indistinguishable on every metric available.
"""
from __future__ import annotations

import sqlite3

import pytest

from karaoke import localcache


@pytest.fixture()
def conn(tmp_path):
    c = localcache.connect(tmp_path / "support.db")
    for artist, title in (("GAUPA", "Febersvan"),
                          ("King Buffalo", "Locusts"),
                          ("TOOL", "Opiate²")):
        c.execute("INSERT INTO tracks (artist, title) VALUES (%s, %s)",
                  (artist, title))
    c.commit()
    yield c
    c.close()


def _report(lines: int, anchored: int, gap: float = 0.0,
            fraction: float = 0.0) -> dict:
    return {"lines": lines, "anchored": anchored,
            "longest_gap_s": gap, "unanchored_fraction": fraction}


# --- storing ---------------------------------------------------------------

def test_support_round_trips(conn):
    localcache.record_alignment_support(
        1, _report(20, 8, gap=288.0, fraction=0.77), conn,
        source="whisper_aligned")
    row = localcache.alignment_support(1, conn)
    assert row["lines"] == 20
    assert row["anchored"] == 8
    assert row["longest_gap_s"] == pytest.approx(288.0)
    assert row["unanchored_fraction"] == pytest.approx(0.77)
    assert row["source"] == "whisper_aligned"


def test_re_syncing_replaces_rather_than_accumulates(conn):
    """One row per track: a re-sync is a new answer, not more evidence."""
    localcache.record_alignment_support(1, _report(20, 8), conn)
    localcache.record_alignment_support(1, _report(20, 18), conn)
    assert localcache.alignment_support(1, conn)["anchored"] == 18


def test_an_empty_report_stores_nothing(conn):
    """A caller that did not ask for a report must not write a fake one."""
    localcache.record_alignment_support(1, {}, conn)
    assert localcache.alignment_support(1, conn) is None


def test_the_table_is_added_to_a_database_predating_it(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE tracks (track_id INTEGER PRIMARY KEY)")
    old.commit()
    old.close()

    c = localcache.connect(path)
    try:
        names = {r["name"] for r in
                 c.execute("SELECT table_name as name FROM information_schema.tables WHERE table_schema='public'")}
        assert "alignment_support" in names
    finally:
        c.close()


# --- reading the flag ------------------------------------------------------

def test_a_well_anchored_alignment_is_trustworthy(conn):
    """TOOL - Opiate²: 35/36 anchored, and it scored 0.42s of jitter."""
    localcache.record_alignment_support(3, _report(36, 35), conn)
    row = localcache.alignment_support(3, conn)
    assert localcache.anchored_fraction(row) == pytest.approx(35 / 36)
    assert localcache.alignment_is_trustworthy(row) is True


def test_a_sparsely_anchored_alignment_is_not_certified(conn):
    """Not "wrong" -- uncertain. Locusts scored 0.77s on 45%."""
    localcache.record_alignment_support(2, _report(24, 11), conn)
    row = localcache.alignment_support(2, conn)
    assert localcache.alignment_is_trustworthy(row) is False


def test_the_threshold_matches_what_was_measured(conn):
    """Every track at or above this scored under 0.9s jitter, nine of nine."""
    assert localcache.GOOD_ANCHOR_FRACTION == pytest.approx(0.83)
    localcache.record_alignment_support(1, _report(100, 83), conn)
    assert localcache.alignment_is_trustworthy(
        localcache.alignment_support(1, conn)) is True


def test_nothing_recorded_is_unknown_rather_than_untrustworthy(conn):
    """Every alignment stored before this existed must not read as bad."""
    assert localcache.alignment_is_trustworthy(None) is None
    assert localcache.anchored_fraction(None) is None


def test_zero_lines_is_unknown_rather_than_a_division_error(conn):
    localcache.record_alignment_support(1, _report(0, 0), conn)
    row = localcache.alignment_support(1, conn)
    assert localcache.anchored_fraction(row) is None
    assert localcache.alignment_is_trustworthy(row) is None


# --- the worklist ----------------------------------------------------------

def test_review_lists_the_weakest_first(conn):
    localcache.record_alignment_support(1, _report(20, 8), conn)    # 40%
    localcache.record_alignment_support(2, _report(24, 11), conn)   # 46%
    localcache.record_alignment_support(3, _report(36, 35), conn)   # 97%

    review = localcache.alignments_for_review(conn)
    assert [r["title"] for r in review] == ["Febersvan", "Locusts"]
    assert review[0]["fraction"] < review[1]["fraction"]


def test_review_never_lists_a_certified_alignment(conn):
    localcache.record_alignment_support(3, _report(36, 35), conn)
    assert localcache.alignments_for_review(conn) == []


def test_review_is_a_worklist_and_removes_nothing(conn):
    """The flag must never cost the user lyrics. This is the whole design."""
    conn.execute("INSERT INTO lyrics (track_id, kind, source, synced_lyrics) "
                 "VALUES (1, 'approved', 'whisper_aligned', '[00:10.00]hi')")
    conn.commit()
    localcache.record_alignment_support(1, _report(20, 8), conn)

    assert len(localcache.alignments_for_review(conn)) == 1
    still_there = conn.execute(
        "SELECT synced_lyrics FROM lyrics WHERE track_id = 1").fetchone()
    assert still_there["synced_lyrics"] == "[00:10.00]hi"


def test_the_threshold_can_be_loosened_for_a_shorter_worklist(conn):
    localcache.record_alignment_support(1, _report(20, 8), conn)    # 40%
    localcache.record_alignment_support(2, _report(24, 11), conn)   # 46%
    assert len(localcache.alignments_for_review(conn, threshold=0.45)) == 1
