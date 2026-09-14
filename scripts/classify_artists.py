#!/usr/bin/env python3
"""Batch classify artists in the Karaoke library into broad genres.

Uses:
1. Curated offline seed database of canonical artists (Django Reinhardt, B.B. King, etc.).
2. MusicBrainz API consensus tags and Wikidata fallbacks.
3. 16 Canonical Broad Genres taxonomy.
4. Caches results into SQLite `artist_genres` table.

Usage:
    python scripts/classify_artists.py [--limit N] [--offline] [--force] [--db PATH]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Ensure src/ is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import artist_classifier, localcache
from karaoke.config import settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Classify artists in the karaoke database into broad genres."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of unclassified artists to process.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not make external API requests (use offline seed list + heuristics only).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-classify artists even if they already exist in artist_genres.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to SQLite database (defaults to settings.local_db).",
    )
    args = parser.parse_args()

    db_path = args.db or settings.local_db
    print(f"Connecting to database: {db_path}")
    conn = localcache.connect(db_path)

    # 1. Seed database with curated canonical artist list
    seeded_count = artist_classifier.seed_database(conn)
    print(f"Curated seed list loaded ({seeded_count} canonical artists).")

    # 2. Query distinct artists sorted by track frequency
    cur = conn.execute(
        """
        SELECT artist, count(*) as track_count
        FROM tracks
        WHERE artist IS NOT NULL AND length(trim(artist)) > 0
        GROUP BY artist
        ORDER BY track_count DESC
        """
    )
    all_artists = cur.fetchall()
    total_artists = len(all_artists)
    print(f"Total distinct artists in library: {total_artists}")

    # 3. Check existing classified artists
    already_classified: set[str] = set()
    if not args.force:
        c_exist = conn.execute("SELECT DISTINCT artist_normalized FROM artist_genres")
        already_classified = {str(r[0]).casefold() for r in c_exist.fetchall()}
        print(f"Already classified: {len(already_classified)} artists.")

    unclassified = [
        (str(r["artist"]).strip(), int(r["track_count"]))
        for r in all_artists
        if args.force or artist_classifier.normalize_artist(str(r["artist"])) not in already_classified
    ]

    if args.limit:
        unclassified = unclassified[: args.limit]

    print(f"Artists to classify in this run: {len(unclassified)}")
    if not unclassified:
        print("All artists are already classified!")
        _print_genre_stats(conn)
        return 0

    processed = 0
    start_time = time.time()
    for artist, count in unclassified:
        processed += 1
        spec, broad = artist_classifier.classify_artist(
            artist,
            conn=conn,
            online=not args.offline,
        )
        spec_str = ", ".join(spec[:3]) if spec else "unknown"
        broad_str = ", ".join(broad) if broad else "unmapped"
        print(
            f"[{processed}/{len(unclassified)}] '{artist}' ({count} tracks) -> "
            f"broad: [{broad_str}] (specific: {spec_str})"
        )

    elapsed = time.time() - start_time
    print(f"\nFinished classifying {processed} artists in {elapsed:.1f}s.")
    _print_genre_stats(conn)
    return 0


def _print_genre_stats(conn) -> None:
    print("\n--- Broad Genre Distribution ---")
    cur = conn.execute(
        """
        SELECT ag.broad_genre, count(DISTINCT ag.artist_normalized) as artist_count,
               count(DISTINCT t.track_id) as track_count
        FROM artist_genres ag
        LEFT JOIN tracks t ON lower(trim(t.artist)) = ag.artist_normalized
        GROUP BY ag.broad_genre
        ORDER BY track_count DESC
        """
    )
    rows = cur.fetchall()
    print(f"{'Broad Genre':<16} | {'Artists':<8} | {'Tracks (approx)':<14}")
    print("-" * 44)
    for r in rows:
        bg = str(r[0])
        artists = r[1]
        tracks = r[2]
        print(f"{bg:<16} | {artists:<8} | {tracks:<14}")


if __name__ == "__main__":
    sys.exit(main())
