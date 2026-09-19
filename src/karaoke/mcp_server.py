"""Model Context Protocol (MCP) server for the Karaoke AI DJ & Music Library.

Exposes tools for Obot, Claude, and local LLMs (Ollama) to inspect the 18,000+
song library, track harmonic key and Camelot codes, query current desktop
playback, perform smart DJ track transitions, and trigger playback.
"""
from __future__ import annotations

import argparse
import json
import logging
from typing import Any, Optional

import uvicorn
from mcp.server import MCPServer
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from . import localcache, musictheory, playerctl, sentiment, visuals
from .logger import log

dj_mcp = MCPServer(
    name="karaoke-dj",
    version="1.0.0",
    instructions=(
        "You are the resident AI Karaoke DJ & MC. Use these tools to query the "
        "local song library (18,000+ tracks), analyze playback vibes, find harmonically "
        "compatible tracks using the Camelot wheel and BPM, inspect current playing audio, "
        "and help singers pick their next killer song."
    ),
)


@dj_mcp.tool()
def search_songs(
    query: str = "",
    genre: str = "",
    key: str = "",
    min_bpm: float = 0.0,
    max_bpm: float = 300.0,
    only_synced_lyrics: bool = False,
    limit: int = 10,
) -> str:
    """Search tracks in the karaoke library by title, artist, genre, key (e.g. 'Am', '8A', 'C'), or BPM range.

    Args:
        query: Artist, title, or album substring match.
        genre: Genre filter (e.g. 'Rock', 'Metal', 'Pop', 'Indie').
        key: Musical key (e.g. 'A minor', 'Am') or Camelot wheel code (e.g. '8A', '8B').
        min_bpm: Minimum tempo in beats per minute.
        max_bpm: Maximum tempo in beats per minute.
        only_synced_lyrics: If true, only return tracks that have timestamped karaoke lyrics ready.
        limit: Max results to return (default 10, max 50).
    """
    limit = max(1, min(limit, 50))
    target_camelot = key.strip().upper() if key else ""

    with localcache.connect() as conn:
        cur = conn.cursor()
        sql = """
            SELECT t.track_id, t.artist, t.title, t.album, t.duration, t.play_count,
                   a.detected_key, a.bpm, a.energy, a.brightness, g.genre, s.url, s.kind,
                   EXISTS(
                       SELECT 1 FROM lyrics l
                       WHERE l.track_id = t.track_id AND l.synced_lyrics != ''
                   ) AS has_synced
            FROM tracks t
            LEFT JOIN track_analysis a ON a.track_id = t.track_id
            LEFT JOIN track_genre g ON g.track_id = t.track_id
            LEFT JOIN sources s ON s.source_id = (
                SELECT s2.source_id FROM sources s2
                WHERE s2.track_id = t.track_id
                ORDER BY
                    CASE
                        WHEN s2.kind = 'youtube_music' THEN 0
                        WHEN s2.kind = 'youtube' THEN 1
                        WHEN s2.url LIKE 'http%%' THEN 2
                        WHEN s2.kind = 'spotify' THEN 3
                        ELSE 4
                    END,
                    s2.source_id
                LIMIT 1
            )
        """
        where_clauses = []
        params: list[Any] = []

        if query.strip():
            pat = f"%{query.strip()}%"
            where_clauses.append("(t.artist ILIKE %s OR t.title ILIKE %s OR t.album ILIKE %s OR (t.artist || ' ' || t.title) ILIKE %s)")
            params.extend([pat, pat, pat, pat])

        if genre.strip():
            where_clauses.append("g.genre ILIKE %s")
            params.append(f"%{genre.strip()}%")

        if min_bpm > 0:
            where_clauses.append("a.bpm >= %s")
            params.append(min_bpm)

        if max_bpm < 300:
            where_clauses.append("a.bpm <= %s")
            params.append(max_bpm)

        if only_synced_lyrics:
            where_clauses.append("EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id AND l.synced_lyrics != '')")

        if where_clauses:
            sql += f" WHERE {' AND '.join(where_clauses)}"

        sql += " ORDER BY t.play_count DESC, t.artist, t.title LIMIT %s"
        # Fetch extra if we need to filter by key/Camelot in python
        fetch_limit = limit * 4 if target_camelot else limit
        params.append(fetch_limit)

        cur.execute(sql, params)
        rows = cur.fetchall()

        results = []
        for r in rows:
            detected_key = r["detected_key"] or ""
            key_obj = musictheory.parse_key(detected_key) if detected_key else None
            camelot = key_obj.camelot if key_obj else "?"

            if target_camelot:
                # Match either Camelot code (8A) or key name (Am / A minor)
                matches_camelot = (camelot.upper() == target_camelot)
                matches_name = key_obj and (
                    key_obj.short.upper() == target_camelot
                    or key_obj.name.upper() == target_camelot
                )
                if not (matches_camelot or matches_name):
                    continue

            results.append({
                "track_id": r["track_id"],
                "artist": r["artist"],
                "title": r["title"],
                "album": r["album"] or "",
                "duration_s": round(r["duration"] or 0, 1),
                "key": detected_key,
                "camelot": camelot,
                "bpm": round(r["bpm"], 1) if r["bpm"] else None,
                "energy": round(r["energy"], 2) if r["energy"] is not None else None,
                "genre": r["genre"] or "",
                "has_synced_lyrics": bool(r["has_synced"]),
                "play_count": r["play_count"] or 0,
                "url": r["url"] or "",
            })

            if len(results) >= limit:
                break

        return json.dumps({
            "count": len(results),
            "tracks": results,
        }, indent=2)


@dj_mcp.tool()
def get_now_playing() -> str:
    """Check what song is currently playing on the desktop (Spotify, VLC, browser/YouTube, MPRIS).

    Returns track title, artist, album, player, position, and detected key/BPM/sentiment if known in library.
    """
    try:
        active_player = playerctl.playing_player()
        meta = playerctl.current_metadata(active_player) if active_player else None
        if not meta or not (meta.artist or meta.title):
            # Fallback to playerctl default or recent play
            meta = playerctl.current_metadata()
    except Exception as exc:
        meta = None

    if meta and (meta.artist or meta.title):
        artist = meta.artist
        title = meta.title
        # Look up in library
        with localcache.connect() as conn:
            tid = localcache.find_track_id_relaxed(artist, title, conn)
            if not tid and meta.url:
                found = localcache.find_track_by_url(meta.url, conn)
                if found:
                    tid = found[0]

            cur = conn.cursor()
            if tid is not None:
                cur.execute("""
                    SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre
                    FROM tracks t
                    LEFT JOIN track_analysis a ON a.track_id = t.track_id
                    LEFT JOIN track_genre g ON g.track_id = t.track_id
                    WHERE t.track_id = %s
                """, (tid,))
            else:
                cur.execute("""
                    SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre
                    FROM tracks t
                    LEFT JOIN track_analysis a ON a.track_id = t.track_id
                    LEFT JOIN track_genre g ON g.track_id = t.track_id
                    WHERE (t.artist ILIKE %s AND t.title ILIKE %s)
                       OR ((t.artist || ' ' || t.title) ILIKE %s)
                    LIMIT 1
                """, (f"%{artist}%", f"%{title}%", f"%{artist} {title}%"))
            row = cur.fetchone()

            track_id = row["track_id"] if row else tid
            key_info = None
            if row and row["detected_key"]:
                k = musictheory.parse_key(row["detected_key"])
                key_info = {
                    "key": row["detected_key"],
                    "camelot": k.camelot if k else "?",
                    "bpm": round(row["bpm"], 1) if row["bpm"] else None,
                    "energy": round(row["energy"], 2) if row["energy"] is not None else None,
                    "genre": row["genre"] or "",
                }

            return json.dumps({
                "status": "playing",
                "player": meta.player or active_player or "mpris",
                "artist": row["artist"] if row else artist,
                "title": row["title"] if row else title,
                "track_id": track_id,
                "album": meta.album or "",
                "duration_s": meta.duration,
                "url": meta.url,
                "library_match": key_info,
            }, indent=2)

    # If nothing is playing right now, fetch the last played song from stats
    summary = localcache.summarize(limit=5)
    last_tracks = summary.top_tracks[:3] if summary.top_tracks else []
    return json.dumps({
        "status": "idle",
        "message": "No active media player detected playing right now.",
        "recent_popular_tracks": [
            {"artist": a, "title": t, "plays": p} for a, t, p in last_tracks
        ],
    }, indent=2)


@dj_mcp.tool()
def suggest_next_tracks(
    track_id: Optional[int] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    strategy: str = "harmonic",
    limit: int = 5,
    exclude_playlist: str = "dj-list",
) -> str:
    """Suggest the best next songs to follow a given track for smooth DJ mixing or energy flow.

    Args:
        track_id: The ID of the seed track (optional if artist & title provided).
        artist: Artist name of the current track (used if track_id is omitted).
        title: Title of the current track (used if track_id is omitted).
        exclude_playlist: Skip songs already in this playlist, so repeated calls
            keep offering fresh material. Defaults to the DJ list; pass an empty
            string to allow songs already queued.
        strategy:
            - 'harmonic': Camelot wheel adjacent keys (+/- 1 hour or relative major/minor) + matching BPM (+/- 8%).
            - 'energy_up': Step up the party tempo (+5 to +20 BPM) and higher energy score.
            - 'cool_down': Wind down with slower tempo and softer energy.
            - 'acoustic': Timbre / sound similarity via CLAP vector embeddings.
        limit: Number of recommendations (default 5, max 20).
    """
    limit = max(1, min(limit, 20))
    seed_track = None

    with localcache.connect() as conn:
        cur = conn.cursor()
        if track_id is not None:
            cur.execute("""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre
                FROM tracks t
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE t.track_id = %s
            """, (track_id,))
            seed_track = cur.fetchone()
        elif artist and title:
            cur.execute("""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre
                FROM tracks t
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE t.artist ILIKE %s AND t.title ILIKE %s
                LIMIT 1
            """, (f"%{artist}%", f"%{title}%"))
            seed_track = cur.fetchone()

        if not seed_track:
            cur.execute("""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre
                FROM tracks t
                JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE a.bpm IS NOT NULL
                ORDER BY t.play_count DESC, t.track_id ASC
                LIMIT 1
            """)
            seed_track = cur.fetchone()

        if not seed_track:
            return json.dumps({
                "error": "Could not find seed track in library. Provide a valid track_id or artist/title.",
            })

        seed_id = seed_track["track_id"]
        seed_bpm = seed_track["bpm"] or 120.0
        seed_key_str = seed_track["detected_key"] or ""
        seed_key = musictheory.parse_key(seed_key_str) if seed_key_str else None
        seed_camelot = seed_key.camelot if seed_key else "?"

        candidates = []

        # Songs already queued are not suggestions. Without this a DJ working
        # through /suggest keeps being handed the same tracks they just added,
        # and `/queue all` would append duplicates. Read locally: this runs on
        # every suggestion, so it must not depend on YouTube Music being
        # reachable.
        excluded: set[int] = set()
        if exclude_playlist:
            try:
                excluded = {
                    t["track_id"]
                    for t in localcache.get_saved_playlist_tracks(exclude_playlist, conn=conn)
                    if t.get("track_id")
                }
            except Exception as exc:
                log.warning("Could not read playlist %s for exclusion: %s", exclude_playlist, exc)

        if strategy == "acoustic":
            from . import queue_suggest
            # Over-fetch so filtering cannot leave us short of `limit`.
            raw = queue_suggest.suggest_for_track(seed_id, limit=limit + len(excluded))
            suggestions = [s for s in raw if s.track_id not in excluded][:limit]
            return json.dumps({
                "seed": {
                    "track_id": seed_id,
                    "artist": seed_track["artist"],
                    "title": seed_track["title"],
                    "key": seed_key_str,
                    "camelot": seed_camelot,
                    "bpm": seed_bpm,
                },
                "strategy": "acoustic_vector",
                "suggestions": [
                    {
                        "track_id": s.track_id,
                        "artist": s.artist,
                        "title": s.title,
                        "score": round(s.score, 3),
                        "similarity_space": s.space,
                    }
                    for s in suggestions
                ],
            }, indent=2)

        # Harmonic or Energy transition
        # Determine compatible Camelot keys if seed_camelot is known
        compatible_camelots = set()
        if seed_camelot and seed_camelot != "?":
            num = int(seed_camelot[:-1])
            letter = seed_camelot[-1]
            other_letter = "B" if letter == "A" else "A"
            compatible_camelots.add(f"{num}{letter}")          # Same key
            compatible_camelots.add(f"{num}{other_letter}")    # Relative major/minor
            compatible_camelots.add(f"{(num % 12) + 1}{letter}")   # +1 hour
            compatible_camelots.add(f"{((num - 2) % 12) + 1}{letter}") # -1 hour

        bpm_filter_sql = ""
        params: list[Any] = [seed_id]

        if strategy == "energy_up":
            bpm_filter_sql = "AND a.bpm >= %s AND a.bpm <= %s"
            params.extend([seed_bpm + 3.0, seed_bpm + 30.0])
            order_sql = "ORDER BY a.bpm ASC, a.energy DESC"
        elif strategy == "cool_down":
            bpm_filter_sql = "AND a.bpm <= %s AND a.bpm >= %s"
            params.extend([seed_bpm - 3.0, max(50.0, seed_bpm - 35.0)])
            order_sql = "ORDER BY a.bpm DESC, a.energy ASC"
        else:  # harmonic
            bpm_filter_sql = "AND a.bpm >= %s AND a.bpm <= %s"
            params.extend([seed_bpm * 0.88, seed_bpm * 1.12])
            order_sql = "ORDER BY ABS(a.bpm - %s) ASC"
            params.append(seed_bpm)

        query_sql = f"""
            SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre,
                   EXISTS(
                       SELECT 1 FROM lyrics l
                       WHERE l.track_id = t.track_id AND l.synced_lyrics != ''
                   ) AS has_synced
            FROM tracks t
            JOIN track_analysis a ON a.track_id = t.track_id
            LEFT JOIN track_genre g ON g.track_id = t.track_id
            WHERE t.track_id != %s
              AND a.detected_key IS NOT NULL
              {bpm_filter_sql}
            {order_sql}
            LIMIT 60
        """
        cur.execute(query_sql, params)
        pool = cur.fetchall()

        for row in pool:
            if row["track_id"] in excluded:
                continue

            k = musictheory.parse_key(row["detected_key"])
            cam = k.camelot if k else "?"

            is_harmonic = cam in compatible_camelots if compatible_camelots else True
            if strategy == "harmonic" and not is_harmonic:
                continue

            reason = []
            if is_harmonic and compatible_camelots:
                if cam == seed_camelot:
                    reason.append(f"Identical key {cam}")
                elif cam[-1] != seed_camelot[-1]:
                    reason.append(f"Relative key {cam}")
                else:
                    reason.append(f"Adjacent key {cam}")

            tempo_diff = round((row["bpm"] - seed_bpm), 1)
            sign = "+" if tempo_diff >= 0 else ""
            reason.append(f"BPM {round(row['bpm'], 1)} ({sign}{tempo_diff})")

            candidates.append({
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "key": row["detected_key"],
                "camelot": cam,
                "bpm": round(row["bpm"], 1),
                "energy": round(row["energy"], 2) if row["energy"] is not None else None,
                "has_synced_lyrics": bool(row["has_synced"]),
                "reason": " • ".join(reason),
            })

            if len(candidates) >= limit:
                break

        return json.dumps({
            "seed": {
                "track_id": seed_id,
                "artist": seed_track["artist"],
                "title": seed_track["title"],
                "key": seed_key_str,
                "camelot": seed_camelot,
                "bpm": round(seed_bpm, 1),
            },
            "strategy": strategy,
            "suggestions": candidates,
        }, indent=2)


@dj_mcp.tool()
def analyze_lyric_vibe(track_id: int) -> str:
    """Analyze the lyric mood, emotional arc, and singing tips for a track.

    Args:
        track_id: The ID of the track in the library.
    """
    with localcache.connect() as conn:
        lyrics = localcache.get_lyrics_by_track_id(track_id, conn)
        cur = conn.cursor()
        cur.execute("""
            SELECT t.artist, t.title, a.detected_key, a.bpm, a.energy
            FROM tracks t
            LEFT JOIN track_analysis a ON a.track_id = t.track_id
            WHERE t.track_id = %s
        """, (track_id,))
        t_row = cur.fetchone()

        if not t_row:
            return json.dumps({"error": f"Track {track_id} not found."})

        lyrics_text = lyrics.plain if lyrics else ""
        if not lyrics_text and lyrics and lyrics.synced_lyrics:
            lyrics_text = "\n".join(t for _, t in lyrics.lines)

        if not lyrics_text and t_row:
            try:
                from . import lyrics as lyrics_mod
                fetched = lyrics_mod.fetch_lrclib(t_row["artist"], t_row["title"])
                if fetched:
                    localcache.save_lyrics(track_id, fetched, conn)
                    lyrics = fetched
                    lyrics_text = lyrics.plain or "\n".join(t for _, t in lyrics.lines)
            except Exception:
                pass

        profile = visuals.analyze_sentiment(lyrics_text or "")
        arc = visuals.sentiment_arc(profile, width=20)
        bars = visuals.sentiment_bars(profile, width=10)

        key_obj = musictheory.parse_key(t_row["detected_key"]) if t_row["detected_key"] else None

        # Build singing tips
        tips = []
        if profile.dominant == "angry":
            tips.append("High-aggression vocal delivery — belt the chorus with grit!")
        elif profile.dominant == "tender":
            tips.append("Warm, emotive ballad vibe — focus on soft resonance and breath control.")
        elif profile.dominant == "happy":
            tips.append("Upbeat crowd-pleaser — get the room clapping along!")
        elif profile.dominant == "sad":
            tips.append("Moody, introspective track — lean into the emotional melancholy.")
        else:
            tips.append("Balanced neutral mood — let the rhythm drive the vocal cadence.")

        if t_row["bpm"] and t_row["bpm"] > 140:
            tips.append(f"Fast tempo ({round(t_row['bpm'])} BPM) — watch for rapid lyrical pacing.")
        elif t_row["bpm"] and t_row["bpm"] < 90:
            tips.append(f"Slow tempo ({round(t_row['bpm'])} BPM) — hold sustained notes cleanly.")

        return json.dumps({
            "track_id": track_id,
            "artist": t_row["artist"],
            "title": t_row["title"],
            "key": t_row["detected_key"],
            "key_character": key_obj.character if key_obj else "unknown",
            "dominant_mood": profile.dominant,
            "sentiment_counts": profile.counts,
            "has_synced_lyrics": lyrics.has_synced if lyrics else False,
            "line_count": len(lyrics.lines) if lyrics else 0,
            "ascii_arc": arc,
            "ascii_bars": bars,
            "dj_singer_tips": tips,
        }, indent=2)


@dj_mcp.tool()
def get_dj_stats() -> str:
    """Get overall library summary, crowd top-favorites, and playback statistics.

    Useful for understanding what the room has been listening to and picking sure-fire crowd pleasers.
    """
    summary = localcache.summarize(limit=10)
    return json.dumps({
        "total_events": summary.total_events,
        "plays": summary.plays,
        "discoveries": summary.discoveries,
        "cache_hit_rate": f"{round(summary.cache_hit_rate * 100, 1)}%",
        "distinct_tracks": summary.distinct_tracks,
        "distinct_artists": summary.distinct_artists,
        "top_tracks": [
            {"artist": a, "title": t, "plays": p} for a, t, p in summary.top_tracks
        ],
        "top_artists": [
            {"artist": a, "plays": p} for a, p in summary.top_artists
        ],
        "playback_modes": dict(summary.by_mode),
    }, indent=2)


@dj_mcp.tool()
def play_track(track_id: int, prefer_audio_only: bool = False) -> str:
    """Trigger playback of a track on the host session via the Karaoke player.

    Args:
        track_id: The ID of the track to play.
        prefer_audio_only: If true, opens audio source directly; otherwise launches full karaoke experience.
    """
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.artist, t.title, s.url, s.kind
            FROM tracks t
            LEFT JOIN sources s ON s.source_id = (
                SELECT s2.source_id FROM sources s2
                WHERE s2.track_id = t.track_id
                ORDER BY
                    CASE
                        WHEN s2.kind = 'youtube_music' THEN 0
                        WHEN s2.kind = 'youtube' THEN 1
                        WHEN s2.url LIKE 'http%%' THEN 2
                        WHEN s2.kind = 'spotify' THEN 3
                        ELSE 4
                    END,
                    s2.source_id
                LIMIT 1
            )
            WHERE t.track_id = %s
        """, (track_id,))
        row = cur.fetchone()

        if not row:
            return json.dumps({"error": f"Track {track_id} not found."})

        artist = row["artist"]
        title = row["title"]
        url = row["url"]
        kind = row["kind"]

        # Call the local control API or open song URL
        from .player_open import open_song_url
        target = url or f"{artist} - {title}"
        # A pid comes back only from the xdg-open path; the kiosk/CDP path
        # returns None on success too, so absence of a pid is not a failure.
        pid = open_song_url(
            url or "",
            kind,
            artist=artist,
            title=title,
            prefer_audio=prefer_audio_only,
        )

        return json.dumps({
            "status": "launched",
            "pid": pid,
            "track_id": track_id,
            "artist": artist,
            "title": title,
            "target": target,
            "karaoke_cli_command": f'karaoke "{artist} - {title}"',
        }, indent=2)


@dj_mcp.tool()
def add_to_dj_playlist(
    track_id: Optional[int] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    playlist_id: str = "dj-list",
) -> str:
    """Add a song to the DJ playlist ('Karaoke: DJ List') by track ID or artist/title, syncing with YouTube Music.

    Args:
        track_id: Specific track ID from the karaoke library.
        artist: Artist name (used if track_id is omitted).
        title: Title of song (used if track_id is omitted).
        playlist_id: Identifier of target saved playlist (default 'dj-list').
    """
    if track_id is None and not (artist or title):
        return json.dumps({"error": "Either track_id or artist/title must be provided."})

    with localcache.connect() as conn:
        cur = conn.cursor()
        if track_id is not None:
            cur.execute("""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, s.url, s.kind
                FROM tracks t
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN sources s ON s.source_id = (
                    SELECT s2.source_id FROM sources s2
                    WHERE s2.track_id = t.track_id
                    ORDER BY
                        CASE
                            WHEN s2.kind = 'youtube_music' THEN 0
                            WHEN s2.kind = 'youtube' THEN 1
                            WHEN s2.url LIKE 'http%%' THEN 2
                            WHEN s2.kind = 'spotify' THEN 3
                            ELSE 4
                        END,
                        s2.source_id
                    LIMIT 1
                )
                WHERE t.track_id = %s
            """, (track_id,))
            row = cur.fetchone()
        else:
            pat_artist = f"%{artist.strip()}%" if artist else "%"
            pat_title = f"%{title.strip()}%" if title else "%"
            cur.execute("""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, s.url, s.kind
                FROM tracks t
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN sources s ON s.source_id = (
                    SELECT s2.source_id FROM sources s2
                    WHERE s2.track_id = t.track_id
                    ORDER BY
                        CASE
                            WHEN s2.kind = 'youtube_music' THEN 0
                            WHEN s2.kind = 'youtube' THEN 1
                            WHEN s2.url LIKE 'http%%' THEN 2
                            WHEN s2.kind = 'spotify' THEN 3
                            ELSE 4
                        END,
                        s2.source_id
                    LIMIT 1
                )
                WHERE t.artist ILIKE %s AND t.title ILIKE %s
                ORDER BY t.play_count DESC
                LIMIT 1
            """, (pat_artist, pat_title))
            row = cur.fetchone()

        if not row:
            return json.dumps({"error": f"Track not found in library (track_id={track_id}, artist={artist}, title={title})."})

        # Resolve real YouTube Music playlist
        yt_pid = playlist_id
        yt_url = ""
        if playlist_id in ("dj-list", "Karaoke: DJ List"):
            try:
                from . import ytmusic_playlist
                yt_pid, yt_url = ytmusic_playlist.resolve_or_create_dj_playlist(conn=conn)
            except Exception:
                yt_pid = "dj-list"

        # Ensure playlist exists locally
        localcache.ensure_dj_playlist(playlist_id=yt_pid, conn=conn)

        # Adding a song twice is a no-op, not an error: a DJ re-queueing a
        # track should not get a duplicate. Checked before the remote sync so
        # a repeat add costs no YouTube Music call either.
        for existing in localcache.get_saved_playlist_tracks(yt_pid, conn=conn):
            if existing.get("track_id") == row["track_id"]:
                return json.dumps({
                    "ok": True,
                    "already_present": True,
                    "playlist_id": yt_pid,
                    "position": existing.get("position"),
                    "track": {
                        "track_id": row["track_id"],
                        "artist": row["artist"],
                        "title": row["title"],
                    },
                    "message": f"{row['artist']} — {row['title']} is already in the playlist.",
                }, indent=2)

        url = row["url"] or ""
        vid = localcache.extract_youtube_id(url) if url else ""

        # Sync to remote YouTube Music playlist if authenticated
        if yt_pid.startswith(("PL", "VL", "RD", "OLAK5")):
            try:
                from . import ytmusic_playlist
                ok, res_vid = ytmusic_playlist.add_track_to_ytmusic_playlist(yt_pid, row["artist"], row["title"], video_id=vid)
                if ok and res_vid and not vid:
                    vid = res_vid
                    url = f"https://music.youtube.com/watch?v={vid}"
            except Exception as exc:
                log.warning("Could not sync track to YouTube Music playlist %s: %s", yt_pid, exc)

        track_payload = {
            "track_id": row["track_id"],
            "artist": row["artist"],
            "title": row["title"],
            "video_id": vid,
            "url": url,
        }
        pos = localcache.append_playlist_track(yt_pid, track_payload, conn=conn)

        key_obj = musictheory.parse_key(row["detected_key"]) if row["detected_key"] else None
        return json.dumps({
            "ok": True,
            "playlist_id": yt_pid,
            "playlist_url": yt_url or (f"https://music.youtube.com/playlist?list={yt_pid}" if yt_pid.startswith("PL") else ""),
            "position": pos,
            "track": {
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "key": row["detected_key"] or "",
                "camelot": key_obj.camelot if key_obj else "?",
                "bpm": round(row["bpm"], 1) if row["bpm"] else None,
                "url": url,
            }
        }, indent=2)


@dj_mcp.tool()
def get_dj_playlist(playlist_id: str = "dj-list") -> str:
    """Retrieve all songs in the DJ playlist ('Karaoke: DJ List') with position, metadata, and YouTube Music URL.

    Args:
        playlist_id: The saved playlist ID (default 'dj-list').
    """
    with localcache.connect() as conn:
        yt_pid = playlist_id
        yt_url = ""
        if playlist_id in ("dj-list", "Karaoke: DJ List"):
            try:
                from . import ytmusic_playlist
                yt_pid, yt_url = ytmusic_playlist.resolve_or_create_dj_playlist(conn=conn)
            except Exception:
                yt_pid = "dj-list"

        pl = localcache.find_saved_playlist_by_id(yt_pid, conn=conn) or localcache.find_saved_playlist_by_id("dj-list", conn=conn)
        tracks = localcache.get_saved_playlist_tracks(yt_pid, conn=conn)
        if not tracks and yt_pid != "dj-list":
            tracks = localcache.get_saved_playlist_tracks("dj-list", conn=conn)

    return json.dumps({
        "playlist_id": yt_pid,
        "name": (pl.get("name") if pl else "") or "Karaoke: DJ List",
        "url": yt_url or (pl.get("url") if pl else "") or (f"https://music.youtube.com/playlist?list={yt_pid}" if yt_pid.startswith("PL") else ""),
        "track_count": len(tracks),
        "tracks": tracks,
    }, indent=2)


@dj_mcp.tool()
def clear_dj_playlist(playlist_id: str = "dj-list") -> str:
    """Empty all tracks from the DJ playlist ('Karaoke: DJ List') locally and on YouTube Music.

    Args:
        playlist_id: The saved playlist ID to clear (default 'dj-list').
    """
    with localcache.connect() as conn:
        yt_pid = playlist_id
        if playlist_id in ("dj-list", "Karaoke: DJ List"):
            try:
                from . import ytmusic_playlist
                yt_pid, _ = ytmusic_playlist.resolve_or_create_dj_playlist(conn=conn)
            except Exception:
                yt_pid = "dj-list"

        if yt_pid.startswith(("PL", "VL", "RD", "OLAK5")):
            try:
                from . import ytmusic_playlist
                ytmusic_playlist.clear_ytmusic_playlist(yt_pid)
            except Exception as exc:
                log.warning("Failed to clear remote YouTube Music playlist %s: %s", yt_pid, exc)

        localcache.clear_saved_playlist_tracks(yt_pid, conn=conn)
        if yt_pid != "dj-list":
            localcache.clear_saved_playlist_tracks("dj-list", conn=conn)

    return json.dumps({
        "ok": True,
        "playlist_id": yt_pid,
        "message": f"Cleared all tracks from playlist '{yt_pid}'.",
    }, indent=2)


class CombinedMCPApp:
    """Dispatches requests between MCP Streamable HTTP, Server-Sent Events (SSE), and health checks.

    Supports both legacy SSE clients (GET /sse -> event-stream) and Streamable HTTP clients
    (POST /sse -> JSON-RPC, like Obot) on the same endpoint without 405 Method Not Allowed.
    """

    def __init__(
        self,
        sse_transport: SseServerTransport,
        session_mgr: StreamableHTTPSessionManager,
        lowlevel_server: Any,
    ) -> None:
        self.sse = sse_transport
        self.streamable = StreamableHTTPASGIApp(session_mgr)
        self.lowlevel_server = lowlevel_server

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            method = scope.get("method", "GET")
            if method == "HEAD":
                response = Response(status_code=200)
                await response(scope, receive, send)
                return
            if method == "GET":
                headers = dict(scope.get("headers", []))
                accept = headers.get(b"accept", b"").decode("utf-8", errors="ignore")
                has_streamable_session = b"mcp-session-id" in headers
                if "text/event-stream" in accept and not has_streamable_session:
                    async with self.sse.connect_sse(scope, receive, send) as streams:
                        await self.lowlevel_server.run(
                            streams[0],
                            streams[1],
                            self.lowlevel_server.create_initialization_options(),
                        )
                    return
        await self.streamable(scope, receive, send)


def create_asgi_app(server: MCPServer = dj_mcp) -> Starlette:
    """Build the unified Starlette ASGI application supporting SSE and Streamable HTTP."""
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"],
    )
    sse = SseServerTransport("/messages/", security_settings=security)
    session_manager = StreamableHTTPSessionManager(
        app=server._lowlevel_server,
        security_settings=security,
    )
    combined = CombinedMCPApp(sse, session_manager, server._lowlevel_server)

    routes = [
        Route("/", endpoint=lambda r: JSONResponse({"status": "ok", "server": "karaoke-dj"})),
        Route("/health", endpoint=lambda r: JSONResponse({"status": "ok"})),
        Route("/sse", endpoint=combined),
        Route("/sse/", endpoint=combined),
        Route("/mcp", endpoint=combined),
        Route("/mcp/", endpoint=combined),
        Mount("/messages", app=sse.handle_post_message),
    ]

    return Starlette(routes=routes, lifespan=lambda a: session_manager.run())


def main() -> None:
    parser = argparse.ArgumentParser(description="Karaoke AI DJ Model Context Protocol (MCP) Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8888, help="Port to listen on (default: 8888)")
    parser.add_argument("--stdio", action="store_true", help="Run in stdio mode instead of HTTP")
    args = parser.parse_args()

    if args.stdio:
        import asyncio
        asyncio.run(dj_mcp.run_stdio_async())
    else:
        app = create_asgi_app(dj_mcp)
        print(f"Starting Karaoke DJ MCP Server on http://{args.host}:{args.port} (SSE: /sse, Streamable: /sse & /mcp) ...")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
