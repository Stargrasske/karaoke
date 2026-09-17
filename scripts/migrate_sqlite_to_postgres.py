"""Migrate karaoke database from SQLite to PostgreSQL 18 with referential integrity and NUL char cleaning."""
from __future__ import annotations

import sqlite3
import psycopg
from pathlib import Path

# Postgres DDL
POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS tracks (
    track_id    SERIAL PRIMARY KEY,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    album       TEXT,
    duration    REAL,
    play_count  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(artist, title)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id   SERIAL PRIMARY KEY,
    track_id    INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    url         TEXT UNIQUE,
    player_name TEXT
);

CREATE TABLE IF NOT EXISTS lyrics (
    lyric_id        SERIAL PRIMARY KEY,
    track_id        INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
    kind            TEXT NOT NULL DEFAULT 'approved',
    source          TEXT,
    synced_lyrics   TEXT,
    plain_lyrics    TEXT
);

CREATE TABLE IF NOT EXISTS lyric_gaps (
    gap_id          SERIAL PRIMARY KEY,
    artist          TEXT NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      DOUBLE PRECISION NOT NULL,
    processed_at    DOUBLE PRECISION,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    UNIQUE(artist, title)
);

CREATE TABLE IF NOT EXISTS play_events (
    id         SERIAL PRIMARY KEY,
    ts         DOUBLE PRECISION NOT NULL,
    mode       TEXT NOT NULL,
    artist     TEXT DEFAULT '',
    title      TEXT DEFAULT '',
    event      TEXT NOT NULL,
    source     TEXT DEFAULT '',
    has_synced INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS staged_lyrics (
    id            SERIAL PRIMARY KEY,
    key           TEXT NOT NULL,
    artist        TEXT NOT NULL,
    title         TEXT NOT NULL,
    album         TEXT DEFAULT '',
    duration      REAL,
    source_kind   TEXT NOT NULL,
    source_url    TEXT DEFAULT '',
    confidence    REAL DEFAULT 0.0,
    status        TEXT NOT NULL DEFAULT 'pending',
    plain_lyrics  TEXT DEFAULT '',
    synced_lyrics TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    created_at    DOUBLE PRECISION NOT NULL,
    updated_at    DOUBLE PRECISION NOT NULL,
    UNIQUE(key, source_kind)
);

CREATE TABLE IF NOT EXISTS track_analysis (
    track_id        INTEGER PRIMARY KEY REFERENCES tracks(track_id) ON DELETE CASCADE,
    detected_key    TEXT DEFAULT '',
    key_confidence  REAL DEFAULT 0.0,
    key_agreement   TEXT DEFAULT '',
    reference_key   TEXT DEFAULT '',
    reference_src   TEXT DEFAULT '',
    resolved_key    TEXT DEFAULT '',
    key_relation    TEXT DEFAULT '',
    bpm             REAL,
    method          TEXT DEFAULT '',
    analyzer_version INTEGER DEFAULT 0,
    updated_at      DOUBLE PRECISION NOT NULL,
    energy          REAL,
    brightness      REAL
);

CREATE TABLE IF NOT EXISTS track_sync_offsets (
    track_id    INTEGER PRIMARY KEY REFERENCES tracks(track_id) ON DELETE CASCADE,
    offset_s    REAL NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL,
    mode        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS spotify_lookups (
    track_id   INTEGER PRIMARY KEY REFERENCES tracks(track_id) ON DELETE CASCADE,
    uri        TEXT,
    checked_at DOUBLE PRECISION NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recordings (
    recording_id SERIAL PRIMARY KEY,
    started_at   DOUBLE PRECISION NOT NULL,
    ended_at     DOUBLE PRECISION,
    source       TEXT NOT NULL,
    dir          TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'recording',
    keep_audio   INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS recording_marks (
    mark_id      SERIAL PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(recording_id) ON DELETE CASCADE,
    at_wall      DOUBLE PRECISION NOT NULL,
    at_mono      DOUBLE PRECISION,
    at_offset    DOUBLE PRECISION,
    artist       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    ok           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS restricted_tracks (
    track_id   INTEGER PRIMARY KEY REFERENCES tracks(track_id) ON DELETE CASCADE,
    query      TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    noted_at   DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS track_notes (
    note_id    SERIAL PRIMARY KEY,
    track_id   INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    source     TEXT NOT NULL,
    confidence REAL,
    noted_at   DOUBLE PRECISION NOT NULL,
    UNIQUE(track_id, kind, source)
);

CREATE TABLE IF NOT EXISTS recording_silence (
    recording_id INTEGER NOT NULL REFERENCES recordings(recording_id) ON DELETE CASCADE,
    file         TEXT NOT NULL,
    start_s      REAL NOT NULL,
    end_s        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS recording_silence_scans (
    recording_id INTEGER NOT NULL REFERENCES recordings(recording_id) ON DELETE CASCADE,
    file         TEXT NOT NULL,
    duration_s   REAL,
    scanned_at   DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (recording_id, file)
);

CREATE TABLE IF NOT EXISTS alignment_support (
    track_id     INTEGER PRIMARY KEY REFERENCES tracks(track_id) ON DELETE CASCADE,
    lines        INTEGER NOT NULL,
    anchored     INTEGER NOT NULL,
    longest_gap_s REAL,
    unanchored_fraction REAL,
    source       TEXT NOT NULL DEFAULT '',
    noted_at     DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS cover_art (
    art_key    TEXT PRIMARY KEY,
    cols       INTEGER NOT NULL,
    rows       INTEGER NOT NULL,
    pixels     BYTEA NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    stored_at  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS track_art (
    track_id  INTEGER PRIMARY KEY REFERENCES tracks(track_id) ON DELETE CASCADE,
    art_key   TEXT NOT NULL,
    noted_at  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS track_genre (
    track_id        INTEGER PRIMARY KEY REFERENCES tracks(track_id) ON DELETE CASCADE,
    genre           TEXT NOT NULL,
    score           REAL,
    runner_up       TEXT NOT NULL DEFAULT '',
    runner_up_score REAL,
    method          TEXT NOT NULL DEFAULT '',
    labelled_at     DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS track_tone (
    track_id        INTEGER PRIMARY KEY REFERENCES tracks(track_id) ON DELETE CASCADE,
    tone            TEXT NOT NULL,
    score           REAL,
    runner_up       TEXT NOT NULL DEFAULT '',
    runner_up_score REAL,
    method          TEXT NOT NULL DEFAULT '',
    labelled_at     DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS track_analysis_history (
    history_id      SERIAL PRIMARY KEY,
    track_id        INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
    source_kind     TEXT DEFAULT '',
    detected_key    TEXT DEFAULT '',
    key_confidence  REAL DEFAULT 0.0,
    key_agreement   TEXT DEFAULT '',
    bpm             REAL,
    method          TEXT DEFAULT '',
    energy          REAL,
    brightness      REAL,
    analyzer_version INTEGER DEFAULT 0,
    created_at      DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_play_events_ts ON play_events (ts);
CREATE INDEX IF NOT EXISTS idx_play_events_track ON play_events (artist, title);
CREATE INDEX IF NOT EXISTS idx_staged_lyrics_status ON staged_lyrics (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_staged_lyrics_key ON staged_lyrics (key);
CREATE INDEX IF NOT EXISTS idx_sources_track ON sources (track_id);
CREATE INDEX IF NOT EXISTS idx_sources_kind ON sources (kind);
CREATE INDEX IF NOT EXISTS idx_lyrics_track ON lyrics (track_id);
CREATE INDEX IF NOT EXISTS idx_lyrics_track_kind ON lyrics (track_id, kind);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks (artist);
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks (title);
CREATE INDEX IF NOT EXISTS idx_recording_marks_rec ON recording_marks (recording_id, at_wall);
CREATE INDEX IF NOT EXISTS idx_track_notes_track ON track_notes (track_id);
CREATE INDEX IF NOT EXISTS idx_recording_silence_rec ON recording_silence (recording_id, file, start_s);
CREATE INDEX IF NOT EXISTS idx_track_art_key ON track_art (art_key);
CREATE INDEX IF NOT EXISTS idx_track_genre_genre ON track_genre (genre);
CREATE INDEX IF NOT EXISTS idx_track_tone_tone ON track_tone (tone);

CREATE TABLE IF NOT EXISTS saved_searches (
    query        TEXT PRIMARY KEY,
    result_count INTEGER DEFAULT 0,
    created_at   DOUBLE PRECISION NOT NULL,
    last_used_at DOUBLE PRECISION NOT NULL,
    use_count    INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_saved_searches_last_used ON saved_searches(last_used_at);

CREATE TABLE IF NOT EXISTS saved_playlists (
    playlist_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    search_query TEXT DEFAULT '',
    track_count  INTEGER DEFAULT 0,
    url          TEXT DEFAULT '',
    source_kind  TEXT DEFAULT 'youtube_music',
    created_at   DOUBLE PRECISION NOT NULL,
    updated_at   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_playlists_updated ON saved_playlists(updated_at);

CREATE TABLE IF NOT EXISTS saved_playlist_tracks (
    playlist_id TEXT NOT NULL REFERENCES saved_playlists(playlist_id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    track_id    INTEGER,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    video_id    TEXT DEFAULT '',
    url         TEXT DEFAULT '',
    PRIMARY KEY (playlist_id, position)
);
CREATE INDEX IF NOT EXISTS idx_saved_playlist_tracks_pid ON saved_playlist_tracks(playlist_id);

CREATE TABLE IF NOT EXISTS artist_genres (
    artist_normalized TEXT NOT NULL,
    genre             TEXT NOT NULL,
    broad_genre       TEXT NOT NULL,
    weight            REAL NOT NULL DEFAULT 1.0,
    source            TEXT NOT NULL,
    fetched_at        DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (artist_normalized, genre)
);
CREATE INDEX IF NOT EXISTS idx_artist_genres_broad ON artist_genres (broad_genre);
CREATE INDEX IF NOT EXISTS idx_artist_genres_artist ON artist_genres (artist_normalized);

CREATE TABLE IF NOT EXISTS radio_sessions (
    session_id    SERIAL PRIMARY KEY,
    started_at    DOUBLE PRECISION NOT NULL,
    ended_at      DOUBLE PRECISION,
    source        TEXT NOT NULL DEFAULT 'mic',
    is_recording  INTEGER NOT NULL DEFAULT 0,
    recording_id  INTEGER REFERENCES recordings(recording_id) ON DELETE SET NULL,
    track_count   INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_radio_sessions_started ON radio_sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_radio_sessions_status ON radio_sessions(status);

CREATE TABLE IF NOT EXISTS radio_session_tracks (
    id                 SERIAL PRIMARY KEY,
    session_id         INTEGER NOT NULL,
    artist             TEXT NOT NULL,
    title              TEXT NOT NULL,
    first_seen_at      DOUBLE PRECISION NOT NULL,
    last_seen_at       DOUBLE PRECISION NOT NULL,
    play_count         INTEGER NOT NULL DEFAULT 1,
    offset_s           REAL,
    has_synced_lyrics  INTEGER NOT NULL DEFAULT 0,
    lyric_source       TEXT DEFAULT '',
    imported           INTEGER NOT NULL DEFAULT 0,
    imported_track_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_radio_session_tracks_sess ON radio_session_tracks(session_id);
CREATE INDEX IF NOT EXISTS idx_radio_session_tracks_artist_title ON radio_session_tracks(artist, title);

CREATE TABLE IF NOT EXISTS queue_events (
    id           SERIAL PRIMARY KEY,
    ts           DOUBLE PRECISION NOT NULL,
    event_type   TEXT NOT NULL,
    track_id     INTEGER,
    artist       TEXT DEFAULT '',
    title        TEXT DEFAULT '',
    queue_index  INTEGER DEFAULT 0,
    payload_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_queue_events_ts ON queue_events(ts);

"""

TABLES = [
    ("tracks", "track_id"),
    ("sources", "source_id"),
    ("lyrics", "lyric_id"),
    ("lyric_gaps", "gap_id"),
    ("play_events", "id"),
    ("staged_lyrics", "id"),
    ("track_analysis", None),
    ("track_sync_offsets", None),
    ("spotify_lookups", None),
    ("recordings", "recording_id"),
    ("recording_marks", "mark_id"),
    ("restricted_tracks", None),
    ("track_notes", "note_id"),
    ("recording_silence", None),
    ("recording_silence_scans", None),
    ("alignment_support", None),
    ("cover_art", None),
    ("track_art", None),
    ("track_genre", None),
    ("track_tone", None),
    ("track_analysis_history", "history_id"),
    ("saved_searches", None),
    ("saved_playlists", None),
    ("saved_playlist_tracks", None),
    ("artist_genres", None),
    ("radio_sessions", "session_id"),
    ("radio_session_tracks", "id"),
    ("queue_events", "id"),
]

def clean_postgres_string(val):
    if isinstance(val, str):
        return val.replace("\x00", "")
    return val

def main():
    sqlite_db = Path.home() / ".local" / "share" / "karaoke" / "karaoke.db"
    pg_url = "postgresql://karaoke@localhost/karaoke"

    if not sqlite_db.exists():
        print(f"SQLite DB not found at: {sqlite_db}")
        return

    print("Connecting to databases...")
    sqlite_conn = sqlite3.connect(sqlite_db)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg.connect(pg_url)

    try:
        # Get set of valid parent keys to prevent foreign key violations on orphan SQLite rows
        print("Pre-fetching valid parent IDs from SQLite...")
        valid_tracks = set(r[0] for r in sqlite_conn.execute("SELECT track_id FROM tracks").fetchall())
        valid_recordings = set(r[0] for r in sqlite_conn.execute("SELECT recording_id FROM recordings").fetchall())

        # 1. Create schemas in Postgres
        print("Creating schemas in Postgres...")
        with pg_conn.cursor() as pg_cur:
            pg_conn.autocommit = True
            pg_cur.execute(POSTGRES_DDL)
            pg_conn.autocommit = False

        # 2. Iterate over tables and migrate
        for table, serial_col in TABLES:
            print(f"Migrating table {table}...")
            sqlite_cur = sqlite_conn.cursor()
            try:
                sqlite_cur.execute(f"SELECT * FROM {table}")
            except sqlite3.OperationalError:
                print(f"Table {table} does not exist in SQLite database. Skipping.")
                continue

            rows = sqlite_cur.fetchall()
            if not rows:
                print(f"Table {table} has no rows. Skipping.")
                continue

            columns = rows[0].keys()

            # Filter out records violating foreign key constraints
            orig_len = len(rows)
            if "track_id" in columns and table != "tracks":
                rows = [r for r in rows if r["track_id"] in valid_tracks]
            if "recording_id" in columns and table != "recordings":
                rows = [r for r in rows if r["recording_id"] in valid_recordings]
            
            filtered_len = len(rows)
            if filtered_len < orig_len:
                print(f"Filtered out {orig_len - filtered_len} orphan rows from {table} to preserve referential integrity.")

            if not rows:
                print(f"No records left for {table} after filtering.")
                continue

            cols_str = ", ".join(columns)
            placeholders = ", ".join(["%s"] * len(columns))
            insert_query = f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})"

            # Insert rows in chunks
            with pg_conn.cursor() as pg_cur:
                # Truncate table first to prevent duplicate key errors on rerun
                pg_cur.execute(f"TRUNCATE TABLE {table} CASCADE")
                
                chunk_size = 1000
                for i in range(0, len(rows), chunk_size):
                    chunk = rows[i:i+chunk_size]
                    data = [[clean_postgres_string(r[col]) for col in columns] for r in chunk]
                    pg_cur.executemany(insert_query, data)
                
                print(f"Migrated {len(rows)} rows into {table}.")

                # If the table has a SERIAL/IDENTITY column, set the sequence value
                if serial_col:
                    seq_name = f"{table}_{serial_col}_seq"
                    pg_cur.execute(f"SELECT setval('{seq_name}', COALESCE((SELECT MAX({serial_col}) FROM {table}), 1), true)")

        pg_conn.commit()
        print("Data migration completed successfully with 100% referential integrity and cleaned strings!")

    except Exception as exc:
        pg_conn.rollback()
        print(f"Migration failed: {exc}")
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()

if __name__ == "__main__":
    main()
