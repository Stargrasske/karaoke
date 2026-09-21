# Local cache and stats

!!! warning "Backend is now PostgreSQL"
    This page predates the Postgres cutover and still describes the SQLite
    engine, its WAL tuning and `sqlite3` access patterns. The *roles* described
    here (offline lyrics cache, play/discovery stats, lookup order) are still
    accurate; the storage engine is not. The operational database is now
    PostgreSQL, reached via `KARAOKE_PG_URL` — see
    [Database backend](database.md). The `karaoke.db` path below is a frozen
    pre-migration snapshot.

Karaoke keeps a small, always-available operational database that is independent
of the OpenSearch/kind cluster. It has two jobs:

1. **Offline lyrics cache** — serve a known song's lyrics without the cluster and
   without a fresh online request.
2. **Play and discovery stats** — record what was played and what radio mode
   discovered, surfaced by `karaoke-stats`.

Default location: `~/.local/share/karaoke/karaoke.db`
(override with `KARAOKE_DATA_DIR`).

## Why there is a local operational database

The local database is the source of truth for normal playback and automation. OpenSearch is optional and derived: useful for semantic/vector search and future training features, but not required for exact lyric lookup or player control.

Live modes (`--radio`, `--listen`, `--output`, `--spotify`, player/browser modes) should keep working — and keep known songs offline — even with no cluster. The local database provides that stable base.

## Lyrics lookup order

`player.get_synced()` now checks local state first:

```mermaid
flowchart TD
    A[Need lyrics for artist/title or URL] --> B{use_cache?}
    B -- yes --> L[1. Local SQLite tracks/sources/lyrics]
    L -- hit --> Z[Return lyrics offline]
    L -- miss --> F[2. LRCLIB online]
    F -- found --> W[Write-through to SQLite] --> Z
    F -- none + local file + --transcribe --> WH[Whisper transcription] --> W
    F -- none --> G[Log lyric_gaps row for backfill]
    B -- no --> F
```

OpenSearch is deliberately not in this hot path. Reintroducing vector search should happen as a separate indexing command that learns from SQLite and writes derived documents to OpenSearch. See [Vector search and training plan](vector-search-plan.md).

## Offline song identification (limitation)

The local cache stores the **result** of a recognition (`artist/title -> lyrics`),
not an audio fingerprint. Identifying an *unknown* song from sound still uses
`songrec`, which queries Shazam **online**; there is no offline audio-fingerprint
match today.

True offline audio identification would require a local fingerprint database
(e.g. AcoustID/Chromaprint `fpcalc`, or a self-hosted fingerprint index). That is
a possible future addition; `scripts/soundcheck.sh` flags that songrec is
online-only so the limitation is visible.

## Stats

Every play/identification writes a `play_events` row. `karaoke-stats` aggregates
them.

```bash
karaoke-stats                # summary + top tracks/artists + by-mode
karaoke-stats --days 7       # only the last week
karaoke-stats -n 20          # longer top lists
karaoke-stats --json         # machine-readable
make stats                   # same summary via make
```

Recorded event types:

| Event | Meaning |
| --- | --- |
| `discover` | Radio/live mode identified a new song via songrec. |
| `relock` | Radio re-anchored the same song (drift correction). |
| `play` | A song's synced lyrics started rendering. |
| `cache_hit` | Lyrics served from the local cache. |
| `cache_miss` | Local cache had no lyrics; went to cluster/LRCLIB. |
| `no_lyrics` | No lyrics found anywhere for the track. |

Each row also records the `mode` (`radio`, `spotify`, `listen`, `output`, `file`,
`query`) and lyric `source` (`local`, `lrclib`, `whisper`, `none`), so the summary
can show discovery counts, per-mode activity, and local-cache hit rate.

## Schema

```sql
CREATE TABLE tracks (
    track_id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist   TEXT NOT NULL,
    title    TEXT NOT NULL,
    album    TEXT,
    duration REAL,
    UNIQUE(artist, title)
);

CREATE TABLE sources (
    source_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER NOT NULL,
    kind        TEXT NOT NULL,      -- youtube | spotify | local | player URL source
    url         TEXT UNIQUE,
    player_name TEXT,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE lyrics (
    lyric_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id      INTEGER NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'approved', -- approved | staged | rejected
    source        TEXT,              -- lrclib | whisper | youtube_caption | user_submitted
    synced_lyrics TEXT,
    plain_lyrics  TEXT,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE lyric_gaps (
    gap_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    artist       TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending', -- pending | processed | failed
    created_at   REAL NOT NULL,
    processed_at REAL,
    UNIQUE(artist, title)
);

CREATE TABLE play_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    artist TEXT DEFAULT '',
    title TEXT DEFAULT '',
    event TEXT NOT NULL,
    source TEXT DEFAULT '',
    has_synced INTEGER DEFAULT 0
);
```

The `tracks` table owns identity. `sources` links tracks to YouTube URLs, Spotify URIs, local paths or player sources. `lyrics` stores approved synced/plain lyric text. `lyric_gaps` is the work queue for later backfill.

All writes are best-effort: a cache or stats failure never interrupts playback.
