# Bulk collection ingestion

How to ingest a large local music collection into the karaoke database, track
progress, and recover files that were skipped.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/overnight_collection_scan.py` | Full first-pass scan of the whole collection root. |
| `scripts/retry_skipped_collection.py` | Resumable second pass: re-ingests only files not yet in the DB. Supports `--dry-run`. |

Both run the same pipeline per file:

```
tags → (fingerprint if tags missing) → key/BPM/energy analysis
     → CLAP genre vector (when audio model available)
     → YouTube + Spotify source resolution
     → SQLite ingest (track, local source, lyrics, analysis, genre)
```

The overnight scan rebuilds the OpenSearch vectors once at the end; the retry
pass rebuilds them only if it ingested anything.

Logs:

- Overnight: `~/.local/share/karaoke/logs/overnight_collection_scan.log`
- Retry: `~/.local/share/karaoke/logs/retry_skipped_collection.log`

Follow live with `tail -f <logfile>`.

## Checking ingestion stats

The database is the source of truth for how far ingestion has got. Quick counts:

```bash
sqlite3 ~/.local/share/karaoke/karaoke.db "
  SELECT 'tracks',        count(*) FROM tracks
  UNION ALL SELECT 'local source',   count(DISTINCT track_id) FROM sources WHERE kind='local'
  UNION ALL SELECT 'youtube source', count(DISTINCT track_id) FROM sources WHERE kind IN ('youtube','youtube_music')
  UNION ALL SELECT 'spotify source', count(DISTINCT track_id) FROM sources WHERE kind='spotify'
  UNION ALL SELECT 'analyzed',       count(*) FROM track_analysis;"
```

Progress against the collection on disk (how many files still need a local
source row) is exactly what the retry pass reports in dry-run:

```bash
PYTHONPATH=src .venv/bin/python scripts/retry_skipped_collection.py --dry-run
# -> On disk=<N> already ingested(local)=<M> missing=<N-M>
```

### Example snapshot (mid first-pass, 2026-09-09)

A ~20k-file backup collection, partway through the overnight scan:

| Metric | Value |
|---|---|
| Files on disk | 20,293 |
| First-pass position | ~11,800 / 20,293 (~58%) |
| Tracks in DB | 10,412 |
| With local file source | 9,236 |
| With YouTube / YT Music source | 8,882 |
| With Spotify source | 815 |
| Key/BPM analyzed | 9,904 |
| Skipped on `database is locked` | 107 |
| Other errors | 1 |

Throughput is roughly 800 files/hour on this machine (dominated by online
source lookups). WAL mode keeps the TUI responsive while the scan runs.

## Recovering skipped files (resumable)

The first pass skips a file whenever the SQLite write loses the lock
(`database is locked`) or a file has no usable tags. Those files never get a
`kind='local'` source row, so the retry pass finds them and re-ingests only
them:

```bash
# Preview what would be retried (read-only)
PYTHONPATH=src .venv/bin/python scripts/retry_skipped_collection.py --dry-run

# Run for real
PYTHONPATH=src .venv/bin/python scripts/retry_skipped_collection.py
```

It is **resumable**: each run recomputes the missing set from the database, so
you can stop it (Ctrl-C) and relaunch later and it continues where it left off.
With WAL mode active, lock-skips are rare on the retry.

Run the retry pass **after** the first pass finishes, so the two do not compete
for the writer.

### Running from Admin TUI

The Admin TUI (`python -m karaoke.admin_tui`) exposes these operations directly in the **File Ingestion & Folder Scan** panel:

- **`Retry Skipped (p)`** (`btn-retry-skipped`): Ingests skipped files for the specified or default collection folder, updating OpenSearch vectors on completion.
- **`Dry Run Check (d)`** (`btn-retry-dryrun`): Rapidly previews how many on-disk files are missing from SQLite without modifying anything.
- **`Index YT Cache (y)`** (`btn-index-cache`): Scans downloaded YouTube cache files (`~/.local/share/karaoke/youtube/`) and adds unindexed tracks to SQLite.

### Files that still won't ingest

Some skipped files have empty ID3 tags and do not resolve via fingerprint; they
log `SKIP ... missing artist/title after tags & fingerprint`. That is a property
of those files, not a scan bug. A filename-based artist/title fallback (for the
common `Artist - Title.mp3` pattern) is a possible future enhancement.
