"""Automated lyric backfill system."""
from __future__ import annotations
import argparse
from typing import Optional

from . import localcache

def backfill_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke-backfill` CLI to fill lyric gaps."""
    ap = argparse.ArgumentParser(
        prog="karaoke-backfill",
        description="Automated lyric backfill system",
    )
    ap.add_argument("--list", action="store_true", help="list gaps")
    ap.add_argument("--run", action="store_true", help="run the backfill process")
    ap.add_argument("--retry-failed", action="store_true",
                    help="also reprocess gaps previously marked failed")
    ap.add_argument("--failed-only", action="store_true",
                    help="reprocess only gaps previously marked failed")
    ap.add_argument("--status", default="pending",
                    help="status to list: pending | failed | processed | all")
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N gaps this run")
    args = ap.parse_args(argv)

    if args.list:
        with localcache.connect() as conn:
            cur = conn.execute(
                """
                SELECT artist, title, status, attempts, last_error FROM lyric_gaps
                WHERE (%s = 'all' OR status = %s)
                ORDER BY status, gap_id
                """,
                (args.status, args.status),
            )
            for row in cur:
                line = f"{row['artist']} - {row['title']}"
                if args.status != "pending":
                    line += f"  [{row['status']} x{row['attempts']}]"
                    if row["last_error"]:
                        line += f" {row['last_error']}"
                print(line)
        return 0

    if args.run:
        from . import backfill_runner
        backfill_runner.run(
            retry_failed=args.retry_failed,
            failed_only=args.failed_only,
            limit=args.limit,
        )
        return 0

    return 0
