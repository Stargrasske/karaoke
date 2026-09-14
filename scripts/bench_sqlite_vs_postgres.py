"""Read-only A/B benchmark: SQLite vs PostgreSQL on representative karaoke queries.

Runs the same logical queries the app actually issues against both engines and
reports median wall-clock over N repeats. Touches neither the write path nor the
running scan.
"""
from __future__ import annotations

import sqlite3
import statistics
import time
from pathlib import Path

import psycopg2

SQLITE_DB = Path.home() / ".local" / "share" / "karaoke" / "karaoke.db"
PG_URL = "postgresql://karaoke@localhost/karaoke"
REPEATS = 7

# (label, sqlite_sql, postgres_sql) — differ only in placeholder / dialect.
QUERIES = [
    (
        "library_browse (tracks + preferred source + approved lyrics)",
        """
        SELECT t.track_id, t.artist, t.title, t.album, t.duration,
               s.kind, s.url,
               l.synced_lyrics IS NOT NULL AS has_synced
        FROM tracks t
        LEFT JOIN sources s ON s.source_id = (
            SELECT source_id FROM sources WHERE track_id = t.track_id
            ORDER BY source_id LIMIT 1)
        LEFT JOIN lyrics l ON l.lyric_id = (
            SELECT lyric_id FROM lyrics
            WHERE track_id = t.track_id AND kind = 'approved'
            ORDER BY lyric_id DESC LIMIT 1)
        ORDER BY t.artist, t.title
        """,
        None,  # identical SQL works on both here
    ),
    (
        "smartlist load_candidates (tracks JOIN approved lyrics + analysis)",
        """
        SELECT t.track_id, t.artist, t.title,
               COALESCE(l.plain_lyrics, l.synced_lyrics, '') AS words,
               a.bpm, a.energy
        FROM tracks t
        JOIN lyrics l ON l.track_id = t.track_id AND l.kind = 'approved'
        LEFT JOIN track_analysis a ON a.track_id = t.track_id
        WHERE length(COALESCE(l.plain_lyrics, l.synced_lyrics, '')) > 40
        """,
        None,
    ),
    (
        "keyword search (artist/title LIKE)",
        """
        SELECT t.track_id, t.artist, t.title
        FROM tracks t
        WHERE lower(t.artist) LIKE '%love%' OR lower(t.title) LIKE '%love%'
        ORDER BY t.artist, t.title
        """,
        None,
    ),
    (
        "genre facet count",
        """
        SELECT genre, count(*) AS n
        FROM track_genre
        GROUP BY genre
        ORDER BY n DESC
        """,
        None,
    ),
    (
        "point lookup by artist/title (case-insensitive)",
        "SELECT track_id FROM tracks WHERE lower(artist)=lower('Portishead') AND lower(title)=lower('Glory Box')",
        None,
    ),
]


def bench(run_once, repeats=REPEATS):
    times = []
    rows = 0
    for _ in range(repeats):
        t0 = time.perf_counter()
        rows = run_once()
        times.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(times), min(times), rows


def main():
    sconn = sqlite3.connect(SQLITE_DB)
    sconn.row_factory = sqlite3.Row
    pconn = psycopg2.connect(PG_URL)

    print(f"{'query':<58} {'SQLite(ms)':>12} {'PG(ms)':>10} {'rows':>7}  winner")
    print("-" * 100)
    try:
        for label, ssql, psql in QUERIES:
            psql = psql or ssql

            def run_sqlite():
                cur = sconn.execute(ssql)
                return len(cur.fetchall())

            def run_pg():
                with pconn.cursor() as c:
                    c.execute(psql)
                    return len(c.fetchall())

            s_med, s_min, s_rows = bench(run_sqlite)
            p_med, p_min, p_rows = bench(run_pg)
            winner = "SQLite" if s_med < p_med else "Postgres"
            ratio = (max(s_med, p_med) / max(min(s_med, p_med), 1e-6))
            print(f"{label[:58]:<58} {s_med:>9.2f}    {p_med:>7.2f}   {s_rows:>7}  "
                  f"{winner} ({ratio:.1f}x)")
    finally:
        sconn.close()
        pconn.close()


if __name__ == "__main__":
    main()
