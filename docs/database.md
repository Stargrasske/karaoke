# Database backend

The karaoke platform stores its operational data (tracks, sources, lyrics,
analysis, genre/tone, recordings, play stats, saved playlists) in a local
**PostgreSQL** database. OpenSearch remains a *derived* index used only for
semantic/vector search and is always rebuilt from this database.

!!! info "Migration status — complete (cutover)"
    The SQLite → Postgres migration has been **cut over**. Postgres is now the
    only backend the application opens at runtime; SQLite is no longer used
    outside the one-time migration script. See
    [Migration status](#migration-status) for what moved and what is left.

## PostgreSQL (current backend)

| Setting | Value |
|---|---|
| Server | PostgreSQL 18.4 |
| Database / role | `karaoke` / `karaoke` |
| Driver | `psycopg` 3.3.5 (`psycopg[binary,pool]>=3.0.0`) |
| Pool | `psycopg_pool.ConnectionPool` — `min_size=1`, `max_size=500` |
| Connection style | `autocommit=True`, `row_factory=dict_row` |

`localcache.connect()` leases a connection from a process-wide pool created by
`get_pool()`. The returned connection's `close()` is rebound to return it to the
pool (rolling back first) rather than actually closing the socket, so existing
`with connect() as conn:` call sites keep working unchanged.

Rows come back as dicts (`row_factory=dict_row`), which is why call sites use
`row["column"]` rather than tuple indexing.

### Configuration

The connection string is read **directly from the environment** by
`localcache.get_pool()`:

| Env var | Default | Purpose |
|---|---|---|
| `KARAOKE_PG_URL` | `postgresql://karaoke:karaoke@localhost:5432/karaoke` | Postgres connection string. This is the only variable that affects which database is opened. |
| `KARAOKE_DATA_DIR` | `~/.local/share/karaoke` | Caches, artwork and the legacy `karaoke.db` snapshot. |

!!! warning "`KARAOKE_DB_BACKEND` and `KARAOKE_DB_URL` are vestigial"
    `config.py` still parses `KARAOKE_DB_BACKEND` (default `sqlite`) and
    `KARAOKE_DB_URL` into `settings`, and `settings.uses_postgres` still exists.
    **Nothing in the connection path reads them.** `localcache` sets a
    module-level `_IS_PG = True` and `connect()` always returns a pooled
    Postgres connection — including when passed an explicit `db_path`, which is
    now ignored. Setting `KARAOKE_DB_BACKEND=sqlite` does *not* give you SQLite.
    Use `KARAOKE_PG_URL` to point at a different database; the older two
    variables are pending removal.

### Checking the connection

```bash
psql "$KARAOKE_PG_URL" -c '\dt'                       # 29 tables expected
psql "$KARAOKE_PG_URL" -c 'SELECT count(*) FROM tracks;'
```

## Migration status

### What moved

`scripts/migrate_sqlite_to_postgres.py` creates the Postgres schema and copies
28 tables. Current row counts (Postgres, versus the frozen SQLite snapshot):

| Table | Postgres | SQLite snapshot |
|---|---|---|
| `tracks` | 18 242 | 18 244 |
| `sources` | 38 075 | 38 084 |
| `lyrics` | 10 372 | 10 359 |
| `track_analysis` | 17 803 | 17 805 |
| `track_analysis_history` | 22 380 | 22 365 |
| `track_genre` | 13 516 | 13 515 |
| `lyric_gaps` | 1 399 | 1 346 |
| `cover_art` | 1 290 | 1 263 |
| `track_art` | 1 550 | 1 508 |
| `track_tone` | 1 166 | 1 134 |
| `recording_marks` | 1 084 | 1 084 |
| `artist_genres` | 923 | 923 |
| `saved_playlist_tracks` | 701 | 701 |
| `queue_events` | 652 | 594 |
| `alignment_support` | 369 | 375 |
| `play_events` | 2 385 | 2 380 |
| `saved_searches` | 68 | 46 |
| `radio_session_tracks` | 67 | 67 |
| `saved_playlists` | 24 | 24 |
| `track_sync_offsets` | 25 | 24 |

Postgres now leads the snapshot on most tables because it has been the live
store since cutover; `~/.local/share/karaoke/karaoke.db` is a frozen
2026-09-16 copy kept only as a fallback.

The tables originally omitted from the migrator — `saved_playlists`,
`saved_playlist_tracks`, `artist_genres`, `saved_searches`, `radio_sessions`,
`radio_session_tracks` and `queue_events` — are now migrated and populated. The
"No saved playlists found" bug behind the TUI `L` key is resolved: all 24
playlists load.

### Where Postgres counts are lower

A few tables have marginally fewer rows than the snapshot. This is deliberate,
not data loss: the migrator pre-fetches valid `tracks.track_id` and
`recordings.recording_id` keys and **skips orphan rows** that would violate the
Postgres foreign keys. SQLite never enforced those references, so the snapshot
contains rows pointing at deleted parents. The residue also includes test
fixtures (`Alpha :: Song A`, `A Rocker 00 :: Rock Song 0`) that leaked into the
SQLite file from earlier test runs.

### Schema translation notes

- `INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL PRIMARY KEY`.
- `?` placeholders → `%s`; named placeholders use `%(name)s`.
- `PRAGMA table_info(...)` introspection → `information_schema.columns`.
- `PRAGMA`/WAL tuning is gone; `_tune_connection()` is now a no-op.
- Epoch timestamps on `saved_searches` / `saved_playlists` use
  `DOUBLE PRECISION`, not `REAL`. Postgres `REAL` is single-precision and
  rounds epoch values (~1.78e9) to ~128-second buckets, which collapsed
  `created_at`/`updated_at`/`last_used_at` into collisions and broke sorting.
- `clean_postgres_string()` strips `\x00` bytes, which SQLite tolerates in TEXT
  but Postgres rejects.
- The staging cache key separator changed from a literal `NUL` to `U+2400`
  (`␀`) for the same reason.

### Remaining work

- `active_queue_state` is not in the migrator's `TABLES` list. It is created and
  populated by the application's own schema setup, so this is harmless for a
  fresh cutover but means the table is not carried across by a re-run.
- Remove the vestigial `KARAOKE_DB_BACKEND` / `KARAOKE_DB_URL` settings and the
  `uses_postgres` property, or wire them to `get_pool()`. Two
  `tests/test_config.py` cases still assert the old selector semantics.
- The connection pool is never explicitly closed, so interpreter shutdown can
  emit `PythonFinalizationError: cannot join thread` from
  `ConnectionPool.__del__`. Cosmetic — it happens after all work completes —
  but it pollutes CLI and test output.
- The unified `events` table and outbox relay described in the
  [Events & Postgres migration plan](events_and_postgres_plan.md) are **not**
  implemented. Event history still lives in the per-domain tables
  (`play_events`, `queue_events`, `track_analysis_history`,
  `alignment_support`). Note that `track_play_history` and
  `track_sources_history`, named in that plan, do not exist in either database.
- Celery results still persist to a separate `celery-results.sqlite`.

## Re-running the migration

The migrator is idempotent on conflicts but expects to load into the existing
schema. To rebuild from the SQLite snapshot:

```bash
python scripts/migrate_sqlite_to_postgres.py
```

It reads `~/.local/share/karaoke/karaoke.db` and writes to
`postgresql://karaoke@localhost/karaoke` (both currently hardcoded in `main()`).
After a reload, rebuild the derived OpenSearch index — it is never migrated.
