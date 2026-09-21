"""Unified append-only event store (Stage 2 of the Postgres migration).

Historically every domain kept its own event table — ``play_events``,
``queue_events``, ``track_analysis_history``, ``alignment_support`` — each with
a different timestamp column, a different notion of "what happened", and no way
to ask "what happened to this track, in order, across all domains?".

This module adds one ``events`` table that all domains append to, plus the
outbox bookkeeping a relay needs to forward events to external consumers
(OpenSearch, RabbitMQ) exactly once.

Not to be confused with :mod:`karaoke.events`, which *reads* the derived
``karaoke-events`` OpenSearch index written by Argo Events. That module observes
Celery task lifecycle from the cluster side; this one is the durable
system-of-record on the host.

Transactionality
----------------
The connection pool runs with ``autocommit=True``, so a bare :func:`append` is
committed on its own. The outbox guarantee — a domain change and its event land
together or not at all — requires an explicit transaction block::

    with conn.transaction():
        conn.execute("UPDATE tracks SET play_count = play_count + 1 WHERE ...")
        event_store.append(conn, "track", track_id, TRACK_PLAYED, {...})

Both statements commit at the end of the ``with``; if either raises, neither is
durable and no half-published event escapes.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable, Optional

import psycopg
from psycopg import Connection
from psycopg.types.json import Jsonb

from .logger import log

# Namespace for deterministic (uuid5) event ids. Backfills and any other
# replayable producer derive ids from a natural key so a re-run collides with
# the row it already wrote instead of duplicating it.
NAMESPACE = uuid.UUID("6f2a1c74-0f3b-5e8a-9d21-4c7b8e05a3f1")

# Aggregate types. The plan's schema keyed events on `aggregate_id` alone, but
# a bare "42" is ambiguous between track 42 and radio session 42, so the type
# is stored alongside it and every index leads with it.
AGG_TRACK = "track"
AGG_QUEUE = "queue"
AGG_SESSION = "session"
AGG_PLAYLIST = "playlist"
# Some legacy producers are artist/title-keyed and never learn a track_id (a
# radio `cache_miss` fires for songs that are not in the library at all). Those
# events are keyed by the normalised artist/title instead, so an event is never
# dropped just because the track is unknown. See `resolve_track_keys` for the
# pass that folds these into AGG_TRACK once an id exists.
AGG_TRACK_KEY = "track_key"

# Event types, uppercase snake per the migration plan.
TRACK_PLAYED = "TRACK_PLAYED"
TRACK_DISCOVERED = "TRACK_DISCOVERED"
TRACK_LYRICS_MISSING = "TRACK_LYRICS_MISSING"
TRACK_CACHE_HIT = "TRACK_CACHE_HIT"
TRACK_CACHE_MISS = "TRACK_CACHE_MISS"
TRACK_RELOCKED = "TRACK_RELOCKED"
QUEUE_ADVANCED = "QUEUE_ADVANCED"
QUEUE_FOLLOWED = "QUEUE_FOLLOWED"
QUEUE_LOADED = "QUEUE_LOADED"
QUEUE_SYNCED = "QUEUE_SYNCED"
ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED"

# play_events.event -> unified event type. Legacy values are lowercase verbs;
# anything unmapped is passed through uppercased so a new producer is never
# silently dropped.
PLAY_EVENT_TYPES = {
    "play": TRACK_PLAYED,
    "discover": TRACK_DISCOVERED,
    "no_lyrics": TRACK_LYRICS_MISSING,
    "cache_hit": TRACK_CACHE_HIT,
    "cache_miss": TRACK_CACHE_MISS,
    "relock": TRACK_RELOCKED,
}

# queue_events.event_type -> unified event type.
QUEUE_EVENT_TYPES = {
    "advance": QUEUE_ADVANCED,
    "follow": QUEUE_FOLLOWED,
    "loaded": QUEUE_LOADED,
    "ytmusic_sync": QUEUE_SYNCED,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    -- Monotonic within a single writer; used for ordered replay of one
    -- aggregate's stream. Deliberately NOT the relay cursor -- concurrent
    -- inserts make sequence values visible out of order, which would let a
    -- high-water-mark cursor skip a straggler. The relay claims on
    -- published_at instead.
    seq            BIGSERIAL    NOT NULL,
    event_id       UUID         PRIMARY KEY,
    aggregate_type TEXT         NOT NULL,
    aggregate_id   TEXT         NOT NULL,
    event_type     TEXT         NOT NULL,
    payload        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- Outbox bookkeeping: NULL means "not yet forwarded to consumers".
    published_at   TIMESTAMPTZ
);

-- Delivery bookkeeping, added after the initial cut. Without an attempt
-- counter a single undeliverable event sits at the head of the queue forever
-- and every later event starves behind it; past MAX_ATTEMPTS the relay stops
-- claiming it and it becomes a dead letter for an operator to look at.
ALTER TABLE events ADD COLUMN IF NOT EXISTS attempts   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS failed_at  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_events_aggregate
    ON events (aggregate_type, aggregate_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type_created
    ON events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_created
    ON events (created_at DESC);
-- Partial index: the outbox backlog is normally tiny next to the full table,
-- so the relay's hot query touches only pending rows.
CREATE INDEX IF NOT EXISTS idx_events_unpublished
    ON events (seq) WHERE published_at IS NULL;

-- Wake the relay the moment an event commits, so an idle system delivers in
-- milliseconds instead of waiting out the poll interval. NOTIFY is
-- transactional -- it fires on commit and not on rollback -- which is exactly
-- the outbox's own guarantee, so a rolled-back event never wakes anyone.
-- Statement-level (not row-level) so a 1000-row backfill batch sends one
-- notification rather than a thousand.
CREATE OR REPLACE FUNCTION karaoke_events_notify() RETURNS trigger AS $fn$
BEGIN
    PERFORM pg_notify('karaoke_events', '');
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_events_notify ON events;
CREATE TRIGGER trg_events_notify
    AFTER INSERT ON events
    FOR EACH STATEMENT EXECUTE FUNCTION karaoke_events_notify();
"""

# Channel the trigger above notifies; the relay LISTENs on it.
NOTIFY_CHANNEL = "karaoke_events"

# How many times the relay retries an event before parking it as a dead letter.
MAX_ATTEMPTS = 5


# Set once the table is known to exist in this process. There is no central
# schema-init hook in localcache (its _SCHEMA constants are vestigial; the
# real DDL lives in the migration script), so producers call ensure_schema
# themselves — this flag keeps that call off the radio-mode hot path after the
# first one.
_ensured = False


def ensure_schema(conn: Connection, *, force: bool = False) -> None:
    """Create the events table and its indexes (idempotent, once per process).

    Must be called outside an open transaction: a failing DDL statement would
    otherwise poison the caller's transaction block.
    """
    global _ensured
    if _ensured and not force:
        return
    try:
        conn.execute(_SCHEMA)
        conn.commit()
        _ensured = True
    except psycopg.Error as exc:
        log.debug("event store schema setup skipped: %s", exc)


def deterministic_id(*parts: Any) -> uuid.UUID:
    """Stable event id derived from a natural key.

    Lets a replayable producer (a backfill, a retried task) generate the same
    id for the same logical event, so ``ON CONFLICT DO NOTHING`` makes the
    write idempotent.
    """
    return uuid.uuid5(NAMESPACE, "\x1f".join(str(p) for p in parts))


def append(
    conn: Connection,
    aggregate_type: str,
    aggregate_id: Any,
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    event_id: Optional[uuid.UUID] = None,
    created_at: Optional[Any] = None,
) -> Optional[str]:
    """Append one event; return its id, or ``None`` if it already existed.

    Does not commit — the caller owns the transaction, which is what makes the
    outbox guarantee possible. Passing ``event_id`` (see :func:`deterministic_id`)
    makes the append idempotent.
    """
    eid = event_id or uuid.uuid4()
    cols = ["event_id", "aggregate_type", "aggregate_id", "event_type", "payload"]
    vals: list[Any] = [
        str(eid),
        aggregate_type,
        str(aggregate_id),
        event_type,
        Jsonb(payload or {}),
    ]
    if created_at is not None:
        cols.append("created_at")
        vals.append(created_at)

    placeholders = ", ".join(["%s"] * len(cols))
    cur = conn.execute(
        f"INSERT INTO events ({', '.join(cols)}) VALUES ({placeholders})"
        " ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
        vals,
    )
    row = cur.fetchone()
    return str(row["event_id"]) if row else None


def append_many(conn: Connection, events: Iterable[dict[str, Any]]) -> int:
    """Append a batch of events; return how many were newly inserted.

    Each mapping takes the same keys as :func:`append`'s arguments. Used by the
    backfill, where per-row round trips would dominate the runtime.
    """
    rows = [
        (
            str(e.get("event_id") or uuid.uuid4()),
            e["aggregate_type"],
            str(e["aggregate_id"]),
            e["event_type"],
            Jsonb(e.get("payload") or {}),
            e.get("created_at"),
        )
        for e in events
    ]
    if not rows:
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                "INSERT INTO events"
                " (event_id, aggregate_type, aggregate_id, event_type, payload, created_at)"
                " VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))"
                " ON CONFLICT (event_id) DO NOTHING",
                row,
            )
            inserted += cur.rowcount
    return inserted


def read_stream(
    conn: Connection,
    aggregate_type: str,
    aggregate_id: Any,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Every event for one aggregate, oldest first — the replay path."""
    cur = conn.execute(
        "SELECT event_id, aggregate_type, aggregate_id, event_type, payload,"
        " created_at, published_at FROM events"
        " WHERE aggregate_type = %s AND aggregate_id = %s"
        " ORDER BY seq ASC LIMIT %s",
        (aggregate_type, str(aggregate_id), limit),
    )
    return list(cur.fetchall())


def recent(
    conn: Connection,
    *,
    event_type: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Newest events first, optionally narrowed to one type."""
    if event_type:
        cur = conn.execute(
            "SELECT event_id, aggregate_type, aggregate_id, event_type, payload,"
            " created_at, published_at FROM events WHERE event_type = %s"
            " ORDER BY created_at DESC, seq DESC LIMIT %s",
            (event_type, limit),
        )
    else:
        cur = conn.execute(
            "SELECT event_id, aggregate_type, aggregate_id, event_type, payload,"
            " created_at, published_at FROM events"
            " ORDER BY created_at DESC, seq DESC LIMIT %s",
            (limit,),
        )
    return list(cur.fetchall())


def claim_unpublished(
    conn: Connection,
    limit: int = 100,
    *,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[dict[str, Any]]:
    """Lock and return a batch of unforwarded events for the relay.

    ``FOR UPDATE SKIP LOCKED`` lets several relay workers drain the backlog
    concurrently without handing the same event to two of them. The rows stay
    locked until the caller's transaction ends, so this must run inside one::

        with conn.transaction():
            batch = claim_unpublished(conn)
            ...publish...
            mark_published(conn, [e["event_id"] for e in batch])

    Events that have failed ``max_attempts`` times are skipped, so one
    undeliverable event cannot starve everything queued behind it.
    """
    cur = conn.execute(
        "SELECT event_id, aggregate_type, aggregate_id, event_type, payload,"
        " created_at, attempts FROM events"
        " WHERE published_at IS NULL AND attempts < %s"
        " ORDER BY seq ASC LIMIT %s FOR UPDATE SKIP LOCKED",
        (max_attempts, limit),
    )
    return list(cur.fetchall())


def record_failure(conn: Connection, event_ids: Iterable[Any], error: str) -> int:
    """Count a failed delivery attempt against each event.

    Must run in its own transaction, *after* the publishing transaction has
    rolled back — otherwise the increment rolls back with it and the event
    retries forever without ever reaching the dead-letter threshold.
    """
    ids = [str(e) for e in event_ids]
    if not ids:
        return 0
    cur = conn.execute(
        "UPDATE events"
        "   SET attempts = attempts + 1,"
        "       last_error = %s,"
        "       failed_at = now()"
        " WHERE event_id = ANY(%s::uuid[]) AND published_at IS NULL",
        (error[:2000], ids),
    )
    return cur.rowcount


def dead_letters(
    conn: Connection,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Events the relay has given up on, newest failure first."""
    cur = conn.execute(
        "SELECT event_id, aggregate_type, aggregate_id, event_type, payload,"
        " created_at, attempts, last_error, failed_at FROM events"
        " WHERE published_at IS NULL AND attempts >= %s"
        " ORDER BY failed_at DESC NULLS LAST LIMIT %s",
        (max_attempts, limit),
    )
    return list(cur.fetchall())


def dead_letter_count(conn: Connection, *, max_attempts: int = MAX_ATTEMPTS) -> int:
    """How many events the relay has parked. Alert if this is not zero."""
    cur = conn.execute(
        "SELECT count(*) AS n FROM events"
        " WHERE published_at IS NULL AND attempts >= %s",
        (max_attempts,),
    )
    row = cur.fetchone()
    return int(row["n"]) if row else 0


def retry_dead_letters(
    conn: Connection,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    event_ids: Optional[Iterable[Any]] = None,
) -> int:
    """Reset the attempt counter so parked events are claimable again.

    The operator recovery path: fix the consumer, then requeue. Without
    ``event_ids`` every dead letter is retried.
    """
    if event_ids is not None:
        ids = [str(e) for e in event_ids]
        if not ids:
            return 0
        cur = conn.execute(
            "UPDATE events SET attempts = 0, last_error = NULL, failed_at = NULL"
            " WHERE event_id = ANY(%s::uuid[]) AND published_at IS NULL",
            (ids,),
        )
    else:
        cur = conn.execute(
            "UPDATE events SET attempts = 0, last_error = NULL, failed_at = NULL"
            " WHERE published_at IS NULL AND attempts >= %s",
            (max_attempts,),
        )
    return cur.rowcount


def mark_published(conn: Connection, event_ids: Iterable[Any]) -> int:
    """Mark events as forwarded; return how many rows changed."""
    ids = [str(e) for e in event_ids]
    if not ids:
        return 0
    cur = conn.execute(
        "UPDATE events SET published_at = now()"
        " WHERE event_id = ANY(%s::uuid[]) AND published_at IS NULL",
        (ids,),
    )
    return cur.rowcount


def unpublished_count(conn: Connection) -> int:
    """Size of the outbox backlog — the number to alert on if it grows."""
    cur = conn.execute("SELECT count(*) AS n FROM events WHERE published_at IS NULL")
    row = cur.fetchone()
    return int(row["n"]) if row else 0


def track_key(artist: str, title: str) -> str:
    """Normalised artist/title aggregate id for events with no track_id."""
    return f"{(artist or '').strip().casefold()}\x1f{(title or '').strip().casefold()}"


def resolve_track_keys(conn: Connection, *, limit: int = 5000) -> int:
    """Re-key ``track_key`` events onto real track ids where one now exists.

    A ``cache_miss`` for an unknown song becomes a real track once the scanner
    imports it. This folds those orphaned streams into the track's own stream
    so a later replay sees one ordered history instead of two fragments.
    Returns the number of events re-keyed.
    """
    cur = conn.execute(
        """
        UPDATE events e
           SET aggregate_type = %s,
               aggregate_id   = t.track_id::text
          FROM tracks t
         WHERE e.aggregate_type = %s
           AND e.aggregate_id = ANY(
                 SELECT aggregate_id FROM events
                  WHERE aggregate_type = %s LIMIT %s)
           AND e.aggregate_id =
                 lower(btrim(t.artist)) || E'\\x1f' || lower(btrim(t.title))
        """,
        (AGG_TRACK, AGG_TRACK_KEY, AGG_TRACK_KEY, limit),
    )
    return cur.rowcount


def oldest_unpublished(
    conn: Connection, *, max_attempts: int = MAX_ATTEMPTS
) -> Optional[Any]:
    """``created_at`` of the oldest still-claimable event, or ``None``.

    Backlog *age* is the honest signal that the relay has stalled. Backlog
    *size* is not: a bulk import or a backfill legitimately queues tens of
    thousands of events at once, and alerting on a count would cry wolf every
    time while still missing a relay that quietly died with three events
    pending.

    Ordering by ``seq`` rather than ``created_at`` lets this ride the partial
    index on unpublished rows instead of scanning them.
    """
    cur = conn.execute(
        "SELECT created_at FROM events"
        " WHERE published_at IS NULL AND attempts < %s"
        " ORDER BY seq ASC LIMIT 1",
        (max_attempts,),
    )
    row = cur.fetchone()
    return row["created_at"] if row else None


def stats(conn: Connection, *, max_attempts: int = MAX_ATTEMPTS) -> dict[str, Any]:
    """Counts by event type plus the outbox backlog, for `karaoke-stats`.

    ``max_attempts`` must match the relay's setting, or the dead-letter split
    will not describe the relay actually running.
    """
    cur = conn.execute(
        "SELECT event_type, count(*) AS n FROM events"
        " GROUP BY event_type ORDER BY n DESC"
    )
    by_type = {r["event_type"]: int(r["n"]) for r in cur.fetchall()}
    pending = unpublished_count(conn)
    dead = dead_letter_count(conn, max_attempts=max_attempts)
    return {
        "total": sum(by_type.values()),
        "by_type": by_type,
        # `unpublished` is the whole backlog including parked events;
        # `claimable` is what the relay will actually attempt.
        "unpublished": pending,
        "dead_letters": dead,
        "claimable": pending - dead,
    }
