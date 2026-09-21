"""Build a small, shareable demo database from the radio-derived tracks.

The fork (Stargrasske/karaoke, PR #84) has a frontend but no data to render.
Most of the radio recordings here are her music, so they are the natural seed.
This carves those tracks out of the pre-migration SQLite library and writes two
files:

    demo/karaoke-demo.db   SQLite, the schema her branch still expects
    demo/karaoke-demo.sql  Postgres INSERTs, for after she rebases onto main

The source is ``~/.local/share/karaoke/karaoke.db`` -- the SQLite file left
behind by the Postgres migration. It is already in her schema, so nothing has
to be migrated backwards.

Neither output carries a schema. The SQLite file reuses the source's own
CREATE statements; the Postgres file is data-only and expects a database
already built from POSTGRES_DDL in ``migrate_sqlite_to_postgres.py``, which is
the single schema authority (``tests/conftest.py`` parses the same constant).

What ships, and why:

    tracks, sources          the library and where each track plays from
    lyrics                   the karaoke view is the point of the demo
    track_analysis           key/BPM/energy drive browse and harmonic sort
    track_genre, track_tone  genre filter and mood labels
    track_art, cover_art     cover thumbnails in browse
    track_sync_offsets       per-track lyric timing corrections
    artist_genres            genre rollup for the demo's artists

Deliberately withheld: play_events, queue_events, radio_sessions,
saved_playlists and saved_searches are a listening history rather than a
library; recordings and recording_* reference audio files that are not shipped;
staged_lyrics, lyric_gaps, spotify_lookups, restricted_tracks and
track_analysis_history are working state; events and outbox belong to the
relay. OpenSearch vectors are excluded too -- they are rebuildable and useless
without a cluster.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_SOURCE = Path.home() / ".local" / "share" / "karaoke" / "karaoke.db"
DEFAULT_OUT_DIR = Path("demo")
MIGRATION_SCRIPT = Path(__file__).with_name("migrate_sqlite_to_postgres.py")

# Radio-derived analysis rows: tracks identified off a recorded stream rather
# than from a local file or a search.
DEMO_TRACK_QUERY = """
    SELECT DISTINCT track_id FROM track_analysis
    WHERE method LIKE '%sample%' OR method LIKE '%recording%'
"""

# Tables scoped by track_id, in foreign-key order: tracks first, everything
# else references it.
TRACK_SCOPED = [
    "tracks",
    "sources",
    "lyrics",
    "track_analysis",
    "track_genre",
    "track_tone",
    "track_art",
    "track_sync_offsets",
]

# Extra conditions on a track-scoped table's rows.
FILTERS = {
    # 'local' sources are absolute paths into this machine's youtube cache and
    # recording directories. They leak a filesystem layout, and the audio is
    # not shipped, so on any other machine they are dead entries. Remote
    # sources (youtube, spotify, youtube_music) survive the handover.
    "sources": "kind <> 'local'",
}

# Tables reached through another key rather than track_id. Each is a SQL
# fragment taking the already-selected rows as its input.
DERIVED = {
    # Cover pixels are keyed by art_key, which track_art points at.
    "cover_art": (
        "SELECT * FROM cover_art WHERE art_key IN "
        "(SELECT art_key FROM track_art WHERE track_id IN ({ids}))"
    ),
    # Genre rollups are per artist, normalised the same way the app does it.
    "artist_genres": (
        "SELECT * FROM artist_genres WHERE artist_normalized IN "
        "(SELECT DISTINCT lower(trim(artist)) FROM tracks WHERE track_id IN ({ids}))"
    ),
}

# Postgres SERIAL columns needing their sequence advanced past the copied rows,
# or her first insert collides with an existing id.
SEQUENCES = {
    "tracks": "track_id",
    "sources": "source_id",
    "lyrics": "lyric_id",
}


def connect_source(path: Path) -> sqlite3.Connection:
    """Open the source library read-only, so a build can never alter it."""
    if not path.exists():
        raise SystemExit(f"Source database not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def demo_track_ids(conn: sqlite3.Connection) -> list[int]:
    ids = [r[0] for r in conn.execute(DEMO_TRACK_QUERY)]
    if not ids:
        raise SystemExit(
            "No radio-derived tracks found. Either the source library predates "
            "recording analysis, or track_analysis.method no longer says "
            "'sample'/'recording'."
        )
    return sorted(ids)


def select_rows(
    conn: sqlite3.Connection, table: str, ids: Sequence[int]
) -> list[sqlite3.Row]:
    """Fetch this table's slice of the demo, or an empty list if absent."""
    placeholders = ",".join("?" * len(ids))
    if table in DERIVED:
        sql = DERIVED[table].format(ids=placeholders)
        params: list[Any] = list(ids)
    elif table == "tracks":
        sql = f"SELECT * FROM tracks WHERE track_id IN ({placeholders})"
        params = list(ids)
    else:
        sql = f"SELECT * FROM {table} WHERE track_id IN ({placeholders})"
        params = list(ids)

    if table in FILTERS:
        sql += f" AND {FILTERS[table]}"

    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        # An older source library may simply not have the table yet.
        if "no such table" in str(exc):
            print(f"  {table:20} absent from the source, skipped")
            return []
        raise


def collect(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[str, list[sqlite3.Row]]:
    data: dict[str, list[sqlite3.Row]] = {}
    for table in [*TRACK_SCOPED, *DERIVED]:
        rows = select_rows(conn, table, ids)
        if rows:
            data[table] = rows
    return data


def source_ddl(conn: sqlite3.Connection, tables: Iterable[str]) -> list[str]:
    """The source's own CREATE statements for the copied tables and indexes."""
    wanted = set(tables)
    statements = []
    for row in conn.execute(
        "SELECT type, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index') AND sql IS NOT NULL "
        "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END"
    ):
        if row["tbl_name"] in wanted:
            statements.append(row["sql"])
    return statements


def write_sqlite(
    path: Path, ddl: Sequence[str], data: dict[str, list[sqlite3.Row]]
) -> None:
    path.unlink(missing_ok=True)
    out = sqlite3.connect(path)
    try:
        for statement in ddl:
            out.execute(statement)
        for table, rows in data.items():
            columns = rows[0].keys()
            cols = ", ".join(columns)
            placeholders = ", ".join("?" * len(columns))
            out.executemany(
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                [tuple(r[c] for c in columns) for r in rows],
            )
        out.commit()
        out.execute("VACUUM")
    finally:
        out.close()


def pg_literal(value: Any) -> str:
    """Render one SQLite value as a Postgres literal.

    Deliberately hand-rolled rather than dumped through a server: building the
    demo must not need a running Postgres, only loading it does. The value
    space is narrow -- SQLite stores NULL, int, float, str and bytes, and the
    target columns are TEXT, INTEGER, REAL, DOUBLE PRECISION and one BYTEA.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):  # sqlite3 never returns these, but be explicit
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return f"'{value}'::double precision"  # 'NaN', 'Infinity'
        return repr(value)
    if isinstance(value, bytes):
        return f"'\\x{value.hex()}'::bytea"
    # Text. NUL bytes are legal in SQLite and rejected by Postgres, the same
    # cleaning the migration script does. Backslashes need no escaping:
    # standard_conforming_strings has been on by default since 9.1.
    text = str(value).replace("\x00", "")
    return "'" + text.replace("'", "''") + "'"


def write_postgres(path: Path, data: dict[str, list[sqlite3.Row]]) -> None:
    lines = [
        "-- Karaoke demo data (radio-derived tracks).",
        "--",
        "-- Data only. Create the schema first from POSTGRES_DDL in",
        "-- scripts/migrate_sqlite_to_postgres.py, then:",
        "--",
        "--   psql postgresql://karaoke:karaoke@localhost:5432/karaoke -f karaoke-demo.sql",
        "--",
        "-- Generated by scripts/make_demo_db.py -- edit that, not this file.",
        "",
        "BEGIN;",
        "",
    ]

    for table, rows in data.items():
        columns = list(rows[0].keys())
        cols = ", ".join(columns)
        lines.append(f"-- {table}: {len(rows)} rows")
        for row in rows:
            values = ", ".join(pg_literal(row[c]) for c in columns)
            lines.append(f"INSERT INTO {table} ({cols}) VALUES ({values});")
        lines.append("")

    for table, serial_col in SEQUENCES.items():
        if table in data:
            lines.append(
                f"SELECT setval('{table}_{serial_col}_seq', "
                f"COALESCE((SELECT MAX({serial_col}) FROM {table}), 1), true);"
            )
    lines += ["", "COMMIT;", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def load_ddl() -> str:
    """Read POSTGRES_DDL out of the migration script without importing it.

    The module imports psycopg at top level; verification should not require
    the driver just to read a string constant.
    """
    match = re.search(
        r'POSTGRES_DDL = """(.*?)"""', MIGRATION_SCRIPT.read_text(), re.DOTALL
    )
    if not match:
        raise SystemExit(f"Could not find POSTGRES_DDL in {MIGRATION_SCRIPT}")
    return match.group(1)


def verify_postgres(pg_url: str, schema: str, sql_path: Path) -> None:
    """Load the dump into a throwaway schema and count what arrived."""
    import psycopg  # imported here: only verification needs the driver

    ddl = load_ddl()
    sql = sql_path.read_text(encoding="utf-8")

    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            conn.execute(f'SET search_path TO "{schema}"')
            conn.execute(ddl)
            conn.execute(sql)
            print(f"\nLoaded into Postgres schema '{schema}':")
            for table in [*TRACK_SCOPED, *DERIVED]:
                row = conn.execute(f'SELECT count(*) FROM "{schema}".{table}').fetchone()
                print(f"  {table:20} {row[0]:>6} rows")
            nextval = conn.execute(
                f"SELECT last_value FROM \"{schema}\".tracks_track_id_seq"
            ).fetchone()
            print(f"  tracks sequence at {nextval[0]}")
        finally:
            # The dump wraps itself in BEGIN/COMMIT, so a failure part-way
            # leaves an aborted transaction that would swallow the cleanup.
            conn.rollback()
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    print("Verified, throwaway schema dropped.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help=f"source SQLite library (default: {DEFAULT_SOURCE})")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"where to write the demo files (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the row counts without writing anything")
    parser.add_argument("--verify-pg", metavar="URL",
                        help="load the generated dump into a throwaway schema on "
                             "this Postgres and report what arrived")
    parser.add_argument("--verify-schema", default="karaoke_demo_verify",
                        help="name of that throwaway schema")
    args = parser.parse_args()

    conn = connect_source(args.source)
    try:
        ids = demo_track_ids(conn)
        print(f"Source: {args.source}")
        print(f"Radio-derived tracks: {len(ids)}\n")

        data = collect(conn, ids)
        for table, rows in data.items():
            print(f"  {table:20} {len(rows):>6} rows")

        if args.dry_run:
            print("\nDry run: nothing written.")
            return

        args.out_dir.mkdir(parents=True, exist_ok=True)
        sqlite_path = args.out_dir / "karaoke-demo.db"
        pg_path = args.out_dir / "karaoke-demo.sql"

        write_sqlite(sqlite_path, source_ddl(conn, data), data)
        write_postgres(pg_path, data)

        print(f"\nWrote {sqlite_path} ({sqlite_path.stat().st_size / 1e6:.1f} MB)")
        print(f"Wrote {pg_path} ({pg_path.stat().st_size / 1e6:.1f} MB)")
    finally:
        conn.close()

    if args.verify_pg:
        verify_postgres(args.verify_pg, args.verify_schema, args.out_dir / "karaoke-demo.sql")


if __name__ == "__main__":
    sys.exit(main())
