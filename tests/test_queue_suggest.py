"""Tests for whole-queue 'keep the vibe going' suggestions."""
from __future__ import annotations

from karaoke import queue_suggest
from karaoke.search import SoundHit


def _hit(tid, artist, title, sim):
    return SoundHit(track_id=tid, artist=artist, title=title,
                    similarity=sim, source="clap")


def test_pools_across_seeds_and_ranks_breadth_first(monkeypatch):
    """A track close to two seeds beats one close to a single seed harder."""
    neighbours = {
        1: [_hit(10, "A", "x", 0.99), _hit(11, "B", "y", 0.80)],
        2: [_hit(10, "A", "x", 0.70), _hit(12, "C", "z", 0.95)],
    }
    monkeypatch.setattr(queue_suggest.search, "sounds_like_track",
                        lambda tid, k, os_client=None: neighbours.get(tid, []))

    out = queue_suggest.suggest_for_queue([1, 2], limit=5)
    # Track 10 matched both seeds -> ranks first despite 12's higher single score.
    assert out[0].track_id == 10
    assert out[0].seeds_matched == 2
    assert {s.track_id for s in out} == {10, 11, 12}


def test_excludes_tracks_already_in_queue(monkeypatch):
    monkeypatch.setattr(queue_suggest.search, "sounds_like_track",
                        lambda tid, k, os_client=None: [
                            _hit(1, "A", "x", 0.99),   # already queued
                            _hit(20, "D", "w", 0.88),
                        ])
    out = queue_suggest.suggest_for_queue([1], limit=5)
    assert all(s.track_id != 1 for s in out)
    assert out[0].track_id == 20


def test_falls_back_to_spectral_when_no_clap(monkeypatch):
    monkeypatch.setattr(queue_suggest.search, "sounds_like_track",
                        lambda tid, k, os_client=None: [])
    monkeypatch.setattr(queue_suggest.search, "similar_sounding",
                        lambda tid, k, os_client=None: [_hit(30, "E", "v", 0.9)])
    out = queue_suggest.suggest_for_queue([1], limit=5)
    assert out and out[0].track_id == 30
    assert out[0].space == "spectral"


def test_per_artist_cap(monkeypatch):
    monkeypatch.setattr(queue_suggest.search, "sounds_like_track",
                        lambda tid, k, os_client=None: [
                            _hit(40, "Same", "a", 0.99),
                            _hit(41, "Same", "b", 0.98),
                            _hit(42, "Same", "c", 0.97),
                            _hit(43, "Other", "d", 0.5),
                        ])
    out = queue_suggest.suggest_for_queue([1], limit=10, per_artist=2)
    same = [s for s in out if s.artist == "Same"]
    assert len(same) == 2


def test_empty_queue_returns_nothing():
    assert queue_suggest.suggest_for_queue([], limit=5) == []


def test_suggest_for_track(monkeypatch):
    monkeypatch.setattr(queue_suggest.search, "sounds_like_track",
                        lambda tid, k, os_client=None: [_hit(50, "Artist", "Song", 0.95)])
    out = queue_suggest.suggest_for_track(1, limit=5)
    assert len(out) == 1
    assert out[0].track_id == 50
    assert out[0].space == "clap"


def test_falls_back_to_acoustic_genre_when_no_vectors(tmp_path, monkeypatch):
    from karaoke import localcache

    db_path = tmp_path / "acoustic_fallback.db"
    conn = localcache.connect(db_path)
    try:
        conn.execute("""
            INSERT INTO tracks (track_id, artist, title) VALUES
                (1, 'Seed Artist', 'Seed Track'),
                (2, 'Peer Artist', 'Similar Sonic Track'),
                (3, 'Other Artist', 'Different Track');
            INSERT INTO track_genre (track_id, genre, score, labelled_at) VALUES
                (1, 'acoustic blues', 0.92, 1),
                (2, 'acoustic blues', 0.88, 1),
                (3, 'techno', 0.95, 1);
        """)
        conn.commit()
    finally:
        conn.close()

    real_connect = localcache.connect
    monkeypatch.setattr(localcache, "connect", lambda *a, **kw: real_connect(db_path))
    monkeypatch.setattr(queue_suggest.search, "sounds_like_track", lambda tid, k, os_client=None: [])
    monkeypatch.setattr(queue_suggest.search, "similar_sounding", lambda tid, k, os_client=None: [])

    out = queue_suggest.suggest_for_track(1, limit=5)
    assert len(out) == 1
    assert out[0].track_id == 2
    assert out[0].space == "clap_acoustic"
