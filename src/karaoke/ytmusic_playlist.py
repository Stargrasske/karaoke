"""Build, sync, and import YouTube Music playlists from the karaoke library in SQLite.

Provides:
- Exporting karaoke-ready tracks (tracks with approved synced lyrics) to a YouTube Music playlist.
- Exporting an active queue to a YouTube Music playlist.
- Importing an existing YouTube Music playlist into local SQLite tracks + sources.
- Listing user playlists on YouTube Music.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import localcache
from .logger import log
from .ytmusic_client import YTMusicAuthError, YTMusicClient

DEFAULT_PLAYLIST_NAME = "Karaoke (synced lyrics)"
DEFAULT_DESCRIPTION = "Tracks with time-synced lyrics in the local karaoke library."
TEMP_PLAYLIST_PREFIX = "Karaoke Temp Queue"
TEMP_DESCRIPTION = "Temporary playback queue mirrored from the karaoke TUI search results."

_YT_VIDEO_ID_REGEX = re.compile(
    r"(?:v=|/v/|youtu\.be/|/embed/|/watch\?v=|/track/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str) -> str:
    """Extract an 11-character YouTube video ID from a URL, or return empty string."""
    if not url:
        return ""
    if len(url) == 11 and re.match(r"^[A-Za-z0-9_-]{11}$", url):
        return url
    m = _YT_VIDEO_ID_REGEX.search(url)
    return m.group(1) if m else ""


@dataclass
class Candidate:
    """One karaoke-ready track and the YouTube videoId resolved for it."""

    artist: str
    title: str
    video_id: str = ""
    resolved_by: str = ""   # stored | search | unresolved


@dataclass
class PlaylistResult:
    """Outcome of a YouTube Music playlist build or sync."""

    playlist_id: str = ""
    name: str = ""
    candidates: int = 0
    resolved: int = 0
    added: int = 0
    already_present: int = 0
    unresolved: list[Candidate] = field(default_factory=list)
    dry_run: bool = False
    completed: bool = True


def karaoke_tracks(conn: sqlite3.Connection) -> list[Candidate]:
    """Return tracks that have approved synced lyrics, with any stored YouTube video ID."""
    rows = conn.execute(
        """
        SELECT t.artist, t.title,
               (SELECT s.url FROM sources s
                 WHERE s.track_id = t.track_id AND s.kind IN ('youtube', 'youtube_music', 'http')
                   AND s.url LIKE '%youtu%'
                 ORDER BY CASE s.kind WHEN 'youtube_music' THEN 1 WHEN 'youtube' THEN 2 ELSE 3 END
                 LIMIT 1) AS yt_url
          FROM tracks t
          JOIN lyrics l ON l.track_id = t.track_id
         WHERE l.kind = 'approved'
           AND length(COALESCE(l.synced_lyrics, '')) > 0
           AND length(TRIM(COALESCE(t.artist, ''))) > 0
           AND length(TRIM(COALESCE(t.title, ''))) > 0
         GROUP BY t.track_id
         ORDER BY t.artist COLLATE NOCASE, t.title COLLATE NOCASE
        """
    ).fetchall()

    candidates: list[Candidate] = []
    for r in rows:
        vid = extract_video_id(r["yt_url"] or "")
        resolved = "stored" if vid else ""
        candidates.append(Candidate(artist=r["artist"], title=r["title"], video_id=vid, resolved_by=resolved))
    return candidates


def resolve_candidate(c: Candidate, client: YTMusicClient) -> Candidate:
    """Resolve a video ID for an unresolved candidate via YouTube Music search."""
    if c.video_id:
        return c
    vid = client.search_track(c.artist, c.title)
    if vid:
        c.video_id = vid
        c.resolved_by = "search"
    else:
        c.resolved_by = "unresolved"
    return c


def build_or_sync_ytmusic_playlist(
    *,
    candidates: list[Candidate],
    name: str = DEFAULT_PLAYLIST_NAME,
    description: str = DEFAULT_DESCRIPTION,
    dry_run: bool = False,
    client: Optional[YTMusicClient] = None,
) -> PlaylistResult:
    """Create or update a YouTube Music playlist containing the given candidate tracks."""
    yt = client or YTMusicClient()
    result = PlaylistResult(name=name, candidates=len(candidates), dry_run=dry_run)

    # 1. Resolve video IDs
    resolved_candidates: list[Candidate] = []
    for c in candidates:
        c = resolve_candidate(c, yt)
        if c.video_id:
            resolved_candidates.append(c)
            result.resolved += 1
        else:
            result.unresolved.append(c)

    if dry_run:
        return result

    yt.require_auth()

    # 2. Find or create playlist
    playlists = yt.get_library_playlists(limit=100)
    target_id: str | None = None
    for pl in playlists:
        if pl.get("title", "").strip().lower() == name.strip().lower():
            target_id = pl.get("playlistId")
            break

    video_ids = [c.video_id for c in resolved_candidates]

    if not target_id:
        target_id = yt.create_playlist(
            title=name,
            description=description,
            privacy_status="PRIVATE",
            video_ids=video_ids,
        )
        result.playlist_id = target_id
        result.added = len(video_ids)
        return result

    result.playlist_id = target_id

    # 3. Add only tracks not already in the playlist
    existing = yt.get_playlist_tracks(target_id, limit=500)
    existing_vids = {t["videoId"] for t in existing}

    needed = [vid for vid in video_ids if vid not in existing_vids]
    result.already_present = len(video_ids) - len(needed)

    if needed:
        yt.add_playlist_items(target_id, needed)
        result.added = len(needed)

    return result


def queue_rows_to_ytmusic_candidates(rows: list[dict[str, Any]]) -> list[Candidate]:
    """Convert TUI queue/search rows to YouTube-only playlist candidates.

    This deliberately does not search YouTube Music for missing items: a current
    search queue is an ordered playback intent, and resolving unknown rows by
    title can silently add the wrong recording. Only rows with an already-known
    YouTube/YT Music video ID are mirrored into the temporary playlist.
    """
    seen: set[str] = set()
    candidates: list[Candidate] = []
    for row in rows:
        url = str(row.get("url") or "")
        vid = extract_video_id(url)
        if not vid or vid in seen:
            continue
        seen.add(vid)
        candidates.append(Candidate(
            artist=str(row.get("artist") or ""),
            title=str(row.get("title") or ""),
            video_id=vid,
            resolved_by="stored",
        ))
    return candidates


def clean_playlist_name_for_query(query: str | None) -> str:
    """Format a clean, readable playlist title from a search query.

    For example, 'radiohead' becomes 'Karaoke: Radiohead', '90s rock' becomes
    'Karaoke: 90s Rock'. Empty or wildcard queries fall back to the default
    daily temp queue title ('Karaoke Temp Queue YYYY-MM-DD').
    """
    import time

    if not query or not query.strip() or query.strip() == "*":
        return time.strftime(f"{TEMP_PLAYLIST_PREFIX} %Y-%m-%d")

    # Clean up whitespace and special search tokens
    q = query.strip()
    # Remove leading search command syntax if typed (like '/' or 'search:')
    q = re.sub(r"^(?:search:|\/)\s*", "", q, flags=re.IGNORECASE).strip()
    # Collapse multiple whitespaces
    q = re.sub(r"\s+", " ", q)
    if not q or q == "*":
        return time.strftime(f"{TEMP_PLAYLIST_PREFIX} %Y-%m-%d")

    words = q.split(" ")
    formatted_words = []
    for w in words:
        if w.isupper() and len(w) <= 4:
            formatted_words.append(w)
        else:
            formatted_words.append(w.capitalize())
    clean_title = " ".join(formatted_words)
    if len(clean_title) > 50:
        clean_title = clean_title[:47].rstrip() + "…"

    return f"Karaoke: {clean_title}"


def create_temp_queue_playlist(
    rows: list[dict[str, Any]],
    *,
    name: str | None = None,
    client: Optional[YTMusicClient] = None,
    search_query: str = "",
    max_tracks: int = 50,
    conn: Optional[sqlite3.Connection] = None,
) -> PlaylistResult:
    """Create or overwrite today's private YouTube Music temp queue.

    The playlist name defaults to a clean representation of the search query
    (or today's daily temp queue). Tracks are capped to `max_tracks` (default 50)
    to keep YouTube Music API requests responsive and reliable. The resulting
    playlist and tracks are saved to SQLite for local tracking and offline history.
    """
    import time

    yt = client or YTMusicClient()
    yt.require_auth()

    # Cap rows to max_tracks
    capped_rows = rows[:max_tracks] if max_tracks and max_tracks > 0 else rows
    candidates = queue_rows_to_ytmusic_candidates(capped_rows)

    if name:
        playlist_name = name
    elif search_query:
        playlist_name = clean_playlist_name_for_query(search_query)
    else:
        playlist_name = time.strftime(f"{TEMP_PLAYLIST_PREFIX} %Y-%m-%d")

    result = PlaylistResult(name=playlist_name, candidates=len(candidates))
    video_ids = [c.video_id for c in candidates if c.video_id]
    result.resolved = len(video_ids)

    playlists = yt.get_library_playlists(limit=1000)
    temp_playlists = [
        p for p in playlists
        if str(p.get("title") or "").startswith((TEMP_PLAYLIST_PREFIX, "Karaoke:"))
    ]
    target_id = ""
    for pl in temp_playlists:
        if str(pl.get("title") or "").strip() == playlist_name:
            target_id = str(pl.get("playlistId") or "")
            break

    if not target_id:
        target_id = yt.create_playlist(
            title=playlist_name,
            description=TEMP_DESCRIPTION,
            privacy_status="PRIVATE",
            video_ids=[],
        )
    result.playlist_id = target_id

    raw = yt.get_playlist(target_id, limit=500)
    existing = [
        track for track in (raw.get("tracks") or [])
        if track.get("videoId") and track.get("setVideoId")
    ]
    if existing:
        yt.remove_playlist_items(target_id, existing)

    if video_ids:
        # Add everything explicitly after creation/removal. In practice this is
        # more reliable than passing a long video_ids list to create_playlist,
        # which can leave only the first item in the playable queue.
        yt.add_playlist_items(target_id, video_ids, duplicates=True)
        result.added = len(video_ids)

    # Keep only the two newest daily temp playlists. Names end in ISO dates, so
    # lexical order matches chronological order for our generated names.
    refreshed = yt.get_library_playlists(limit=1000)
    stale = sorted(
        [p for p in refreshed
         if str(p.get("title") or "").startswith(TEMP_PLAYLIST_PREFIX)
         and p.get("playlistId") != target_id],
        key=lambda p: str(p.get("title") or ""),
        reverse=True,
    )[1:]
    for pl in stale:
        pid = str(pl.get("playlistId") or "")
        if pid:
            yt.delete_playlist(pid)

    # Persist playlist and tracks to local SQLite DB
    try:
        saved_tracks = []
        for pos, cand in enumerate(candidates, 1):
            matched_row = next(
                (
                    r for r in capped_rows
                    if (str(r.get("artist") or "").strip().lower() == cand.artist.strip().lower()
                        and str(r.get("title") or "").strip().lower() == cand.title.strip().lower())
                ),
                {},
            )
            saved_tracks.append({
                "position": pos,
                "track_id": matched_row.get("track_id"),
                "artist": cand.artist,
                "title": cand.title,
                "video_id": cand.video_id,
                "url": matched_row.get("url") or (f"https://music.youtube.com/watch?v={cand.video_id}" if cand.video_id else ""),
            })

        localcache.save_playlist(
            target_id,
            playlist_name,
            search_query=search_query or "",
            tracks=saved_tracks,
            url=f"https://music.youtube.com/playlist?list={target_id}",
            conn=conn,
        )
    except Exception as exc:
        log.warning("Failed to save synced playlist to SQLite: %s", exc)

    return result


def get_latest_temp_queue_playlist(
    client: Optional[YTMusicClient] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict[str, Any]]:
    """Fetch the newest temp queue playlist from YouTube Music and map its tracks.

    Returns a dict with 'playlist_id', 'name', and 'rows' (in playlist order),
    or None if YouTube Music is unauthenticated or no temp playlist exists.
    """
    try:
        yt = client or YTMusicClient()
        if not yt.is_authenticated:
            return None
        playlists = yt.get_library_playlists(limit=50)
    except Exception as exc:
        log.debug("fetch latest temp queue playlists failed: %s", exc)
        return None

    temp_playlists = sorted(
        [
            p for p in playlists
            if str(p.get("title") or "").startswith(TEMP_PLAYLIST_PREFIX)
            and p.get("playlistId")
        ],
        key=lambda p: str(p.get("title") or ""),
        reverse=True,
    )
    if not temp_playlists:
        return None

    target = temp_playlists[0]
    target_id = str(target.get("playlistId") or "")
    target_name = str(target.get("title") or "")
    if not target_id:
        return None

    try:
        raw = yt.get_playlist(target_id, limit=500)
    except Exception as exc:
        log.debug("get playlist %s failed: %s", target_id, exc)
        return None

    tracks = raw.get("tracks") or []
    if not tracks:
        return {"playlist_id": target_id, "name": target_name, "rows": []}

    own_conn = conn is None
    c = conn or localcache.connect()
    try:
        try:
            from . import track_analysis
            track_analysis.ensure_schema(c)
        except Exception:
            pass
        try:
            from . import genre
            genre.ensure_schema(c)
        except Exception:
            pass

        rows: list[dict[str, Any]] = []
        for t in tracks:
            vid = str(t.get("videoId") or "").strip()
            if not vid:
                continue
            title = str(t.get("title") or "").strip()
            artists = t.get("artists") or []
            artist = str(artists[0].get("name") if artists and isinstance(artists[0], dict) else "").strip()

            found = c.execute(
                """
                SELECT t.track_id, t.artist, t.title, s.url, s.kind,
                       g.genre, a.energy, a.bpm, a.detected_key AS key
                FROM sources s
                JOIN tracks t ON t.track_id = s.track_id
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE s.url LIKE ?
                LIMIT 1
                """,
                (f"%{vid}%",),
            ).fetchone()

            if found:
                rows.append({
                    "track_id": found["track_id"],
                    "artist": found["artist"] or artist,
                    "title": found["title"] or title,
                    "url": found["url"] or f"https://music.youtube.com/watch?v={vid}",
                    "kind": found["kind"] or "youtube_music",
                    "genre": found["genre"],
                    "energy": found["energy"],
                    "bpm": found["bpm"],
                    "key": found["key"],
                })
            else:
                rows.append({
                    "track_id": None,
                    "artist": artist,
                    "title": title,
                    "url": f"https://music.youtube.com/watch?v={vid}",
                    "kind": "youtube_music",
                    "genre": None,
                    "energy": None,
                    "bpm": None,
                    "key": None,
                })
        return {
            "playlist_id": target_id,
            "name": target_name,
            "rows": rows,
        }
    finally:
        if own_conn:
            c.close()


def export_synced_tracks_to_ytmusic(
    *,
    name: str = DEFAULT_PLAYLIST_NAME,
    description: str = DEFAULT_DESCRIPTION,
    limit: Optional[int] = None,
    dry_run: bool = False,
    client: Optional[YTMusicClient] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> PlaylistResult:
    """Export all karaoke-ready tracks in SQLite to a YouTube Music playlist."""
    own_conn = conn is None
    c = conn or localcache.connect()
    try:
        candidates = karaoke_tracks(c)
        if limit:
            candidates = candidates[:limit]
    finally:
        if own_conn:
            c.close()

    return build_or_sync_ytmusic_playlist(
        candidates=candidates,
        name=name,
        description=description,
        dry_run=dry_run,
        client=client,
    )


def add_track_to_ytmusic_playlist(
    playlist_id: str,
    artist: str,
    title: str,
    video_id: str = "",
    client: Optional[YTMusicClient] = None,
) -> tuple[bool, str]:
    """Resolve video ID if missing and append track to a remote YouTube Music playlist."""
    if not playlist_id:
        return False, "No playlist ID provided"
    yt = client or YTMusicClient()
    vid = video_id
    if not vid:
        vid = yt.search_track(artist, title) or ""
    if not vid:
        return False, f"Could not resolve YouTube video ID for {artist} - {title}"

    try:
        yt.require_auth()
        yt.add_playlist_items(playlist_id, [vid], duplicates=False)
        return True, vid
    except Exception as exc:
        log.warning("Failed to add track %s to YouTube Music playlist %s: %s", vid, playlist_id, exc)
        return False, str(exc)


def reconcile_playlist_with_remote(
    playlist_id: str,
    local_rows: list[dict[str, Any]],
    client: Optional[YTMusicClient] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Compare local queue rows with remote YouTube Music playlist tracks and reconcile drift.

    Returns (updated_rows, has_drift).
    """
    if not playlist_id:
        return local_rows, False
    yt = client or YTMusicClient()
    if not yt.is_authenticated:
        return local_rows, False

    try:
        remote_tracks = yt.get_playlist_tracks(playlist_id, limit=500)
    except Exception as exc:
        log.debug("reconcile_playlist_with_remote: failed fetching remote tracks: %s", exc)
        return local_rows, False

    remote_vids = [str(t.get("videoId") or "").strip() for t in remote_tracks if str(t.get("videoId") or "").strip()]
    local_vids = [
        localcache.extract_youtube_id(str(r.get("url") or ""))
        for r in local_rows
    ]
    local_vids = [v for v in local_vids if v]

    if remote_vids == local_vids:
        return local_rows, False

    # Drift detected: map remote tracks to local database rows
    own_conn = conn is None
    c = conn or localcache.connect()
    try:
        try:
            from . import track_analysis
            track_analysis.ensure_schema(c)
        except Exception:
            pass
        try:
            from . import genre
            genre.ensure_schema(c)
        except Exception:
            pass

        reconciled_rows: list[dict[str, Any]] = []
        for t in remote_tracks:
            vid = str(t.get("videoId") or "").strip()
            if not vid:
                continue
            title = str(t.get("title") or "").strip()
            artists = t.get("artists") or []
            artist = str(artists[0].get("name") if artists and isinstance(artists[0], dict) else "").strip()

            found = c.execute(
                """
                SELECT t.track_id, t.artist, t.title, s.url, s.kind,
                       g.genre, a.energy, a.bpm, a.detected_key AS key
                FROM sources s
                JOIN tracks t ON t.track_id = s.track_id
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE s.url LIKE ?
                LIMIT 1
                """,
                (f"%{vid}%",),
            ).fetchone()

            if found:
                reconciled_rows.append({
                    "track_id": found["track_id"],
                    "artist": found["artist"] or artist,
                    "title": found["title"] or title,
                    "video_id": vid,
                    "url": found["url"] or f"https://music.youtube.com/watch?v={vid}",
                    "kind": found["kind"] or "youtube_music",
                    "genre": found["genre"],
                    "energy": found["energy"],
                    "bpm": found["bpm"],
                    "key": found["key"],
                })
            else:
                reconciled_rows.append({
                    "track_id": None,
                    "artist": artist,
                    "title": title,
                    "video_id": vid,
                    "url": f"https://music.youtube.com/watch?v={vid}",
                    "kind": "youtube_music",
                    "genre": None,
                    "energy": None,
                    "bpm": None,
                    "key": None,
                })

        # Update saved playlist in SQLite
        saved_pl = localcache.find_saved_playlist_by_id(playlist_id, conn=c)
        name = saved_pl.get("name", "") if saved_pl else f"Playlist {playlist_id}"
        query = saved_pl.get("search_query", "") if saved_pl else ""
        saved_tracks = [
            {
                "position": idx + 1,
                "track_id": r.get("track_id"),
                "artist": r.get("artist") or "",
                "title": r.get("title") or "",
                "video_id": localcache.extract_youtube_id(r.get("url") or "") or "",
                "url": r.get("url") or "",
            }
            for idx, r in enumerate(reconciled_rows)
        ]
        localcache.save_playlist(playlist_id, name, search_query=query, tracks=saved_tracks, conn=c)
        return reconciled_rows, True
    finally:
        if own_conn:
            c.close()


def import_ytmusic_playlist_to_library(
    playlist_id: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
    resolve_lyrics: bool = True,
    client: Optional[YTMusicClient] = None,
) -> dict[str, Any]:
    """Import tracks from a YouTube Music playlist into local SQLite."""
    from . import lyrics

    yt = client or YTMusicClient()
    tracks = yt.get_playlist_tracks(playlist_id, limit=500)

    own_conn = conn is None
    c = conn or localcache.connect()

    stats = {
        "playlist_id": playlist_id,
        "total_tracks": len(tracks),
        "added_tracks": 0,
        "synced_lyrics": 0,
        "plain_lyrics": 0,
        "items": [],
    }

    try:
        for t in tracks:
            artist = t["artist"]
            title = t["title"]
            album = str(t.get("album") or "")
            duration = t.get("duration_seconds")
            url = t["url"]

            # Add source and track
            track_id = localcache.add_track_source(
                artist, title, album=album, duration=duration,
                url=url, kind="youtube_music", conn=c
            )

            # Fetch lyrics if requested
            ly = None
            if resolve_lyrics:
                ly = lyrics.fetch_lrclib(artist, title, album, duration)
                if ly and ly.synced_raw:
                    stats["synced_lyrics"] += 1
                elif ly and ly.plain:
                    stats["plain_lyrics"] += 1
                localcache.add_track_and_lyrics(
                    artist, title, ly, album=album, duration=duration, conn=c
                )

            stats["added_tracks"] += 1
            stats["items"].append({
                "track_id": track_id,
                "artist": artist,
                "title": title,
                "url": url,
                "has_synced": bool(ly and ly.synced_raw),
            })
    finally:
        if own_conn:
            c.close()

    return stats


def interactive_setup_auth() -> None:
    """Interactively setup authentication headers using ytmusicapi.setup()."""
    from ytmusicapi import setup
    dest = Path("~/.config/karaoke/ytmusic_auth.json").expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print("YouTube Music Browser Headers Setup")
    print("=" * 60)
    print(f"Target destination: {dest}\n")
    print("HOW TO GET HEADERS FROM CHROME (in 4 quick steps):")
    print(" 1. In Chrome, open https://music.youtube.com (logged in).")
    print(" 2. Press F12 (or Ctrl+Shift+I) to open Developer Tools -> click 'Network' (or 'Netwerk').")
    print(" 3. Filter by 'browse' or click on any playlist / song on YouTube Music.")
    print(" 4. In the network list, right click 'browse' -> Copy -> 'Copy request headers'")
    print("    (In Dutch: Rechtermuisklik -> Kopiëren -> Aanvraagheaders kopiëren)")
    print("    (Or: Copy -> 'Copy as cURL (POSIX)')\n")
    print("Now paste your copied headers below and press Enter (or Ctrl+D / Ctrl+Z on an empty line when finished):\n")
    try:
        setup(filepath=str(dest))
        print("\n" + "=" * 60)
        print(f"SUCCESS: Authentication saved to {dest}")
        print("You can now run: karaoke-ytmusic-playlist --list")
        print("=" * 60)
    except Exception as exc:
        print(f"\nSetup failed or cancelled: {exc}")


def ytmusic_playlist_main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for managing YouTube Music playlists."""
    parser = argparse.ArgumentParser(
        description="Sync, build, or import YouTube Music playlists from the karaoke library."
    )
    parser.add_argument("--list", action="store_true", help="List YouTube Music playlists in your library")
    parser.add_argument("--sync", action="store_true", help="Sync karaoke-ready tracks to YouTube Music playlist")
    parser.add_argument("--import-playlist", metavar="PLAYLIST_ID", help="Import a YouTube Music playlist into local library")
    parser.add_argument("--name", default=DEFAULT_PLAYLIST_NAME, help=f"Playlist name (default: '{DEFAULT_PLAYLIST_NAME}')")
    parser.add_argument("--limit", type=int, help="Limit number of tracks to export")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without modifying YouTube Music")
    parser.add_argument("--setup-auth", action="store_true", help="Interactively set up YouTube Music authentication headers")

    args = parser.parse_args(argv)

    if args.setup_auth:
        interactive_setup_auth()
        return

    client = YTMusicClient()

    if args.list:
        try:
            playlists = client.get_library_playlists(limit=50)
            if not playlists:
                print("No playlists found in your YouTube Music library.")
                return
            print(f"Found {len(playlists)} playlist(s):")
            for p in playlists:
                title = p.get("title", "Untitled")
                pid = p.get("playlistId", "")
                count = p.get("count", "?")
                print(f" - {title} ({count} tracks) [ID: {pid}]")
        except YTMusicAuthError as exc:
            print(f"Authentication required: {exc}")
            sys.exit(1)
        return

    if args.import_playlist:
        print(f"Importing YouTube Music playlist {args.import_playlist}...")
        stats = import_ytmusic_playlist_to_library(args.import_playlist, client=client)
        print(f"Imported {stats['added_tracks']}/{stats['total_tracks']} tracks:")
        print(f" - Synced lyrics matched: {stats['synced_lyrics']}")
        print(f" - Plain lyrics matched:  {stats['plain_lyrics']}")
        return

    if args.sync or not (args.list or args.import_playlist):
        print(f"Building/syncing playlist '{args.name}'...")
        try:
            res = export_synced_tracks_to_ytmusic(
                name=args.name,
                limit=args.limit,
                dry_run=args.dry_run,
                client=client,
            )
            print(f"Playlist: {res.name} (ID: {res.playlist_id or 'dry-run'})")
            print(f" - Candidates: {res.candidates}")
            print(f" - Resolved:   {res.resolved}")
            print(f" - Added:      {res.added}")
            print(f" - Present:    {res.already_present}")
            print(f" - Unresolved: {len(res.unresolved)}")
            if res.dry_run:
                print("[DRY RUN] No changes were made to YouTube Music.")
        except YTMusicAuthError as exc:
            print(f"Authentication required: {exc}")
            sys.exit(1)


if __name__ == "__main__":
    ytmusic_playlist_main()
