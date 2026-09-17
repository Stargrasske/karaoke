"""Tests for unapproved lyrics staging."""

import pytest

from karaoke import localcache, staging
from karaoke.lyrics import Lyrics


def _conn(tmp_path):
    return localcache.connect(tmp_path / "karaoke.db")


def test_stage_list_show_and_approve_into_local_cache(tmp_path):
    conn = _conn(tmp_path)
    staged_id = staging.stage_lyrics(
        "The Cure", "A Forest", Lyrics(plain="Come closer and see", source="youtube_caption"),
        source_kind="youtube_caption_manual_en", source_url="https://youtu.be/x",
        confidence=0.65, conn=conn,
    )

    items = staging.list_staged(conn=conn)
    assert [i.id for i in items] == [staged_id]
    assert items[0].status == "pending"
    assert items[0].plain_lyrics == "Come closer and see"

    approved = staging.approve_staged(staged_id, conn=conn)
    assert approved.status == "approved"
    cached = localcache.get_cached_lyrics("The Cure", "A Forest", conn=conn)
    assert cached is not None
    assert cached.plain == "Come closer and see"
    assert cached.source == "youtube_caption_manual_en"


def test_reject_does_not_populate_local_cache(tmp_path):
    conn = _conn(tmp_path)
    staged_id = staging.stage_lyrics(
        "A", "B", Lyrics(plain="candidate", source="web"),
        source_kind="web", conn=conn,
    )

    rejected = staging.reject_staged(staged_id, conn=conn)
    assert rejected.status == "rejected"
    assert localcache.get_cached_lyrics("A", "B", conn=conn) is None


def test_stage_empty_lyrics_rejected(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        staging.stage_lyrics("A", "B", Lyrics(), source_kind="web", conn=conn)


# --- deduplication ---------------------------------------------------------

def _stage(conn, artist="Rick Astley", title="Never Gonna Give You Up",
           kind="youtube_caption", synced="[00:01.00] a"):
    from karaoke.lyrics import Lyrics
    return staging.stage_lyrics(
        artist, title, Lyrics(plain="a", synced_raw=synced, source=kind),
        source_kind=kind, conn=conn)


def test_restaging_the_same_track_does_not_duplicate(tmp_path):
    """The review queue filled with 18 copies of one song before this."""
    conn = localcache.connect(tmp_path / "k.db")
    ids = {_stage(conn) for _ in range(5)}
    assert len(ids) == 1
    assert len(staging.list_staged(status="all", conn=conn)) == 1


def test_restaging_refreshes_the_candidate(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    _stage(conn, synced="[00:01.00] old")
    _stage(conn, synced="[00:02.00] new")
    items = staging.list_staged(status="all", conn=conn)
    assert len(items) == 1
    assert "new" in items[0].synced_lyrics


def test_restaging_does_not_resurrect_a_rejection(tmp_path):
    """A candidate you rejected must not silently return as pending."""
    conn = localcache.connect(tmp_path / "k.db")
    sid = _stage(conn)
    staging.reject_staged(sid, conn=conn)
    _stage(conn)
    assert staging.get_staged(sid, conn=conn).status == "rejected"


def test_different_sources_stay_separate_candidates(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    _stage(conn, kind="youtube_caption")
    _stage(conn, kind="whisper")
    assert len(staging.list_staged(status="all", conn=conn)) == 2


def test_ensure_schema_folds_away_existing_duplicates(tmp_path):
    """Migration path for DBs that already accumulated copies."""
    conn = localcache.connect(tmp_path / "k.db")
    staging.ensure_schema(conn)
    # Recreate the pre-fix state: table present, no uniqueness constraint.
    conn.execute("DROP INDEX IF EXISTS idx_staged_lyrics_unique")
    try:
        conn.execute("ALTER TABLE staged_lyrics DROP CONSTRAINT staged_lyrics_key_source_kind_key")
    except Exception:
        pass  # Ignore if constraint doesn't exist or we're on SQLite
    conn.commit()
    now = 1.0
    for i in range(4):
        conn.execute(
            "INSERT INTO staged_lyrics (key, artist, title, source_kind, status,"
            " plain_lyrics, synced_lyrics, created_at, updated_at)"
            " VALUES (%s,%s,%s,%s,'pending',%s,%s,%s,%s)",
            (localcache._key("A", "B"), "A", "B", "yt", "p", f"s{i}", now, now))
    conn.commit()
    # Counted with raw SQL: list_staged calls ensure_schema, which would fold
    # them away before we could observe the duplicated state.
    assert conn.execute("SELECT count(*) as count FROM staged_lyrics").fetchone()["count"] == 4

    staging.ensure_schema(conn)
    items = staging.list_staged(status="all", conn=conn)
    assert len(items) == 1
    assert items[0].synced_lyrics == "s3"      # newest row survives
