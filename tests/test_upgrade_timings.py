"""Tests for upgrading cached line-level lyrics to Enhanced LRC."""
from __future__ import annotations
import psycopg.rows

import json
import sqlite3
import sys
import types

import pytest

from karaoke import localcache, upgrade_timings
from karaoke.caption_sync import CaptionAvailability, CaptionTrack
from karaoke.lyrics import Lyrics, parse_enhanced_lrc
from karaoke.staging import ensure_schema

_real_connect = localcache.connect

PLAIN_LRC = "[00:10.00]one two three four\n[00:30.00]next line here"
JSON3 = json.dumps({"events": [
    {"tStartMs": 10_000, "segs": [
        {"utf8": "one", "tOffsetMs": 0},
        {"utf8": " two", "tOffsetMs": 200},
        {"utf8": " three", "tOffsetMs": 400},
        {"utf8": " four", "tOffsetMs": 600},
    ]},
    {"tStartMs": 30_000, "segs": [
        {"utf8": "next", "tOffsetMs": 0},
        {"utf8": " line", "tOffsetMs": 300},
        {"utf8": " here", "tOffsetMs": 600},
    ]},
]})


@pytest.fixture(autouse=True)
def reset_cooldown():
    upgrade_timings._COOLDOWN_UNTIL = 0.0
    yield
    upgrade_timings._COOLDOWN_UNTIL = 0.0


@pytest.fixture()
def conn(tmp_path):
    c = _real_connect(tmp_path / "k.db")
    c.row_factory = psycopg.rows.dict_row
    ensure_schema(c)
    lines, _, _ = parse_enhanced_lrc(PLAIN_LRC)
    localcache.add_track_source("A", "S", url="https://youtu.be/x",
                                kind="youtube", conn=c)
    localcache.put_cached_lyrics(
        "A", "S",
        Lyrics(plain="one two three four\nnext line here",
               synced_raw=PLAIN_LRC, lines=lines),
        conn=c,
    )
    yield c
    c.close()


@pytest.fixture()
def fake_youtube(monkeypatch):
    """Stub yt-dlp + the caption fetch so no network is touched."""
    class FakeYDL:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **k): return {}

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)
    monkeypatch.setattr(
        upgrade_timings, "probe_captions",
        lambda info, *a, **k: CaptionAvailability(
            has_manual=True, has_automatic=False,
            manual_languages=("en",), automatic_languages=(),
            best=CaptionTrack(language="en", ext="json3",
                              url="https://c/en.json3", kind="manual"),
        ),
    )

    class Resp:
        headers = {"Content-Type": "application/json"}
        text = JSON3
        def raise_for_status(self): return None

    monkeypatch.setattr(upgrade_timings.requests, "get", lambda *a, **k: Resp())


# --- candidate detection ---

def test_has_word_timings_detects_enhanced():
    assert upgrade_timings.has_word_timings("[00:01.00]<00:01.00>a") is True
    assert upgrade_timings.has_word_timings(PLAIN_LRC) is False
    assert upgrade_timings.has_word_timings("") is False


def test_finds_line_level_track_as_candidate(conn):
    cands = upgrade_timings.find_upgrade_candidates(conn)
    assert [r["artist"] for r in cands] == ["A"]


def test_already_enhanced_track_is_not_a_candidate(conn):
    localcache.put_cached_lyrics(
        "A", "S",
        Lyrics(plain="x", synced_raw="[00:10.00]<00:10.00>one <00:10.20>two"),
        conn=conn,
    )
    assert upgrade_timings.find_upgrade_candidates(conn) == []


def test_candidate_limit_is_respected(conn):
    assert len(upgrade_timings.find_upgrade_candidates(conn, limit=0)) == 0


# --- upgrading ---

def test_upgrade_writes_word_timings(conn, fake_youtube):
    row = upgrade_timings.find_upgrade_candidates(conn)[0]
    res = upgrade_timings.upgrade_track(row, conn)
    assert res.status == "upgraded"

    cached = localcache.get_cached_lyrics("A", "S", conn=conn)
    _, _, words = parse_enhanced_lrc(cached.synced_raw)
    assert words[0] == pytest.approx([10.0, 10.2, 10.4, 10.6])


def test_upgrade_preserves_existing_plain_lyrics(conn, fake_youtube):
    """Captions supply timing; a better plain transcription must survive."""
    row = upgrade_timings.find_upgrade_candidates(conn)[0]
    upgrade_timings.upgrade_track(row, conn)
    cached = localcache.get_cached_lyrics("A", "S", conn=conn)
    assert cached.plain == "one two three four\nnext line here"


def test_dry_run_does_not_write(conn, fake_youtube):
    row = upgrade_timings.find_upgrade_candidates(conn)[0]
    res = upgrade_timings.upgrade_track(row, conn, dry_run=True)
    assert res.status == "upgraded"
    cached = localcache.get_cached_lyrics("A", "S", conn=conn)
    assert upgrade_timings.has_word_timings(cached.synced_raw) is False


def test_upgrade_reports_no_captions_when_absent(conn, monkeypatch, fake_youtube):
    monkeypatch.setattr(
        upgrade_timings, "probe_captions",
        lambda info, *a, **k: CaptionAvailability(
            has_manual=False, has_automatic=False,
            manual_languages=(), automatic_languages=(), best=None,
        ),
    )
    row = upgrade_timings.find_upgrade_candidates(conn)[0]
    assert upgrade_timings.upgrade_track(row, conn).status == "no-captions"


def test_upgrade_rejects_rate_limit_html(conn, monkeypatch, fake_youtube):
    class Resp:
        headers = {"Content-Type": "text/html"}
        text = "<html>429</html>"
        def raise_for_status(self): return None

    monkeypatch.setattr(upgrade_timings.requests, "get", lambda *a, **k: Resp())
    row = upgrade_timings.find_upgrade_candidates(conn)[0]
    with pytest.raises(RuntimeError, match="rate limited"):
        upgrade_timings.upgrade_track(row, conn)


def test_upgrade_all_stops_on_rate_limit(conn, monkeypatch, fake_youtube):
    def boom(*a, **k):
        raise RuntimeError("rate limited by YouTube")

    monkeypatch.setattr(upgrade_timings, "upgrade_track", boom)
    results = upgrade_timings.upgrade_all(conn=conn, delay=0, progress=False)
    assert len(results) == 1                 # stopped rather than looping
    assert results[0].status == "error"


def test_upgrade_rejects_automatic_captions_when_synced_exists(conn, monkeypatch, fake_youtube):
    """Automatic ASR captions must never overwrite existing approved synced lyrics."""
    monkeypatch.setattr(
        upgrade_timings, "probe_captions",
        lambda info, *a, **k: CaptionAvailability(
            has_manual=False, has_automatic=True,
            manual_languages=(), automatic_languages=("en",),
            best=CaptionTrack(language="en", ext="json3",
                              url="https://c/en.json3", kind="automatic"),
        ),
    )
    row = upgrade_timings.find_upgrade_candidates(conn)[0]
    res = upgrade_timings.upgrade_track(row, conn)
    assert res.status == "no-captions"
    assert "automatic captions rejected" in res.detail

    # Existing lyrics intact
    cached = localcache.get_cached_lyrics("A", "S", conn=conn)
    assert cached.synced_raw == PLAIN_LRC


def test_upgrade_rejects_truncated_captions(conn, monkeypatch, fake_youtube):
    """Captions with far fewer lines than existing synced lyrics must be rejected."""
    # Put 10 lines of existing synced lyrics
    ten_lines = "\n".join(f"[00:{i:02d}.00] line {i}" for i in range(10))
    localcache.put_cached_lyrics("A", "S", Lyrics(plain="p", synced_raw=ten_lines), conn=conn)

    # Caption response has only 1 line (e.g. AWOLNATION - Sail 1-line bug)
    one_line_json3 = json.dumps({"events": [
        {"tStartMs": 10_000, "segs": [{"utf8": "you", "tOffsetMs": 0}]},
    ]})
    class Resp:
        headers = {"Content-Type": "application/json"}
        text = one_line_json3
        def raise_for_status(self): return None

    monkeypatch.setattr(upgrade_timings.requests, "get", lambda *a, **k: Resp())

    row = upgrade_timings.find_upgrade_candidates(conn)[0]
    res = upgrade_timings.upgrade_track(row, conn)
    assert res.status == "no-captions"
    assert "incomplete" in res.detail

    # Existing 10 lines preserved
    cached = localcache.get_cached_lyrics("A", "S", conn=conn)
    assert cached.synced_raw == ten_lines
