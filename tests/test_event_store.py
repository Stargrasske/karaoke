"""Unified event store: append semantics, outbox claiming, transactionality."""
from __future__ import annotations

import pytest

from karaoke import event_store as es
from karaoke import localcache


@pytest.fixture
def conn():
    c = localcache.connect()
    es.ensure_schema(c, force=True)
    try:
        yield c
    finally:
        c.close()


def test_append_returns_id_and_is_readable(conn):
    """A appended event comes back on its aggregate's stream."""
    eid = es.append(conn, es.AGG_TRACK, 7, es.TRACK_PLAYED, {"artist": "A"})
    assert eid is not None

    stream = es.read_stream(conn, es.AGG_TRACK, 7)
    assert len(stream) == 1
    assert stream[0]["event_type"] == es.TRACK_PLAYED
    assert stream[0]["payload"] == {"artist": "A"}
    # JSONB round-trips as a dict, not a string.
    assert isinstance(stream[0]["payload"], dict)


def test_append_with_deterministic_id_is_idempotent(conn):
    """Re-appending the same natural key inserts nothing the second time."""
    eid = es.deterministic_id("play_events", 123)
    first = es.append(conn, es.AGG_TRACK, 7, es.TRACK_PLAYED, {}, event_id=eid)
    second = es.append(conn, es.AGG_TRACK, 7, es.TRACK_PLAYED, {}, event_id=eid)

    assert first is not None
    assert second is None, "duplicate append must be a no-op, not a second row"
    assert len(es.read_stream(conn, es.AGG_TRACK, 7)) == 1


def test_deterministic_id_is_stable_and_distinct():
    """Same key -> same id; different key -> different id."""
    assert es.deterministic_id("play_events", 1) == es.deterministic_id("play_events", 1)
    assert es.deterministic_id("play_events", 1) != es.deterministic_id("play_events", 2)
    assert es.deterministic_id("play_events", 1) != es.deterministic_id("queue_events", 1)


def test_stream_is_ordered_by_sequence_not_timestamp(conn):
    """Replay order is insertion order even when timestamps collide."""
    from datetime import datetime, timezone

    same = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for n in range(5):
        es.append(conn, es.AGG_TRACK, 9, f"E{n}", {"n": n}, created_at=same)

    got = [e["payload"]["n"] for e in es.read_stream(conn, es.AGG_TRACK, 9)]
    assert got == [0, 1, 2, 3, 4]


def test_rollback_leaves_no_event(conn):
    """The outbox guarantee: a failed domain write publishes nothing."""
    with pytest.raises(RuntimeError):
        with conn.transaction():
            es.append(conn, es.AGG_TRACK, 11, es.TRACK_PLAYED, {})
            raise RuntimeError("domain write failed")

    assert es.read_stream(conn, es.AGG_TRACK, 11) == []


def test_commit_persists_both_sides(conn):
    """The happy path of the same guarantee: both writes land together."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO play_events (ts, mode, artist, title, event)"
            " VALUES (%s, %s, %s, %s, %s)",
            (1.0, "radio", "A", "B", "play"),
        )
        es.append(conn, es.AGG_TRACK, 12, es.TRACK_PLAYED, {})

    assert len(es.read_stream(conn, es.AGG_TRACK, 12)) == 1
    row = conn.execute(
        "SELECT count(*) AS n FROM play_events WHERE artist = 'A'"
    ).fetchone()
    assert row["n"] == 1


def test_claim_and_mark_published_drains_backlog(conn):
    """Claimed events stop showing up as unpublished once marked."""
    for n in range(3):
        es.append(conn, es.AGG_TRACK, 13, es.TRACK_PLAYED, {"n": n})
    assert es.unpublished_count(conn) == 3

    with conn.transaction():
        batch = es.claim_unpublished(conn, limit=10)
        assert len(batch) == 3
        assert es.mark_published(conn, [e["event_id"] for e in batch]) == 3

    assert es.unpublished_count(conn) == 0


def test_mark_published_is_not_double_counted(conn):
    """Re-marking an already-published event changes no rows."""
    es.append(conn, es.AGG_TRACK, 14, es.TRACK_PLAYED, {})
    with conn.transaction():
        batch = es.claim_unpublished(conn, limit=10)
        ids = [e["event_id"] for e in batch]
        assert es.mark_published(conn, ids) == 1

    assert es.mark_published(conn, ids) == 0


def test_claim_respects_limit_and_order(conn):
    """The relay drains oldest-first in bounded batches."""
    for n in range(5):
        es.append(conn, es.AGG_TRACK, 15, es.TRACK_PLAYED, {"n": n})

    with conn.transaction():
        batch = es.claim_unpublished(conn, limit=2)
        assert [e["payload"]["n"] for e in batch] == [0, 1]


def test_mark_published_with_empty_iterable(conn):
    """No ids is a no-op, not a malformed ANY() query."""
    assert es.mark_published(conn, []) == 0


def test_append_many_is_idempotent(conn):
    """Batch append dedupes on event_id the same way single append does."""
    events = [
        {
            "event_id": es.deterministic_id("batch", n),
            "aggregate_type": es.AGG_TRACK,
            "aggregate_id": 16,
            "event_type": es.TRACK_PLAYED,
            "payload": {"n": n},
        }
        for n in range(4)
    ]
    assert es.append_many(conn, events) == 4
    assert es.append_many(conn, events) == 0
    assert len(es.read_stream(conn, es.AGG_TRACK, 16)) == 4


def test_append_many_empty(conn):
    assert es.append_many(conn, []) == 0


def test_recent_filters_by_type(conn):
    es.append(conn, es.AGG_TRACK, 17, es.TRACK_PLAYED, {})
    es.append(conn, es.AGG_TRACK, 17, es.QUEUE_ADVANCED, {})

    only = es.recent(conn, event_type=es.TRACK_PLAYED, limit=10)
    assert [e["event_type"] for e in only] == [es.TRACK_PLAYED]


def test_stats_counts_by_type_and_backlog(conn):
    es.append(conn, es.AGG_TRACK, 18, es.TRACK_PLAYED, {})
    es.append(conn, es.AGG_TRACK, 18, es.TRACK_PLAYED, {})
    es.append(conn, es.AGG_TRACK, 18, es.QUEUE_ADVANCED, {})

    s = es.stats(conn)
    assert s["total"] == 3
    assert s["by_type"][es.TRACK_PLAYED] == 2
    assert s["unpublished"] == 3


def test_oldest_unpublished_is_none_when_drained(conn):
    assert es.oldest_unpublished(conn) is None

    es.append(conn, es.AGG_TRACK, 30, es.TRACK_PLAYED, {})
    with conn.transaction():
        batch = es.claim_unpublished(conn, 10)
        es.mark_published(conn, [e["event_id"] for e in batch])

    assert es.oldest_unpublished(conn) is None


def test_oldest_unpublished_returns_the_earliest(conn):
    """Backlog age is measured from the oldest pending event, not the newest."""
    from datetime import datetime, timezone

    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 1, 1, tzinfo=timezone.utc)
    es.append(conn, es.AGG_TRACK, 31, es.TRACK_PLAYED, {}, created_at=old)
    es.append(conn, es.AGG_TRACK, 31, es.TRACK_PLAYED, {}, created_at=new)

    assert es.oldest_unpublished(conn) == old


def test_oldest_unpublished_ignores_dead_letters(conn):
    """A parked event must not masquerade as an ever-growing backlog age.

    Dead letters are reported separately; counting them here would leave the
    stall warning permanently on after a single poison event.
    """
    es.append(conn, es.AGG_TRACK, 32, es.TRACK_PLAYED, {})
    conn.execute("UPDATE events SET attempts = 99")

    assert es.oldest_unpublished(conn, max_attempts=5) is None


def test_track_key_normalises_case_and_whitespace():
    assert es.track_key("  Prince ", "1999") == es.track_key("prince", "1999")
    assert es.track_key("A", "B") != es.track_key("B", "A")


def test_resolve_track_keys_folds_stream_onto_real_id(conn):
    """A key-scoped event is re-keyed once the track exists."""
    row = conn.execute(
        "INSERT INTO tracks (artist, title) VALUES (%s, %s) RETURNING track_id",
        ("Resolvable", "Song"),
    ).fetchone()
    track_id = row["track_id"]

    es.append(
        conn,
        es.AGG_TRACK_KEY,
        es.track_key("Resolvable", "Song"),
        es.TRACK_CACHE_MISS,
        {},
    )
    assert es.read_stream(conn, es.AGG_TRACK, track_id) == []

    assert es.resolve_track_keys(conn) == 1
    stream = es.read_stream(conn, es.AGG_TRACK, track_id)
    assert [e["event_type"] for e in stream] == [es.TRACK_CACHE_MISS]


def test_resolve_track_keys_leaves_unknown_songs_alone(conn):
    """An event for a song not in the library stays key-scoped."""
    key = es.track_key("Not In Library", "At All")
    es.append(conn, es.AGG_TRACK_KEY, key, es.TRACK_CACHE_MISS, {})

    assert es.resolve_track_keys(conn) == 0
    assert len(es.read_stream(conn, es.AGG_TRACK_KEY, key)) == 1


# --- producer wiring -------------------------------------------------------

def test_log_event_emits_unified_event(conn):
    """localcache.log_event writes the legacy row and the unified event."""
    localcache.log_event(
        "radio", "play", artist="Wired", title="Up", source="yt", conn=conn
    )

    events = es.recent(conn, limit=10)
    assert len(events) == 1
    assert events[0]["event_type"] == es.TRACK_PLAYED
    assert events[0]["payload"]["mode"] == "radio"
    legacy = conn.execute(
        "SELECT count(*) AS n FROM play_events WHERE artist = 'Wired'"
    ).fetchone()
    assert legacy["n"] == 1


def test_log_event_uses_track_id_when_resolvable(conn):
    """A known track's play event is keyed by track id, not artist/title."""
    row = conn.execute(
        "INSERT INTO tracks (artist, title) VALUES (%s, %s) RETURNING track_id",
        ("Known", "Track"),
    ).fetchone()

    localcache.log_event("radio", "play", artist="Known", title="Track", conn=conn)

    stream = es.read_stream(conn, es.AGG_TRACK, row["track_id"])
    assert [e["event_type"] for e in stream] == [es.TRACK_PLAYED]
    assert stream[0]["payload"]["track_id"] == row["track_id"]


def test_log_event_falls_back_to_key_for_unknown_track(conn):
    """An unknown song still produces an event rather than being dropped."""
    localcache.log_event(
        "radio", "cache_miss", artist="Ghost", title="Song", conn=conn
    )

    stream = es.read_stream(
        conn, es.AGG_TRACK_KEY, es.track_key("Ghost", "Song")
    )
    assert [e["event_type"] for e in stream] == [es.TRACK_CACHE_MISS]


def test_log_event_maps_unknown_verb_to_uppercase(conn):
    """A new producer verb is passed through, not silently dropped."""
    localcache.log_event("radio", "novel_verb", artist="X", title="Y", conn=conn)

    assert es.recent(conn, limit=1)[0]["event_type"] == "NOVEL_VERB"


def test_record_queue_event_emits_unified_event(conn):
    """localcache.record_queue_event writes both sides and links them."""
    qid = localcache.record_queue_event(
        "advance", track_id=55, artist="Q", title="T", queue_index=2, conn=conn
    )

    stream = es.read_stream(conn, es.AGG_TRACK, 55)
    assert [e["event_type"] for e in stream] == [es.QUEUE_ADVANCED]
    assert stream[0]["payload"]["queue_event_id"] == qid
    assert stream[0]["payload"]["queue_index"] == 2
