#!/usr/bin/env python
"""Backfill the unified `events` table from the legacy per-domain tables.

Reads `play_events`, `queue_events` and `track_analysis_history` and appends an
equivalent unified event for each row. Event ids are derived from the source
table and primary key (`event_store.deterministic_id`), so the script is
idempotent — a second run inserts nothing rather than duplicating history.

The legacy tables are left untouched. They remain the system of record until
the relay and its consumers are proven against the unified log.

Usage:
    python scripts/backfill_event_store.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import event_store as es  # noqa: E402
from karaoke import localcache  # noqa: E402

BATCH = 1000


def _batches(rows: list[dict], size: int = BATCH):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def backfill_play_events(conn, limit: int | None, dry_run: bool) -> int:
    sql = (
        "SELECT id, ts, mode, artist, title, event, source, has_synced"
        " FROM play_events ORDER BY id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = list(conn.execute(sql).fetchall())

    events = [
        {
            "event_id": es.deterministic_id("play_events", r["id"]),
            "aggregate_type": es.AGG_TRACK_KEY,
            "aggregate_id": es.track_key(r["artist"] or "", r["title"] or ""),
            "event_type": es.PLAY_EVENT_TYPES.get(
                r["event"], (r["event"] or "UNKNOWN").upper()
            ),
            "payload": {
                "artist": r["artist"] or "",
                "title": r["title"] or "",
                "mode": r["mode"],
                "source": r["source"] or "",
                "has_synced": bool(r["has_synced"]),
                "legacy_id": r["id"],
            },
            "created_at": _ts(conn, r["ts"]),
        }
        for r in rows
    ]
    return _write(conn, events, dry_run)


def backfill_queue_events(conn, limit: int | None, dry_run: bool) -> int:
    import json

    sql = (
        "SELECT id, ts, event_type, track_id, artist, title, queue_index, payload_json"
        " FROM queue_events ORDER BY id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = list(conn.execute(sql).fetchall())

    events = []
    for r in rows:
        try:
            meta = json.loads(r["payload_json"] or "{}")
        except (ValueError, TypeError):
            # A malformed legacy payload must not abort the whole backfill;
            # the raw text is preserved so nothing is lost.
            meta = {"_unparsed": r["payload_json"]}

        if r["track_id"] is not None:
            agg_type, agg_id = es.AGG_TRACK, r["track_id"]
        else:
            agg_type = es.AGG_TRACK_KEY
            agg_id = es.track_key(r["artist"] or "", r["title"] or "")

        events.append(
            {
                "event_id": es.deterministic_id("queue_events", r["id"]),
                "aggregate_type": agg_type,
                "aggregate_id": agg_id,
                "event_type": es.QUEUE_EVENT_TYPES.get(
                    r["event_type"], (r["event_type"] or "UNKNOWN").upper()
                ),
                "payload": {
                    "artist": r["artist"] or "",
                    "title": r["title"] or "",
                    "track_id": r["track_id"],
                    "queue_index": r["queue_index"],
                    "metadata": meta,
                    "legacy_id": r["id"],
                },
                "created_at": _ts(conn, r["ts"]),
            }
        )
    return _write(conn, events, dry_run)


def backfill_analysis_history(conn, limit: int | None, dry_run: bool) -> int:
    sql = (
        "SELECT history_id, track_id, source_kind, detected_key, key_confidence,"
        " key_agreement, bpm, method, energy, brightness, analyzer_version, created_at"
        " FROM track_analysis_history ORDER BY history_id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = list(conn.execute(sql).fetchall())

    events = [
        {
            "event_id": es.deterministic_id("track_analysis_history", r["history_id"]),
            "aggregate_type": es.AGG_TRACK,
            "aggregate_id": r["track_id"],
            "event_type": es.ANALYSIS_COMPLETED,
            "payload": {
                "source_kind": r["source_kind"],
                "detected_key": r["detected_key"],
                "key_confidence": r["key_confidence"],
                "key_agreement": r["key_agreement"],
                "bpm": r["bpm"],
                "method": r["method"],
                "energy": r["energy"],
                "brightness": r["brightness"],
                "analyzer_version": r["analyzer_version"],
                "legacy_id": r["history_id"],
            },
            "created_at": _ts(conn, r["created_at"]),
        }
        for r in rows
    ]
    return _write(conn, events, dry_run)


def _ts(conn, epoch):
    """Epoch float -> aware datetime, matching events.created_at (TIMESTAMPTZ)."""
    if epoch is None:
        return None
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _write(conn, events: list[dict], dry_run: bool) -> int:
    if dry_run:
        return len(events)
    total = 0
    for chunk in _batches(events):
        with conn.transaction():
            total += es.append_many(conn, chunk)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="max rows per source table (default: all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written without writing")
    args = ap.parse_args()

    conn = localcache.connect()
    try:
        es.ensure_schema(conn)
        before = es.stats(conn)["total"]

        plays = backfill_play_events(conn, args.limit, args.dry_run)
        queue = backfill_queue_events(conn, args.limit, args.dry_run)
        analysis = backfill_analysis_history(conn, args.limit, args.dry_run)

        verb = "would append" if args.dry_run else "appended"
        print(f"play_events            {verb}: {plays}")
        print(f"queue_events           {verb}: {queue}")
        print(f"track_analysis_history {verb}: {analysis}")

        if not args.dry_run:
            rekeyed = 0
            with conn.transaction():
                rekeyed = es.resolve_track_keys(conn, limit=100000)
            after = es.stats(conn)
            print(f"re-keyed to real track ids: {rekeyed}")
            print(f"events total: {before} -> {after['total']}")
            print(f"outbox backlog: {after['unpublished']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
