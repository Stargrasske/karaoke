# Saved Playlists & Omitted SQLite Tables Migration Plan

!!! success "Resolved — all four execution steps are done"
    Verified 2026-09-17. The omitted tables are migrated and populated
    (`saved_playlists` 24, `saved_playlist_tracks` 701, `artist_genres` 923,
    `saved_searches` 68, `radio_sessions` 8, `radio_session_tracks` 67,
    `queue_events` 652). `ensure_saved_searches_and_playlists_schema` now uses
    `DOUBLE PRECISION`, and `_check_playlist_detection` handles the list
    returned by `find_playlist_by_video_id`. `localcache.get_saved_playlists`
    returns all 24 playlists and `tests/test_saved_playlists.py` passes.
    Retained for the root-cause analysis; see
    [Database backend](database.md) for current state.

## Problem Summary
When pressing `L` (`action_browse_playlists`) in the TUI, the saved YouTube playlist modal shows **"No saved playlists found"** (returns `None`/empty).

### Root Causes
1. **Omitted Tables in Migration**:
   - `~/.local/share/karaoke/karaoke.db` (SQLite) contains **24 saved playlists** and **701 tracks** (e.g., *Karaoke Temp Queue*, *Karaoke: Sad*, *Karaoke: Medication*, *Karaoke: Work Hate*, etc.).
   - In PostgreSQL, `saved_playlists` and `saved_playlist_tracks` have **0 rows** because `scripts/migrate_sqlite_to_postgres.py` omitted:
     - `saved_playlists` (24 rows in SQLite)
     - `saved_playlist_tracks` (701 rows in SQLite)
     - `artist_genres` (923 rows in SQLite)
     - `saved_searches` (46 rows in SQLite)
     - `radio_sessions` (8 rows in SQLite)
     - `radio_session_tracks` (67 rows in SQLite)
     - `queue_events` (593 rows in SQLite)

2. **Reverse Playlist Video Detection Bug in `src/karaoke/tui.py`**:
   - In `_check_playlist_detection` (line 3121):
     `saved_pl = localcache.find_playlist_by_video_id(vid, conn=conn)`
     `find_playlist_by_video_id` returns a `list[dict[str, Any]]`.
     Line 3136 attempts `saved_pl.get("playlist_id")`, causing an `AttributeError` when a match is found.
     Fix:
     ```python
     matches = localcache.find_playlist_by_video_id(vid, conn=conn)
     if matches:
         saved_pl = matches[0]
     ```

3. **Timestamp Precision in `src/karaoke/localcache.py`**:
   - `ensure_saved_searches_and_playlists_schema` defined timestamps (`created_at`, `updated_at`, `last_used_at`) as `REAL` (32-bit float) instead of `DOUBLE PRECISION` (64-bit float).
   - In PostgreSQL, single-precision floats round epoch timestamps (~1.78e9) to ~128 seconds, causing timestamp collisions and sorting issues.

## Execution Steps for Next Session

1. **Migrate Missing SQLite Data to PostgreSQL**:
   - Run a migration snippet or update `scripts/migrate_sqlite_to_postgres.py` with `ON CONFLICT DO NOTHING / UPDATE` to populate:
     - `saved_playlists`
     - `saved_playlist_tracks`
     - `artist_genres`
     - `saved_searches`
     - `radio_sessions` & `radio_session_tracks`

2. **Update `src/karaoke/localcache.py`**:
   - In `ensure_saved_searches_and_playlists_schema`, change `created_at`, `updated_at`, and `last_used_at` from `REAL` to `DOUBLE PRECISION`.

3. **Update `src/karaoke/tui.py`**:
   - Fix `_check_playlist_detection` to handle the list returned by `localcache.find_playlist_by_video_id`.

4. **Verify**:
   - Run `pytest tests/test_saved_playlists.py` and verify `localcache.get_saved_playlists(100)` returns all 24 playlists.
